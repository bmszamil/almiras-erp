#!/bin/bash
# Run ZATCA SDK R4.0.0 offline (no network namespace) from a work dir that holds a COPY of the SDK.
# Env: SDK4_WORK (work dir with r400/ = copy of zatca-einvoicing-sdk-238-R4.0.0 and r400/Configuration/config.json
#      holding absolute paths), SDK_CONFIG (other config.json), OUT (cwd for outputs),
#      PROVIDERS = jdk (default: JDK provider order; R4.0.0 verifies secp256k1 itself, see findings 2026-10-17 §7)
#      | sunbc (SUN first, BouncyCastle second, as for R3.4.8) | bc (BouncyCastle first).
D=$(cd "$(dirname "$0")" && pwd)
W=${SDK4_WORK:?set SDK4_WORK to the work dir that holds r400/ (a copy of the SDK) and out/}
cd "${OUT:-$W/out}" || exit 1
SEC=()
case "${PROVIDERS:-jdk}" in
  bc) SEC=(-Djava.security.properties==$D/../bc-full.security) ;;
  sunbc) SEC=(-Djava.security.properties==$D/bc-second.security) ;;
esac
exec unshare -n env -u JAVA_TOOL_OPTIONS FATOORA_HOME=$W/r400/Apps SDK_CONFIG=${SDK_CONFIG:-$W/r400/Configuration/config.json} \
  java "${SEC[@]}" -Djdk.module.illegalAccess=deny -Djdk.sunec.disableNative=false \
  -jar $W/r400/Apps/zatca-einvoicing-sdk-238-R4.0.0.jar --globalVersion 238-R4.0.0 "$@"
