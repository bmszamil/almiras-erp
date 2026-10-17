# Almiras ERP deployment kit

Single-host installation of the Almiras ERP on a Linux VM **hosted in Saudi Arabia**. Everything is open
source and runs on that host: no foreign SaaS, no foreign certificate authority, no external monitoring
(zatca D-5, R-43, OQ-20; invoicing D12, R53). The repository holds no secret, certificate, key or real address:
every value below marked *placeholder* is set at install.

| Component | Image / tool (pinned) | Licence | Role |
| --- | --- | --- | --- |
| `web` | `deploy/Dockerfile` (python 3.13.16-slim, gunicorn 26.2.0) | BSD/MIT | Django behind the proxy, app DB role |
| `scheduler` | same image + supercronic v0.2.48 | MIT | management commands on Asia/Riyadh times |
| `migrate` (one-off) | same image | | migrations as the **owner** DB role |
| `db` | `deploy/postgres/Dockerfile` (postgres 16.15 + age 1.2.1) | PostgreSQL | UTF8 / C.UTF-8, roles per access R45 |
| `backup` | same image + supercronic + bao CLI | | nightly encrypted dumps and OpenBao snapshots |
| `openbao` | openbao 2.7.1 | MPL-2.0 | secret store (ZATCA keys), raft storage, file audit |
| `proxy` | caddy 2.11.7 | Apache-2.0 | TLS with the owner's certificate, static files |
| monitoring | prometheus 3.15.0, alertmanager 0.34.1, node-exporter 1.12.1, postgres-exporter 0.20.1, grafana-oss 12.4.3 (optional) | Apache-2.0 / AGPL-3.0 | metrics, alert rules, e-mail via the KSA mail server |

Layout:

```
deploy/
  Dockerfile, Dockerfile.dockerignore, requirements.lock   application image (hashed, wheels only)
  docker-compose.yml, env.example, init-secrets.sh          the stack, site settings, secret generator
  bin/          entrypoint, migrate, release check (dbrole.py), job wrapper, ZATCA deadline watchdog, health
  scheduler/crontab                                         every scheduled management command
  postgres/     Dockerfile, initdb/ (roles), sql/ (roles, grants, AT-47 check), pg_hba.conf, WAL archiving
  backup/       pg-backup, pg-basebackup, openbao-snapshot, restore-test (drill), smoke.sql, crontab
  openbao/      config.hcl, policies/ (templates), render-policies.sh, bootstrap.sh
  proxy/Caddyfile, monitoring/ (prometheus, alert rules, alertmanager, grafana provisioning)
  secrets/      created on the host only (git-ignored)
```

## 1. What the owner must provide

| Item | Who | Notes |
| --- | --- | --- |
| KSA-hosted Linux VM (and a second KSA location for backup copies) | Managing Partner / IT | Provider and region **in KSA** (R-43, TIR §8.3.1). Suggested minimum: 4 vCPU, 16 GB RAM, 200 GB encrypted data disk + separate backup disk |
| Domain name and DNS record | IT | `ALMIRAS_DOMAIN` |
| TLS certificate for the domain | IT | From a CA of the owner's choice; files `secrets/tls/erp.crt` (chain) and `erp.key`. No ACME client runs |
| Internal CA and OpenBao server certificate (name `openbao`) | IT | `secrets/openbao-tls/{ca.crt,server.crt,server.key}` |
| KSA-hosted SMTP server and an account | IT | Django mail and Alertmanager; password in `secrets/smtp_password` |
| Key holders | Managing Partner | MP holds the OpenBao unseal key and the group super key (D-10, D-12); names the per-entity custodians (R-50 c) |
| Backup encryption keys (age) | Managing Partner + one deputy | Public keys on the server, private keys offline only |
| Legal / security sign-offs | see §10 | PDPL (invoicing OQ-31), OQ-34 key design, OQ-3 b non-exportable keys |

## 2. Host preparation (Debian 13 / Ubuntu 24.04 LTS or similar)

1. Full-disk encryption (LUKS) for the data and backup disks (R-6 "software keys on encrypted disk"); swap
   off or encrypted (OpenBao runs with `disable_mlock`).
2. Time zone and NTP: `timedatectl set-timezone Asia/Riyadh`; `timedatectl set-ntp true` (containers also run
   with `TZ=Asia/Riyadh`; ZATCA timestamps are UTC inside the app).
3. Install Docker Engine and the compose plugin from the distribution or Docker's open-source packages; `age`
   (optional, for restore drills on the host: `apt install age`).
4. Firewall: allow 22 (from the admin network only), 80 and 443. Nothing else is published: Prometheus
   (9090), Alertmanager (9093) and Grafana (3000) listen on `127.0.0.1` only (use an SSH tunnel). OpenBao and
   PostgreSQL have no published port.
5. Directories and owners (uids of the images: postgres 999, openbao 100):

   ```bash
   install -d -o 999 -g 999 -m 0750 /srv/almiras/backups /srv/almiras/backups/wal
   install -d -o 100 -g 1000 -m 0750 /srv/almiras/openbao-audit
   ```
6. Get the code (a reviewed tag of `main`) to e.g. `/opt/almiras-erp`; work in `/opt/almiras-erp/deploy`.

## 3. Configuration

### Site settings (`deploy/.env`, not secret)

`cp env.example .env` and edit. Compose reads it for variable substitution.

| Variable | Default | Meaning |
| --- | --- | --- |
| `ALMIRAS_DOMAIN` | *placeholder* | Public name; also `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS` |
| `ALMIRAS_ALLOWED_CIDRS` | all | Optional office / VPN ranges allowed by the proxy |
| `ALMIRAS_IMAGE`, `ALMIRAS_POSTGRES_IMAGE` | `almiras-erp:local`, `almiras-postgres:16` | Image tags |
| `ALMIRAS_BACKUP_ROOT` | `/srv/almiras/backups` | Encrypted backups and WAL archive |
| `ALMIRAS_OPENBAO_AUDIT_DIR` | `/srv/almiras/openbao-audit` | OpenBao audit log on the host |
| `ALMIRAS_BACKEND_SUBNET` | `172.30.20.0/24` | Internal network; the OpenBao AppRole is bound to it |
| `ALMIRAS_WAL_ARCHIVING` | `off` | `on` = WAL archive + weekly base backup (point-in-time recovery) |
| `BACKUP_RETENTION_*_DAYS` | 35 / 400 / 15 / 35 | Daily dumps / monthly dumps / base backups / OpenBao snapshots |
| `DJANGO_EMAIL_HOST`, `_PORT`, `_USER`, `DJANGO_DEFAULT_FROM_EMAIL` | *placeholder* | KSA mail server (SMTP + STARTTLS) |
| `GUNICORN_WORKERS` | 3 | ~2 × vCPU |
| `ZATCA_SANDBOX_BASE_URL`, `ZATCA_SIMULATION_BASE_URL`, `ZATCA_PRODUCTION_BASE_URL` | empty | From ZATCA's portal / Swagger at deployment (zatca OQ-30) |
| `ZATCA_PRODUCTION_ALLOWED` | `0` | `1` only at go-live (R-29) |
| `ALMIRAS_ALERT_WEBHOOK_URL` | empty | Optional KSA-hosted webhook for job failures |

Variables the containers receive (set by `docker-compose.yml`): `DJANGO_DEBUG=0`, `DJANGO_BEHIND_TLS_PROXY=1`,
`DJANGO_SETTINGS_MODULE`, `POSTGRES_DB/HOST/PORT/USER`, `HR_REQUIRE_VERIFIED_PARAMETERS=1` (hr R7),
`ZATCA_SECRET_STORE=zatca.keystore.OpenBaoSecretStore`, `ZATCA_OPENBAO_ADDRESS`, `ZATCA_OPENBAO_AUTH_ROLE`,
`ACCESS_GRANT_MAX_AGE_SECONDS` (access R31), `TZ=Asia/Riyadh`. `DJANGO_STATIC_ROOT` is used at image build.

### Secrets (`deploy/secrets/`, **secret**, never committed)

Every secret is a file mounted as a Docker secret; the entrypoint turns `NAME_FILE` into `NAME` for the
variables Django reads, so no secret appears in the compose file, the image, `docker inspect` or the
repository. Directory `secrets/` is `0700 root`; files are `0644` (the container user must read its own
mount). `./init-secrets.sh` generates the random ones; `./init-secrets.sh --check` lists what is missing.

| File | Secret | Used by | Becomes |
| --- | --- | --- | --- |
| `django_secret_key` | yes (generated, ≥ 50 chars) | web, scheduler, migrate | `DJANGO_SECRET_KEY` |
| `db_superuser_password` | yes (generated) | db (initdb only) | superuser `postgres`; local socket only afterwards |
| `db_owner_password` | yes (generated) | db init, **migrate only** | role `erp_owner` |
| `db_app_password` | yes (generated) | db init, web, scheduler | role `erp_app` (`POSTGRES_PASSWORD`) |
| `db_backup_password` | yes (generated) | db init, backup | role `erp_backup` |
| `db_monitor_password` | yes (generated) | db init, postgres-exporter | role `erp_monitor` |
| `hr_personal_data_keys` | yes (generated; comma-separated, first = current) | web, scheduler | `HR_PERSONAL_DATA_KEYS` (hr R10, check hr.E001) |
| `hr_personal_data_hash_key` | yes (generated; never rotated) | web, scheduler | `HR_PERSONAL_DATA_HASH_KEY` (hr R9) |
| `banking_data_keys` | yes (generated; comma-separated) | web, scheduler | `BANKING_DATA_KEYS` (banking R4, check banking.E001) |
| `smtp_password` | yes (from mail admin) | web, scheduler, alertmanager | `DJANGO_EMAIL_PASSWORD` |
| `grafana_admin_password` | yes (generated) | grafana | |
| `openbao_erp_role_id`, `openbao_erp_secret_id` | yes (bootstrap) | web, scheduler | ERP AppRole login (for the OpenBao client, OQ-34) |
| `openbao_backup_token` | yes (bootstrap) | backup | periodic token, `backup-snapshot` policy only |
| `backup-recipients.txt` | no (public age keys) | db (WAL), backup | encryption recipients |
| `tls/erp.crt`, `tls/erp.key` | key is secret | proxy | site certificate |
| `openbao-tls/ca.crt`, `server.crt`, `server.key` | key is secret | openbao, backup, prometheus | `chown 100:1000 server.key; chmod 0400` |

**Key loss = data loss.** HR, banking and Django keys are not in the database dumps on purpose. Keep a copy
in the offline key envelope (§6); without it, encrypted HR and banking fields in a restored backup cannot be
read.

## 4. Install and first run

```bash
cd /opt/almiras-erp/deploy
cp env.example .env && $EDITOR .env monitoring/alertmanager.yml   # domain, mail server, addresses
./init-secrets.sh                                                   # random secrets
# copy certificates, smtp_password and backup-recipients.txt into secrets/ (see §3), then:
./init-secrets.sh --check                                           # only the 3 OpenBao files may be missing
docker compose build                                                # both images (pinned, hashed)
docker compose up -d db openbao                                     # db: initdb -E UTF8 --locale=C.UTF-8,
                                                                    # roles from postgres/sql/roles.sql
```

**Key ceremony** (§6): initialise and unseal OpenBao, run `openbao/bootstrap.sh`, revoke the root token.

```bash
docker compose run --rm migrate                  # migrations as erp_owner + grants (postgres/sql/grants.sql)
docker compose run --rm web release-check        # as erp_app: check --deploy, migrate --check, R45 / AT-47
docker compose run --rm web manage setup_almiras # Almiras entity, chart of accounts, periods (as erp_app)
docker compose run --rm web manage createsuperuser                     # technical admin only (access R24)
docker compose run --rm web manage access_bootstrap --username <mp> --entity ALM --reason "Initial setup"
docker compose run --rm web manage hr_setup_entity ALM                 # per entity with employees (hr R4)
docker compose up -d                                                   # everything
docker compose ps                                                      # all healthy; openbao healthy = unsealed
```

`release-check` shows two Django warnings (`security.W004` HSTS, `security.W008` SSL redirect): both are done
by the proxy (`Strict-Transport-Security`, HTTP → HTTPS redirect). Any ERROR (e.g. `hr.E001` no HR key,
`banking.E001`, `zatca.E001` local key store, `mail.E001` no SMTP) stops the release.

### How Django uses two database roles

Django has one connection setting set (`POSTGRES_USER` / `POSTGRES_PASSWORD`, `config/settings.py`; the
`access_log` alias copies it). The kit gives it different credentials per container instead of a second
alias: `migrate` gets `POSTGRES_USER=erp_owner` and the owner password, every other container
`POSTGRES_USER=erp_app` and the app password. The app containers never see the owner password.

| Role | Attributes | Owns | Rights |
| --- | --- | --- | --- |
| `erp_owner` | LOGIN, no SUPERUSER / CREATEDB / CREATEROLE / REPLICATION / BYPASSRLS | database, schema `public`, every table, function, trigger | everything on its objects; used by `migrate` only |
| `erp_app` | LOGIN, none of the above, CONNECTION LIMIT 200 | nothing | SELECT/INSERT/UPDATE/DELETE on tables (default privileges of `erp_owner`), USAGE on sequences; no TRUNCATE/TRIGGER/REFERENCES, no CREATE or TEMP; `core_accesslog`/`core_grantuse` insert + select only, `core_membership` no DELETE, `django_migrations` select only (access R33, OQ-10) |
| `erp_backup` | LOGIN, REPLICATION (base backups), read-only default | nothing | `pg_read_all_data` |
| `erp_monitor` | LOGIN | nothing | `pg_monitor` |

`sql/verify_app_role.sql` is access **AT-47**: as `erp_app` it tries `ALTER TABLE invoicing_salesdocument
DISABLE TRIGGER ALL`, `SET session_replication_role = replica`, `CREATE OR REPLACE FUNCTION` / `DROP FUNCTION`
on the trigger function, `DROP TRIGGER`, `CREATE TRIGGER`, `TRUNCATE`, `UPDATE`/`DELETE` on the access log,
`CREATE TABLE`, `CREATE TEMP TABLE`, `ALTER TABLE ... OWNER`, `SET ROLE erp_owner`, and checks role attributes
and ownership. Every statement must fail with SQLSTATE 42501; the transaction is rolled back. It runs in
`release-check`, weekly in the scheduler (`db_role_check`, alert `DbRoleCheckFailed`) and in CI.

## 5. Scheduled jobs (`scheduler/crontab`, Asia/Riyadh)

| Time | Command | Spec |
| --- | --- | --- |
| every minute | `zatca_process_queue` | zatca R-20..R-27, R-35 ("schedule it every minute") |
| every 5 min | `hr_deliver_assignment_events` | hr R25 |
| every 10 min | `bin/zatca_deadline_check.py` (watchdog, exit 1 at risk / 2 overdue / 3 queue stuck / 4 failed) | zatca R-23, R-35, R-47 |
| 00:05 | `hr_refresh_status` | hr (before billing, so ended secondments are known) |
| 00:30 | `billing_run` (non-zero exit when a run aborted) | billing R66 |
| 01:15 | `zatca_daily` | zatca R-10, R-23, R-35 |
| 01:45 on the 1st | `hr_accrue_leave` (previous month) | hr R30 |
| 07:00 / 07:10 / 07:20 | `hr_document_alerts`, `hr_resignation_deadlines`, `hr_gosi_exclusion_reminders` | hr R43, R41, R65 |
| Sunday 05:40 | `bin/dbrole.py verify` | access R45 / AT-47 |
| 02:00 / 02:30 / Fri 03:00 (backup service) | `pg-backup.sh`, `openbao-snapshot.sh`, `pg-basebackup.sh` | zatca OQ-3 |

Every job runs through `bin/run-job.sh`: one run at a time (`flock`; an overlap is skipped, exit 75), a
timeout, a JSON log line at start and end (`docker compose logs scheduler`), Prometheus metrics in the
`metrics` volume (`almiras_job_last_exit_code{task=...}`, `..._last_success_timestamp_seconds`), and on failure
an optional POST to `ALMIRAS_ALERT_WEBHOOK_URL` (job name and exit code only, no output). Missed runs: unlike a
systemd timer with `Persistent=true`, supercronic does not replay, so `bin/scheduler-start.sh` runs the
idempotent daily jobs (`hr_refresh_status`, `billing_run`, `zatca_daily`; billing R11) once whenever the
scheduler starts. Not scheduled: `hr_rehash_personal_data` (one-off after a key rotation), invoicing
anonymisation and any archive purge (no job before the retention rules are confirmed, zatca OQ-8/OQ-31),
payroll jobs (module not built).

## 6. Key ceremony (OpenBao, Managing Partner)

Decisions: the store sits in KSA (D-3, D-5); one unseal key held by the Managing Partner, **no Shamir split and
no auto-unseal** (D-10, kept by D-12); hierarchy group super key → per-entity master key → that entity's EGS
keys (D-12, R-50); custody delegated per entity only, logged and revocable (R-41, access R51). How the
hierarchy is implemented inside OpenBao is **OQ-34 (security sign-off pending)**; the kit implements the
template below.

**Template (for sign-off):**

- Unseal key: 1 share, threshold 1, PGP-encrypted to the MP's key at init; the MP's offline envelope.
- Group super key: transit mount `zatca-group-<GROUP>`, key `super_key`, non-exportable, no plaintext backup,
  deletion disabled; ERP reference `zatca/_group/<GROUP>/super_key`. Only the MP's policy uses or rotates it.
- Entity master key: one transit mount per entity `zatca-<ENTITY>`, key `master_key` (aes256-gcm96,
  non-exportable); reference `zatca/<ENTITY>/master_key`. Only `group-super-admin-<group>` (the MP) may
  create or rotate it (R-50 b, AT-71); rotation is followed by `rewrap` of that entity's items.
- EGS private keys and CSID secrets: KV v2 mount `zatca-kv`, paths `zatca/<ENTITY>/<EGS>/private_key` and
  `.../<kind>_csid_secret` (the ERP's `zatca/keystore.py` paths), stored as ciphertext under the entity master
  key; `cas_required`, all versions kept, destroy denied.
- Policies (`openbao/policies/`, rendered per entity by `render-policies.sh`; entities are data):
  `group-super-admin-<group>` (MP), `entity-custodian-<entity>` (use, not rotate, that entity's master key;
  write/version/soft-delete/undelete its KV paths; issue single-use onboarding logins), `erp-signer-<entity>`
  (the ERP's AppRole `erp-zatca`: read and decrypt that entity only, R-41), `erp-onboard-<entity>` (write
  during a custodian-issued 15-minute window), `backup-snapshot`. Tested: a custodian of SUB1 cannot rotate
  SUB1's master key or touch ALM; the signer for ALM cannot read SUB1; the MP cannot read `sys/raw` or disable
  the audit device; the backup token can only take snapshots.
- Audit: file device declared in `config.hcl` (cannot be disabled through the API); values HMAC-ed.

**Known limits for the security review:** transit does not chain entity master keys cryptographically under
the super key (the chain is enforced by policy and by the barrier key behind the unseal key); per-entity
namespaces or separate stores are the alternatives in OQ-34 a. ZATCA EGS keys are secp256k1, which transit
cannot hold, so the ERP decrypts the EGS key in memory to sign: the "non-exportable keys" question (OQ-3 b)
stays open. After any restart nothing signs until the MP unseals (alert `OpenBaoSealedOrDown`); with D-12 the
store-wide `unseal` delegation (R-49) is withdrawn, so **who unseals when the MP is unavailable is open
(OQ-34 b, OQ-25)** and is the main availability risk for the 24-hour reporting deadline (R-23).

**Ceremony steps** (MP at the console or over SSH; a witness records each step, no screen sharing):

```bash
# 1. MP's public GPG key (exported beforehand, base64 of the binary key) copied into the container:
docker compose exec -T openbao sh -c 'cat > /tmp/mp.b64' < mp-public.b64
docker compose exec openbao bao operator init -key-shares=1 -key-threshold=1 \
    -pgp-keys=/tmp/mp.b64 -root-token-pgp-key=/tmp/mp.b64 -format=json > init.json
#    init.json now holds only PGP-encrypted values; move it to the MP's offline media, then shred it here.
# 2. MP decrypts on their own device and unseals (the key is typed, never stored on the server):
docker compose exec openbao bao operator unseal          # prompts for the key
# 3. Bootstrap mounts, keys, policies and credentials (entities with a ZATCA profile):
read -rs BAO_TOKEN && export BAO_TOKEN                   # decrypted root token, typed
./openbao/bootstrap.sh ALM ALM <SUB1> <SUB2>             # group code, then entity codes
# 4. Named logins for the MP and each custodian (no shared accounts), then record each custody grant in the ERP
#    (KeyCustodianDelegation, scope master_key, openbao_policy_ref = entity-custodian-<entity>):
docker compose exec openbao bao write auth/userpass/users/<mp> token_policies=group-super-admin-alm password=-
docker compose exec openbao bao write auth/userpass/users/<custodian> token_policies=entity-custodian-<entity> password=-
# 5. Revoke the root token; from now on the MP logs in as themself:
docker compose exec -e BAO_TOKEN openbao bao token revoke -self && unset BAO_TOKEN
# 6. Backup encryption: the MP and a deputy each create an age key on an offline device
#    (age-keygen -o almiras-backup.agekey) and give only the PUBLIC line (age1...) for secrets/backup-recipients.txt.
```

Offline envelope (two sealed copies, two places in KSA): OpenBao unseal key (PGP-encrypted) and the MP's GPG
key, age backup identities, `hr_personal_data_keys`, `hr_personal_data_hash_key`, `banking_data_keys`,
`django_secret_key`. Unseal after every restart: `docker compose exec openbao bao operator unseal`.

New subsidiary: `./openbao/bootstrap.sh ALM ALM SUB1 ... NEW` (MP token), then grant custody and record it.
Revoking custody: delete the user's policy binding (`bao write auth/userpass/users/<u> token_policies=`) and
record the revocation in the ERP.

## 7. Backups and restore

- Nightly `pg_dump -Fc` as `erp_backup`, piped into `age` to the public recipients: `backups/pg/daily/`,
  first dump of each month also in `backups/pg/monthly/`, `.sha256` next to each file. No private key and no
  plaintext dump on the server.
- Optional WAL archiving (`ALMIRAS_WAL_ARCHIVING=on`): `archive_command` encrypts each segment with age into
  `backups/wal/`; weekly `pg_basebackup` (tar, `-X fetch`) into `backups/pg/base/`; WAL older than the oldest
  kept base backup is pruned.
- Nightly OpenBao raft snapshot (barrier-encrypted, then age-encrypted) into `backups/openbao/`.
- Copies to the second KSA location: from the host, e.g. a systemd timer running
  `rsync -a --delete-after /srv/almiras/backups/ backup@<ksa-second-site>:/almiras/` (*placeholder*). Never to
  a location outside KSA (R-43).
- Alerts: `BackupFailed`, `BackupMissing` (> 26 h), `BackupNeverRan`, `PostgresWalArchiveFailing`,
  `RestoreDrillOverdue` (> 35 days).

**Restore drill** (monthly, before go-live and before each upgrade):

```bash
ALMIRAS_RESTORE_IDENTITY=/media/offline/almiras-backup.agekey ./backup/restore-test.sh   # newest daily dump
```

It checks the checksum, decrypts, restores into the throwaway `restore-db` (profile `drill`: tmpfs, own
network, no port, removed afterwards; same roles), runs `backup/smoke.sql` (tables, ≥ 20 triggers, entities,
every posted entry balanced and with 2+ lines, trial-balance totals per entity for the drill record), then as
`erp_app` against the copy `release-check` (Django deploy checks, `migrate --check`, AT-47) and
`manage.py check`. Record the totals and compare them with the production trial balance of that day.

**Real restore** (after a loss), keeping the database-level grants of `roles.sql`:

```bash
docker compose stop web scheduler backup
age -d -i <identity> <dump.age> | docker compose exec -T --user postgres db \
    pg_restore -d almiras --clean --if-exists --exit-on-error --single-transaction
docker compose run --rm migrate && docker compose run --rm web release-check
docker compose up -d
```

On a new host, start `db` once first (it creates the roles from the same secret files), then restore.

**Point-in-time recovery** (WAL archiving on): restore the newest base backup before the target time into an
empty data directory (`age -d ... | tar -x`), decrypt the needed WAL into a directory, set
`restore_command = 'cp /restore-wal/%f %p'` and `recovery_target_time`, create `recovery.signal`, start, check,
`pg_wal_replay_resume()`.

**OpenBao restore drill** (zatca OQ-3: secret-store backup/restore and DR test): start a throwaway OpenBao with
`config.hcl` on an empty volume, `bao operator init` it (throwaway keys), log in with its root token, `bao
operator raft snapshot restore -force <decrypted snapshot>`; it is now sealed with the **production** unseal
key: the MP unseals it and checks `bao secrets list` and one KV metadata read. Destroy the throwaway store.

## 8. Monitoring

Prometheus scrapes node-exporter (disk, memory; plus the textfile metrics of jobs, backups, the ZATCA
watchdog and the restore drill), postgres-exporter (as `erp_monitor`), OpenBao (`/v1/sys/metrics`) and
Alertmanager. Rules (`monitoring/alert-rules.yml`, en/ar): disk < 15 % / < 5 %, targets down, PostgreSQL down,
WAL archive failing, backups failed/missing, restore drill overdue, any job failed, `billing_run` aborted,
daily jobs missed, scheduler stopped, AT-47 drift, ZATCA reporting deadline at risk / passed (R-23, R-35 failure
notice), ZATCA queue stuck (> 30 min), watchdog not running, OpenBao sealed or down (R-47). Alertmanager sends
e-mail through the KSA mail server only (IT; ZATCA/OpenBao alerts also to the ZATCA admins and the MP).
Grafana OSS is optional: `docker compose --profile grafana up -d grafana`, then `ssh -L 3000:127.0.0.1:3000`.
node-exporter needs the host root mounted `rslave` (standard on systemd hosts).

## 9. Upgrades

1. Read the release notes and the diff of `deploy/` and migrations; the change must be merged to `main` after
   the reviewer agent and a human approved it (CLAUDE.md), and CI green (`tests`, `deploy-check`).
2. Run the restore drill on last night's backup; take an extra dump: `docker compose exec backup
   /almiras/backup/pg-backup.sh`.
3. `git fetch && git checkout <tag>`; `docker compose build`.
4. `docker compose stop scheduler web` (the queue stops; keep the window short: simplified invoices have a
   24-hour reporting deadline, R-23).
5. `docker compose run --rm migrate` then `docker compose run --rm web release-check`.
6. `docker compose up -d`; check `docker compose ps`, the admin login and the scheduler log.
7. Rollback: `git checkout <previous tag>`, `docker compose build`; if migrations ran, restore the pre-upgrade
   dump (§7). Posted entries are never edited: a data correction is a reversal through the ERP, never SQL.

Base images and dependencies are pinned (image digests, `requirements.lock` with hashes, Go modules through
the checksum database). To update: change the pin, refresh the hash / digest, rebuild, run CI.

Key rotation: HR keys: put the new key first in `hr_personal_data_keys` (comma-separated), restart web and
scheduler, run `docker compose run --rm web manage hr_rehash_personal_data`, then drop the old key. Banking
keys likewise. Never rotate `hr_personal_data_hash_key`. Entity master key: the MP rotates it in OpenBao
(`bao write -f zatca-<ENTITY>/keys/master_key/rotate`) and records the new version in the ERP (R-50 b).

## 10. Go-live checklist

Each line must be ticked by the named person with date and evidence before `ZATCA_PRODUCTION_ALLOWED=1` and
before real personal data is loaded.

| # | Item | Source | Owner | How to check |
| --- | --- | --- | --- | --- |
| 1 | VM, disks, backups, logs, OpenBao and mail all in KSA; no config points outside KSA | zatca D-5, R-43, OQ-20; invoicing D12, R53 | MP / IT | provider contract + region; `docker compose config` reviewed by code-reviewer |
| 2 | Application role cannot bypass triggers or run DDL | access **R45, AT-47**, OQ-10 (DB part) | IT | `docker compose run --rm web release-check` passes; weekly `db_role_check` green |
| 3 | AccessLog retention and readers decided | access OQ-10 (remainder) | MP + legal | decision recorded |
| 4 | MFA for approver, zatca_admin, access_admin, MP and superusers | access **OQ-4** | MP | **not built yet** (no `django-otp` in the code); until then restrict the site with `ALMIRAS_ALLOWED_CIDRS` / VPN |
| 5 | Group roles' coverage of partly-owned / minority entities | access OQ-3 | MP + legal | decision recorded before their real data |
| 6 | OpenBao design and non-exportable keys signed off; who unseals when the MP is unavailable | zatca **OQ-34**, OQ-3 b, OQ-25 | security expert + MP | sign-off document; §6 limits answered |
| 7 | OpenBao client implemented (`zatca.keystore.OpenBaoSecretStore` is a fail-closed stub today: every key operation raises `SecretStoreUnavailable`) | zatca OQ-34, R-47 | module-builder + reviewer | ZATCA sandbox onboarding of one EGS succeeds against this store |
| 8 | Secret-store backup/restore and DR test done | zatca OQ-3 (remainder) | IT + MP | OpenBao restore drill (§7) record |
| 9 | Database restore drill passed | this kit | IT | `backup/restore-test.sh` PASSED, totals compared |
| 10 | ZATCA go-live dates per entity, Swagger paths and base URLs set | zatca OQ-1, OQ-30, R-29 | MP / zatca_admin | `.env` values, `ZATCA_API_PATHS` filled in code by the builder |
| 11 | Key ceremony done, root token revoked, envelopes stored | zatca D-3, D-10, D-12, R-50 | MP | ceremony record (witnessed) |
| 12 | HR keys and hash key set; statutory parameters verified by the MP | hr R7, R9, R10; `HR_REQUIRE_VERIFIED_PARAMETERS=1` | MP / HR | `release-check` without hr.E001; parameter rows `verified` |
| 13 | PDPL: no offshore processor; privacy notice; transfer assessments | invoicing **OQ-31**, zatca OQ-20 | MP + legal | legal sign-off |
| 14 | WHT rates/categories signed off (non-resident payments) | invoicing OQ-8 | tax adviser | sign-off |
| 15 | Deposit VAT sign-off | invoicing OQ-16 | tax adviser | sign-off |
| 16 | Billing → invoicing functions implemented; contract approval accepted for clearance posting | billing **OQ-22** (R52 to R56, R53) | builders + reviewer | tests green; billing_run on staging |
| 17 | Bank codes verified, banking retention, cutover balances tie to the opening TB | banking OQ-4, OQ-15, OQ-17 | accountant + migration-agent | tie-out report |
| 18 | Opening balances from Falcon (or other legacy system) tie out | README "Next steps" 1 | migration-agent + accountant | tie-out report |
| 19 | TLS certificate valid; HSTS on; HTTP redirects; `/admin` reachable only as intended | this kit | IT | browser + `curl -I` |
| 20 | Alerts reach people: test each receiver | this kit | IT | test alert received by IT, ZATCA admins and MP |
| 21 | `lxml` added to `requirements.txt` (zatca imports it; `deploy/requirements.lock` already pins it) | zatca §6 | builder | CI `tests` installs only `requirements.txt` |

## 11. Verification done for this kit (2026-10-17, local, throwaway values only)

Both images built; full stack started (db, openbao, web, scheduler, backup, proxy, prometheus, alertmanager,
postgres-exporter; node-exporter with the host mount removed, which the sandbox did not allow); migrate as
`erp_owner`; `release-check` and AT-47 passed as `erp_app` (and failed, as it must, after a test `GRANT
TRUNCATE`); first-run commands and every scheduled command ran as `erp_app`; HTTPS through Caddy with HSTS,
secure cookies, static files and a CSRF-protected admin login; OpenBao initialised with one key, unsealed,
bootstrapped twice (idempotent), policies tested as described in §6, raft snapshot taken; nightly dump and
snapshot written encrypted; restore drill PASSED; Prometheus scraped the job, backup and ZATCA watchdog
metrics; `promtool check rules` / `check config`, `amtool check-config`, shellcheck 0.11, hadolint 2.15,
yamllint clean. Not exercised: the ZATCA overdue path with real e-invoices, WAL archiving / PITR, Grafana, PGP-
encrypted init (no GPG agent in the sandbox), e-mail delivery.
