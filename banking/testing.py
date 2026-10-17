"""Test helpers: synthetic IBANs and statement files in the standard public structures. Fake data only.

Never used outside tests. The IBANs are computed (valid check digits) from made-up account numbers; the statement
files follow the SWIFT MT940 tag layout, ISO 20022 camt.053.001.02 and a simple bank CSV export.
"""
from datetime import date
from decimal import Decimal
from xml.sax.saxutils import escape


def make_iban(bank_code, account, country="SA"):
    """A Saudi-structured IBAN (24 characters) with correct ISO 13616 check digits for a made-up account."""
    body = f"{bank_code}{account}".upper()
    rearranged = body + country + "00"
    digits = "".join(str(int(ch, 36)) for ch in rearranged)
    check = 98 - int(digits) % 97
    return f"{country}{check:02d}{body}"


def _mt_amount(amount):
    return f"{Decimal(amount):f}".replace(".", ",")


def _yymmdd(d):
    return d.strftime("%y%m%d")


def mt940(*, statement_id, sequence, currency, opening, opening_date, closing_date, lines, account="",
          closing=None):
    """MT940 bytes. ``lines``: (booking date, "C"|"D", amount, type, customer ref, bank ref, narrative)."""
    total = Decimal(opening)
    body = [f":20:{statement_id}", f":25:{account}", f":28C:{sequence:05d}/001"]
    mark = "C" if Decimal(opening) >= 0 else "D"
    body.append(f":60F:{mark}{_yymmdd(opening_date)}{currency}{_mt_amount(abs(Decimal(opening)))}")
    for booked, cd, amount, ttype, custref, bankref, narrative in lines:
        total += Decimal(amount) if cd == "C" else -Decimal(amount)
        ref = (custref or "NONREF") + (f"//{bankref}" if bankref else "")
        body.append(f":61:{_yymmdd(booked)}{booked:%m%d}{cd}{_mt_amount(amount)}{ttype}{ref}")
        if narrative:
            body.append(f":86:{narrative}")
    closing = total if closing is None else Decimal(closing)
    mark = "C" if closing >= 0 else "D"
    body.append(f":62F:{mark}{_yymmdd(closing_date)}{currency}{_mt_amount(abs(closing))}")
    text = "{1:F01TESTSARIAXXX0000000000}{2:O9401200270131TESTSARIAXXX00000000002701311200N}{4:\r\n" + \
        "\r\n".join(body) + "\r\n-}"
    return text.encode("utf-8")


def camt053(*, statement_id, sequence, currency, opening, opening_date, closing_date, lines, account="",
            closing=None, extra_entries=""):
    """camt.053.001.02 bytes. ``lines`` as for :func:`mt940`."""
    total = Decimal(opening)
    entries = []
    for booked, cd, amount, ttype, custref, bankref, narrative in lines:
        total += Decimal(amount) if cd == "C" else -Decimal(amount)
        entries.append(f"""
      <Ntry>
        <Amt Ccy="{currency}">{Decimal(amount):f}</Amt>
        <CdtDbtInd>{'CRDT' if cd == 'C' else 'DBIT'}</CdtDbtInd>
        <Sts>BOOK</Sts>
        <BookgDt><Dt>{booked.isoformat()}</Dt></BookgDt>
        <ValDt><Dt>{booked.isoformat()}</Dt></ValDt>
        <AcctSvcrRef>{escape(bankref or '')}</AcctSvcrRef>
        <BkTxCd><Prtry><Cd>{ttype}</Cd></Prtry></BkTxCd>
        <NtryDtls><TxDtls>
          <Refs><EndToEndId>{escape(custref or 'NOTPROVIDED')}</EndToEndId></Refs>
          <RmtInf><Ustrd>{escape(narrative or '')}</Ustrd></RmtInf>
        </TxDtls></NtryDtls>
      </Ntry>""")
    closing = total if closing is None else Decimal(closing)

    def bal(code, value, on):
        return (f"<Bal><Tp><CdOrPrtry><Cd>{code}</Cd></CdOrPrtry></Tp><Amt Ccy=\"{currency}\">{abs(value):f}</Amt>"
                f"<CdtDbtInd>{'CRDT' if value >= 0 else 'DBIT'}</CdtDbtInd><Dt><Dt>{on.isoformat()}</Dt></Dt></Bal>")

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02">
  <BkToCstmrStmt>
    <GrpHdr><MsgId>MSG-{escape(statement_id)}</MsgId><CreDtTm>{closing_date.isoformat()}T18:00:00</CreDtTm></GrpHdr>
    <Stmt>
      <Id>{escape(statement_id)}</Id>
      <ElctrncSeqNb>{sequence}</ElctrncSeqNb>
      <CreDtTm>{closing_date.isoformat()}T18:00:00</CreDtTm>
      <FrToDt><FrDtTm>{opening_date.isoformat()}T00:00:00</FrDtTm><ToDtTm>{closing_date.isoformat()}T23:59:59</ToDtTm></FrToDt>
      <Acct><Id><IBAN>{account}</IBAN></Id><Ccy>{currency}</Ccy></Acct>
      {bal('OPBD', Decimal(opening), opening_date)}
      {bal('CLBD', closing, closing_date)}{''.join(entries)}{extra_entries}
    </Stmt>
  </BkToCstmrStmt>
</Document>"""
    return xml.encode("utf-8")


CSV_COLUMNS = {"booking_date": "Date", "amount": "Amount", "bank_reference": "Reference", "narrative": "Description",
               "transaction_code": "Code"}


def csv_export(lines, *, with_balance=False, opening=None):
    """A CSV export: Date (dd/mm/yyyy), Amount (signed, money out negative), Reference, Description, Code
    [, Balance]."""
    header = "Date,Amount,Reference,Description,Code" + (",Balance" if with_balance else "")
    rows = [header]
    balance = Decimal(opening or "0")
    for booked, cd, amount, ttype, custref, bankref, narrative in lines:
        signed = Decimal(amount) if cd == "C" else -Decimal(amount)
        balance += signed
        row = f"{booked:%d/%m/%Y},{signed:f},{bankref or ''},\"{narrative or ''}\",{ttype}"
        rows.append(row + (f",{balance:f}" if with_balance else ""))
    return ("\r\n".join(rows) + "\r\n").encode("utf-8")


D = Decimal
# AT-3: RIYAD-SAR January statement, opening 100,000.00, six lines, closing 112,450.00.
AT3_LINES = [
    (date(2027, 1, 3), "C", "15000.00", "NTRF", "", "BR0001", "Customer transfer"),
    (date(2027, 1, 7), "D", "4000.00", "NTRF", "", "BR0002", "Supplier payment"),
    (date(2027, 1, 10), "D", "25.00", "NCHG", "", "BR0003", "CHARGES"),
    (date(2027, 1, 15), "C", "11475.00", "NTRF", "TRX88", "BR0004", "Incoming TRX88"),
    (date(2027, 1, 20), "D", "10100.00", "NTRF", "", "BR0005", "Transfer out"),
    (date(2027, 1, 28), "C", "100.00", "NTRF", "", "BR0006", "Refund"),
]
