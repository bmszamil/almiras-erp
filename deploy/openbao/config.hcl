# OpenBao server configuration for the Almiras ERP secret store (zatca D-3, D-5, D-10, D-12, R-41, R-43, R-50).
# Production template: no dev mode, integrated (raft) storage on the KSA host's encrypted disk, TLS only,
# a single unseal key held by the Managing Partner (no Shamir split, no auto-unseal: D-10, R-50), file audit.
# The design of the key hierarchy inside OpenBao is OQ-34 (security sign-off pending): see deploy/README.md.

ui            = false
cluster_name  = "almiras-ksa"
log_level     = "info"
log_format    = "json"

# Integrated storage needs no mlock; the host must have swap disabled or encrypted (deploy/README.md).
disable_mlock = true

# Data on the named volume mounted at /openbao/file (the image entrypoint chowns it to the openbao user).
storage "raft" {
  path    = "/openbao/file/raft"
  node_id = "almiras-bao-1"
}

listener "tcp" {
  address         = "0.0.0.0:8200"
  cluster_address = "0.0.0.0:8201"
  # Server certificate issued by the owner's internal CA for the name "openbao" (and the host name used by the
  # Managing Partner's CLI). Placeholders: the files are mounted from deploy/secrets/openbao-tls/ at install.
  tls_cert_file   = "/openbao/tls/server.crt"
  tls_key_file    = "/openbao/tls/server.key"
  tls_min_version = "tls13"
  # Metrics for Prometheus without a token, only from inside the Docker networks.
  telemetry {
    unauthenticated_metrics_access = true
  }
}

api_addr     = "https://openbao:8200"
cluster_addr = "https://openbao:8201"

telemetry {
  prometheus_retention_time = "24h"
  disable_hostname          = true
}

# Audit device: every request and response, HMAC-ed (secret values never in clear), to a file on the KSA host
# (R-43: logs stay in KSA). Declared here so it exists before the first request and cannot be disabled through
# the API. If OpenBao cannot write the audit log it refuses requests (fail closed).
audit "file" "almiras-file" {
  description = "Almiras ERP secret store audit log"
  options {
    file_path = "/openbao/logs/audit.log"
    mode      = "0600"
  }
}
