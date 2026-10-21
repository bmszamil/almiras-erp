"""Secret store for ZATCA keys and CSID secrets (zatca D-3, D-12, R-6, R-41, R-47, R-50). References only in the DB.

The ERP database holds only references ``<path>#v<version>`` (``EgsUnit.private_key_ref``,
``EgsCredential.secret_ref``, ``EntityMasterKey.key_ref``, ``GroupSuperKey.key_ref``). Key material lives in
the store. The hierarchy (D-12, R-50): the group super key protects each entity master key; an entity
master key protects only that entity's EGS private keys and CSID secrets. Every operation names the master
key it opens the item under, and the store refuses an item that is not protected by that entity's master key
(R-50 a, AT-70). Rotation (OQ-34 d) writes a new master-key version and **re-wraps every item** of that
entity under it in the same store operation (OpenBao transit ``rewrap``), so references held in the DB stay
valid; an item is always opened under the master-key version that protects it.

Backends:

- :class:`OpenBaoSecretStore`: the interface to the KSA-hosted OpenBao (D-3, D-5). Only version retirement is
  built (KV v2 metadata, OQ-34 (f)); every other call raises :class:`SecretStoreUnavailable`, so a misconfigured
  environment fails closed (OQ-34).
- :class:`LocalSecretStore`: in-memory store for tests and local development only. It wraps keys with
  AES-GCM along the hierarchy and can be "sealed" or have one entity's master key made unavailable.

Retired versions (D-19, R-57 to R-60): every store keeps all versions with their data and can mark one version
**retired**; a retired version is refused for signing, reading and public-key reads (``SecretStoreRefused``,
code ``secret_version_retired``). No store method deletes, destroys, trims or un-retires a version.

The setting ``ZATCA_SECRET_STORE`` (dotted path) chooses the backend; tests install a local store with
:func:`override`.
"""
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from datetime import timezone as dt_timezone

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

from . import crypto


class SecretStoreUnavailable(Exception):
    """The store is sealed or unreachable, or the entity's master key is unavailable (R-47). Transient."""

    def __init__(self, en="The secret store is sealed or unreachable.", ar="مخزن الأسرار مغلق أو لا يمكن الوصول إليه."):
        self.en, self.ar = en, ar
        super().__init__(f"{en} / {ar}")


class SecretStoreRefused(Exception):
    """The request breaks the key hierarchy (e.g. another entity's master key, R-50 a) or names a retired version
    (R-57, ``code="secret_version_retired"``). Never transient."""

    def __init__(self, en, ar, code=None):
        self.en, self.ar, self.code = en, ar, code
        super().__init__(f"{en} / {ar}")


# R-57: the refusal of a retired key or secret version (en/ar as in the spec).
RETIRED_EN = "This key or secret version was retired after a certificate renewal and can no longer be used."
RETIRED_AR = "أُوقف إصدار المفتاح أو السر هذا بعد تجديد الشهادة ولم يعد قابلاً للاستخدام."
RETIRED_CODE = "secret_version_retired"


def retired_refusal():
    return SecretStoreRefused(RETIRED_EN, RETIRED_AR, code=RETIRED_CODE)


FLAG_LIMIT_CODE = "retirement_flag_limit"


def flag_limit_refusal():
    """OQ-34 (f): the store cannot record one more retired version for this path (KV v2 metadata limit)."""
    return SecretStoreRefused(
        "The secret store cannot record another retired version for this key path (metadata size limit); the "
        "Managing Partner must handle it in OpenBao.",
        "لا يستطيع مخزن الأسرار تسجيل إصدار موقوف آخر لمسار هذا المفتاح (حد حجم البيانات الوصفية)؛ يعالجه الشريك "
        "المدير في OpenBao.", code=FLAG_LIMIT_CODE)


def same_entity(path, master_ref):
    """R-50 a / R-57: an item path belongs to the entity whose master key is named (``zatca/<entity>/...``)."""
    master_path, _ = parse_ref(master_ref)
    return path.startswith(master_path.rpartition("/")[0] + "/")


def _refuse_other_entity(path, master_ref):
    if not same_entity(path, master_ref):
        raise SecretStoreRefused("This key belongs to another entity's master key (R-50).",
                                 "هذا المفتاح محمي بالمفتاح الرئيسي لكيان آخر.")


def make_ref(path, version):
    return f"{path}#v{version}"


def parse_ref(ref):
    path, _, version = ref.rpartition("#v")
    if not path or not version.isdigit():
        raise ValueError(f"not a secret-store reference: {ref!r}")
    return path, int(version)


def entity_prefix(entity_code):
    return f"zatca/{entity_code}/"


def super_key_path(group_code):
    return f"zatca/_group/{group_code}/super_key"


def master_key_path(entity_code):
    return f"{entity_prefix(entity_code)}master_key"


def private_key_path(entity_code, egs_code):
    return f"{entity_prefix(entity_code)}{egs_code}/private_key"


def secret_path(entity_code, egs_code, kind):
    return f"{entity_prefix(entity_code)}{egs_code}/{kind}_csid_secret"


class OpenBaoSecretStore:
    """Interface to OpenBao (D-3). Address, KV mount and AppRole credential *files* come from settings; no token,
    role id or secret id is ever stored in the ERP database.

    Built (OQ-34 (f) default, D-19; security sign-off pending): version **retirement** on the KV v2 engine
    (``ZATCA_OPENBAO_KV_MOUNT``, default ``zatca-kv`` as ``deploy/openbao/bootstrap.sh`` creates it):

    - :meth:`retire_version` adds the version number to **one** ``custom_metadata`` key, ``retired_versions``
      (ascending, comma-separated, e.g. ``"1,2,5"``), with a JSON merge patch (``PATCH <mount>/metadata/<path>``)
      and reads it back. It only ever adds a number: it never sends a null or a shorter list (asserted), never
      deletes, destroys or trims a version and reads no ``data/``. It runs as the separate **retirement
      identity** (AppRole ``erp-retire``, policy ``erp-retire-<entity>``: metadata read and patch of its own
      entity, ``custom_metadata`` only), whose credentials only the scheduler container holds; only the
      retirement job calls it, one run at a time and under the entity's lock, so the read-modify-write of the
      list never races.
    - :meth:`list_versions` and :meth:`check_usable` read ``metadata/`` as the signing identity, which may read
      ``data/`` and ``metadata/`` but never write ``metadata/`` (policy ``erp-signer-<entity>``).
    - every read of a version (:meth:`sign`, :meth:`public_key_der`, :meth:`read_secret`) names one explicit
      version and first checks the list: a retired version is refused with ``SecretStoreRefused``
      (``secret_version_retired``, R-57) before any data is read.

    Limit: KV v2 allows 512 bytes per ``custom_metadata`` value, i.e. about 140 retired versions of one path (a
    renewal adds one per path). Beyond it the store refuses with ``retirement_flag_limit``, checked locally before
    the patch and mapped from OpenBao's 400 (OQ-34 (f)). Redirects are never followed (as R-44 for ZATCA).

    Not built until the OpenBao design is signed off (OQ-3 b, OQ-34 a): reading and unwrapping key material,
    writes and the key hierarchy. Those calls raise :class:`SecretStoreUnavailable` (reads only after the
    retirement check), so no environment silently signs without the real store.

    ``transport`` (tests): ``callable(method, url, headers, body_bytes) -> (status, dict)``; ``handlers`` (tests):
    extra urllib handlers for the default transport. Tests never reach a real OpenBao.
    """

    IDENTITIES = {
        "signer": ("ZATCA_OPENBAO_ROLE_ID_FILE", "ZATCA_OPENBAO_SECRET_ID_FILE"),
        "retire": ("ZATCA_OPENBAO_RETIRE_ROLE_ID_FILE", "ZATCA_OPENBAO_RETIRE_SECRET_ID_FILE"),
    }
    RETIRED_KEY = "retired_versions"
    VALUE_LIMIT = 512  # bytes per custom_metadata value (KV v2)

    def __init__(self, transport=None, handlers=()):
        self.address = getattr(settings, "ZATCA_OPENBAO_ADDRESS", "").rstrip("/")
        self.auth_role = getattr(settings, "ZATCA_OPENBAO_AUTH_ROLE", "")
        self.kv_mount = getattr(settings, "ZATCA_OPENBAO_KV_MOUNT", "") or "zatca-kv"
        self.ca_file = getattr(settings, "ZATCA_OPENBAO_CA_FILE", "") or None
        self._handlers = tuple(handlers)
        self._transport = transport or self._http
        self._tokens = {}  # identity -> (token, monotonic expiry); process memory only, never logged or stored
        self._lock = threading.Lock()

    def _unavailable(self, *args, **kwargs):
        raise SecretStoreUnavailable("The OpenBao client is not deployed yet (zatca OQ-34).",
                                     "لم يُفعَّل الاتصال بمخزن OpenBao بعد.")

    create_super_key = create_master_key = rotate_master_key = check = _unavailable
    write_private_key = write_secret = _unavailable

    # -- transport and login --
    def _opener(self):
        from .client import NoRedirects  # never follow a redirect (R-44 pattern)

        context = ssl.create_default_context(cafile=self.ca_file)
        return urllib.request.build_opener(NoRedirects, urllib.request.HTTPSHandler(context=context), *self._handlers)

    def _http(self, method, url, headers, body):
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with self._opener().open(request, timeout=10) as response:  # noqa: S310 (https only)
                raw = response.read()
                return response.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:  # also every 3xx: NoRedirects returns it as the answer
            try:
                raw = exc.read()
                return exc.code, (json.loads(raw) if raw else {})
            except ValueError:
                return exc.code, {}
        except (urllib.error.URLError, OSError, ValueError):
            raise SecretStoreUnavailable() from None

    def _call(self, method, api_path, *, identity=None, body=None, content_type="application/json"):
        if not self.address.startswith("https://"):
            raise SecretStoreUnavailable("OpenBao is not configured (ZATCA_OPENBAO_ADDRESS, https only).",
                                         "لم يُضبط عنوان مخزن OpenBao.")
        headers = {"Content-Type": content_type, "Accept": "application/json"}
        if identity:
            headers["X-Vault-Token"] = self._token(identity)
        data = json.dumps(body).encode() if body is not None else None
        status, payload = self._transport(method, f"{self.address}/v1/{api_path}", headers, data)
        if status is None or status >= 500 or status == 429:  # 503 = sealed (R-47)
            raise SecretStoreUnavailable()
        if 300 <= status < 400:
            raise SecretStoreRefused("OpenBao answered with a redirect; the ERP never follows redirects.",
                                     "أجاب مخزن OpenBao بإعادة توجيه؛ لا يتبع النظام إعادة التوجيه.",
                                     code="openbao_redirect")
        if status in (401, 403):
            if identity:
                self._tokens.pop(identity, None)
            raise SecretStoreRefused("OpenBao refused this request for the ERP's identity (policy).",
                                     "رفض مخزن OpenBao هذا الطلب لهوية النظام (السياسة).", code="openbao_forbidden")
        return status, payload if isinstance(payload, dict) else {}

    @staticmethod
    def _read_file(setting):
        path = getattr(settings, setting, "")
        if not path:
            raise SecretStoreUnavailable(f"OpenBao credentials are not configured ({setting}).",
                                         "لم تُضبط بيانات الدخول إلى مخزن OpenBao.")
        try:
            with open(path, encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            raise SecretStoreUnavailable(f"OpenBao credentials cannot be read ({setting}).",
                                         "تعذرت قراءة بيانات الدخول إلى مخزن OpenBao.") from None

    def _token(self, identity):
        with self._lock:
            cached = self._tokens.get(identity)
            if cached and cached[1] > time.monotonic():
                return cached[0]
        role_setting, secret_setting = self.IDENTITIES[identity]
        status, payload = self._call("POST", "auth/approle/login", body={
            "role_id": self._read_file(role_setting), "secret_id": self._read_file(secret_setting)})
        auth = payload.get("auth") or {}
        token = auth.get("client_token")
        if status != 200 or not token:
            raise SecretStoreUnavailable("OpenBao login failed.", "تعذر تسجيل الدخول إلى مخزن OpenBao.")
        lifetime = max(0, int(auth.get("lease_duration") or 0) - 60)
        with self._lock:
            self._tokens[identity] = (token, time.monotonic() + lifetime)
        return token

    # -- KV v2 metadata (R-57 to R-60) --
    @staticmethod
    def _parse_time(text):
        if not text:
            return None
        head, _, tail = text.rstrip("Z").partition(".")
        fraction = (tail[:6] if tail else "0").ljust(6, "0")
        return datetime.fromisoformat(f"{head}.{fraction}+00:00")

    @classmethod
    def _retired_set(cls, metadata):
        text = ((metadata or {}).get("custom_metadata") or {}).get(cls.RETIRED_KEY) or ""
        return {int(v) for v in text.split(",") if v.strip().isdigit()}

    def _metadata(self, path, identity):
        status, payload = self._call("GET", f"{self.kv_mount}/metadata/{path}", identity=identity)
        if status == 404:
            return None
        if status != 200:
            raise SecretStoreUnavailable()
        return payload.get("data") or {}

    @staticmethod
    def _known(metadata, version):
        info = ((metadata or {}).get("versions") or {}).get(str(version))
        if info is None or info.get("destroyed"):
            raise SecretStoreRefused("Unknown secret-store item.", "عنصر غير معروف في مخزن الأسرار.")

    def list_versions(self, path, master_ref):
        """Every version at ``path`` with its write time and retired flag (R-60); signing identity, metadata only."""
        _refuse_other_entity(path, master_ref)
        metadata = self._metadata(path, "signer")
        if metadata is None:
            return []
        retired = self._retired_set(metadata)
        return sorted(({"version": int(v), "created_at": self._parse_time(info.get("created_time")),
                        "retired": int(v) in retired}
                       for v, info in (metadata.get("versions") or {}).items() if not info.get("destroyed")),
                      key=lambda row: row["version"])

    def check_usable(self, ref, master_ref):
        """Refuse a retired (or unknown) version before anything reads it (R-57)."""
        path, version = parse_ref(ref)
        _refuse_other_entity(path, master_ref)
        metadata = self._metadata(path, "signer")
        self._known(metadata, version)
        if version in self._retired_set(metadata):
            raise retired_refusal()

    def retire_version(self, ref, master_ref, *, reason, at=None):
        """Add one version to the path's retired list (R-57). Returns False when it already was retired (nothing
        written). Time and reason are kept in the ERP's events, not in the store."""
        path, version = parse_ref(ref)
        _refuse_other_entity(path, master_ref)
        metadata = self._metadata(path, "retire")
        self._known(metadata, version)
        before = self._retired_set(metadata)
        if version in before:
            return False
        after = before | {version}
        value = ",".join(str(v) for v in sorted(after))
        # Never a null, never a shorter list: the ERP cannot un-retire (R-57).
        assert value and after > before and self._retired_set({"custom_metadata": {self.RETIRED_KEY: value}}) == after
        if len(value.encode()) > self.VALUE_LIMIT:
            raise flag_limit_refusal()
        status, payload = self._call("PATCH", f"{self.kv_mount}/metadata/{path}", identity="retire",
                                     body={"custom_metadata": {self.RETIRED_KEY: value}},
                                     content_type="application/merge-patch+json")
        if 400 <= status < 500:
            errors = " ".join(str(e) for e in (payload.get("errors") or []))
            if status == 400 and "custom_metadata" in errors:  # e.g. the 512-byte value limit
                raise flag_limit_refusal()
            raise SecretStoreRefused(f"OpenBao refused the retirement patch (HTTP {status}).",
                                     f"رفض مخزن OpenBao تعديل الإيقاف (HTTP {status}).", code="openbao_patch_refused")
        if status not in (200, 204):
            raise SecretStoreUnavailable()
        if version not in self._retired_set(self._metadata(path, "retire")):  # read back: never assume
            raise SecretStoreUnavailable("OpenBao did not keep the retirement flag; it is retried.",
                                         "لم يحتفظ مخزن OpenBao بعلامة الإيقاف؛ ستُعاد المحاولة.")
        return True

    # -- reads: the retirement check runs first; reading key material is not built (OQ-3 b, OQ-34 a) --
    def sign(self, key_ref, master_ref, raw_hash):
        self.check_usable(key_ref, master_ref)
        self._unavailable()

    def public_key_der(self, key_ref, master_ref):
        self.check_usable(key_ref, master_ref)
        self._unavailable()

    def read_secret(self, ref, master_ref):
        self.check_usable(ref, master_ref)
        self._unavailable()

class LocalSecretStore:
    """In-memory store (tests and local development only; nothing survives a restart)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._super = {}  # path -> {version: key bytes}
        self._master = {}  # path -> {version: (nonce, wrapped, super_ref)}
        self._items = {}  # path -> {version: (nonce, ciphertext, master_ref)}
        self._written_at = {}  # path -> {version: datetime (UTC)}
        self._retired = {}  # path -> {version: (datetime, reason)} (R-57; the data stays in _items)
        self.sealed = False
        self.unavailable_master_paths = set()
        self.retirement_unavailable = False  # tests: retirement alone fails as "unavailable" (R-58, AT-97)
        self.retirement_refusal = None  # tests: a callable returning the SecretStoreRefused that retirement raises
        self.clock = lambda: datetime.now(dt_timezone.utc)  # tests may move it
        self.calls = []  # operation names, for tests (never values)

    # -- helpers --
    def _check_open(self):
        if self.sealed:
            raise SecretStoreUnavailable()

    def _next_version(self, table, path):
        return max(table.get(path, {}), default=0) + 1

    def _super_key(self, super_ref):
        path, version = parse_ref(super_ref)
        try:
            return self._super[path][version]
        except KeyError:
            raise SecretStoreRefused("Unknown group super key.", "المفتاح الأعلى للمجموعة غير معروف.") from None

    def _master_key(self, master_ref):
        path, version = parse_ref(master_ref)
        if path in self.unavailable_master_paths:
            raise SecretStoreUnavailable("The entity's master key is unavailable.", "المفتاح الرئيسي للكيان غير متاح.")
        try:
            nonce, wrapped, super_ref = self._master[path][version]
        except KeyError:
            raise SecretStoreRefused("Unknown entity master key.", "المفتاح الرئيسي للكيان غير معروف.") from None
        return AESGCM(self._super_key(super_ref)).decrypt(nonce, wrapped, path.encode())

    def _open(self, ref, master_ref):
        """Open an item of the caller's entity. The item is decrypted under the master-key version that protects
        it (recorded with the item), so a rotation never strands an item; another entity's master key is refused
        (R-50 a)."""
        path, version = parse_ref(ref)
        master_path, _ = parse_ref(master_ref)
        if not path.startswith(master_path.rpartition("/")[0] + "/"):
            raise SecretStoreRefused("This key belongs to another entity's master key (R-50).",
                                     "هذا المفتاح محمي بالمفتاح الرئيسي لكيان آخر.")
        try:
            nonce, data, protected_by = self._items[path][version]
        except KeyError:
            raise SecretStoreRefused("Unknown secret-store item.", "عنصر غير معروف في مخزن الأسرار.") from None
        if version in self._retired.get(path, {}):  # R-57: refused before anything is decrypted
            raise retired_refusal()
        if parse_ref(protected_by)[0] != master_path:
            raise SecretStoreRefused("This item is protected by another master key.",
                                     "هذا العنصر محمي بمفتاح رئيسي آخر.")
        return AESGCM(self._master_key(protected_by)).decrypt(nonce, data, path.encode())

    def _write(self, path, value: bytes, master_ref):
        master_path, _ = parse_ref(master_ref)
        if not path.startswith(master_path.rpartition("/")[0] + "/"):
            raise SecretStoreRefused("A key is written only under its own entity's master key (R-50).",
                                     "يُكتب المفتاح تحت المفتاح الرئيسي لكيانه فقط.")
        key = self._master_key(master_ref)
        nonce = os.urandom(12)
        version = self._next_version(self._items, path)
        self._items.setdefault(path, {})[version] = (nonce, AESGCM(key).encrypt(nonce, value, path.encode()),
                                                       master_ref)
        self._written_at.setdefault(path, {})[version] = self.clock()
        return make_ref(path, version)

    # -- hierarchy --
    def create_super_key(self, path):
        with self._lock:
            self._check_open()
            self.calls.append("create_super_key")
            version = self._next_version(self._super, path)
            self._super.setdefault(path, {})[version] = AESGCM.generate_key(256)
            return make_ref(path, version)

    def create_master_key(self, path, super_ref):
        """A new master key (or a new version of it: rotation) wrapped by the group super key (R-50 b)."""
        with self._lock:
            self._check_open()
            self.calls.append("create_master_key")
            wrapping = self._super_key(super_ref)
            nonce = os.urandom(12)
            version = self._next_version(self._master, path)
            wrapped = AESGCM(wrapping).encrypt(nonce, AESGCM.generate_key(256), path.encode())
            self._master.setdefault(path, {})[version] = (nonce, wrapped, super_ref)
            return make_ref(path, version)

    def rotate_master_key(self, path, super_ref):
        """A new master-key version; every item protected by an older version of this master key is re-wrapped
        under the new one in the same operation (item references and versions unchanged; OQ-34 d)."""
        with self._lock:
            self._check_open()
            self.calls.append("rotate_master_key")
            if path in self.unavailable_master_paths:
                raise SecretStoreUnavailable("The entity's master key is unavailable.", "المفتاح الرئيسي للكيان غير متاح.")
            if not self._master.get(path):
                raise SecretStoreRefused("Unknown entity master key.", "المفتاح الرئيسي للكيان غير معروف.")
            wrapping = self._super_key(super_ref)
            nonce = os.urandom(12)
            version = self._next_version(self._master, path)
            new_ref = make_ref(path, version)
            new_master = AESGCM.generate_key(256)
            rewrapped = {}
            for item_path, versions in self._items.items():
                for item_version, (item_nonce, data, protected_by) in versions.items():
                    if parse_ref(protected_by)[0] != path:
                        continue
                    plain = AESGCM(self._master_key(protected_by)).decrypt(item_nonce, data, item_path.encode())
                    fresh = os.urandom(12)
                    rewrapped[(item_path, item_version)] = (
                        fresh, AESGCM(new_master).encrypt(fresh, plain, item_path.encode()), new_ref)
            # all or nothing: the new version and the re-wrapped items are written together
            self._master.setdefault(path, {})[version] = (
                nonce, AESGCM(wrapping).encrypt(nonce, new_master, path.encode()), super_ref)
            for (item_path, item_version), value in rewrapped.items():
                self._items[item_path][item_version] = value
            return new_ref

    def protecting_version(self, ref):
        """The master-key version protecting an item (tests and diagnostics; never key material)."""
        path, version = parse_ref(ref)
        return parse_ref(self._items[path][version][2])[1]

    def check(self, master_ref):
        """Raise SecretStoreUnavailable if the store is sealed or this master key is unavailable (R-47)."""
        with self._lock:
            self._check_open()
            self._master_key(master_ref)

    # -- EGS keys and CSID secrets --
    def write_private_key(self, path, pem: bytes, master_ref):
        with self._lock:
            self._check_open()
            self.calls.append("write_private_key")
            return self._write(path, pem, master_ref)

    def sign(self, key_ref, master_ref, raw_hash: bytes) -> bytes:
        """Sign inside the store: the private key is never returned to the caller (R-6)."""
        with self._lock:
            self._check_open()
            self.calls.append("sign")
            key = crypto.load_private_key(self._open(key_ref, master_ref))
        return crypto.sign_hash(key, raw_hash)

    def public_key_der(self, key_ref, master_ref) -> bytes:
        with self._lock:
            self._check_open()
            key = crypto.load_private_key(self._open(key_ref, master_ref))
        return crypto.spki_der(key.public_key())

    def write_secret(self, path, value: str, master_ref):
        with self._lock:
            self._check_open()
            self.calls.append("write_secret")
            return self._write(path, value.encode("utf-8"), master_ref)

    def read_secret(self, ref, master_ref) -> str:
        with self._lock:
            self._check_open()
            self.calls.append("read_secret")
            return self._open(ref, master_ref).decode("utf-8")

    # -- retired versions (D-19, R-57 to R-60) --
    def _item_version(self, ref, master_ref):
        path, version = parse_ref(ref)
        _refuse_other_entity(path, master_ref)
        if version not in self._items.get(path, {}):
            raise SecretStoreRefused("Unknown secret-store item.", "عنصر غير معروف في مخزن الأسرار.")
        return path, version

    def check_usable(self, ref, master_ref):
        """Refuse a retired (or unknown, or another entity's) version before it is used (R-57)."""
        with self._lock:
            self._check_open()
            path, version = self._item_version(ref, master_ref)
            if version in self._retired.get(path, {}):
                raise retired_refusal()

    def retire_version(self, ref, master_ref, *, reason, at=None):
        """Mark one version retired (R-57). Its data stays; returns False when it already was retired."""
        with self._lock:
            self._check_open()
            if self.retirement_unavailable:
                raise SecretStoreUnavailable()
            if self.retirement_refusal is not None:
                raise self.retirement_refusal()
            self.calls.append("retire_version")
            path, version = self._item_version(ref, master_ref)
            if version in self._retired.get(path, {}):
                return False
            self._retired.setdefault(path, {})[version] = (at or self.clock(), reason)
            return True

    def list_versions(self, path, master_ref):
        """Every version at ``path``: number, write time, retired flag (R-60). Never values."""
        with self._lock:
            self._check_open()
            _refuse_other_entity(path, master_ref)
            self.calls.append("list_versions")
            return [{"version": v, "created_at": self._written_at.get(path, {}).get(v), "retired":
                     v in self._retired.get(path, {})} for v in sorted(self._items.get(path, {}))]

    # -- test hooks (never values) --
    def is_retired(self, ref):
        path, version = parse_ref(ref)
        return version in self._retired.get(path, {})

    def has_data(self, ref):
        path, version = parse_ref(ref)
        return bool(self._items.get(path, {}).get(version, (None, b""))[1])

    def backdate(self, ref, at):
        """Set when a version was written (R-60 grace tests)."""
        path, version = parse_ref(ref)
        self._written_at[path][version] = at


_override = None
_instances = {}


LOCAL_STORE_PATH = "zatca.keystore.LocalSecretStore"


def local_store_refusal():
    """Why the configured store may not be used, or "" (system check ``zatca.E001`` and :func:`get_store`): the
    in-memory :class:`LocalSecretStore` is for tests and local development only, so it is refused when DEBUG is
    off or production is allowed (R-6, R-29)."""
    path = getattr(settings, "ZATCA_SECRET_STORE", "zatca.keystore.OpenBaoSecretStore")
    try:
        is_local = path == LOCAL_STORE_PATH or issubclass(import_string(path), LocalSecretStore)
    except ImportError:
        return ""
    if not is_local:
        return ""
    if getattr(settings, "ZATCA_PRODUCTION_ALLOWED", False):
        return "ZATCA_SECRET_STORE is the in-memory LocalSecretStore while ZATCA_PRODUCTION_ALLOWED is on."
    if not settings.DEBUG:
        return "ZATCA_SECRET_STORE is the in-memory LocalSecretStore while DEBUG is off."
    return ""


def get_store():
    if _override is not None:
        return _override
    path = getattr(settings, "ZATCA_SECRET_STORE", "zatca.keystore.OpenBaoSecretStore")
    refusal = local_store_refusal()
    if refusal:
        raise ImproperlyConfigured(f"{refusal} Use the OpenBao store (zatca D-3, R-6).")
    if path not in _instances:
        _instances[path] = import_string(path)()
    return _instances[path]


def override(store):
    """Install ``store`` (tests); returns the previous override."""
    global _override
    previous, _override = _override, store
    return previous
