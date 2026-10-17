#!/bin/bash
# One-time OpenBao bootstrap, run by the MANAGING PARTNER during the key ceremony (deploy/README.md), AFTER
# "bao operator init" and the first unseal, with the initial root token typed in (never stored):
#
#   read -rs BAO_TOKEN && export BAO_TOKEN
#   ./openbao/bootstrap.sh ALM ALM SUB1 SUB2        # group code, then every entity with a ZATCA profile
#   bao token revoke -self                          # then revoke the root token
#
# Idempotent: re-running for a new subsidiary adds its mount, master key and policies (entities are data).
# What it does (template for zatca OQ-34; security sign-off pending):
#   - checks the configuration-managed audit device is active (fails otherwise),
#   - kv-v2 "zatca-kv" for EGS private keys and CSID secrets (stored as ciphertext under the entity master key),
#   - transit "zatca-group-<GROUP>" with the non-exportable group super key "super_key",
#   - transit "zatca-<ENTITY>" with the non-exportable entity master key "master_key", per entity,
#   - the rendered policies, AppRole "erp-zatca" (runtime signer, reads only its entities' paths) and
#     "erp-onboard-<ENTITY>" (single-use secret-ids issued by a custodian), userpass for named humans,
#   - a periodic token with only the backup-snapshot policy for backup/openbao-snapshot.sh.
# Credentials it creates (AppRole role-id/secret-id, backup token) are written to files under
# $ALMIRAS_SECRETS_DIR (default deploy/secrets: directory 0700, git-ignored), never printed.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
secrets_dir="${ALMIRAS_SECRETS_DIR:-${here}/../secrets}"
app_cidr="${ALMIRAS_BAO_APP_CIDR:-172.30.20.0/24}"
# The bao CLI inside the running openbao container; BAO_TOKEN is passed from this shell's environment.
read -r -a BAO <<< "${BAO_CMD:-docker compose exec -T -e BAO_TOKEN openbao bao}"

[[ $# -ge 2 ]] || { sed -n '2,9p' "$0" >&2; exit 64; }
[[ -n "${BAO_TOKEN:-}" ]] || { echo "BAO_TOKEN is not set (type it with: read -rs BAO_TOKEN; export BAO_TOKEN)" >&2; exit 64; }
export BAO_TOKEN
group="$1"
shift
entities=("$@")

rendered="$(mktemp -d)"
trap 'rm -rf "${rendered}"' EXIT
"${here}/render-policies.sh" "${group}" "${entities[@]}" --out "${rendered}" > /dev/null

bao() { "${BAO[@]}" "$@"; }
# Capture first: "bao ... | grep -q" would fail under pipefail when grep exits early (SIGPIPE).
has_mount() { local out; out="$(bao secrets list -format=json)"; [[ "${out}" == *"\"$1/\""* ]]; }
has_auth() { local out; out="$(bao auth list -format=json)"; [[ "${out}" == *"\"$1/\""* ]]; }
write_secret_file() {  # write_secret_file <name>  (value on stdin)
    # The directory is 0700 root (host protection); the file must be readable by the container user that
    # receives it as a Docker secret (bind mount keeps host permissions).
    mkdir -p "${secrets_dir}"
    chmod 0700 "${secrets_dir}"
    (umask 022 && cat > "${secrets_dir}/$1")
    chmod 0644 "${secrets_dir}/$1"
    echo "  wrote ${secrets_dir}/$1"
}

echo "== status"
bao status > /dev/null || { echo "OpenBao is sealed or unreachable" >&2; exit 1; }
audits="$(bao audit list)"
[[ "${audits}" == *"almiras-file/"* ]] || { echo "audit device almiras-file missing: check config.hcl" >&2; exit 1; }

echo "== secrets engines"
has_mount zatca-kv || bao secrets enable -path=zatca-kv -version=2 -description="ZATCA EGS keys and CSID secrets (ciphertext)" kv
bao write zatca-kv/config max_versions=0 cas_required=true delete_version_after=0s > /dev/null

if ! has_mount "zatca-group-${group}"; then
    bao secrets enable -path="zatca-group-${group}" -description="Group ${group} super key (D-12)" transit
fi
bao write -f "zatca-group-${group}/keys/super_key" type=aes256-gcm96 exportable=false allow_plaintext_backup=false > /dev/null
bao write "zatca-group-${group}/keys/super_key/config" deletion_allowed=false exportable=false > /dev/null
echo "  group super key ref: zatca/_group/${group}/super_key  (mount zatca-group-${group}, key super_key)"

for e in "${entities[@]}"; do
    if ! has_mount "zatca-${e}"; then
        bao secrets enable -path="zatca-${e}" -description="Entity ${e} master key (D-12, R-50)" transit
    fi
    bao write -f "zatca-${e}/keys/master_key" type=aes256-gcm96 exportable=false allow_plaintext_backup=false > /dev/null
    bao write "zatca-${e}/keys/master_key/config" deletion_allowed=false exportable=false > /dev/null
    echo "  entity ${e} master key ref: zatca/${e}/master_key  (mount zatca-${e}, key master_key)"
done

echo "== policies"
for f in "${rendered}"/*.hcl; do
    name="$(basename "${f}" .hcl)"
    bao policy write "${name}" - < "${f}" > /dev/null
    echo "  ${name}"
done

echo "== auth methods"
has_auth approle || bao auth enable approle
has_auth userpass || bao auth enable userpass

signer_policies="$(printf 'erp-signer-%s,' "${entities[@],,}")"
bao write auth/approle/role/erp-zatca \
    token_policies="${signer_policies%,}" token_ttl=1h token_max_ttl=4h \
    secret_id_ttl=2160h token_bound_cidrs="${app_cidr}" secret_id_bound_cidrs="${app_cidr}" > /dev/null
for e in "${entities[@]}"; do
    bao write "auth/approle/role/erp-onboard-${e}" \
        token_policies="erp-onboard-${e,,}" token_ttl=15m token_max_ttl=30m \
        secret_id_num_uses=1 secret_id_ttl=15m token_bound_cidrs="${app_cidr}" > /dev/null
done

echo "== credentials for the ERP and the backup service"
if [[ ! -s "${secrets_dir}/openbao_erp_role_id" ]]; then
    bao read -field=role_id auth/approle/role/erp-zatca/role-id | write_secret_file openbao_erp_role_id
    bao write -f -field=secret_id auth/approle/role/erp-zatca/secret-id | write_secret_file openbao_erp_secret_id
fi
if [[ ! -s "${secrets_dir}/openbao_backup_token" ]]; then
    bao token create -orphan -policy=backup-snapshot -period=768h -display-name=almiras-backup -field=token \
        | write_secret_file openbao_backup_token
fi

cat <<EOF
== done
Next (Managing Partner):
  1. Create your own named login and the custodians' (no shared accounts):
       bao write auth/userpass/users/<mp-username> token_policies=group-super-admin-${group,,} password=-
       bao write auth/userpass/users/<custodian> token_policies=entity-custodian-<entity> password=-
     and record each custody grant in the ERP (KeyCustodianDelegation, scope master_key, openbao_policy_ref).
  2. Log in as yourself and check you can act without the root token; then revoke it:  bao token revoke -self
  3. Store the unseal key (PGP-encrypted at init) and the age backup identity offline (README key ceremony).
EOF
