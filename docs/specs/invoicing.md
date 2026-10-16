# Invoicing (Track A): accounts receivable and accounts payable, with VAT

Django app: `invoicing`. Status: **draft spec. No business decisions recorded yet** (see section 2). Written 2026-10-16.
Companion spec: `docs/specs/zatca.md` (ZATCA Phase 2 e-invoicing), written in parallel. Hand-off points are in sections 4 (rules R40 to R46) and 6.

## 1. Purpose

The invoicing module keeps the customer and vendor sub-ledgers for **every entity** in the group: Almiras, subsidiaries, and any entity added later as an `Entity` row. On the AR side it covers customers, sales invoices, sales credit notes and debit notes, and customer receipts and refunds with allocation to invoices. On the AP side it covers vendors, vendor bills, vendor credit notes, and vendor payments with allocation. It calculates KSA VAT on every document. It creates and posts the matching journal entries **only** through `ledger.services` (`create_entry`, `post_entry`, `reverse_entry`). It passes sales documents to the `zatca` module for clearance or reporting. Each entity's Sharia setting is applied to every document line, and every user-facing text is held in English and Arabic.

## 2. Decisions

**The Managing Partner and the accountant have not recorded any business decisions for this module yet.** The table lists only what CLAUDE.md, the README and the existing code already fix. Every other business choice is in section 9.

| # | Decision | Source | Date |
|---|---|---|---|
| D1 | Every business record has an `entity` FK. Views and APIs are always filtered by entity and by user permission. New subsidiaries are data, not new code paths. | CLAUDE.md, "Multi-entity rules" | in repo as of 2026-10-16 (no meeting date recorded) |
| D2 | Sharia is enforced per entity (`Entity.sharia_enforced`). Sharia-prohibited accounts are blocked. Non-compliant income is flagged for purification. | CLAUDE.md rule 5; `ledger.services.validate_entry` | same |
| D3 | Every user-facing text has English and Arabic (`name_en` / `name_ar`, or Django translations). | CLAUDE.md | same |
| D4 | Money is `Decimal`, never float. Amounts are stored with 2 decimals, and the currency is explicit. | CLAUDE.md rule 7 | same |
| D5 | Posted journal entries are never edited or deleted. Corrections are reversals. Posting goes only through `post_entry`. Closed periods reject postings. An entity never posts to another entity's accounts. | CLAUDE.md rules 1 to 4; `JournalEntry.save/delete`; `validate_entry` | same |
| D6 | Intercompany lines always carry a counterparty entity. | CLAUDE.md rule 6; `validate_entry` | same |
| D7 | Numbering is gap-free per entity and year (journal entries: `{entity.code}-JE-{year}-{000001}`, via `EntrySequence` with `select_for_update`). | `ledger.services._next_number`; README | same |
| D8 | Agents create drafts only, act as named service users, and every action is logged. Business records carry a `source` field (human / import / agent). | `docs/ai-agent-gateway.md` | same |
| D9 | Business logic lives in `services.py`. Every rule gets a test. | CLAUDE.md | same |

### Ledger API this module must use (actual signatures)

| Function | Signature | Notes for invoicing |
|---|---|---|
| `create_entry` | `create_entry(entity, date, description, lines, user=None, reference="", post=False)` | `lines` is an iterable of dicts: `account` (code or `Account`), `debit`, `credit`, `description`, `counterparty` (an `Entity` or an entity code). Account codes are looked up **within `entity`**. Invoicing passes `Decimal` amounts and sets `reference` = document number (max 100 chars). |
| `post_entry` | `post_entry(entry, user=None)` | Draft only. It runs `validate_entry` (balance, 2+ lines, entity match, postable, Sharia, intercompany counterparty), finds the open period for `entry.date`, and assigns the JE number. |
| `reverse_entry` | `reverse_entry(entry, date=None, user=None, reason="")` | Posted only. `date` defaults to the **original** date, so if that period is closed the caller must pass a date inside an open period. |
| `close_period` | `close_period(period)` | Rejects the close while draft JEs exist in the period. Invoicing relies on this (R43). |

## 3. Data model

Conventions: every model has `entity` FK → `core.Entity` (`on_delete=PROTECT`, required). Money is `DecimalField(max_digits=18, decimal_places=2)`. Every model also has `created_by`, `created_at`, `updated_at` and `source` (choices `human` / `import` / `agent:<name>`, required, default `human`). "Bi" means the field exists as both `_en` and `_ar`.

### 3.1 Parties

**Customer** (unique `(entity, code)`)
| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| entity | FK Entity | Y | | Owning entity. Customers are not shared across entities (OQ-14). |
| code | Char(20) | Y | | |
| name_en / name_ar | Char(200) | Y / Y | Bi | Arabic name is required (ZATCA buyer name, D3) |
| customer_kind | Choice: `business`, `individual`, `government` | Y | | Drives the standard vs simplified default (OQ-19) |
| vat_number | Char(15) | N | | If set: 15 digits, starts and ends with `3` (ZATCA format) |
| other_id_scheme | Choice: `CRN`, `MOM`, `MLS`, `700`, `SAG`, `NAT`, `GCC`, `IQA`, `PAS`, `OTH` | N | | Buyer ID when there is no VAT number (ZATCA) |
| other_id | Char(30) | N | | |
| address: street, building_no, additional_no, district, city, postal_code, country (ISO-2, default `SA`) | Char | Y for standard invoices | Bi for street, district, city | ZATCA buyer address. `building_no` is 4 digits and `postal_code` 5 digits when country = SA |
| group_entity | FK Entity, null | N | | Set when the customer is a group entity. Makes its invoices intercompany. Must not equal `entity`. |
| payment_terms_days | PositiveInt, null | N | | Undecided (OQ-3) |
| currency | Char(3) | Y | | Default `entity.base_currency` (OQ-6) |
| is_active | Bool | Y | | |

**Vendor** (unique `(entity, code)`). Same pattern as Customer, plus:
| Field | Type | Req | Notes |
|---|---|---|---|
| is_resident | Bool | Y | False → reverse-charge VAT (R30) and possible withholding tax (OQ-8) |
| vat_number | Char(15) | N | Needed to recover input VAT on resident vendors' bills (R29) |
| bank_iban | Char(34) | N | For payment files (OQ-21). Store masked in fixtures. |
| group_entity | FK Entity, null | N | Intercompany vendor |

### 3.2 Reference data

**VatRate** (global statutory table, not per entity)
| Field | Type | Req | Notes |
|---|---|---|---|
| category | Choice: `S` standard, `Z` zero-rated, `E` exempt, `O` out of scope | Y | ZATCA UNCL5305 codes |
| rate | Decimal(5,2) | Y | e.g. 15.00 for S, 0.00 otherwise |
| valid_from / valid_to | Date / Date null | Y / N | Rates are data with effective dates, never constants |
| name_en / name_ar | Char | Y | Bi |

**VatExemptionReason**: `code` (ZATCA `VATEX-SA-…`), `name_en`, `name_ar`. Required on lines with category Z, E or O.

**DocumentSequence** (unique `(entity, doc_type, year)`): `entity`, `doc_type` (`INV`, `CN`, `DN`, `BILL`, `VCN`, `RCPT`, `PAY`), `year`, `last_number`. Locked with `select_for_update`, the same pattern as `EntrySequence`.

### 3.3 Sales documents

**SalesDocument** (one model; `doc_type` = `invoice` / `credit_note` / `debit_note`)
| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| entity | FK Entity | Y | | Seller |
| doc_type | Choice | Y | | Maps to ZATCA type code 388 / 381 / 383 |
| zatca_profile | Choice: `standard` (B2B/B2G, clearance) or `simplified` (B2C, reporting) | Y | | |
| zatca_flags | JSON / bool set: third_party, nominal, export, summary, self_billed | N | | ZATCA invoice transaction code positions 3 to 7 |
| number | Char(40), blank until issue | | | Gap-free; format undecided (OQ-4) |
| uuid | UUID | Y | | Generated at creation, never changed (ZATCA) |
| status | Choice: `draft`, `issued`, `final`, `rejected` | Y | | See R10 |
| customer | FK Customer (same entity) | Y | | |
| buyer snapshot: name_en, name_ar, vat_number, other_id_scheme, other_id, full address | Char | Y for standard | Bi | Copied from the Customer at issue and frozen after |
| seller snapshot: name_en, name_ar, vat_number, cr_number, full address | Char | Y | Bi | Copied from Entity at issue. **Entity has no address fields yet** (OQ-1). |
| issue_date / issue_time | Date / Time | Y at issue | | ZATCA needs both |
| supply_date / supply_end_date | Date / Date null | Y for standard | | Actual supply date or period |
| due_date | Date | Y | | Default rule undecided (OQ-3) |
| currency | Char(3) | Y | | Must equal entity base currency for now (OQ-6) |
| payment_means_code | Char(3) (UNCL4461: 10, 30, 42, 48, 1) | N | | |
| original_document | FK SalesDocument (invoice), null | Y for CN/DN | | ZATCA BillingReference |
| reason_en / reason_ar | Char(300) | Y for CN/DN | Bi | ZATCA instruction note |
| document_discount | Money | N | | Document-level allowance |
| total_net / total_vat / total_gross | Money | computed | | Stored at issue |
| amount_allocated | Money | computed | | Sum of allocations |
| payment_state | Choice: `unpaid`, `partial`, `paid`, `credited` | computed | | |
| requires_purification | Bool | computed | | True if any line hits an account with `requires_purification` |
| is_intercompany | Bool | computed | | `customer.group_entity` is set |
| journal_entry | FK ledger.JournalEntry, null, PROTECT | | | Draft at issue, posted at final (R41) |
| notes_en / notes_ar | Text | N | Bi | |
| **ZATCA fields (written only by `zatca.services`, read-only here)** | | | | Ownership to be agreed with zatca.md (OQ-2) |
| zatca_icv | PositiveInt, null | | | Invoice counter value |
| zatca_previous_hash | Char, null | | | PIH link |
| zatca_hash | Char, null | | | |
| zatca_qr | Text, null | | | TLV base64 |
| zatca_status | Choice: `not_submitted`, `pending`, `cleared`, `reported`, `cleared_with_warnings`, `reported_with_warnings`, `rejected` | Y | | Default `not_submitted` |
| zatca_submission | FK zatca.Submission, null | | | Full XML, responses and errors live in zatca |

**SalesDocumentLine**
| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| document | FK SalesDocument | Y | | `entity` is the same as the document's (denormalised and checked) |
| line_no | PositiveInt | Y | | |
| description_en / description_ar | Char(300) | at least one; Arabic required on sales documents (default, OQ-18) | Bi | |
| quantity | Decimal(12,3) | Y | | > 0 |
| unit_code | Char(3) (UN/ECE rec 20, e.g. `C62`, `MON`, `HUR`) | Y | | |
| unit_price | Decimal(18,2) | Y | | ≥ 0. Precision undecided (OQ-12) |
| line_discount | Money | N | | |
| net_amount | Money | computed | | round(qty × unit_price − discount) |
| vat_category | FK VatRate | Y | | Rate effective on `supply_date` (or `issue_date`) |
| vat_exemption_reason | FK VatExemptionReason | Y if Z/E/O | | |
| vat_amount | Money | computed | | Rounding policy undecided (OQ-11) |
| income_account | FK ledger.Account (same entity, type income, postable) | Y | | |

### 3.4 Purchase documents

**PurchaseDocument** (`doc_type` = `bill` / `vendor_credit_note`)
| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| entity, doc_type, vendor (FK Vendor, same entity) | | Y | | |
| number | Char(40), blank until posting | | | Internal gap-free number |
| vendor_document_number | Char(60) | Y | | Vendor's invoice / credit note number |
| vendor_document_date | Date | Y | | |
| vendor_vat_number | Char(15) | N | | Snapshot. Needed to recover input VAT (R29) |
| accounting_date | Date | Y | | JE date |
| due_date | Date | Y | | |
| currency | Char(3) | Y | | = base currency for now (OQ-6) |
| status | Choice: `draft`, `submitted`, `approved`, `posted`, `reversed` | Y | | |
| original_document | FK PurchaseDocument, null | Y for vendor credit note (default, OQ-10) | | |
| reverse_charge | Bool | computed | | `not vendor.is_resident` |
| totals, amount_allocated, payment_state, is_intercompany, requires_purification | as for SalesDocument | | | |
| attachment | File | N | | Vendor PDF. Agent "invoice reader" drafts from it (D8) |
| approved_by / approved_at | FK User / DateTime | | | |
| journal_entry / reversal_entry | FK JournalEntry, null, PROTECT | | | |

**PurchaseDocumentLine**: `description_en/_ar` (Bi, at least one), `quantity`, `unit_price`, `line_discount`, `net_amount`, `vat_category`, `vat_amount`, `vat_recoverable` (Bool, default True, OQ-9), `account` (FK Account, same entity; type expense or asset; postable).

### 3.5 Money movements and allocation

**CustomerReceipt** (`kind` = `receipt` / `refund`) and **VendorPayment** (`kind` = `payment` / `refund_received`)
| Field | Type | Req | Notes |
|---|---|---|---|
| entity, party (Customer / Vendor, same entity) | FK | Y | |
| number | Char(40) | at posting | Gap-free |
| date | Date | Y | |
| amount | Money | Y | > 0 |
| bank_charge | Money | N | Receipts only. Booked to 5700 |
| currency | Char(3) | Y | = base currency for now |
| bank_account | FK ledger.Account (same entity, asset) | Y | Default 1100 (OQ-20) |
| reference_en / _ar, bank_reference | Char | N | |
| status | Choice: `draft`, `posted`, `reversed` | Y | |
| journal_entry / reversal_entry | FK JournalEntry | | |
| amount_allocated / amount_unallocated | Money | computed | |

**Allocation** (`ReceivableAllocation`, `PayableAllocation`)
| Field | Type | Req | Notes |
|---|---|---|---|
| entity | FK Entity | Y | Must equal the entity of both sides |
| source | FK receipt / payment, or credit-note document | Y | |
| target | FK invoice / debit note / bill | Y | |
| amount | Money | Y | Non-zero. Negative only as an un-allocation of an earlier row (R36) |
| date, created_by | | Y | |
| reverses | FK self, null | N | |

## 4. Rules

Each rule ends with **[enforcement]**. "svc" = `invoicing.services`; "DB" = DB constraint; "ledger" = enforced by existing ledger code; "test" = covered by acceptance test.

### Entity separation
1. Every invoicing record has an `entity`. Party, lines, accounts, allocations and journal entries on a document all belong to that same entity. **[svc validation on every create/issue/post; ledger `validate_entry` rejects foreign accounts; test AT-3]**
2. Account codes are always resolved inside the document's entity (`create_entry` looks up `Account(entity=entity, code=…)`). **[ledger]**
3. Querysets in views and APIs are filtered by entity and by the user's entity permissions. A user with no permission on entity B can neither see nor reference B's parties or documents. **[svc/queryset manager + permission check; test AT-4]**
4. A customer or vendor whose `group_entity` is set makes the document intercompany. The AR/AP line goes to 1210 / 2110 with `counterparty = group_entity`. `group_entity` can never equal `entity`. **[svc; ledger `validate_entry`; model `clean`; test AT-20]**

### Sharia
5. A line account flagged `sharia_prohibited` cannot be used on any document of an entity with `sharia_enforced=True`. The check runs when the line is saved (early error) and again by the ledger at posting. **[svc + ledger; test AT-12]**
6. A document with any line on an account flagged `requires_purification` gets `requires_purification=True` and appears on the purification report. Posting it is allowed. **[svc computed flag; test AT-13]**
7. No interest, late-payment penalty or time-value charge can be posted as income in a Sharia-enforcing entity. No late-payment charge feature is built until OQ-15 is decided. **[svc: no such feature; account checks per R5]**

### Money and VAT
8. Every amount is `Decimal` with 2 decimal places. A float input is rejected with an error, not converted. Currency is stored on every document and must equal `entity.base_currency` until OQ-6 is decided. **[svc input validation; DB decimal fields; test AT-14]**
9. Line `net_amount` = `quantity × unit_price − line_discount`, rounded to 2 dp. `vat_amount` = `net_amount × rate`, rounded to 2 dp, per line. Document totals are sums of line values. Rounding mode is ROUND_HALF_UP by default (OQ-11). **[svc; test AT-1]**
10. The VAT rate is taken from `VatRate` effective on the supply date (or the issue date if no supply date). Lines with category Z, E or O need an exemption reason. **[svc; test AT-15]**
11. An entity with an empty `vat_number` cannot issue sales documents with category S lines. **[svc; default pending OQ-17; test AT-16]**

### Sales document lifecycle
12. Sales document status runs `draft → issued → final`, or `issued → rejected`. No other transitions exist. **[svc state machine; test AT-2]**
13. Only drafts can be edited or deleted. Issued, final and rejected documents are immutable except for fields written by `zatca.services` and the computed payment fields. **[model `save` guard like `JournalEntry.save`; svc; test AT-5]**
14. On issue, the system assigns the number from `DocumentSequence(entity, doc_type, year of issue_date)`. The number is gap-free and never reused. Assigning the number and changing the status happen in one transaction. A failure rolls back both. **[svc `select_for_update`; DB unique `(entity, doc_type, number)`; test AT-6]**
15. On issue, the buyer and seller snapshots are frozen. Later edits to the Customer or Entity do not change issued documents. **[svc; test AT-7]**
16. A standard (B2B) document needs a buyer name in Arabic, a full buyer address, and either a buyer VAT number or an `other_id`. **[svc validation at issue; test AT-8]**
17. Issued invoices are never cancelled or deleted. A final invoice is corrected only by a credit note or debit note that references it. **[svc: no cancel path; ZATCA requirement; test AT-9]**
18. A credit note must reference a `final` invoice of the same entity and customer, and it needs a reason (EN or AR). Its gross amount cannot exceed the invoice gross amount minus credit notes already issued against that invoice. **[svc; test AT-10]**
19. A debit note must reference a `final` invoice of the same entity and customer, and it needs a reason. **[svc]**
20. A rejected document keeps its number and has no posted GL effect. The correction is a new document. **[svc; test AT-19]**

### Purchase document lifecycle
21. Vendor bill status runs `draft → submitted → approved → posted`, then `posted → reversed`. Approval thresholds are undecided (OQ-5). By default every bill needs one approval by a user other than its creator. **[svc; test AT-21]**
22. A document created by an agent (`source=agent:*`) stays a draft until a human submits it. **[svc; D8; test AT-22]**
23. `(entity, vendor, vendor_document_number)` is unique among non-reversed purchase documents (duplicate-bill guard, default pending OQ-10). **[DB partial unique + svc; test AT-23]**
24. A posted bill is never edited. It is corrected by a vendor credit note, or (when it has no allocations) by reversal through `ledger.services.reverse_entry`, which sets the status to `reversed`. **[svc; ledger lock; test AT-24]**
25. A bill with allocations cannot be reversed until those allocations are un-allocated (R36). **[svc]**
26. The internal number is assigned at posting from `DocumentSequence`, gap-free. **[svc]**

### Posting (all documents)
27. Every journal entry is created with `ledger.services.create_entry(..., reference=document.number)` and posted only with `post_entry`. Invoicing never sets `JournalEntry.status`. **[svc; code-reviewer; test AT-1]**
28. The JE date is `issue_date` for sales documents, `accounting_date` for purchase documents and `date` for receipts and payments. Posting fails when no open period covers that date. The document stays in its previous status and no number is consumed. **[ledger `_find_open_period`; svc single transaction; test AT-17]**
29. Input VAT on a bill line goes to 1310 only when the line is `vat_recoverable` and the vendor document has a vendor VAT number. Otherwise the VAT is added to the line's expense or asset account (default, OQ-9). **[svc; test AT-25]**
30. A bill from a non-resident vendor with S-category services is posted with reverse-charge VAT: Dr 1310 and Cr 2200 for the same amount, and AP holds net only. **[svc; KSA VAT reverse-charge; test AT-26]**
31. Payment posting to a non-resident vendor is blocked until OQ-8 (withholding tax) is decided. **[svc; test AT-27]**
32. Reversals use `reverse_entry(entry, date=..., user=..., reason=...)`. If the original period is closed, the service passes a date in the current open period. It never relies on the default `date=None`. **[svc; test AT-24]**

### Receipts, payments and allocation
33. A receipt or payment posts at once (`create_entry(..., post=True)`). It can be reversed only while it has no allocations. **[svc; test AT-28]**
34. An allocation needs source and target to be the same entity and the same party, both posted/final, and an amount no higher than the open amount on either side. **[svc with row locks; DB check amount ≠ 0; test AT-29]**
35. Allocation has no GL effect (both sides sit on the same control account). It changes `payment_state` only. **[svc]**
36. Allocations are never edited or deleted. An un-allocation is a new row with negative amount and `reverses` set. **[model `save/delete` guard; test AT-30]**
37. On reaching `final`, a credit note is automatically allocated to its original invoice up to the invoice's open amount. Any remainder stays as customer credit. **[svc; test AT-10]**
38. Unallocated receipts are allowed. They stay as a credit balance on the customer and appear on the unallocated-cash report. VAT treatment of advances is open (OQ-16). **[svc + report]**
39. The open amounts in the sub-ledger tie out to the GL: the sum of open customer balances per entity equals the trial balance of 1200, plus 1210 per counterparty for intercompany customers. The same holds for AP against 2100 and 2110. **[svc `reconcile_subledger(entity, as_of)`; test AT-31]**

### ZATCA hand-off (details in `docs/specs/zatca.md`; service names provisional, OQ-2)
40. When a sales document is issued, invoicing calls `zatca.services.submit_document(document, user)` in the same request after the issue transaction commits. Standard documents are submitted for clearance and simplified ones for reporting. **[svc; test AT-18 with zatca stub]**
41. At issue, invoicing builds the GL entry with `create_entry(..., post=False)`. That draft JE is posted with `post_entry` only when `zatca.services` reports `cleared`, `reported` or `*_with_warnings`. The document then becomes `final`. **[svc callback `on_zatca_result(document, result)`; test AT-18]**
42. On ZATCA `rejected`, the document becomes `rejected` and its draft JE is deleted (drafts are deletable per `JournalEntry.delete`). **[svc; test AT-19]**
43. A period cannot close while any sales document dated inside it waits for ZATCA. This follows from the draft JE, which `close_period` refuses. **[ledger `close_period`; test AT-32]**
44. A document is not final, cannot be allocated, and cannot be credited until it is cleared or reported. A standard invoice is not shared with the buyer before clearance. **[svc; test AT-18]**
45. Fields `zatca_*` are written only by `zatca.services`. Invoicing never computes the hash, ICV, PIH or QR. **[model guard / code-reviewer]**
46. A purchase document is never sent to ZATCA. **[svc]**

## 5. Accounting

Example amounts are SAR. Account codes come from `ledger/coa_template.py`. The income or expense account comes from each document line. The tables show one line for clarity.

**E1. Sales invoice or debit note, third party, final** (S-rated, net 10,000.00, VAT 15%)
| Account | Dr | Cr | Counterparty |
|---|---|---|---|
| 1200 Trade receivables | 11,500.00 | | |
| 4100 / 4200 / 4300 (per line) | | 10,000.00 | |
| 2200 VAT output | | 1,500.00 | |

**E2. Sales invoice, intercompany** (customer `group_entity` = SUB)
| Account | Dr | Cr | Counterparty |
|---|---|---|---|
| 1210 Due from group entities | 11,500.00 | | SUB |
| 4110 / 4210 (per line) | | 10,000.00 | |
| 2200 VAT output | | 1,500.00 | |
VAT inside the group is subject to OQ-13 (VAT group). There is no intercompany IP income account yet (OQ-22).

**E3. Zero-rated, exempt or out-of-scope lines**: same as E1/E2 with no 2200 line. A document with all lines at zero VAT produces a 2-line entry.

**E4. Sales credit note, final** (mirror of E1/E2, on the original invoice's accounts)
| Account | Dr | Cr | Counterparty |
|---|---|---|---|
| 4100 / 4200 / 4300 / 4110 / 4210 | 10,000.00 | | |
| 2200 VAT output | 1,500.00 | | |
| 1200 (or 1210) | | 11,500.00 | (SUB if intercompany) |

**E5. Customer receipt** (with optional bank charge 25.00)
| Account | Dr | Cr | Counterparty |
|---|---|---|---|
| 1100 Cash and bank | 11,475.00 | | |
| 5700 Bank charges | 25.00 | | |
| 1200 (or 1210) | | 11,500.00 | (SUB) |
VAT on bank charges: OQ-23.

**E6. Customer refund**: Dr 1200 (or 1210 + counterparty) / Cr 1100.

**E7. Vendor bill, resident, recoverable VAT**
| Account | Dr | Cr | Counterparty |
|---|---|---|---|
| 5xxx expense or 15xx/1320 asset (per line) | 10,000.00 | | |
| 1310 VAT input | 1,500.00 | | |
| 2100 Trade payables (or 2110) | | 11,500.00 | (SUB) |

**E8. Vendor bill, non-recoverable VAT** (default, OQ-9): Dr expense 11,500.00 / Cr 2100 11,500.00.

**E9. Vendor bill, non-resident, reverse charge**
| Account | Dr | Cr |
|---|---|---|
| 5xxx expense (per line) | 10,000.00 | |
| 1310 VAT input | 1,500.00 | |
| 2100 Trade payables | | 10,000.00 |
| 2200 VAT output | | 1,500.00 |
Withholding tax is not booked: there is no WHT payable account (OQ-8).

**E10. Vendor credit note**: Dr 2100 (or 2110 + counterparty) / Cr expense or asset / Cr 1310 (mirror of E7).

**E11. Vendor payment**: Dr 2100 (or 2110 + counterparty) / Cr 1100. **Vendor refund received**: Dr 1100 / Cr 2100.

**E12. Reversal of a bill, receipt or payment**: `reverse_entry` swaps debit and credit on every line and keeps the counterparty. It is posted with its own JE number.

**E13. Allocation**: no journal entry.

**Accounts missing from the template** (not invented here, see OQ-22): retention receivable and payable, withholding tax payable, customer advances and deferred income, vendor advances, FX gain/loss, rounding difference, ECL allowance and bad-debt expense, separate reverse-charge VAT accounts, intercompany IP income. The template also has no "control account" flag that would stop manual journals hitting 1200, 2100, 1210 and 2110.

## 6. Integrations

| System | Direction | What flows | Status |
|---|---|---|---|
| ZATCA (via `zatca` module only) | Out | Issued sales documents (all fields in 3.3) for clearance or reporting | Hand-off R40 to R46. XML, hash, QR and CSID belong to zatca.md |
| ZATCA (via `zatca`) | In | Status, ICV, PIH, hash, QR, cleared XML, warnings and errors | Written to the `zatca_*` fields |
| Banks | Out | Vendor payment file (format such as SARIE/WPS undecided) | Not in scope until OQ-21 |
| Banks | In | Statement lines matched to receipts and payments | README step 4 (bank module), not built here |
| GOSI / Mudad | none | Payroll is out of scope | n/a |
| SAP B1 / Daftra | In (one-off) | Open AR/AP items at cutover as `source=import` documents. No ZATCA resubmission. They must tie to the opening balances in 1200/2100 | migration-agent. Method in OQ-24 |
| Agent gateway | In | Draft bills from the "invoice reader" agent (`source=agent:invoice-reader`) | Drafts only (R22) |

## 7. Permissions

No entity-scoped role model exists in `core` yet (OQ-25). The proposed roles below are defaults, granted **per entity**. A user who is AP Clerk in ALM and Viewer in SUB has exactly those rights in each.

| Action | AR Clerk | AP Clerk | Accountant | Approver | Viewer / Auditor | Agent service user |
|---|---|---|---|---|---|---|
| Create/edit customer or vendor | C (customer) | C (vendor) | Y | | | |
| Create/edit draft sales document | Y | | Y | | | Draft only, if granted |
| Issue sales invoice or debit note | | | Y | Y | | No |
| Issue credit note | | | Y (approval OQ-7) | Y | | No |
| Create/submit draft bill | | Y | Y | | | Draft only |
| Approve bill | | | | Y (not own) | | No |
| Post bill / reverse bill | | | Y | | | No |
| Post receipt or payment | Y (receipt) | | Y | Y (payment) | | No |
| Allocate / un-allocate | Y | Y | Y | | | No |
| View documents and reports | Y | Y | Y | Y | Y | Scoped |

Segregation of duties (creator ≠ approver) is on by default and configurable per entity (OQ-5).

## 8. Acceptance tests

Fixtures: entities ALM (Sharia enforced, VAT number set) and SUB (child of ALM, Sharia enforced), each with the template chart and 2027 periods. Customers and vendors are anonymised. All amounts are Decimal.

| # | Given | When | Then |
|---|---|---|---|
| AT-1 | ALM draft invoice, 2 lines: 3 × 1,000.00 at S 15%, and 1 × 333.33 at S 15% | Issue, then ZATCA stub returns `cleared` | Line VAT 450.00 and 50.00 (49.9995 rounds half-up). Totals net 3,333.33, VAT 500.00, gross 3,833.33. JE posted: Dr 1200 3,833.33 / Cr 4100 3,333.33 / Cr 2200 500.00. Reference = invoice number. Status `final`. |
| AT-2 | Final invoice | Try to set status to `draft` or `issued` | Error. Status unchanged. |
| AT-3 | ALM invoice line uses SUB's account 4100 | Save line / issue | Rejected with "belongs to another entity". No number consumed. |
| AT-4 | User with rights on ALM only | List invoices, or create an invoice for a SUB customer | Sees only ALM documents. Create rejected. |
| AT-5 | Issued invoice | Edit a line's quantity, or delete the document | Rejected. |
| AT-6 | Two ALM invoices issued concurrently in 2027 | Both issue | Consecutive numbers, no gap or duplicate. A third issue that fails validation consumes no number. |
| AT-7 | Final invoice; the customer's address is then changed | Read the invoice | The invoice still shows the old address snapshot. |
| AT-8 | Standard draft invoice, customer with no Arabic name / no address / no VAT number and no other ID | Issue | Rejected, listing each missing field. |
| AT-9 | Final invoice | Call cancel or delete; call `reverse_entry` through invoicing services | No cancel path exists. The invoicing service refuses. The only correction is a credit note. |
| AT-10 | Final invoice gross 11,500.00, open 11,500.00 | Credit note 4,600.00 (incl. VAT) cleared, then a second credit note of 7,000.00 | First: JE as E4 for 4,000/600, auto-allocated, invoice open 6,900.00, `partial`. Second rejected (exceeds 6,900.00 creditable). |
| AT-11 | Credit note without reason, or referencing another customer's invoice | Issue | Rejected. |
| AT-12 | ALM (Sharia enforced) bill line on 5610 conventional interest | Save line / post | Rejected "Sharia-prohibited". Same bill in an entity with `sharia_enforced=False`: posts. |
| AT-13 | Invoice line on 4900 non-compliant income | Final | Posts. Document `requires_purification=True`. Appears on the purification report. |
| AT-14 | Line `unit_price` given as float `0.1`, or currency `USD` on an ALM (SAR) document | Save | Rejected. |
| AT-15 | VatRate S 15% valid from 2020-07-01; line category E with no exemption reason | Save | Rejected. With a reason: VAT 0.00 and no 2200 line in the JE. |
| AT-16 | Entity with blank `vat_number`, invoice with an S line | Issue | Rejected. |
| AT-17 | Invoice dated in a closed period (2027-01 closed) | Issue | Rejected "Period … is closed". Document stays draft, no number used, no JE. |
| AT-18 | Issued standard invoice, ZATCA stub `pending` | Try to allocate a receipt, or post the JE | Allocation rejected. JE is still draft. After the stub returns `cleared_with_warnings`: JE posted, status `final`. |
| AT-19 | Issued invoice, ZATCA stub `rejected` | Callback | Status `rejected`. Draft JE deleted. Number not reused. Trial balance unchanged. |
| AT-20 | ALM customer with `group_entity`=SUB | Invoice final | JE has 1210 with counterparty SUB. `intercompany_balances` shows (ALM, SUB). A customer with `group_entity`=ALM in ALM: rejected. |
| AT-21 | Bill submitted by user U | U approves | Rejected (segregation). Approver V approves, accountant posts: JE as E7. |
| AT-22 | Bill created with `source=agent:invoice-reader` | Agent calls submit or post | Rejected. Stays draft. |
| AT-23 | Posted bill, vendor X, vendor number "A-17" | Second bill with the same vendor and number | Rejected as duplicate. Allowed after the first is reversed. |
| AT-24 | Posted bill in 2027-02 (now closed), no allocations | Reverse | `reverse_entry` called with a date in the open 2027-03. Bill `reversed`. Balances net to zero. A bill with an allocation: reversal rejected. |
| AT-25 | Bill from a resident vendor with no VAT number, S line 1,000 + 150 | Post | E8: Dr expense 1,150.00 / Cr 2100 1,150.00. No 1310 line. |
| AT-26 | Bill from a non-resident vendor, services 10,000 S | Post | JE as E9. AP open 10,000.00. |
| AT-27 | Non-resident vendor bill posted | Post payment | Rejected pending the WHT decision (OQ-8). |
| AT-28 | Posted receipt with an allocation | Reverse | Rejected. After un-allocation: reversal posts, status `reversed`. |
| AT-29 | Receipt 5,000 for customer A; invoice for customer B; invoice of SUB | Allocate | Both rejected. Allocation above the open amount rejected. |
| AT-30 | Allocation row | Edit or delete | Rejected. Un-allocate creates a negative row. Open amounts are restored. |
| AT-31 | Fixture set of invoices, credit notes, receipts and bills in ALM and SUB | `reconcile_subledger(entity, as_of)` | The sub-ledger equals the TB for 1200, 2100, 1210 and 2110 per counterparty, for each entity separately. |
| AT-32 | Issued invoice in 2027-03 awaiting ZATCA | `close_period(2027-03)` | Rejected "Draft entries exist". |
| AT-33 | Any document, customer, vendor or VAT category | Render the label in the `ar` locale | Arabic text shown. Missing Arabic on a required Bi field is rejected at save. |
| AT-34 | New entity NEW added as data (chart + periods) | Run AT-1 for NEW | Passes with no code change. Numbers use the `NEW` prefix. |

## 9. Open questions

**BLOCKING** means the builder cannot build that part without an answer. **Default** means a safe default exists that the builder may use (shown in the rules) and that is easy to change later.

| # | Question | Owner | Status |
|---|---|---|---|
| OQ-1 | Where do seller addresses (street, building no., additional no., district, city, postal code) live: new fields on `core.Entity` or a zatca config? `Entity` has none today. | Dev + zatca.md | **BLOCKING** for issuing standard invoices. Drafts can be built. |
| OQ-2 | Exact zatca service names, the callback contract, and which model owns ICV/PIH/hash/QR (invoice fields vs `zatca.Submission`). | zatca.md author | **BLOCKING** for R40 to R45. Build against a stub interface. |
| OQ-3 | Payment terms: standard terms per customer/vendor, due-date rules, early-payment discounts. | Accountant | Default: due date entered manually, prefilled = document date. |
| OQ-4 | Document number format and prefixes (INV, CN, DN, BILL, VCN, RCPT, PAY), and whether CN/DN share the invoice sequence. ZATCA requires uniqueness. | Accountant | Default: `{entity.code}-{TYPE}-{year}-{000001}`, separate sequence per type, gap-free. |
| OQ-5 | Approval thresholds for bills, payments and invoices (amounts, who approves, the Managing Partner's limit). Is segregation of duties mandatory for small entities? | Managing Partner | Default: one approval on every bill and payment, creator ≠ approver, configurable per entity. |
| OQ-6 | Multi-currency: foreign-currency invoices and bills, FX rate source, realised/unrealised FX. `JournalLine` has no currency field and the template has no FX account. | Accountant + Dev | **BLOCKING** for non-SAR documents. Default: base currency only, others rejected. |
| OQ-7 | Do credit notes (and debit notes) need approval, and above what amount? | Managing Partner | Default: accountant or approver role only, no threshold. |
| OQ-8 | Withholding tax on payments to non-resident vendors (rates by service type, WHT payable account, monthly return). | Accountant | **BLOCKING** for non-resident payments (R31). Bills can be posted. |
| OQ-9 | Input VAT recovery: is any entity partly exempt (e.g. residential rentals exempt, dividends out of scope), and is apportionment needed? How is non-recoverable VAT booked? | Accountant / tax adviser | Default: recoverable if vendor VAT number present, else capitalised to the line account (E8). |
| OQ-10 | Duplicate-bill rule and whether a vendor credit note must reference an original bill. | Accountant | Default: unique vendor + number. Reference required. |
| OQ-11 | Rounding policy (per line vs per document, half-up vs half-even) and how to absorb a 0.01 difference against vendor totals. There is no rounding account. | Accountant | Default: per line, ROUND_HALF_UP. Bill totals must match the vendor document exactly. |
| OQ-12 | Unit price precision beyond 2 dp (e.g. hourly rates). | Accountant | Default: 2 dp. |
| OQ-13 | Are any group entities in a KSA VAT group (no VAT on intra-group supplies)? | Accountant / tax adviser | Default: no VAT group. VAT is charged on intercompany invoices. |
| OQ-14 | Shared party master across entities (one customer for ALM and SUB) or per entity. | Managing Partner | Default: per entity. |
| OQ-15 | Late payment: are any late charges contracted? If so, are they donated (Cr 2600 Charity payable), and what is their VAT treatment? Sharia allows no penalty income. | Managing Partner + Sharia adviser | Default: no late-charge feature. |
| OQ-16 | Customer advances and deposits (e.g. rent in advance, security deposits): VAT on advance receipt, prepayment invoices, deferred income account. | Accountant | **BLOCKING** for advance invoices and deposits. Default: unallocated receipts allowed and reported (R38), no VAT on them. compliance-checker to confirm. |
| OQ-17 | Can a non-VAT-registered group entity issue invoices, and in what form? | Accountant | Default: S lines blocked (R11). Z/E/O allowed. |
| OQ-18 | Must every sales line description be in Arabic (ZATCA/VAT regulations), or is English-only acceptable on B2B? | compliance-checker | Default: Arabic required on sales documents, English optional. |
| OQ-19 | Which entities issue simplified (B2C) invoices, e.g. rentals to individuals? | Managing Partner | Default: standard unless customer_kind = individual. |
| OQ-20 | Bank accounts: one sub-account per bank or IBAN under 1100? | Accountant | Default: 1100, selectable asset account. |
| OQ-21 | Bank payment file format and bank (SARIE, bulk upload, H2H). | Accountant | Not in scope for this module. |
| OQ-22 | Missing accounts: retention receivable/payable, WHT payable, customer advances/deferred income, vendor advances, FX gain/loss, rounding, ECL allowance/bad debt, reverse-charge VAT split, intercompany IP income. Also add an `is_control` flag on 1200/2100/1210/2110 to block manual journals. | Accountant | **BLOCKING** for the related features (retention, write-off, FX, advances). Core AR/AP is not blocked. |
| OQ-23 | VAT on bank charges deducted from receipts. | Accountant | Default: booked gross to 5700, no VAT split. |
| OQ-24 | Migration: how open Daftra/SAP B1 AR/AP items are loaded (per-document vs per-party balance), and whether their ZATCA data (hash chain) carries over. | Accountant + migration-agent | Default: per-document `source=import`, status `final`, no ZATCA call, tie-out to opening TB. |
| OQ-25 | Entity-scoped role/permission model (user × entity × role) is missing from `core`. Role names and who holds them. | Managing Partner + Dev | **BLOCKING** for views/APIs. Services can be built and tested now. |
| OQ-26 | Retention on contracts (percentage held, release terms). | Managing Partner | Default: not built. |
| OQ-27 | Bad-debt write-off and ECL policy. | Accountant | Default: not built. Use credit notes only. |
| OQ-28 | Ledger protection: should `ledger.services.reverse_entry` refuse entries owned by a sub-ledger (needs a `source_module` field on `JournalEntry`), so a final invoice can't be reversed outside invoicing? | Dev + code-reviewer | Default: invoicing services refuse it. The code-reviewer checks there is no direct call. |
