# Policy "backup-snapshot": the backup service's periodic token (backup/openbao-snapshot.sh). Raft snapshot only.
path "sys/storage/raft/snapshot" {
  capabilities = ["read"]
}
