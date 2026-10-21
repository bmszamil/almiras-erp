#!/bin/bash
# Render the per-group and per-entity OpenBao policies from the templates in policies/.
# New subsidiaries are data, not code (CLAUDE.md): add the entity code to the list and re-run.
#
#   ./render-policies.sh <GROUP_CODE> <ENTITY_CODE>... [--out DIR]
#   ./render-policies.sh ALM ALM SUB1 SUB2 --out /tmp/almiras-policies
#
# Writes <out>/group-super-admin-<group>.hcl, <out>/entity-custodian-<e>.hcl, <out>/erp-signer-<e>.hcl,
# <out>/erp-onboard-<e>.hcl, <out>/erp-retire-<e>.hcl (policy names in lower case, as OpenBao stores them) and
# backup-snapshot.hcl.
# Contains no secret; review the files, then load them with bootstrap.sh (or "bao policy write <name> <file>").
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
out="${here}/rendered"
args=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --out) out="$2"; shift 2 ;;
        *) args+=("$1"); shift ;;
    esac
done
if [[ ${#args[@]} -lt 2 ]]; then
    sed -n '2,12p' "$0" >&2
    exit 64
fi
group="${args[0]}"
entities=("${args[@]:1}")

valid() { [[ "$1" =~ ^[A-Z0-9_]{2,16}$ ]]; }
valid "${group}" || { echo "invalid group code: ${group}" >&2; exit 65; }
for e in "${entities[@]}"; do
    valid "${e}" || { echo "invalid entity code: ${e}" >&2; exit 65; }
done

mkdir -p "${out}"
render() {  # render <template> <output> <VAR=value>...
    local tmpl="$1" dest="$2" text
    shift 2
    text="$(cat "${tmpl}")"
    for kv in "$@"; do
        text="${text//\$\{${kv%%=*}\}/${kv#*=}}"
    done
    # shellcheck disable=SC2016  # a literal "${" is what we look for
    if grep -q '\${' <<< "${text}"; then
        echo "unrendered placeholder left in ${dest}" >&2
        exit 66
    fi
    printf '%s\n' "${text}" > "${dest}"
}

# OpenBao stores policy names in lower case; mount paths keep the entity code as written (zatca-SUB1).
# The Managing Partner's policy names every entity explicitly (policy globs cannot match "zatca-<code>").
blocks=""
for e in "${entities[@]}"; do
    block="$(cat "${here}/policies/group-super-admin-entity.hcl.tmpl")"
    blocks+="${block//\$\{ENTITY\}/${e}}"$'\n'
done
render "${here}/policies/group-super-admin.hcl.tmpl" "${out}/group-super-admin-${group,,}.hcl" "GROUP=${group}" \
    "ENTITY_BLOCKS=${blocks}"
for e in "${entities[@]}"; do
    render "${here}/policies/entity-custodian.hcl.tmpl" "${out}/entity-custodian-${e,,}.hcl" "ENTITY=${e}"
    render "${here}/policies/erp-signer.hcl.tmpl" "${out}/erp-signer-${e,,}.hcl" "ENTITY=${e}"
    render "${here}/policies/erp-onboard.hcl.tmpl" "${out}/erp-onboard-${e,,}.hcl" "ENTITY=${e}"
    render "${here}/policies/erp-retire.hcl.tmpl" "${out}/erp-retire-${e,,}.hcl" "ENTITY=${e}"
done
cp "${here}/policies/backup-snapshot.hcl" "${out}/backup-snapshot.hcl"
echo "rendered $(find "${out}" -name '*.hcl' | wc -l) policies into ${out}"
