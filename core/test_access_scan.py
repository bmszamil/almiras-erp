"""R1 / AT-26: every public service function checks rights, or is on the reviewed allow-list."""
import ast
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CHECKERS = {"require", "verify_grant", "require_dual_control", "_evaluate", "_deny", "deny"}

# Reviewed allow-list: function -> why it does not call require itself.
ALLOW = {
    "core/services.py:entity_ready_to_issue": "pure check on an entity the caller already holds",
    "core/services.py:load_setup_data": "reads a setup file; operator command only",
    "core/services.py:apply_entity_setup": "setup_almiras command only (operator, not a user action)",
    "core/services.py:reactivate_entity_by_operator": "reactivate_entity recovery command only (operator, logged, R32)",
    "ledger/services.py:validate_entry": "pure validation, called inside post",
    "ledger/services.py:signed_purification": "pure sum over lines the caller has already read",
    "ledger/services.py:settlement_for_document": "prices a settlement inside a document's build_lines, after create_entry authorised the posting",
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
    "ledger/services.py:counterparty_choices": "choice list for admin; posting checks every line",
    "invoicing/services.py:party_problems": "pure check of a party record the caller passes (model clean, services)",
    "invoicing/services.py:customers": "the scope mechanism for customers (access.scope with invoicing.view, R2)",
    "invoicing/services.py:vendors": "the scope mechanism for vendors (access.scope with invoicing.view, R2)",
    "invoicing/services.py:sales_documents": "the scope mechanism for sales documents (access.scope, R2)",
    "invoicing/services.py:purchase_documents": "the scope mechanism for purchase documents (access.scope, R2)",
    "invoicing/services.py:create_from_billing": "stub until billing.md is built: always raises NotImplementedError",
    "invoicing/services.py:create_adjustment_from_billing": "stub until billing.md is built: always raises",
    # Setup run by operators (setup_almiras, new-entity setup); no user action.
    "ledger/services_setup.py:install_chart_of_accounts": "operator setup of a new entity (CLAUDE.md: entities are data)",
    "ledger/services_setup.py:create_monthly_periods": "operator setup of a new entity",
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
}

# Modules with public functions that are not called services.py but act like them.
EXTRA_MODULES = ("ledger/services_setup.py", "ledger/fx.py")


def service_modules():
    extra = [REPO / name for name in EXTRA_MODULES]
    for path in sorted(REPO.glob("*/services.py")) + sorted(REPO.glob("*/*/services.py")) + extra:
        if any(part.startswith(".") or part in {"venv", ".venv"} for part in path.parts):
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
