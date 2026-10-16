from approvals.registry import ApprovalAdapter

from .models import Bill, Payment


class _Adapter(ApprovalAdapter):
    label = ("Document", "مستند")

    def approval_amount(self, doc):
        return doc.amount, doc.currency

    def approval_summary(self, doc):
        en, ar = self.label
        return f"{en} to {doc.counterparty}", f"{ar} إلى {doc.counterparty}"

    def _set(self, doc, status):
        doc.status = status
        doc.save(update_fields=["status"])

    def on_submitted(self, doc, request):
        self._set(doc, doc.Status.PENDING_APPROVAL)

    def on_approved(self, doc, request, actor):
        self._set(doc, doc.Status.APPROVED)
        self._set(doc, doc.Status.EXECUTED)

    def on_rejected(self, doc, request):
        self._set(doc, doc.Status.REJECTED)

    def on_returned(self, doc, request):
        self._set(doc, doc.Status.DRAFT)


class PaymentApproval(_Adapter):
    model = Payment
    label = ("Payment", "دفعة")


class BillApproval(_Adapter):
    model = Bill
    label = ("Supplier bill", "فاتورة مورد")
