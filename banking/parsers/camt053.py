"""ISO 20022 camt.053 bank-to-customer statement (``BkToCstmrStmt``, versions 001.02 to 001.08)
**[CC-verify: element use per bank, OQ-1]**. Standard library XML only.

Read per ``Stmt``: ``Id`` (statement id), ``ElctrncSeqNb`` or ``LglSeqNb`` (sequence), ``FrToDt`` (period),
``Acct/Id/IBAN`` or ``Acct/Id/Othr/Id`` and ``Acct/Ccy``, balances ``OPBD`` (or ``PRCD``) and ``CLBD`` with
``CdtDbtInd``, and booked entries ``Ntry`` (``Sts`` = ``BOOK``; pending entries are left out): ``Amt``,
``CdtDbtInd``, ``BookgDt``, ``ValDt``, ``AcctSvcrRef``, ``BkTxCd`` (domain family code or proprietary code),
``NtryDtls/TxDtls`` (``EndToEndId``, instructed amount in another currency, related party name and IBAN,
``RmtInf/Ustrd``) and ``AddtlNtryInf``. One statement per file.

Files with a DOCTYPE or entity declarations are refused before parsing (no external or expanded entities).
"""
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal, InvalidOperation

from . import ParsedLine, ParsedStatement, parse_error


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _child(el, *names):
    for name in names:
        if el is None:
            return None
        el = next((c for c in el if _local(c.tag) == name), None)
    return el


def _children(el, name):
    return [c for c in el if _local(c.tag) == name] if el is not None else []


def _text(el, *names):
    found = _child(el, *names) if names else el
    return (found.text or "").strip() if found is not None and found.text else ""


def _date(text, where):
    try:
        return date.fromisoformat(text[:10])
    except (TypeError, ValueError):
        raise parse_error(f"{where}: {text!r} is not a date.", f"{where}: {text!r} ليس تاريخًا.") from None


def _amount(el, where):
    try:
        return Decimal((el.text or "").strip())
    except (InvalidOperation, AttributeError):
        raise parse_error(f"{where}: not an amount.", f"{where}: ليس مبلغًا.") from None


def _balance(stmt, codes):
    for bal in _children(stmt, "Bal"):
        code = _text(bal, "Tp", "CdOrPrtry", "Cd")
        if code in codes:
            amt = _child(bal, "Amt")
            value = _amount(amt, f"balance {code}")
            if _text(bal, "CdtDbtInd") == "DBIT":
                value = -value
            on = _text(bal, "Dt", "Dt") or _text(bal, "Dt", "DtTm")
            return value, (amt.get("Ccy") or ""), (_date(on, f"balance {code}") if on else None)
    return None, "", None


def parse(data):
    head = data[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in data.lower():
        raise parse_error("XML statements with a DOCTYPE or entity declarations are refused.",
                          "تُرفض كشوف XML التي تحتوي على DOCTYPE أو تعريفات كيانات.")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise parse_error("Not a camt.053 statement: the XML cannot be read.", "ليس كشف camt.053: تعذرت قراءة XML.") from None
    report = _child(root, "BkToCstmrStmt") if _local(root.tag) == "Document" else None
    if report is None:
        raise parse_error("Not a camt.053 statement: BkToCstmrStmt is missing.", "ليس كشف camt.053: العنصر BkToCstmrStmt مفقود.")
    statements = _children(report, "Stmt")
    if len(statements) != 1:
        raise parse_error("Import one camt.053 statement (Stmt) per file.", "استورد كشف camt.053 واحدًا (Stmt) لكل ملف.")
    stmt = statements[0]
    seq = _text(stmt, "ElctrncSeqNb") or _text(stmt, "LglSeqNb")
    opening, open_ccy, open_date = _balance(stmt, ("OPBD", "PRCD"))
    closing, close_ccy, close_date = _balance(stmt, ("CLBD",))
    currency = _text(stmt, "Acct", "Ccy") or open_ccy or close_ccy
    if open_ccy and close_ccy and open_ccy != close_ccy:
        raise parse_error(f"Opening balance in {open_ccy}, closing balance in {close_ccy}.",
                          f"الرصيد الافتتاحي بعملة {open_ccy} والختامي بعملة {close_ccy}.")
    start = _text(stmt, "FrToDt", "FrDtTm") or _text(stmt, "FrToDt", "FrDt")
    end = _text(stmt, "FrToDt", "ToDtTm") or _text(stmt, "FrToDt", "ToDt")
    statement = ParsedStatement(
        statement_id=_text(stmt, "Id")[:35], sequence_no=int(seq) if seq.isdigit() else None,
        period_start=_date(start, "FrToDt") if start else open_date,
        period_end=_date(end, "FrToDt") if end else close_date,
        currency=currency, opening_balance=opening, closing_balance=closing,
        account_identifier=_text(stmt, "Acct", "Id", "IBAN") or _text(stmt, "Acct", "Id", "Othr", "Id"),
    )
    for entry in _children(stmt, "Ntry"):
        status = _text(entry, "Sts", "Cd") or _text(entry, "Sts")
        if status and status != "BOOK":
            statement.warnings.append(f"A {status} entry was left out (only booked entries count). / "
                                      f"استُبعد قيد بحالة {status} (تُحتسب القيود المقيدة فقط).")
            continue
        statement.lines.append(_entry(entry, len(statement.lines) + 1, currency))
    return statement


def _entry(entry, line_no, currency):
    where = f"Ntry {line_no}"
    amt = _child(entry, "Amt")
    if amt is None:
        raise parse_error(f"{where}: Amt is missing.", f"{where}: المبلغ مفقود.")
    indicator = _text(entry, "CdtDbtInd")
    if indicator not in ("CRDT", "DBIT"):
        raise parse_error(f"{where}: CdtDbtInd must be CRDT or DBIT.", f"{where}: يجب أن يكون المؤشر CRDT أو DBIT.")
    booking = _text(entry, "BookgDt", "Dt") or _text(entry, "BookgDt", "DtTm")
    if not booking:
        raise parse_error(f"{where}: BookgDt is missing.", f"{where}: تاريخ القيد مفقود.")
    value = _text(entry, "ValDt", "Dt") or _text(entry, "ValDt", "DtTm")
    tx = _child(entry, "NtryDtls", "TxDtls")
    domain = _child(entry, "BkTxCd", "Domn")
    code = _text(entry, "BkTxCd", "Prtry", "Cd") or (
        "-".join(filter(None, (_text(domain, "Fmly", "Cd"), _text(domain, "Fmly", "SubFmlyCd")))) if domain is not None else "")
    line = ParsedLine(
        line_no=line_no, booking_date=_date(booking, where), value_date=_date(value, where) if value else None,
        direction="credit" if indicator == "CRDT" else "debit", amount=_amount(amt, where),
        currency=amt.get("Ccy") or currency, bank_reference=_text(entry, "AcctSvcrRef")[:35],
        transaction_code=code[:10],
    )
    narrative = [_text(entry, "AddtlNtryInf")]
    if tx is not None:
        line.end_to_end_id = _text(tx, "Refs", "EndToEndId")[:35]
        if line.end_to_end_id == "NOTPROVIDED":
            line.end_to_end_id = ""
        if not line.bank_reference:
            line.bank_reference = _text(tx, "Refs", "AcctSvcrRef")[:35]
        instructed = _child(tx, "AmtDtls", "InstdAmt", "Amt")
        if instructed is not None and instructed.get("Ccy") and instructed.get("Ccy") != line.currency:
            line.original_amount = _amount(instructed, where)
            line.original_currency = instructed.get("Ccy")
        party = "Dbtr" if line.direction == "credit" else "Cdtr"
        line.counterparty_name = (_text(tx, "RltdPties", party, "Nm") or _text(tx, "RltdPties", party, "Pty", "Nm"))[:140]
        line.counterparty_iban = _text(tx, "RltdPties", f"{party}Acct", "Id", "IBAN")[:34]
        narrative += [_text(u) for u in _children(_child(tx, "RmtInf"), "Ustrd")]
        narrative.append(_text(tx, "AddtlTxInf"))
    line.narrative = " ".join(" ".join(filter(None, narrative)).split())
    return line
