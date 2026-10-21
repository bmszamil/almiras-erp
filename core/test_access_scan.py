"""R1 / AT-26: every public service function checks rights, or is on the reviewed allow-list."""
import ast
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CHECKERS = {"require", "verify_grant", "require_dual_control", "request_dual_control", "decide_dual_control", "_evaluate",
            "_deny", "deny"}

# Reviewed allow-list: function -> why it does not call require itself.
ALLOW = {
    "core/services.py:entity_ready_to_issue": "pure check on an entity the caller already holds",
    "core/services.py:load_setup_data": "reads a setup file; operator command only",
    "core/services.py:apply_entity_setup": "setup_almiras command only (operator, not a user action)",
    "core/services.py:reactivate_entity_by_operator": "reactivate_entity recovery command only (operator, logged, R32)",
    "core/services.py:sharia_off_payload": "pure: the payload a Sharia switch-off request approves (R48)",
    "core/services.py:new_entity_sharia_off_payload": "pure: the payload a Sharia-off creation request approves (R48)",
    "ledger/services.py:validate_entry": "pure validation, called inside post",
    "ledger/services.py:signed_purification": "pure sum over lines the caller has already read",
    "ledger/services.py:rates_visible_to": "the scope mechanism for rate rows (entities_for + global rows, ledger-fx R28)",
    "core/access/services.py:now": "clock",
    "core/access/services.py:profile_of": "helper",
    "core/access/services.py:kind_of": "helper",
    "core/access/services.py:source_of": "helper",
    "core/access/services.py:log": "writes the access log",
    "core/access/services.py:log_denial": "writes the access log (denials, survives rollback)",
    "core/access/services.py:entities_for": "the scope mechanism itself",
    "core/access/services.py:scope": "the scope mechanism itself",
    "core/access/services.py:get_scoped_or_404": "the scope mechanism itself",
    "core/access/services.py:covering_entities": "helper",
    "core/access/services.py:payload_hash": "helper",
    "core/access/services.py:system_user": "lookup used by system jobs (R20)",
    "core/access/services.py:bootstrap_managing_partner": "access_bootstrap command only (R26)",
    "core/access/services.py:group_parent_of": "helper (walks parent links)",
    "core/access/services.py:recorded_approvers": "reads the access log for a ledger check that already verified a grant",
    "core/access/services.py:visible_access_log": "the scope mechanism for access-log rows (entities_for + group parent)",
    "core/access/services.py:visible_entities": "the scope mechanism of the web entity switcher (union of "
                                                 "entities_for over the catalogue, web.md R9)",
    "core/access/services.py:holds_any_role": "reads the user's own memberships (web session timeout, R13); "
                                               "grants nothing",
    "core/access/services.py:set_preferred_language": "the signed-in person's own UI language; logged "
                                                       "profile_changed (web.md R27)",
    "invoicing/services.py:listed_customer_name": "pure: masks an individual's name on a row the caller read through "
                                             "list_sales_documents (checked); never unmasks (web.md R24, R51)",
    # web/services.py: UI preferences, sessions and the sign-in throttle (web.md §1); no business data.
    "web/services.py:language_for": "pure: picks the UI language from the profile, cookie or header (R27)",
    "web/services.py:set_language": "the signed-in person's own language through set_preferred_language (R27)",
    "web/services.py:switcher_entities": "the entities the user may see (visible_entities, R9)",
    "web/services.py:entity_from": "pure: picks the URL's entity among the visible ones, else 404 (R4, R10)",
    "web/services.py:start_entity": "the user's own remembered entity, only among the visible ones (R10)",
    "web/services.py:remember_entity": "the user's own UI preference, only for a visible entity (R10, R11)",
    "web/services.py:is_web_user": "helper: active human users only (R12)",
    "web/services.py:idle_timeout": "session timeout setting (OQ-8)",
    "web/services.py:absolute_timeout": "session timeout setting (OQ-8)",
    "web/services.py:log_login": "writes the access log (R26)",
    "web/services.py:log_logout": "writes the access log (R26)",
    "web/services.py:client_ip": "pure: the client address for the sign-in throttle (OQ-27)",
    "web/services.py:sign_in": "the password check itself, under the sign-in throttle (R12, R18)",
    "ledger/services.py:counterparty_choices": "choice list for admin; posting checks every line",
    "invoicing/services.py:party_problems": "pure check of a party record the caller passes (model clean, services)",
    "invoicing/services.py:deferred_sale_of": "payroll invoicing port only: the live bai' ajel sale of a contract payroll "
                                              "already holds and checked (R112); amounts and status, no personal data",
    "invoicing/services.py:customers": "the scope mechanism for customers (access.scope with invoicing.view, R2)",
    "invoicing/services.py:vendors": "the scope mechanism for vendors (access.scope with invoicing.view, R2)",
    "invoicing/services.py:sales_documents": "the scope mechanism for sales documents (access.scope, R2)",
    "invoicing/services.py:purchase_documents": "the scope mechanism for purchase documents (access.scope, R2)",
    "invoicing/services.py:anonymisation_candidates": "invoicing_anonymise job only: reads retention dates (R52)",
    "invoicing/services.py:anonymise_expired_personal_data": "invoicing_anonymise operator job only (R52; off by "
                                                              "default, dry run unless applied)",
    "payroll/services.py:runs": "the scope mechanism for payroll runs (access.scope with payroll.view, payroll R2)",
    "payroll/services.py:get_run": "the scope mechanism for one run (get_scoped_or_404 with payroll.view, R2)",
    "payroll/services.py:get_payslip": "the scope mechanism for one payslip (get_scoped_or_404, payroll.view_payslip)",
    "payroll/services.py:setup_entity": "payroll_setup_entity command only (operator setup of a new entity, R3)",
    "payroll/services.py:send_alerts": "payroll_alerts job only (R10, R22, R53; retention nomination)",
    "payroll/services.py:skip_installment": "always refuses (R27: a skip needs a new approved schedule)",
    "invoicing/services.py:register_wht_return_source": "called once from an AppConfig.ready() to add another module's "
                                                        "WHT rows to the return (payroll R43, OQ-30); no user action",
    "hr/services.py:employees": "the scope mechanism for employees (access.scope with hr.view, R2)",
    "invoicing/services.py:send_charity_recipient_alerts": "invoicing_charity_recipient_alerts job only (owner "
                                                            "decision C3: expiry alerts 30 and 7 days before)",
    "hr/services.py:get_employee": "the scope mechanism for one employee (get_scoped_or_404 with hr.view, R2)",
    "hr/services.py:param": "reads global statutory reference data (no entity, no personal data; hr R6)",
    # HR operator jobs (management commands, scheduled; hr R4, R25, R30, R43); no user action.
    "hr/services.py:setup_entity": "hr_setup_entity command only (operator setup of a new entity, hr R4)",
    "hr/services.py:accrue_leave": "hr_accrue_leave job only (month-end accrual, hr R30)",
    "hr/services.py:deliver_assignment_events": "hr_deliver_assignment_events job (acts as the HR user who ended it)",
    "hr/services.py:send_document_alerts": "hr_document_alerts job only (hr R43)",
    "hr/services.py:refresh_statuses": "hr_refresh_status job only (dated status changes, hr R8, R23, R38)",
    "migration/services.py:account_maps": "the scope mechanism for account maps (access.scope with migration.view)",
    "migration/services.py:imported_entries": "the scope mechanism for imported entries (access.scope with migration.view)",
    "zatca/services.py:einvoices": "the scope mechanism for e-invoices (access.scope with zatca.view, R-3)",
    "zatca/services.py:einvoice_statuses": "the scope mechanism for statuses (access.scope with zatca.view_status, R-34)",
    "zatca/services.py:is_issued": "invoicing port read: may the buyer get this document (R-22, R-23); writes nothing",
    # Setup run by operators (setup_almiras, new-entity setup); no user action.
    "ledger/services_setup.py:install_chart_of_accounts": "operator setup of a new entity (CLAUDE.md: entities are data)",
    "ledger/services_setup.py:create_monthly_periods": "operator setup of a new entity",
    "ledger/services_setup.py:install_bank_sub_account": "operator setup: a 1100 sub-account per bank account (banking R5)",
    # Pure arithmetic and validation on values the caller passes; no records read or written.
    "ledger/fx.py:error": "pure helper",
    "ledger/fx.py:to_decimal": "pure helper",
    "ledger/fx.py:has_places": "pure helper",
    "ledger/fx.py:check_txn_decimals": "pure helper",
    "ledger/fx.py:check_functional_decimals": "pure helper",
    "ledger/fx.py:check_rate": "pure helper",
    "ledger/fx.py:round_functional": "pure helper",
    "ledger/fx.py:convert": "pure helper",
    "ledger/fx.py:inverse": "pure helper",
    "ledger/fx.py:implied_rate": "pure helper",
    # access §10: the approval engine is the mechanism that require() itself calls for every A permission (R61);
    # its helpers read the matrix and governance data for a check that require() is making.
    "core/access/engine.py:register": "called once per type from the owning app's AppConfig.ready(); no user action",
    "core/access/engine.py:adapters": "read-only view of the code registry",
    "core/access/engine.py:adapter_for": "pure lookup in the code registry",
    "core/access/engine.py:activity_row": "the catalogue row of a code activity (global data, like roles)",
    "core/access/engine.py:describe": "reads the document a check is being made for (inside require)",
    "core/access/engine.py:document_people": "helper of the R14 / R81 check inside require",
    "core/access/engine.py:effective_version": "helper of the rule resolution inside require (R77)",
    "core/access/engine.py:resolve": "rule resolution inside require / submit (R69 to R73); refuses gaps",
    "core/access/engine.py:find_request": "helper of authorise (inside require)",
    "core/access/engine.py:eligible_users": "helper of the R72 alert and R82 step skipping inside require",
    "core/access/engine.py:refuse": "logs a permission_denied row and raises, like deny",
    "core/access/engine.py:purpose": "context manager used by require(purpose=...)",
    "core/access/engine.py:inbox": "the scope mechanism for the inbox: requests the user is eligible to decide",
    "core/access/engine.py:alert": "writes an in-app alert from a check or job that already ran",
    "core/access/engine.py:governance_of": "reads an entity's governance settings for a check (R67, R79)",
    "core/access/engine.py:ensure_governance": "called by create_entity / request_governance_change after their checks",
    "core/access/engine.py:default_top_authority": "pure (R67 default)",
    "core/access/engine.py:is_group_parent": "pure",
    "core/access/engine.py:check_amount_places": "pure (CLAUDE.md rule 7)",
    "core/access/engine.py:grant_rule_source": "pure read of a grant",
    "core/access/engine.py:top_authority_holders": "helper of the alert jobs",
    "core/access/matrix.py:versions": "the scope mechanism for matrix versions (entities_for, no cascade, R90)",
    "core/access/matrix.py:get_version": "the scope mechanism for one version (404 outside, R2)",
    "core/access/matrix.py:rules": "the scope mechanism for a version's rows (404 outside, R2)",
    "core/access/matrix.py:entities_reading": "the scope mechanism itself (entities_for of the §10 rights)",
    "core/access/matrix.py:activate_due_versions": "matrix job (and called by decide_version); records effective dates",
    "core/access/matrix.py:decider_roles": "pure (R76, OQ-42)",
    "core/access/matrix.py:rows_hash": "pure (R76 payload hash)",
    "core/access/matrix.py:governance_payload": "pure (R76 payload)",
    "core/access/delegations.py:delegations": "the scope mechanism for delegations (no cascade, R90)",
    "core/access/delegations.py:get_delegation": "the scope mechanism for one delegation (404 outside, R2)",
    "core/access/delegations.py:assistant_assignments": "the scope mechanism for assistant assignments (R90)",
    "core/access/delegations.py:send_delegation_reminders": "daily reminder job (R87); alerts only",
    "core/access/reports.py:reports": "the scope mechanism for governance reports (reader roles, R86, R90)",
    "core/access/reports.py:get_report": "the scope mechanism for one report (404 outside, R2)",
    "core/access/reports.py:send_unreviewed_alerts": "daily alert job (R86)",
    "core/access/reports.py:month_start": "pure",
    "core/access/reports.py:previous_month": "pure",
}

# Modules with public functions that are not called services.py but act like them.
EXTRA_MODULES = ("ledger/services_setup.py", "ledger/fx.py", "core/access/engine.py", "core/access/matrix.py",
                 "core/access/delegations.py", "core/access/reports.py")


def service_modules():
    extra = [REPO / name for name in EXTRA_MODULES]
    for path in sorted(REPO.glob("*/services.py")) + sorted(REPO.glob("*/*/services.py")) + extra:
        # Parts relative to the repository: a checkout inside a dot-folder (e.g. a git worktree under .claude/)
        # must not make the scan skip every module.
        if any(part.startswith(".") or part in {"venv", ".venv"} for part in path.relative_to(REPO).parts):
            continue
        yield path


def _called_names(fn):
    names = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                names.add(f.id)
            elif isinstance(f, ast.Attribute):
                names.add(f.attr)
    return names


def unchecked_functions():
    found, public = [], []
    for path in service_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        calls = {name: _called_names(fn) for name, fn in funcs.items()}
        checked = {name for name, c in calls.items() if c & CHECKERS}
        changed = True
        while changed:  # follow same-module helpers (e.g. _authorise_post)
            changed = False
            for name, c in calls.items():
                if name not in checked and c & checked:
                    checked.add(name)
                    changed = True
        rel = path.relative_to(REPO).as_posix()
        for name in funcs:
            if not name.startswith("_"):
                public.append(f"{rel}:{name}")
                if name not in checked:
                    found.append(f"{rel}:{name}")
    return found, public


def test_at26_every_public_service_checks_rights_or_is_allow_listed():
    unchecked, public = unchecked_functions()
    missing = sorted(set(unchecked) - set(ALLOW))
    assert not missing, "Public service functions without require (add a check, or review and allow-list):\n" + "\n".join(missing)
    stale = sorted(set(ALLOW) - set(unchecked))
    assert not stale, f"Allow-list entries for functions that are gone or now check rights (remove them): {stale}"


def test_scan_covers_setup_and_fx_modules():  # R1: helpers outside services.py are listed too
    _, public = unchecked_functions()
    assert "ledger/services_setup.py:install_chart_of_accounts" in public
    assert "ledger/fx.py:convert" in public


def test_ledger_posting_services_are_checked():
    unchecked, _ = unchecked_functions()
    for name in ("post_entry", "reverse_entry", "create_entry", "update_draft", "submit_entry", "close_period",
                 "trial_balance", "intercompany_balances"):
        assert f"ledger/services.py:{name}" not in unchecked
