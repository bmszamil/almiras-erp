#!/bin/bash
# Create the random secrets of a new install in deploy/secrets/ (directory 0700; files 0644 so the container
# user can read its Docker secret; git-ignored). Run ON THE KSA HOST, once. Never overwrites an existing file.
# It does NOT create: TLS certificates (secrets/tls/, secrets/openbao-tls/: from the owner's CA), the age backup
# recipients (secrets/backup-recipients.txt: the owner's PUBLIC keys), the SMTP password (from the mail admin)
# or the OpenBao credentials (written by openbao/bootstrap.sh during the key ceremony).
#
#   ./init-secrets.sh            # generate what is missing
#   ./init-secrets.sh --check    # only list what is still missing (exit 1 if anything is)
set -euo pipefail

dir="$(cd "$(dirname "$0")" && pwd)/secrets"
mkdir -p "${dir}" "${dir}/tls" "${dir}/openbao-tls"
chmod 0700 "${dir}"

random() {  # 64 random bytes, URL-safe base64 (no padding): ~86 characters
    head -c 64 /dev/urandom | base64 -w0 | tr '+/' '-_' | tr -d '='
}

generated=(django_secret_key db_superuser_password db_owner_password db_app_password db_backup_password
           db_monitor_password hr_personal_data_keys hr_personal_data_hash_key banking_data_keys
           grafana_admin_password)
provided=(smtp_password backup-recipients.txt tls/erp.crt tls/erp.key openbao-tls/server.crt
          openbao-tls/server.key openbao-tls/ca.crt)
from_bootstrap=(openbao_backup_token openbao_erp_role_id openbao_erp_secret_id)

if [[ "${1:-}" == "--check" ]]; then
    missing=0
    for f in "${generated[@]}" "${provided[@]}" "${from_bootstrap[@]}"; do
        if [[ ! -s "${dir}/${f}" ]]; then
            echo "missing: secrets/${f}"
            missing=1
        fi
    done
    exit "${missing}"
fi

umask 022
for f in "${generated[@]}"; do
    if [[ -s "${dir}/${f}" ]]; then
        echo "kept:      secrets/${f}"
    else
        random > "${dir}/${f}"
        chmod 0644 "${dir}/${f}"
        echo "generated: secrets/${f}"
    fi
done
# Placeholders so "docker compose up" can start before the key ceremony; bootstrap.sh fills them.
for f in "${from_bootstrap[@]}"; do
    [[ -e "${dir}/${f}" ]] || { : > "${dir}/${f}"; chmod 0644 "${dir}/${f}"; }
done
for f in "${provided[@]}"; do
    [[ -s "${dir}/${f}" ]] || echo "TO PROVIDE: secrets/${f}"
done
echo "Copy secrets/hr_personal_data_*, banking_data_keys and django_secret_key to the offline key envelope too:"
echo "without them encrypted HR and banking data in any backup cannot be read (README 'Key ceremony')."
