"""Secret store for ZATCA keys and CSID secrets (zatca D-3, D-12, R-6, R-41, R-47, R-50). References only in the DB.

The ERP database holds only references ``<path>#v<version>`` (``EgsUnit.private_key_ref``,
``EgsCredential.secret_ref``, ``EntityMasterKey.key_ref``, ``GroupSuperKey.key_ref``). Key material lives in
the store. The hierarchy (D-12, R-50): the group super key protects each entity master key; an entity
master key protects only that entity's EGS private keys and CSID secrets. Every operation names the master
key it opens the item under, and the store refuses an item that is not protected by that very master key
(R-50 a, AT-70).

Backends:

- :class:`OpenBaoSecretStore`: the interface to the KSA-hosted OpenBao (D-3, D-5). Not deployed yet; every
  call raises :class:`SecretStoreUnavailable`, so a misconfigured environment fails closed (OQ-34).
- :class:`LocalSecretStore`: in-memory store for tests and local development only. It wraps keys with
  AES-GCM along the hierarchy and can be "sealed" or have one entity's master key made unavailable.

The setting ``ZATCA_SECRET_STORE`` (dotted path) chooses the backend; tests install a local store with
:func:`override`.
"""
import os
import threading

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.utils.module_loading import import_string

from . import crypto


class SecretStoreUnavailable(Exception):
    """The store is sealed or unreachable, or the entity's master key is unavailable (R-47). Transient."""

    def __init__(self, en="The secret store is sealed or unreachable.", ar="مخزن الأسرار مغلق أو لا يمكن الوصول إليه."):
        self.en, self.ar = en, ar
        super().__init__(f"{en} / {ar}")


class SecretStoreRefused(Exception):
    """The request breaks the key hierarchy (e.g. another entity's master key, R-50 a). Never transient."""

    def __init__(self, en, ar):
        self.en, self.ar = en, ar
        super().__init__(f"{en} / {ar}")


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
    """Interface to OpenBao (D-3). Address and auth role come from settings; no token is stored in the ERP.

    Not implemented until the OpenBao design is signed off (OQ-3 b, OQ-34): every method raises
    :class:`SecretStoreUnavailable`, so no environment silently signs without the real store.
    """

    def __init__(self):
        self.address = getattr(settings, "ZATCA_OPENBAO_ADDRESS", "")
        self.auth_role = getattr(settings, "ZATCA_OPENBAO_AUTH_ROLE", "")

    def _unavailable(self, *args, **kwargs):
        raise SecretStoreUnavailable("The OpenBao client is not deployed yet (zatca OQ-34).",
                                     "لم يُفعَّل الاتصال بمخزن OpenBao بعد.")

    create_super_key = create_master_key = rotate_master_key = _unavailable
    write_private_key = sign = public_key_der = write_secret = read_secret = _unavailable


class LocalSecretStore:
    """In-memory store (tests and local development only; nothing survives a restart)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._super = {}  # path -> {version: key bytes}
        self._master = {}  # path -> {version: (nonce, wrapped, super_ref)}
        self._items = {}  # path -> {version: (nonce, ciphertext, master_ref)}
        self.sealed = False
        self.unavailable_master_paths = set()
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
        path, version = parse_ref(ref)
        master_path, _ = parse_ref(master_ref)
        if not path.startswith(master_path.rpartition("/")[0] + "/"):
            raise SecretStoreRefused("This key belongs to another entity's master key (R-50).",
                                     "هذا المفتاح محمي بالمفتاح الرئيسي لكيان آخر.")
        try:
            nonce, data, protected_by = self._items[path][version]
        except KeyError:
            raise SecretStoreRefused("Unknown secret-store item.", "عنصر غير معروف في مخزن الأسرار.") from None
        if protected_by != master_ref:
            raise SecretStoreRefused("This item is protected by another master key version.",
                                     "هذا العنصر محمي بنسخة أخرى من المفتاح الرئيسي.")
        return AESGCM(self._master_key(master_ref)).decrypt(nonce, data, path.encode())

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

    rotate_master_key = create_master_key

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


_override = None
_instances = {}


def get_store():
    if _override is not None:
        return _override
    path = getattr(settings, "ZATCA_SECRET_STORE", "zatca.keystore.OpenBaoSecretStore")
    if path not in _instances:
        _instances[path] = import_string(path)()
    return _instances[path]


def override(store):
    """Install ``store`` (tests); returns the previous override."""
    global _override
    previous, _override = _override, store
    return previous
