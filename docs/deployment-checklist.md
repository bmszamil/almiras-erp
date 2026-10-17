# Deployment checklist (approvals and access)

Tick every item before the ERP serves real users. Data stays in a Saudi cloud region (D5).

## Reverse proxy
- [ ] The proxy **overwrites** `X-Forwarded-For` with the real client address (or appends to a header it has first stripped of client-supplied values). It never passes a client-supplied `X-Forwarded-For` through unchanged.
- [ ] The proxy **overwrites** `X-Forwarded-Proto` (`https` / `http`) and strips any client-supplied value.
- [ ] `ERP_TRUSTED_PROXIES` lists the proxy addresses exactly as Django sees them in `REMOTE_ADDR` (comma-separated). Only then is `X-Forwarded-For` used for audit IPs and sign-in lockout, and `X-Forwarded-Proto` used for HTTPS detection.
- [ ] The app server is not reachable except through the proxy (firewall / private network).
- [ ] TLS terminates at the proxy with a current certificate; plain HTTP is only redirected.

## Django settings (environment)
- [ ] `DJANGO_DEBUG` is unset or `0`.
- [ ] `DJANGO_SECRET_KEY` is a long random value from the secret store (startup fails without it).
- [ ] `DJANGO_ALLOWED_HOSTS` lists only the real host names.
- [ ] `ERP_ADMIN_SETUP_MODE` is unset or `0` after initial setup. Superuser flags are removed from day-to-day accounts.
- [ ] `POSTGRES_*` point to PostgreSQL in the Saudi region; `ATOMIC_REQUESTS` stays on (the approval action views opt out on purpose).
- [ ] `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS` point to the approved SMTP server (OQ11).
- [ ] `python manage.py check --deploy` shows no errors.
- [ ] **HSTS preload decision:** `SECURE_HSTS_PRELOAD` is off. Turning it on and submitting the domain to browser preload lists is hard to undo. Needs an explicit decision (all subdomains must serve HTTPS forever).

## Accounts and access
- [ ] Every human user has an authenticator app enrolled (`manage.py setup_totp <user>`), and the enrolment shows in the audit log.
- [ ] Service tokens are issued with `manage.py issue_service_token <user> --days N`; expiry dates are recorded and rotated.
- [ ] Initial grants are made (`manage.py grant_access`), then reviewed with `manage.py access_review --as <reviewer>`. The review is repeated quarterly.
- [ ] Cron: `run_escalations` every 15 minutes; `send_notifications` (email retry) every 15 minutes; `expire_chairman_changes` (D21 dual-control requests) hourly.
- [ ] `ERP_CHAIRMAN_CHANGE_EXPIRY_DAYS` is set if the default of 7 days is not wanted (D21).
- [ ] `/access/enrol-2fa/` (2FA self-enrolment after a dual-controlled reset, D21) is reachable only over HTTPS and is not cached by any proxy (the page sends `Cache-Control: no-store`).

## Development databases
- [ ] **Rebuild dev databases created before 17 Oct 2026 migration renumbering.** On `fix/approvals-followups` the access migrations were renumbered after main's intercompany migrations (`0008_user_manager_guards_system_user`, `0009_d20_agents_do_not_submit`, then `0010`, `0011`). A local database that applied the old `0006_user_manager_guards_system_user` / `0007_d20_agents_do_not_submit` has a migration history that no longer matches: delete it (`db.sqlite3` or the scratch PostgreSQL database) and run `python manage.py migrate` again. No production database exists yet.

## Pending (not built yet)
- [ ] **Database-level guards.** PostgreSQL triggers must block UPDATE/DELETE of posted journal entries and lines and of approval requests' final states. A database role without UPDATE/DELETE on `access_auditlog` and `approvals_approvalaction` is needed. An exclusion constraint for overlapping approval policies is also needed. Until then the guards are application-level only.
- [ ] Keycloak SSO (identity adapter seam: `ERP_IDENTITY_ADAPTER`).
- [ ] Backups in the Saudi region; retention per D18 (keep forever; PDPL storage-limitation review pending legal sign-off).
