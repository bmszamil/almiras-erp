#!/bin/bash
# Nightly OpenBao raft snapshot (zatca OQ-3 remainder: secret-store backup/restore and DR test).
# The snapshot is already encrypted by OpenBao's barrier (it is useless without the unseal key held by the
# Managing Partner); it is additionally encrypted with age to the owner's public key(s) before it is stored.
#
# Token: a periodic token with ONLY the "backup-snapshot" policy (openbao/policies/backup-snapshot.hcl),
# delivered as a Docker secret file (BAO_TOKEN_FILE). Never the root token.
# Env: BAO_ADDR (https://openbao:8200), BAO_CACERT (internal CA), BAO_TOKEN_FILE,
#      BACKUP_RETENTION_OPENBAO_DAYS (35).
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "$0")/lib.sh"

keep_days="${BACKUP_RETENTION_OPENBAO_DAYS:-35}"
snap_dir="${BACKUP_ROOT}/openbao"

fail() {
    log "openbao-snapshot: FAILED: $*"
    backup_metric openbao_snapshot 1
    exit 1
}
trap 'fail "line ${LINENO}"' ERR

[[ -n "${BAO_TOKEN_FILE:-}" && -s "${BAO_TOKEN_FILE}" ]] || fail "BAO_TOKEN_FILE missing"
BAO_TOKEN="$(tr -d '\n' < "${BAO_TOKEN_FILE}")"
export BAO_TOKEN BAO_ADDR="${BAO_ADDR:-https://openbao:8200}"
require_recipients || fail "no recipients"
mkdir -p "${snap_dir}"

stamp="$(date +%Y%m%dT%H%M%S%z)"
raw="$(mktemp /tmp/openbao-snap.XXXXXX)"
trap 'rm -f "${raw}"' EXIT
bao operator raft snapshot save "${raw}"
[[ -s "${raw}" ]] || fail "empty snapshot"
out="${snap_dir}/openbao-${stamp}.snap.age"
age --encrypt --recipients-file "${BACKUP_RECIPIENTS}" --output "${out}.partial" "${raw}"
sync "${out}.partial"
mv "${out}.partial" "${out}"
(cd "${snap_dir}" && sha256sum "$(basename "${out}")" > "$(basename "${out}").sha256")

prune "${snap_dir}" "openbao-*.snap.age" "${keep_days}" || true
prune "${snap_dir}" "openbao-*.snap.age.sha256" "${keep_days}" || true
backup_metric openbao_snapshot 0 "$(stat -c %s "${out}")"
log "openbao-snapshot: OK ${out}"
