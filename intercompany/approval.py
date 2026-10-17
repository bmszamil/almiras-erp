"""Intercompany documents in the approvals engine (approvals spec §3.3, D11; intercompany spec R10, D7).

Same contract as ledger.approval.JournalEntryApproval:

  agreement  draft/suspended --submit--> pending_approval --final approve--> active
                                                          --reject--> ended (never active) / suspended
                                                          --return/cancel/expire--> draft / suspended
  invoice    draft --submit--> pending_approval --final approve--> approved --issue (same transaction)--> issued
  (and credit note)                             --reject--> rejected (billed records released, R14)
                                                --return/cancel/expire--> draft
  bill       draft --submit--> pending_approval --final approve--> approved --post (same transaction)--> posted
                                                --reject--> disputed (resolved by an issuer credit note, R16)
                                                --return/cancel/expire--> draft

Required approvers come from the approvals matrix (ApprovalPolicy), which replaces the spec's
billing.services.required_approvals(): today one group-wide step, the Managing Partner. If issuing or
posting fails a ledger rule, the approvals engine rolls the whole approval back (D11).
"""
from django.utils import timezone

from approvals.registry import ApprovalAdapter

from . import services
from .models import IntercompanyBill, IntercompanyInvoice, ServiceAgreement

AS, IS, BS = ServiceAgreement.Status, IntercompanyInvoice.Status, IntercompanyBill.Status


class _Base(ApprovalAdapter):
    def guard_submitter(self, request):
        """D15: agents and integrations never submit (also through the generic approvals API)."""
        services.require_can_submit(request.submitted_by)

    def source(self, doc):
        return doc.source_label

    def editor_ids(self, doc):
        ids = services.draft_editor_ids(doc)
        creator = self.created_by(doc)
        if creator is not None:
            ids.add(creator.pk)
        return ids


class AgreementApproval(_Base):
    model = ServiceAgreement
    submit_permission = "intercompany.agreement.create"
    fingerprint_exclude = {"status", "activated_at", "created_at"}

    def is_submittable(self, a):
        return a.status in (AS.DRAFT, AS.SUSPENDED)

    def approval_amount(self, a):
        return (a.fixed_fee if a.pricing_method == a.Pricing.FIXED_FEE else None), a.currency

    def approval_summary(self, a):
        resume = a.status == AS.SUSPENDED
        return (
            f"{'Resume ' if resume else ''}Service agreement {a.number} {a.entity.code} to {a.counterparty.code}: "
            f"{a.service_type.name_en}, {a.get_pricing_method_display()}",
            f"{'استئناف ' if resume else ''}اتفاقية خدمات {a.number} من {a.entity.code} إلى {a.counterparty.code}: "
            f"{a.service_type.name_ar}",
        )

    def fingerprint_data(self, a):
        data = super().fingerprint_data(a)
        data["rates"] = [[r.pk, r.role_en, r.role_ar, str(r.hourly_rate), str(r.valid_from), str(r.valid_to)]
                         for r in a.rates.order_by("id")]
        return data

    def snapshot(self, a):
        return {"status": a.status, "number": a.number}

    def on_submitted(self, a, request):
        self.guard_submitter(request)
        services.validate_agreement(a)
        services._move(a, AS.PENDING_APPROVAL)

    def on_approved(self, a, request, actor):
        services.validate_agreement(a)
        # Review 5: freeze the account codes on the agreement, so later service-type edits cannot move them.
        services._move(a, AS.ACTIVE, activated_at=a.activated_at or timezone.now(),
                       income_account_code=a.income_account_code or a.service_type.income_account_code,
                       expense_account_code=a.expense_account_code or a.service_type.expense_account_code)

    def on_rejected(self, a, request):
        services._move(a, AS.SUSPENDED if a.activated_at else AS.ENDED)

    def on_returned(self, a, request):
        services._move(a, AS.SUSPENDED if a.activated_at else AS.DRAFT)


class InvoiceApproval(_Base):
    model = IntercompanyInvoice
    submit_permission = "intercompany.invoice.create"
    fingerprint_exclude = {"status", "number", "journal_entry", "issued_at", "approved_by", "zatca_uuid",
                           "zatca_ready", "zatca_status", "zatca_hash", "zatca_updated_at", "created_at",
                           "seller_vat_number", "buyer_vat_number", "deadline_alerted_at"}

    def approval_amount(self, inv):
        return inv.gross_total, inv.currency

    def approval_summary(self, inv):
        if inv.is_credit_note:
            return (
                f"Intercompany credit note {inv.entity.code} to {inv.counterparty.code} on "
                f"{inv.credit_of.number}: {inv.credit_reason_en}",
                f"إشعار دائن بين شركات المجموعة من {inv.entity.code} إلى {inv.counterparty.code} على "
                f"{inv.credit_of.number}: {inv.credit_reason_ar}",
            )
        return (
            f"Intercompany invoice {inv.entity.code} to {inv.counterparty.code} {inv.billing_period} "
            f"({inv.agreement.number}): {inv.agreement.description_en}",
            f"فاتورة بين شركات المجموعة من {inv.entity.code} إلى {inv.counterparty.code} {inv.billing_period} "
            f"({inv.agreement.number}): {inv.agreement.description_ar}",
        )

    def fingerprint_data(self, inv):
        data = super().fingerprint_data(inv)
        data["lines"] = [
            [l.pk, l.description_en, l.description_ar, str(l.quantity), str(l.unit_price), str(l.net_amount),
             str(l.vat_rate), str(l.vat_amount), l.income_account_id, l.credits_line_id]
            for l in inv.lines.order_by("id")
        ]
        return data

    def snapshot(self, inv):
        return {"status": inv.status, "number": inv.number}

    def on_submitted(self, inv, request):
        self.guard_submitter(request)
        services.validate_for_issue(inv)
        services._move(inv, IS.PENDING_APPROVAL)

    def on_approved(self, inv, request, actor):
        services.validate_for_issue(inv)  # T22 again at approval
        services._move(inv, IS.APPROVED, approved_by=actor)
        services._issue(inv, approver=actor, actor=actor)

    def on_rejected(self, inv, request):
        services._release(inv)
        services._move(inv, IS.REJECTED)

    def on_returned(self, inv, request):
        services._move(inv, IS.DRAFT)


class BillApproval(_Base):
    model = IntercompanyBill
    submit_permission = "intercompany.bill.edit"
    fingerprint_exclude = {"status", "journal_entry", "dispute_reason", "prepared_by", "created_at"}

    def created_by(self, bill):
        """System-made bills: the receiver user who prepared and submitted it (services.submit_bill)."""
        return bill.prepared_by

    def approval_amount(self, bill):
        return bill.gross_total, bill.currency

    def approval_summary(self, bill):
        kind_en = "bill credit" if bill.kind == bill.Kind.CREDIT else "bill"
        kind_ar = "إشعار دائن مقابل" if bill.kind == bill.Kind.CREDIT else "فاتورة مقابلة"
        return (
            f"Intercompany {kind_en} {bill.number} from {bill.counterparty.code} ({bill.invoice.number})",
            f"{kind_ar} {bill.number} من {bill.counterparty.code} ({bill.invoice.number})",
        )

    def fingerprint_data(self, bill):
        data = super().fingerprint_data(bill)
        data["lines"] = [[l.pk, l.expense_account_id, l.cost_centre, str(l.net_amount), str(l.vat_amount)]
                         for l in bill.lines.order_by("id")]
        return data

    def snapshot(self, bill):
        return {"status": bill.status, "number": bill.number}

    def on_submitted(self, bill, request):
        self.guard_submitter(request)
        services.check_bill_postable(bill)
        services._move(bill, BS.PENDING_APPROVAL)

    def on_approved(self, bill, request, actor):
        services._move(bill, BS.APPROVED)
        services.post_bill(bill)

    def on_rejected(self, bill, request):
        comment = request.actions.filter(action="reject").order_by("-at").values_list("comment", flat=True).first()
        services._move(bill, BS.DISPUTED, dispute_reason=(comment or "Rejected in approval")[:500])

    def on_returned(self, bill, request):
        services._move(bill, BS.DRAFT)

