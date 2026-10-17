#!/bin/bash
# PostgreSQL archive_command (optional WAL archiving, ALMIRAS_WAL_ARCHIVING=on in deploy/env):
#   archive_command = '/almiras/bin/wal-archive.sh "%p" "%f"'
# Encrypts the WAL segment with age to the PUBLIC recipient keys in /almiras/backup-recipients.txt (no private
# key on the server) and writes it to /backups/wal/<file>.age atomically. Exit 0 only once the file is complete
# and fsynced; PostgreSQL retries on any other exit code and never recycles an unarchived segment.
set -euo pipefail

src="$1"
name="$2"
dest_dir="${ALMIRAS_WAL_DIR:-/backups/wal}"
recipients="${ALMIRAS_BACKUP_RECIPIENTS:-/almiras/backup-recipients.txt}"
dest="${dest_dir}/${name}.age"

if [[ -f "${dest}" ]]; then
    # Written completely by an earlier attempt (only complete files are renamed into place).
    echo "wal-archive: ${name} already archived" >&2
    exit 0
fi

tmp="$(mktemp "${dest_dir}/.${name}.XXXXXX")"
trap 'rm -f "${tmp}"' EXIT
age --encrypt --recipients-file "${recipients}" --output "${tmp}" "${src}"
sync "${tmp}"
mv "${tmp}" "${dest}"
trap - EXIT
