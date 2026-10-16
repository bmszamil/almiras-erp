"""ledger-fx R37 / item 18: every refusal text written in ledger.services and ledger.models has Arabic too.

Scans the source: each string literal given to LedgerError(...), to
problems.append(...) or as the Arabic argument of error(en, ar) / _bi(en, ar)
must contain Arabic letters. Messages built elsewhere and passed through
(exc.messages, a list of problems) are checked where they are written.
"""
import ast
import re
from pathlib import Path

import pytest

LEDGER = Path(__file__).resolve().parent
ARABIC = re.compile(r"[؀-ۿ]")


def _literal_text(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values if isinstance(v, ast.Constant))
    if isinstance(node, ast.BinOp):  # "a" + "b"
        left, right = _literal_text(node.left), _literal_text(node.right)
        return None if left is None and right is None else (left or "") + (right or "")
    return None


def _messages(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
        if name in ("error", "_bi"):
            if len(call.args) < 2:
                yield call.lineno, f"{name}() without the Arabic text"
                continue
            yield call.lineno, _literal_text(call.args[1])
        elif name == "LedgerError" or (name == "append" and isinstance(func.value, ast.Name) and func.value.id == "problems"):
            for arg in call.args[:1]:
                yield call.lineno, _literal_text(arg)


@pytest.mark.parametrize("module", ["services.py", "models.py", "fx.py"])
def test_every_ledger_refusal_has_arabic(module):
    missing = [
        f"{module}:{line}: {text!r}" for line, text in _messages(LEDGER / module)
        if text is not None and re.search(r"[A-Za-z]", text) and not ARABIC.search(text)  # " / " alone: the formatter
    ]
    assert not missing, "\n".join(missing)
