#!/bin/bash
# run ZATCA CLI with no network namespace
cd /tmp/claude-0/-home-user-almiras-erp/ea5f57b4-d147-5a57-9a1e-1750b3cc3f70/scratchpad/sdk-work/out
exec unshare -n env -u JAVA_TOOL_OPTIONS FATOORA_HOME=/tmp/claude-0/-home-user-almiras-erp/ea5f57b4-d147-5a57-9a1e-1750b3cc3f70/scratchpad/sdk-work/sdk/Apps SDK_CONFIG=/tmp/claude-0/-home-user-almiras-erp/ea5f57b4-d147-5a57-9a1e-1750b3cc3f70/scratchpad/sdk-work/sdk/Configuration/config.json java -Djdk.module.illegalAccess=deny -Djdk.sunec.disableNative=false -jar /tmp/claude-0/-home-user-almiras-erp/ea5f57b4-d147-5a57-9a1e-1750b3cc3f70/scratchpad/sdk-work/sdk/Apps/cli-3.0.8-jar-with-dependencies.jar --globalVersion 3.0.8 -certpassword 123456789 "$@"
