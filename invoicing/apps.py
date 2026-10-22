from django.apps import AppConfig


class InvoicingConfig(AppConfig):
    """Invoicing (docs/specs/invoicing.md). Registers its document types with the ledger document registry.

    Every type registers a ``build_lines`` hook: the document builds its own journal lines from its
    stored data, so a caller cannot book other lines for it (access R34, R35).
    """

    name = "invoicing"
    verbose_name = "Invoicing / الفوترة"

    def ready(self):
        from ledger import documents, restricted

        from . import services
        from .models import (
            CustomerReceipt,
            DeferredProfitRelease,
            MigratedSettlement,
            PurchaseDocument,
            PurificationTransfer,
            SalesDocument,
            VendorPayment,
        )

        # R97 (policy A, D32(a)): the sub-ledger control accounts each type books (data in the registry; manual
        # journals are refused on them). Receivables 1200 / 1210 for sales documents and receipts, payables 2100 /
        # 2110 for bills and payments, all four for the settlement of migrated open items (R129, each on its own
        # item's account). Purification transfers and bai' ajel profit releases book none.
        receivable = (services.AR, services.AR_GROUP)
        payable = (services.AP, services.AP_GROUP)
        # Sales documents post only on ZATCA clearance or reporting (R41); they are never reversed (R17, OQ-28).
        documents.register(
            SalesDocument, post_permissions={"invoicing.post_on_clearance"},
            build_lines=services._sales_document_lines, validate_lines=services._check_document_rate,
            pending_documents=services._pending_sales_documents,
            contract_approval=services._contract_approval,  # billing R53 auto-issued invoices; R112 bai' ajel sales
            sama_tax_point=services._sama_tax_point,  # owner decision H2: booked at the SAMA tax-point rate
            control_accounts=receivable,
        )
        documents.register(
            PurchaseDocument, post_permissions={"invoicing.approve_bill"},
            reverse_permissions={"invoicing.reverse_document"},
            build_lines=services._purchase_document_lines, validate_lines=services._check_document_rate,
            reverse_guard=services._purchase_reverse_guard,  # R124: reversed only through reverse_document
            control_accounts=payable,
        )
        # D26 / access R46: the person who records a customer receipt may post it; refunds keep a second person.
        documents.register(
            CustomerReceipt, post_permissions={"invoicing.post_receipt"},
            reverse_permissions={"invoicing.reverse_document"},
            build_lines=services._receipt_lines, validate_lines=services._check_document_rate,
            creator_may_post=services._receipt_creator_may_post,
            reverse_guard=services._money_reverse_guard,  # R129: not while a migrated settlement uses it
            control_accounts=receivable,
        )
        documents.register(
            VendorPayment, post_permissions={"invoicing.approve_vendor_payment"},
            reverse_permissions={"invoicing.reverse_document"},
            build_lines=services._payment_lines, validate_lines=services._check_document_rate,
            reverse_guard=services._money_reverse_guard,  # R129: not while a migrated settlement uses it
            control_accounts=payable,
        )
        # R54: E14 by a second person with invoicing.post_purification_transfer; the funding (owner decision C2) and
        # E15 approved by the CFO. All are corrected by reversal (reverse_purification_transfer) with
        # invoicing.reverse_document.
        documents.register(
            PurificationTransfer,
            post_permissions={"invoicing.post_purification_transfer", "invoicing.approve_purification_funding",
                              "invoicing.approve_purification_payout"},
            reverse_permissions={"invoicing.reverse_document"},
            build_lines=services._purification_lines,
        )
        # R112 (payroll E12, E13): bai' ajel profit recognised as payroll collects the instalments (Dr 1241 /
        # Cr 4610), posted by the deferred-sale system user after the payroll approval that collected them; reversed
        # only through invoicing.services.reverse_deferred_profit with the payroll.reverse_run grant of a person.
        documents.register(
            DeferredProfitRelease, post_permissions={"invoicing.post_deferred_profit"},
            reverse_permissions={"payroll.reverse_run"}, changed_at_field=None,
            build_lines=services._deferred_profit_lines, reverse_guard=services._deferred_profit_reverse_guard,
        )
        # R129 (OQ-79): a foreign-currency settlement of a migrated open item, posted by its approval in the engine
        # (invoicing.migrated_settlement); undone only through invoicing.services.unallocate_migrated_item.
        documents.register(
            MigratedSettlement, post_permissions={services.MIGRATED_SETTLEMENT_APPROVAL},
            reverse_permissions={"invoicing.reverse_document"}, changed_at_field=None,
            build_lines=services._migrated_settlement_lines, reverse_guard=services._migrated_settlement_reverse_guard,
            control_accounts=receivable + payable,
        )
        # D31b review fix 1 and 2: GL 1140 (the purification account) is posted only by purification fundings and
        # payouts, the banking kinds banking allows on it and migration openings (and their reversals); every such
        # entry takes the purification lock and never takes the account below zero.
        restricted.restrict(
            services.PURIFICATION_BANK, name_en="Bank - purification account", name_ar="البنك - حساب التطهير",
            posted_by_en="purification fundings and payouts (invoicing), bank charges, Islamic profit and conventional "
                         "interest on the purification account (banking), migration opening balances and their "
                         "reversals",
            posted_by_ar="تمويل حساب التطهير والصرف منه (الفوترة)، والرسوم البنكية والأرباح الإسلامية والفوائد "
                         "التقليدية على حساب التطهير (البنوك)، والأرصدة الافتتاحية المرحّلة، وعكسها",
            lock=services._lock_purification, check=services._purification_gl_check,
            # owner decision C6: restricted cash under IFRS; it never returns to the company
            restricted_cash=True, presentation_en=services.PURIFICATION_CASH_NOTE_EN,
            presentation_ar=services.PURIFICATION_CASH_NOTE_AR,
        )
        restricted.allow(services.PURIFICATION_BANK, document_type="invoicing.purificationtransfer",
                         predicate=services._purification_gl_allowed)
        # Review fix 3 (R127): the bai' ajel accounts are posted only by their documents and their reversals: 1240
        # by the sale invoice (and its payroll-approved adjustment credit note) and payroll's runs and final
        # settlements (instalments collected, registered in payroll/apps.py); 1241 by the sale invoice, its
        # adjustment and the profit recognitions; 4610 by the profit recognitions only. Manual journals never.
        restricted.restrict(
            services.EMPLOYEE_RECEIVABLE, name_en="Employee deferred-sale (bai' ajel) receivable",
            name_ar="ذمم البيع الآجل للموظفين",
            posted_by_en="the bai' ajel sale invoice and its payroll-approved adjustment (invoicing), payroll runs and "
                         "final settlements collecting the instalments, and their reversals",
            posted_by_ar="فاتورة البيع الآجل وتسويتها المعتمدة في الرواتب (الفوترة)، ومسيرات الرواتب والمخالصات "
                         "النهائية التي تحصّل الأقساط، وعكسها",
        )
        restricted.restrict(
            services.DEFERRED_PROFIT, name_en="Deferred bai' ajel profit", name_ar="أرباح البيع الآجل المؤجلة",
            posted_by_en="the bai' ajel sale invoice, its payroll-approved adjustment and the bai' ajel profit "
                         "recognitions (invoicing), and their reversals",
            posted_by_ar="فاتورة البيع الآجل وتسويتها المعتمدة في الرواتب وقيود تحقق أرباح البيع الآجل (الفوترة)، وعكسها",
        )
        restricted.restrict(
            services.SALE_PROFIT_INCOME, name_en="Bai' ajel profit income", name_ar="إيرادات أرباح البيع الآجل",
            posted_by_en="the bai' ajel profit recognitions (invoicing) and their reversals",
            posted_by_ar="قيود تحقق أرباح البيع الآجل (الفوترة) وعكسها",
        )
        for code in (services.EMPLOYEE_RECEIVABLE, services.DEFERRED_PROFIT):
            restricted.allow(code, document_type="invoicing.salesdocument",
                             predicate=services._bai_ajel_sale_entry_allowed)
        for code in (services.DEFERRED_PROFIT, services.SALE_PROFIT_INCOME):
            restricted.allow(code, document_type="invoicing.deferredprofitrelease",
                             predicate=services._deferred_profit_entry_allowed)

        from . import approval_adapters

        approval_adapters.register()  # access R61: the module's documents go through the one approval engine
