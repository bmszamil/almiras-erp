"""migration.md R19: every message written in the migration services, readers and commands has Arabic too.

Scans the source: the Arabic argument of error(code, en, ar) / warning(code, en, ar) and of bi(en, ar) / _fail(en, ar)
must contain Arabic letters; so must every literal given to CommandError(...).
"""
import ast
import re
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent
ARABIC = re.compile(r"[؀-ۿ]")
AR_ARG = {"error": 2, "warning": 2, "bi": 1, "_fail": 1}


def _text(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values if isinstance(v, ast.Constant))
    if isinstance(node, ast.BinOp):
        left, right = _text(node.left), _text(node.right)
        return None if left is None and right is None else (left or "") + (right or "")
    return None


def _messages(path):
    for call in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(call, ast.Call):
            continue
        f = call.func
        name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
        if name in AR_ARG:
            i = AR_ARG[name]
            if len(call.args) <= i:
                yield call.lineno, f"{name}() without the Arabic text"
            else:
                yield call.lineno, _text(call.args[i])
        elif name in ("CommandError", "MigrationError", "ReaderError") and call.args:
            yield call.lineno, _text(call.args[0])


FILES = ["services.py", "readers.py", "models.py", *[f"management/commands/{p.name}" for p in
                                                      sorted((APP / "management" / "commands").glob("*.py"))
                                                      if p.name != "__init__.py"]]


@pytest.mark.parametrize("name", FILES)
def test_every_migration_message_has_arabic(name):
    missing = [f"{name}:{line}: {text!r}" for line, text in _messages(APP / name)
               if text is not None and re.search(r"[A-Za-z]", text) and not ARABIC.search(text)]
    assert not missing, "\n".join(missing)
