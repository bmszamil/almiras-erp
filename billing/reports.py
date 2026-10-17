"""Exports of billing reports with English or Arabic labels (billing R64, R69).

Pure formatting of rows a service has already scoped to the user (``billing.services.intercompany_reconciliation``):
nothing here reads the database or checks rights. CSV only; a PDF export needs an open-source PDF library that is
not in ``requirements.txt`` yet.
"""
import csv
import io

RECONCILIATION_COLUMNS = (
    ("as_of", "As of", "حتى تاريخ"),
    ("seller", "Seller", "البائع"),
    ("buyer", "Buyer", "المشتري"),
    ("currency", "Currency", "العملة"),
    ("seller_position", "Seller position", "مركز البائع"),
    ("buyer_position", "Buyer position", "مركز المشتري"),
    ("difference", "Difference", "الفرق"),
    ("billing_open", "Billing documents open (seller)", "مستندات الفوترة المفتوحة (البائع)"),
    ("billing_unposted", "Billing documents awaiting ZATCA (seller)", "مستندات الفوترة بانتظار الهيئة (البائع)"),
    ("buyer_billing_open", "Billing documents open (buyer)", "مستندات الفوترة المفتوحة (المشتري)"),
    ("buyer_billing_unposted", "Billing documents awaiting ZATCA (buyer)", "مستندات الفوترة بانتظار الهيئة (المشتري)"),
)
HIDDEN = {"seller_position": "seller_note", "buyer_position": "buyer_note"}


def reconciliation_csv(rows, language="en"):
    """CSV text of reconciliation rows; a side the user cannot see shows "not visible" (en/ar), never an amount."""
    lang = "ar" if language == "ar" else "en"
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([ar if lang == "ar" else en for _, en, ar in RECONCILIATION_COLUMNS])
    for row in rows:
        values = []
        for key, _, _ in RECONCILIATION_COLUMNS:
            value = row.get(key)
            if value is None and key in HIDDEN:
                value = row.get(f"{HIDDEN[key]}_{lang}", "")
            values.append("" if value is None else str(value))
        writer.writerow(values)
    return out.getvalue()
