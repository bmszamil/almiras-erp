#!/bin/bash
# Usage: validate_all.sh OUTDIR file.xml...   -> OUTDIR/<name>.log per file, OUTDIR/summary.txt
# env SDK_CONFIG / PROVIDERS passed through to fat348.sh; JOBS (default 4)
D=$(cd "$(dirname "$0")" && pwd); OUTD=$1; shift; mkdir -p "$OUTD"
printf '%s\n' "$@" | xargs -P "${JOBS:-4}" -I{} bash -c 'f="{}"; n=$(basename "$f" .xml); OUT='"$OUTD"' '"$D"'/fat348.sh -validate -invoice "$f" > '"$OUTD"'/"$n".log 2>&1'
python3 -I "$D/summarise.py" "$OUTD"/*.log > "$OUTD/summary.txt"; cat "$OUTD/summary.txt"
