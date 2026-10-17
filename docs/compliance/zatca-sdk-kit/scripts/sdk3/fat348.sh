#!/bin/bash
# Run ZATCA SDK R3.4.8 offline (no network namespace) from a work dir that holds a COPY of the SDK.
# Env: SDK3_WORK (work dir with r348/ = copy of zatca-einvoicing-sdk-Java-238-R3.4.8 and r348/Configuration/config.json
#      holding absolute paths), SDK_CONFIG (other config.json, e.g. R4.0.0 rules), OUT (cwd for outputs),
#      PROVIDERS = sunbc (default: SUN first so X509IssuerName is the RFC 2253 / SUN form, BouncyCastle second for
#      secp256k1 on Java >= 16) | bc (BouncyCastle first: BC issuer-name style) | jdk (JDK default; secp256k1 fails on 17+).
D=$(cd "$(dirname "$0")" && pwd)
W=${SDK3_WORK:?set SDK3_WORK to the work dir that holds r348/ (a copy of the SDK) and out/}
cd "${OUT:-$W/out}" || exit 1
SEC=()
case "${PROVIDERS:-sunbc}" in
  bc) SEC=(-Djava.security.properties==$D/../bc-full.security) ;;
  sunbc) SEC=(-Djava.security.properties==$D/bc-second.security) ;;
esac
exec unshare -n env -u JAVA_TOOL_OPTIONS FATOORA_HOME=$W/r348/Apps SDK_CONFIG=${SDK_CONFIG:-$W/r348/Configuration/config.json} \
  java "${SEC[@]}" -Djdk.module.illegalAccess=deny -Djdk.sunec.disableNative=false \
  -jar $W/r348/Apps/zatca-einvoicing-sdk-238-R3.4.8.jar --globalVersion 238-R3.4.8 "$@"
