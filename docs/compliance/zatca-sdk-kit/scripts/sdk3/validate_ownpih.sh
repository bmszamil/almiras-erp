#!/bin/bash
# Like validate_all.sh, but each file is validated with an SDK config whose pih.txt holds the file's own PIH
# (so a chained document's PIH stage is checked against the PIH it carries, i.e. the SDK checks format only).
# Base config: $BASE_CONFIG (default: R3.4.8 copy). Usage: validate_ownpih.sh OUTDIR file.xml...
D=$(cd "$(dirname "$0")" && pwd); OUTD=$1; shift; mkdir -p "$OUTD"
BASE=${BASE_CONFIG:-${SDK3_WORK:?set SDK3_WORK}/r348/Configuration/config.json}
for f in "$@"; do
  n=$(basename "$f" .xml)
  python3 -I - "$f" "$BASE" "$OUTD/$n.pih" "$OUTD/$n.config.json" <<'PY'
import json, sys
from lxml import etree
f, base, pihf, cfgf = sys.argv[1:]
ns = {"cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
      "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"}
pih = etree.parse(f).xpath("//cac:AdditionalDocumentReference[cbc:ID='PIH']//cbc:EmbeddedDocumentBinaryObject", namespaces=ns)[0].text.strip()
open(pihf, "w").write(pih)
cfg = json.load(open(base)); cfg["pihPath"] = pihf
json.dump(cfg, open(cfgf, "w"))
PY
done
printf '%s\n' "$@" | xargs -P "${JOBS:-4}" -I{} bash -c 'f="{}"; n=$(basename "$f" .xml); OUT='"$OUTD"' SDK_CONFIG='"$OUTD"'/"$n".config.json '"$D"'/fat348.sh -validate -invoice "$f" > '"$OUTD"'/"$n".log 2>&1'
python3 -I "$D/summarise.py" "$OUTD"/*.log > "$OUTD/summary.txt"; cat "$OUTD/summary.txt"
