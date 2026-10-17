#!/bin/bash
# SDK -sign each file into OUTDIR/<name>.xml (SDK dummy key/cert). Usage: sign_all.sh OUTDIR file.xml...
D=$(cd "$(dirname "$0")" && pwd); OUTD=$1; shift; mkdir -p "$OUTD"
printf '%s\n' "$@" | xargs -P "${JOBS:-4}" -I{} bash -c 'f="{}"; n=$(basename "$f" .xml); OUT='"$OUTD"' '"$D"'/fat348.sh -sign -invoice "$f" -signedInvoice '"$OUTD"'/"$n".xml > '"$OUTD"'/"$n".sign.log 2>&1; grep -h "INVOICE HASH" '"$OUTD"'/"$n".sign.log | sed "s/.*= /$n /"'
