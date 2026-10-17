# Invoicing (Track A): accounts receivable and accounts payable, with VAT

**Compliance status: Technical rules checked 2026-10-16 against official ZATCA documents (see docs/compliance/zatca-verification-2026-10-16.md) and, for the e-invoicing hand-off, offline against ZATCA SDK 2.0.3 (see docs/compliance/zatca-sdk-findings-2026-10-16.md; affects R9, R65 and OQ-11 here); SDK-confirm and expert items remain.** (Earlier record: `docs/compliance/track-a-2026-10-16.md`.)

Django app: `invoicing`. Status: **draft spec.** Written 2026-10-16; revised 2026-10-16 (compliance review applied); revised 2026-10-16 (project-owner decisions D10 to D19 recorded); revised 2026-10-16 (ZATCA verification report I1–I20 applied); revised 2026-10-16 (project-owner decisions D20 to D23); revised 2026-10-16 (ZATCA SDK 2.0.3 findings: R9, R65, OQ-11 notes); amended 2026-10-16 (ledger build on branch `claude/funny-davinci-7yd88s`, commits aad0126..b690c12: R6, R24, R32, R33, R54 notes, §7 note, AT-69 to AT-71, OQ-49; see access.md R34 to R37, R42); amended 2026-10-17 (invoicing slice 1 as built, commits e8ec8d0..73c1220, and the slice-1 compliance report `docs/compliance/invoicing-slice1-compliance-2026-10-17.md`: §2 ledger API table, §3 notes, R9, R16, R27, R33, R34, R41 to R43, R48, R50, R56, R61 notes, R75 to R85, §5 note, §7 note, AT-18/19/32 notes, AT-72 to AT-85, OQ-50 to OQ-57); amended 2026-10-17 (project-owner decisions D24 to D30: vendor kind and VAT-free bills, issued documents never back to draft, receipts posted by their recorder, 3-decimal currencies, `zatca_admin` resubmit, proposed accounts, note tax date, entities without a VAT number; R91 to R96, AT-93 to AT-102, OQ-58 to OQ-62).
Depends on (written in parallel): `docs/specs/ledger-fx.md` (document currency, rates, FX differences; D13), `docs/specs/billing.md` (contracts and billing schedules; D15, D17).
**Tags.** A citation in parentheses, e.g. "(XML v1.2 BR-KSA-44, p.56)", points to an official ZATCA document checked on 2026-10-16 (abbreviations as in the Sources table of `docs/compliance/zatca-verification-2026-10-16.md`). **[SDK-confirm]** = technical point to settle against the ZATCA SDK/sandbox. **[Needs expert sign-off: tax|legal|security|Sharia]** = adviser decision needed; the default stands until then. Items whose sources were not downloaded (Input Tax Deduction guideline, RCM circular, PDPL) or not covered by the verification carry an expert tag. No [CC-verify] tags remain.
Companion spec: `docs/specs/zatca.md` (ZATCA Phase 2 e-invoicing), written in parallel. Hand-off points are in sections 4 (rules R40 to R46) and 6.

## 1. Purpose

The invoicing module keeps the customer and vendor sub-ledgers for **every entity** in the group: Almiras, subsidiaries, and any entity added later as an `Entity` row. On the AR side it covers customers, sales invoices, sales credit notes and debit notes, and customer receipts and refunds with allocation to invoices. On the AP side it covers vendors, vendor bills, vendor credit notes, and vendor payments with allocation. It calculates KSA VAT on every document. It creates and posts the matching journal entries **only** through `ledger.services` (`create_entry`, `post_entry`, `reverse_entry`). It passes sales documents to the `zatca` module for clearance or reporting. Each entity's Sharia setting is applied to every document line, and every user-facing text is held in English and Arabic.

## 2. Decisions

D1 to D9 are fixed by CLAUDE.md, the README and existing code. D10 to D19 were given by the project owner in the build session on 2026-10-16. Every other business choice is in section 9.

| # | Decision | Source | Date |
|---|---|---|---|
| D1 | Every business record has an `entity` FK. Views and APIs are always filtered by entity and by user permission. New subsidiaries are data, not new code paths. | CLAUDE.md, "Multi-entity rules" | in repo as of 2026-10-16 (no meeting date recorded) |
| D2 | Sharia is enforced per entity (`Entity.sharia_enforced`). Sharia-prohibited accounts are blocked. Non-compliant income is flagged for purification. | CLAUDE.md rule 5; `ledger.services.validate_entry` | same |
| D3 | Every user-facing text has English and Arabic (`name_en` / `name_ar`, or Django translations). | CLAUDE.md | same |
| D4 | Money is `Decimal`, never float. Amounts are stored with 2 decimals, and the currency is explicit. *(Rule 7 amended 2026-10-16 by the project owner: functional SAR 2 dp; foreign transaction-currency amounts up to 3 dp, checked against the currency's own decimals (ledger-fx D5). Effect on document amounts: OQ-48.)* *(2026-10-17: document amounts in 3-decimal currencies decided by D27.)* | CLAUDE.md rule 7 | same |
| D5 | Posted journal entries are never edited or deleted. Corrections are reversals. Posting goes only through `post_entry`. Closed periods reject postings. An entity never posts to another entity's accounts. | CLAUDE.md rules 1 to 4; `JournalEntry.save/delete`; `validate_entry` | same |
| D6 | Intercompany lines always carry a counterparty entity. | CLAUDE.md rule 6; `validate_entry` | same |
| D7 | Numbering is gap-free per entity and year (journal entries: `{entity.code}-JE-{year}-{000001}`, via `EntrySequence` with `select_for_update`). | `ledger.services._next_number`; README | same |
| D8 | Agents create drafts only, act as named service users, and every action is logged. Business records carry a `source` field (human / import / agent). | `docs/ai-agent-gateway.md` | same |
| D9 | Business logic lives in `services.py`. Every rule gets a test. | CLAUDE.md | same |
| D10 | Almiras identity: EN "Almiras Holding Company", AR "شركة المراس القابضة"; LLC, holding; unified national number 7023761708; Jeddah branch CR 4030419505; VAT 310960118600003, effective 2024-04-01, quarterly VAT returns; one branch (Jeddah), one EGS unit. National address in §3.0. These real identifiers may be stored in the repo as Almiras **setup data only**, never in test fixtures (tests stay anonymised). `Entity` gets bilingual national-address fields plus CR and VAT numbers (§3.0). | Project owner | 2026-10-16 |
| D11 | All group entities issue invoices and invoice each other often. No entity is in a VAT group: each entity is a separate VAT taxpayer. **Confirmed:** each subsidiary is its own company with its own VAT number and its own EGS unit(s); the holding owns it fully or holds a share. Partly-owned and minority-stake entities are also separate taxpayers issuing their own invoices. Intercompany invoices (with any of them) are normal VAT invoices carrying the counterparty entity (CLAUDE.md rule 6). | Project owner | 2026-10-16 |
| D12 | ZATCA is built in-house with open-source libraries (zatca.md). A rejected invoice is corrected and re-issued under a new number; the rejected number stays used. All hosting (DB, backups, logs, secret store) is in KSA. | Project owner | 2026-10-16 |
| D13 | Multi-currency is needed now (CNY, EGP, GBP, EUR, USD, AED, etc.). Functional/reporting currency per entity is SAR unless set otherwise (`Entity.base_currency`). The ledger is extended by `docs/specs/ledger-fx.md`: documents carry currency and rate; VAT for ZATCA is also expressed in SAR. | Project owner | 2026-10-16 |
| D14 | Withholding tax is in scope: Almiras pays non-resident suppliers and service providers. WHT per vendor/line category, WHT payable account, treaty certificates, gross-up option, monthly WHT return data. Rates and categories: reference list in OQ-8 (WHT §3 p.28) + tax adviser sign-off. *(Tag updated 2026-10-16 only; decision unchanged.)* | Project owner | 2026-10-16 |
| D15 | Advances and security deposits both occur, varying by arrangement; some contracts have neither. The contract states which applies. Advance receipt → prepayment invoice (386) and deferred income. Security deposit → liability; VAT treatment **needs tax adviser sign-off**. | Project owner | 2026-10-16 |
| D16 | Income types: commercial rent, residential rent (VAT-exempt), dividends (out of scope for invoicing), management fees, service charges, service fees, and others. They feed Art. 51 apportionment (OQ-9); the ratio method keeps tax-adviser sign-off. | Project owner | 2026-10-16 |
| D17 | Accounting staff create documents; the CFO approves; posting is automatic on approval (for sales, after ZATCA clearance/reporting). Roles are assigned per entity. Recurring invoicing (annual car-rental contracts, rent, seconded staff billed until HR ends the secondment) is specified in `docs/specs/billing.md`; invoicing exposes a service to create a document from a billing schedule (`source="system"`) under the same approval/posting rules (approval of auto-generated invoices: OQ-36). | Project owner | 2026-10-16 |
| D18 | Missing accounts may be added to `ledger/coa_template.py` by the builder. Proposed codes in §5 ("proposed, accountant to review"). | Project owner | 2026-10-16 |
| D19 | The safe defaults below are approved as decisions (not those superseded by D10 to D18). | Project owner | 2026-10-16 |
| D20 | Invoices auto-generated from a CFO-approved contract need no per-invoice approval (resolves OQ-36; billing D8). | Project owner | 2026-10-16 |
| D21 | Contracts stay modifiable and cancellable after approval, also retroactively (staff may forget to report a cancelled, shortened or modified contract or service). Issued invoices are never edited or deleted: billing generates **draft** credit notes (debit notes for increases) for the over/under-billed amounts; a person reviews them and the CFO approves them; corrections are dated in an open period (billing D9; R73). | Project owner | 2026-10-16 |
| D22 | Seller identification: use the CR number (scheme `CRN`; for Almiras the Jeddah branch CR, §3.0) **and** also show the unified national number (starts with 7). ZATCA XML allows only one seller ID (BR-KSA-08), so the XML carries `CRN` and the human-readable invoice shows both, labelled en/ar (resolves OQ-33; zatca D-11, R-48; R72). | Project owner | 2026-10-16 |
| D23 | Intercompany: by default no automatic (mirror) bill is created in the buyer entity; the buyer's staff enter it (resolves OQ-37; billing D10). | Project owner | 2026-10-16 |
| D24 | Vendors get an individual/company kind like customers; R86 masking applies to individual vendors. Very small individuals or vendors not registered for VAT exist: a bill from a vendor without a VAT number has **0 VAT** (no input VAT, nothing to 1310). The system must allow and expect VAT-free bills from unregistered vendors (R91, R92; replaces E8 for that case). | Project owner | 2026-10-17 |
| D25 | Agreed with ZATCA: an issued invoice never returns to draft; corrections only by credit or debit notes (R96; zatca D-13). | Project owner | 2026-10-17 |
| D26 | Receipts: a second person is **not** required; the person who records a receipt may post it. Exception to creator ≠ poster for receipts only; segregation stays for all other documents (resolves OQ-49; access A16, R46). | Project owner | 2026-10-17 |
| D27 | 3-decimal currencies (KWD, BHD, OMR, JOD): Almiras invoices in them; documents must support 3-dp transaction amounts. SAR VAT totals stay 2 dp (resolves OQ-48; supersedes the R74 default; R93). Whether ZATCA XML accepts 3-dp amounts: **[SDK-confirm]** (OQ-62). | Project owner | 2026-10-17 |
| D28 | `zatca_admin` is an add-on role that can be given to any basic user (e.g. an accountant or the CFO) as a second membership; resubmission to ZATCA (R79) belongs to `zatca_admin` holders and the ZATCA queue system user (resolves OQ-57; access A19, R50). | Project owner | 2026-10-17 |
| D29 | Proposed account codes (§5) stay **proposed**; the accountant/CFO reviews them later in a Word document being prepared. | Project owner | 2026-10-17 |
| D30 | Tax: (a) the date that counts for a credit or debit note is the note's **own date**; backdated contract corrections are dated on the note date, and the 15-day note warning is not based on a backdated event date (resolves OQ-47; billing OQ-25; R95). (b) Entities without a VAT number do not charge VAT: their invoices carry 0% / no VAT under Saudi law (resolves OQ-17 and OQ-50 / compliance row 6a; R94). They must not issue a document titled "Tax Invoice"; the title/type to use: **[Needs expert sign-off: tax]** (OQ-59). (c) Remaining tax questions (OQ-44, OQ-51, OQ-52, OQ-53, OQ-54, R30 reverse charge, OQ-9 Art. 51, OQ-8 WHT treaty, OQ-16 deposits) stay open and go to the CFO in the Word document. | Project owner | 2026-10-17 |
| D31 | (a) Withholding tax applies to payments by a Saudi entity to vendors abroad (companies or individuals) for services delivered from abroad, invoiced in **any currency**: the entity withholds the tax, pays the vendor the net, and pays the tax to ZATCA through its WHT return; the vendor never receives it. Foreign-currency bills and payments are supported (WHT converted to SAR at the payment rate, posted to 2220, filed from `wht_return_data`). The `wht_requires_sar_functional` refusal (R64 amendment) concerns only an entity whose **own books** are kept in another currency (e.g. a subsidiary registered abroad), not the currency of the invoice. (b) Purification: each entity holds a designated **purification bank account with a Saudi IBAN (SA…)**; purified amounts are moved into it and paid out from it **only to approved charity recipients** (e.g. the Ihsan national platform), each with its own Saudi IBAN (R54 amendment D31). (c) OQ-63: the signed/cleared ZATCA XML is kept unchanged with the buyer data for the ZATCA retention period (resolves OQ-63). | Project owner | 2026-10-17 |

**D19, approved defaults** (each OQ is marked "Resolved by D19"; expert sign-offs listed stay open)
| From | Approved default |
|---|---|
| OQ-3 | Due date entered manually, prefilled = document date. |
| OQ-4 | Number `{entity.code}-{TYPE}-{year}-{000001}`, separate gap-free sequence per type. |
| OQ-5 | One approval on every bill and vendor payment, creator ≠ approver, configurable per entity; the approver is the CFO (D17). |
| OQ-9 | R29: `taxable` + vendor VAT number → 1310; `exempt` → cost; `mixed` → fully to cost. Tax adviser sign-off on Art. 51 still open. |
| OQ-10 | Unique vendor + vendor document number; vendor credit note must reference a bill. |
| OQ-11 | ROUND_HALF_UP; sales VAT per category at document level (R9); bill totals must match the vendor document exactly. |
| OQ-12 | Unit price 2 dp. |
| OQ-14 | Party master per entity. |
| OQ-15 | No late-charge feature; if ever built, Dr 1200 / Cr 2600, never income. Sharia board sign-off still open. |
| OQ-17 | Entity without VAT number: S lines blocked (R11); Z/E/O allowed. *Superseded 2026-10-17 by D30(b): no VAT at all, not titled "Tax Invoice" (R94).* |
| OQ-18 | Arabic required on sales line descriptions; English optional. |
| OQ-19 | Standard unless `customer_kind = individual`; R48 guard applies. |
| OQ-20 | Bank: 1100, selectable asset account. |
| OQ-21 | Bank payment files not in scope for this module. |
| OQ-23 | Bank charges booked gross to 5700, no VAT split. |
| OQ-24 | Migration per document, `source=import`, status `final`, no ZATCA call, tie-out to opening TB. |
| OQ-26 | Contract retention not built. |
| OQ-27 | No bad-debt/ECL feature; credit notes only. |
| OQ-28 | Invoicing services refuse direct reversal of sub-ledger JEs; code-reviewer checks no direct call. |
| OQ-29 | No VAT-return report; customs import VAT as a manual JE outside this module. |
| OQ-30 | Purification E14/E15 by accountant only, human user, no automation. Sharia board sign-off still open. |
| OQ-7 | Superseded by D17: credit and debit notes are approved by the CFO like invoices; no threshold. |

The 2026-10-16 compliance review and the 2026-10-16 verification against official ZATCA documents are technical input, not business decisions; no D-n was changed. Rules they changed are tagged in section 4; choices they raised are in section 9. Where the verification affects the scope of a rule built on a decision (R48 under D19/OQ-19; OQ-18 under D19), the rule/OQ says so.

### Ledger API this module must use (actual signatures)

*Amended 2026-10-17: table replaced with the API as built (`ledger/services.py`, `ledger/documents.py`).*

| Function | Signature | Notes for invoicing |
|---|---|---|
| `create_entry` | `create_entry(entity, date, description, lines, user=None, reference="", post=False, grant=None, posted_by=None, document=None, source=None)` | Every invoicing posting uses `post=True` with a sub-ledger grant issued for `document` (a stored, registered invoicing document of `entity`) and `lines=None`: the registered `build_lines` hook builds the lines (R75). The document books once; its creator, submitter and last modifier cannot post it. Only the `invoicing.post_on_clearance` grant may record a different `posted_by` (the recorded approver, R41). Account codes are looked up **within `entity`**. `reference` = document number (max 100 chars). Invoicing creates no drafts (`post=False` is not used). |
| `post_entry` | `post_entry(entry, grant=None, *, posted_by=None)` | Manual journals only (`ledger.post_entry` grant). It refuses entries linked to a document (`draft_has_links`), so invoicing never calls it. |
| `reverse_entry` | `reverse_entry(entry, grant=None, date=None, reason="")` | Posted only. Grant `invoicing.reverse_document` issued for the document (bills, receipts, payments; never sales documents, R17). `date` defaults to the **original** date; invoicing passes a date in an open period (R32). |
| `close_period` | `close_period(period, *, user)` | Needs `ledger.close_period`. Refuses the close while draft JEs exist in the period **or** while any registered `pending_documents` hook reports documents of that entity dated in the period (`documents_pending`). Invoicing registers that hook for sales documents awaiting ZATCA (R43). |
| `documents.register` | `register(model, *, post_permissions, reverse_permissions=(), entity_field="entity", changed_at_field="modified_at", creator_field="created_by", build_lines=None, validate_lines=None, pending_documents=None)` | Called from `InvoicingConfig.ready()` for each invoicing document type (R75). |

## 3. Data model

Conventions: every model has `entity` FK → `core.Entity` (`on_delete=PROTECT`, required). Money is `DecimalField(max_digits=18, decimal_places=2)`. *Amended 2026-10-17 (D27, R93):* amounts in the document's transaction currency (unit price, line net, discounts, VAT in document currency, totals, receipt/payment amounts, allocations) are `DecimalField(max_digits=21, decimal_places=3)`, with the decimals used limited to the currency's `minor_units` (ledger-fx R8); SAR amounts (`total_vat_sar`, `vat_breakdown.vat_sar`, `*_sar` WHT fields) stay 2 dp. Every model also has `created_by`, `created_at`, `updated_at` and `source` (choices `human` / `import` / `agent:<name>` / `system`, required, default `human`; `system` = billing schedule, D17). "Bi" means the field exists as both `_en` and `_ar`.

### 3.0 Entity fields needed (added to `core.Entity`; D10, resolves OQ-1)

| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| legal_name_en / legal_name_ar | Char(200) | Y to issue | Bi | Seller name on documents and ZATCA XML |
| legal_form | Choice: `llc`, `jsc`, `sole`, `branch_foreign`, `other` | Y | | Display text en/ar via translations |
| unified_number | Char(10) | N | | Unified national number ("700" number). Digits only, first digit `7` (D22). Shown on the human-readable invoice (R72); in ZATCA XML only when no CR exists (zatca.md R-48) |
| cr_number | Char(20) (exists) | Y to issue | | CR of the issuing branch (one branch per entity for now, OQ-35) |
| vat_number | Char(15) (exists) | Y for S lines (R11) | | 15 digits, first and last digit `3` (XML v1.2 BR-KSA-40, p.56) |
| vat_effective_date | Date, null | Y if `vat_number` | | OQ-32 |
| vat_return_period | Choice: `monthly`, `quarterly` | Y if `vat_number` | | Also drives zatca.md R-38 retention dates |
| building_no | Char(4) | Y to issue | | 4 digits (seller: BR-KSA-37, p.56) |
| street_en / street_ar | Char(200) | Y to issue | Bi | Seller address set: BR-KSA-09 (p.49) |
| district_en / district_ar | Char(100) | Y to issue | Bi | |
| additional_no | Char(4) | N | | Secondary number (الرقم الإضافي). Optional in ZATCA XML (DD KSA-23 "O"; zatca.md §3.1); emitted when present. *Changed 2026-10-16 from "Y to issue" (verification Z3).* |
| postal_code | Char(5) | Y to issue | | 5 digits (BR-KSA-66, p.58) |
| city_en / city_ar | Char(100) | Y to issue | Bi | |
| country | Char(2) ISO | Y | | Default `SA` |
| short_address | Char(8) | N | | e.g. 4 letters + 4 digits. Default CSR location (zatca.md §3.2) |
| base_currency | Char(3) (exists) | Y | | Functional/reporting currency, default SAR (D13) |

**Almiras setup data (D10).** Loaded by a setup command from `core/setup_data/almiras.*`, never by tests or fixtures (AT-49).
| Field | Value |
|---|---|
| legal_name_en / _ar | Almiras Holding Company / شركة المراس القابضة |
| legal_form | `llc` (holding) |
| unified_number | 7023761708 |
| cr_number | 4030419505 (Jeddah branch) |
| vat_number / vat_effective_date / vat_return_period | 310960118600003 / 2024-04-01 / `quarterly` |
| building_no / additional_no / postal_code | 7547 / 2111 / 23437 |
| street_en / _ar | Prince Sultan St / شارع الامير سلطان |
| district_en / _ar | As Salamah District / حي السلامة |
| city_en / _ar, country | Jeddah / جدة, `SA` |
| short_address | JESA7547 |
| EGS units | 1 (Jeddah branch; zatca.md) |

**InvoicingSettings** (OneToOne Entity): `segregation_required` (Bool, default True, OQ-5), `auto_issue_from_approved_contract` (Bool, default True, OQ-36), `deposit_vat_treatment` (Choice, null until tax-adviser sign-off, R66).

### 3.1 Parties

**Customer** (unique `(entity, code)`)
| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| entity | FK Entity | Y | | Owning entity. Customers are not shared across entities (OQ-14). |
| code | Char(20) | Y | | |
| name_en / name_ar | Char(200) | Y / Y | Bi | Arabic name is required (ZATCA buyer name, D3) |
| customer_kind | Choice: `business`, `individual`, `government` | Y | | Drives the standard vs simplified default (OQ-19) |
| is_vat_registered | Bool | Y | | Default False. True → `vat_number` required (R16) |
| vat_number | Char(15) | Y if `is_vat_registered` | | 15 digits, first and last digit `3` (XML v1.2 BR-KSA-44, p.56). Not emitted on an export invoice: the buyer VAT number must not exist there (BR-KSA-46, p.57) |
| other_id_scheme | Choice, in priority order: `TIN`, `CRN`, `MOM`, `MLS`, `700`, `SAG`, `NAT`, `GCC`, `IQA`, `PAS`, `OTH` | N | | Buyer ID, required only if the buyer is not VAT-registered (XML v1.2 BR-KSA-14, p.50–51; DD BT-46; DTG p.79). If the buyer holds several IDs, the first in that order is used. `TIN` added 2026-10-16 (verification I2). Personal data for individuals (R49) |
| other_id | Char(30) | N | | Alphanumeric |
| *Amended 2026-10-17 (as built):* `other_id_scheme` / `other_id` are not fields on Customer. Buyer IDs are stored as **CustomerIdentifier** rows (`entity`, `customer`, `scheme` from the list above, `value`), any number per customer, added with `invoicing.services.add_customer_identifier(customer, scheme, value, user)` (`invoicing.manage_customer`). At issue the first ID in the priority order goes to the buyer snapshot (`buyer_other_id_scheme`, `buyer_other_id`). Intercompany customers use the group entity's CR (`CRN`) and unified number (`700`). | | | | |
| address: street, building_no, additional_no, district, city, postal_code, country (ISO-2, default `SA`) | Char | Y for standard invoices (see notes) | Bi for street, district, city | ZATCA buyer address (not required on simplified). All countries: street, city, country (BR-KSA-10, p.50). Country = SA: also building_no, postal_code and district (BR-KSA-63, p.57); postal_code 5 digits (BR-KSA-67, p.58). `building_no` 4 digits for SA buyers is a **house rule**, not ZATCA (BR-KSA-37 applies to the seller only; OQ-45). `additional_no` optional |
| group_entity | FK Entity, null | N | | Set when the customer is a group entity (subsidiary, associate or minority holding, D11). Makes its invoices intercompany. Must not equal `entity`. Buyer snapshot is taken from that entity's §3.0 fields. |
| payment_terms_days | PositiveInt, null | N | | D19 (OQ-3): none by default |
| currency | Char(3) | Y | | Default currency for new documents; default `entity.base_currency` (D13) |
| is_active | Bool | Y | | |

**Vendor** (unique `(entity, code)`). Same pattern as Customer, plus:
| Field | Type | Req | Notes |
|---|---|---|---|
| vendor_kind | Choice: `business`, `individual`, `government` (same as `customer_kind`) | Y | *Added 2026-10-17 (D24, R91).* `individual` → personal data (R49, R51) and masked labels (R86). Default `business` for existing rows (migration) |
| is_resident | Bool | Y | False → reverse-charge VAT (R30) and withholding tax (R61 to R64) |
| residence_country | Char(2) ISO | Y if not resident | Treaty lookup (R62) |
| wht_category | FK WhtCategory, null | Y if not resident | Default for bill lines (R61) |
| wht_gross_up | Bool | Y | Default False. True → the entity bears the WHT (R63; per-contract override OQ-8) |
| vat_number | Char(15) | N | Needed to recover input VAT on resident vendors' bills (R29). *Amended 2026-10-17 (D24):* a resident vendor without one is not registered for VAT; its bills carry 0 VAT (R92) |
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

**DocumentSequence** (unique `(entity, doc_type, year)`): `entity`, `doc_type` (`INV`, `CN`, `DN`, `PRE`, `BILL`, `VCN`, `RCPT`, `PAY`), `year`, `last_number`. Locked with `select_for_update`, the same pattern as `EntrySequence`. `PRE` = prepayment invoice 386 (R65); whether it shares the `INV` sequence: OQ-4 note **[Needs expert sign-off: tax]** (not covered by the 2026-10-16 verification).

**NoteReason** (global reference, added 2026-10-16, verification I5): `code`, `name_en`, `name_ar` (Bi). Loaded with the five statutory credit/debit note reasons (VAT Implementing Regulations Art. 40(1) / 54(3)) in English and Arabic exactly as listed in DD v1.2 KSA-10. Used as a pick list that prefills `reason_ar` / `reason_en` (R18, R19).

**IncomeType** (global reference, D16): `code` (`rent_commercial`, `rent_residential`, `management_fee`, `service_charge`, `service_fee`, `secondment`, `car_rental`, `ip_licence`, `other`), `name_en/_ar` (Bi), `default_vat_category` (FK VatRate category; residential rent `E`, others `S` **[Needs expert sign-off: tax]**, OQ-39), `default_exemption_reason` (null), `art51_class` (`taxable` / `exempt`), `default_account_code` (§5 proposals). Dividends are not an income type here (out of scope, D16). Sales lines carry `income_type` (required); it prefills account and VAT category and feeds the Art. 51 ratio (OQ-9).

**WhtCategory** (global statutory table, D14; reference values in OQ-8 from WHT §3 p.28 + **[Needs expert sign-off: tax]**): `code`, `name_en/_ar` (Bi), `rate` Decimal(5,2), `valid_from` / `valid_to`, `return_line_code`. Rates are data with effective dates, never constants.

**TreatyCertificate** (per entity and vendor): `entity`, `vendor` (same entity, non-resident), `country`, `treaty_rate` Decimal(5,2) (0.00 = exempt), `wht_category` (null = all), `valid_from` / `valid_to`, `document` (File), `received_on`, `verified_by` / `verified_at`. Never edited after verification; a new certificate replaces it.

### 3.3 Sales documents

**SalesDocument** (one model; `doc_type` = `invoice` / `credit_note` / `debit_note`)
| Field | Type | Req | Bi | Notes |
|---|---|---|---|---|
| entity | FK Entity | Y | | Seller |
| doc_type | Choice (+ `prepayment_invoice`, D15) | Y | | Maps to ZATCA type code 388 / 381 / 383 / 386 |
| zatca_profile | Choice: `standard` (B2B/B2G, clearance) or `simplified` (B2C, reporting) | Y | | |
| zatca_flags | JSON / bool set: third_party, nominal, export, summary, self_billed | N | | ZATCA invoice transaction code positions 3 to 7 (P N E S B) (XML v1.2 BR-KSA-06, p.48). No self_billed with export (BR-KSA-07); simplified allows only third_party, nominal, summary (BR-KSA-31, p.55). Checked at submit (svc) and again by zatca.md R-46 |
| number | Char(40), blank until issue | | | Gap-free; format undecided (OQ-4) |
| uuid | UUID | Y | | Generated at creation, never changed (ZATCA) |
| status | Choice: `draft`, `submitted`, `issued`, `final`, `rejected` | Y | | See R57 (`submitted` added by D17) |
| approved_by / approved_at | FK User / DateTime | Y at issue | | CFO approval (R57), or the approved contract for `source=system` (R60) |
| billing_item | FK billing schedule item (model per billing.md), null | Y if `source=system` | | R60 |
| contract | FK billing contract (per billing.md), null | Y for 386 | | R65 |
| customer | FK Customer (same entity) | Y | | |
| buyer snapshot: name_en, name_ar, vat_number, other_id_scheme, other_id, full address | Char | Y for standard | Bi | Copied from the Customer at issue and frozen after |
| seller snapshot: legal_name_en, legal_name_ar, vat_number, cr_number, unified_number, full national address (§3.0) | Char | Y | Bi | Copied from Entity at issue (D10; OQ-1 resolved). `unified_number` added 2026-10-16 (D22) |
| billing_adjustment | FK billing `BillingAdjustment` (billing.md 3.10), null | Y for CN/DN with `source=system` | | R73. Added 2026-10-16 (D21) |
| issue_date / issue_time | Date / Time | Y at issue | | ZATCA needs both. *Amended 2026-10-17 (pending fix, R78):* on a draft `issue_date` is only the requested date; approval sets it to today (Asia/Riyadh) |
| supply_date / supply_end_date | Date / Date null | Y for standard | | Actual supply date or period |
| due_date | Date | Y | | Default rule undecided (OQ-3) |
| currency | Char(3) ISO 4217 | Y | | Document currency, any active currency (D13, R56) |
| fx_rate / fx_rate_date / fx_rate_source | per ledger-fx.md | Y if currency ≠ base | | Rate to `entity.base_currency`, fixed at issue |
| total_vat_sar | Money (SAR) | computed | | VAT in SAR for ZATCA (R56): tax must be stated in SAR (TIR §4.2.2 p.17; XML v1.2 BR-KSA-68, BR-KSA-EN16931-02). For SAR documents = `total_vat` |
| prepayment_deductions | through `PrepaymentDeduction` (prepayment doc, net, vat) | N | | On a 388 settling 386 documents (R65) |
| payment_means_code | Char(3) (UNCL4461: 10, 30, 42, 48, 1) | Y for CN/DN; N otherwise | | BT-81. Required on CN/DN because the note reason KSA-10 sits in `cac:PaymentMeans` and BR-49 requires a code there (XML v1.2 p.38, 52). *Changed 2026-10-16 (verification I5).* |
| original_document | FK SalesDocument (invoice), null | Y for CN/DN | | ZATCA BillingReference BT-25, emitted as the original's `number` (IRN), not its UUID (DD BT-25). One original per note (default; multiple: OQ-43) |
| reason_code | FK NoteReason, null | N | | Pick list of the five statutory reasons; prefills `reason_ar` / `reason_en`, which stay editable |
| reason_ar / reason_en | Char(300) | `reason_ar` Y for CN/DN; `reason_en` N | Bi | ZATCA KSA-10 `cac:PaymentMeans/cbc:InstructionNote`, mandatory on 381/383 (BR-KSA-17, p.52), 1–1,000 chars, visible on output, so Arabic required (DTG p.79; DG p.59, 62) |
| adjustment_event_date | Date, null | N | | CN/DN only: date of the event that triggered the adjustment; drives the R71 deadline warning (OQ-47). *Amended 2026-10-17 (D30a, R95):* information only; the note's tax date is its `issue_date`, and no warning is based on this field |
| retention_class / retention_until | Choice (`standard`, `capital_asset_real_estate`, `movable_capital_asset`) / Date | Y at issue | | Same computation as zatca.md R-38; `movable_capital_asset` set only by explicit flag (OQ-46); drives PDPL anonymisation (R52) |
| document_discount | Money | N | | Document-level allowance. *Amended 2026-10-17:* a non-zero value is refused at issue (bilingual "discount the lines instead") until R9 says how it is split across categories (OQ-55) |
| total_net / total_vat / total_gross | Money | computed | | Stored at issue. `total_vat` = sum of category VAT (R9) |
| vat_breakdown | JSON: per (category, rate): taxable, vat, vat_sar | computed | | Document-level VAT per category (R9, R56) |
| amount_allocated | Money | computed | | Sum of allocations |
| payment_state | Choice: `unpaid`, `partial`, `paid`, `credited` | computed | | |
| requires_purification | Bool | computed | | True if any line hits an account with `requires_purification` |
| is_intercompany | Bool | computed | | `customer.group_entity` is set |
| journal_entry | FK ledger.JournalEntry, null, PROTECT | | | *Amended 2026-10-17:* empty until the document posts; set when it posts in one step on ZATCA clearance/reporting (R41). No draft JE exists at issue |
| notes_en / notes_ar | Text | N; `notes_ar` Y if `notes_en` is set | Bi | Notes are visible fields and must be in Arabic (DG p.62; OQ-18) |
| **ZATCA fields (written only by `zatca.services`, read-only here)** | | | | Ownership to be agreed with zatca.md (OQ-2, still open) |
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
| description_en / description_ar | Char(300) | at least one; Arabic required on sales documents, B2B included (OQ-18, confirmed: DTG p.79; TIR §4.2.1) | Bi | Latin script allowed for proper names |
| quantity | Decimal(12,3) | Y | | > 0 |
| unit_code | Char(3) (UN/ECE rec 20, e.g. `C62`, `MON`, `HUR`) | Y | | |
| unit_price | Decimal(18,2) | Y | | ≥ 0. 2 dp per D19 (OQ-12); ZATCA sets no limit on unit-price decimals, only line net is 2 dp (XML v1.2 p.12, 17; DTG p.70) |
| line_discount | Money | N | | |
| net_amount | Money | computed | | round(qty × unit_price − discount) |
| income_type | FK IncomeType | Y | | D16; prefills `income_account`, `vat_category` |
| vat_category | FK VatRate | Y | | Rate effective on `supply_date` (or `issue_date`) |
| vat_exemption_reason | FK VatExemptionReason | Y if Z/E/O | | |
| exemption_text_en / exemption_text_ar | Char(300), blank | `_ar` Y if reason is `VATEX-SA-OOS`; both refused with any other reason | Bi | *Added 2026-10-17 (R80). Amended 2026-10-17 (as built):* names are `exemption_text_en` / `exemption_text_ar` (not `vat_exemption_text_*`), max 300 characters each (migration `invoicing/0006_oos_exemption_text`). Free-text reason given by the taxpayer case by case (XML v1.2 §11.2.4 p.33; BT-120). Carried into the VAT breakdown of category `O` as `exemption_texts` [{en, ar}] |
| vat_amount | Money | computed | | Display only; not summed into header VAT (R9). Still emitted as line VAT KSA-11 (with line total KSA-12) on standard documents (BR-KSA-52/53) |
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
| currency, fx_rate, fx_rate_date, fx_rate_source | Char(3) / per ledger-fx.md | Y | | Any currency (D13, R56) |
| status | Choice: `draft`, `submitted`, `posted`, `reversed` | Y | | `approved` removed: CFO approval posts at once (R58, D17) |
| original_document | FK PurchaseDocument, null | Y for vendor credit note (default, OQ-10) | | |
| reverse_charge | Bool | computed | | `not vendor.is_resident` |
| totals, amount_allocated, payment_state, is_intercompany, requires_purification | as for SalesDocument | | | |
| attachment | File | N | | Vendor PDF. Agent "invoice reader" drafts from it (D8) |
| approved_by / approved_at | FK User / DateTime | | | |
| journal_entry / reversal_entry | FK JournalEntry, null, PROTECT | | | |

**PurchaseDocumentLine**: `description_en/_ar` (Bi, at least one), `quantity`, `unit_price`, `line_discount`, `net_amount`, `vat_category`, `vat_amount`, `vat_attribution` (Choice `taxable` / `exempt` / `mixed`, required, R29), `supply_kind` (Choice `goods` / `services`, required for non-resident vendors, R30), `vat_recoverable` (computed from R29/R30, not user-set), `account` (FK Account, same entity; type expense or asset; postable), `wht_category` (FK WhtCategory, required for non-resident vendors, default `vendor.wht_category`, R61).

**VatRecoveryRatio** (unique `(entity, year)`): `entity`, `year`, `ratio` Decimal(5,4), `kind` (`provisional` / `final`), `approved_by`, `approved_at`. Used only after OQ-9 sign-off; annual true-up compares provisional with final.

### 3.5 Money movements and allocation

**CustomerReceipt** (`kind` = `receipt` / `refund`) and **VendorPayment** (`kind` = `payment` / `refund_received`)
| Field | Type | Req | Notes |
|---|---|---|---|
| entity, party (Customer / Vendor, same entity) | FK | Y | |
| number | Char(40) | at posting | Gap-free |
| date | Date | Y | |
| amount | Money | Y | > 0 |
| purpose | Choice: `settlement`, `advance`, `deposit` | Y (receipts) | Default `settlement`. `advance` / `deposit` per R65 to R67 |
| contract | FK billing contract (billing.md), null | Y for `advance` / `deposit` | Contract must state that advance / deposit applies (D15) |
| bank_charge | Money | N | Receipts only. Booked to 5700 |
| currency, fx_rate, fx_rate_date | Char(3) / per ledger-fx.md | Y | Any currency (D13) |
| status (payments) | Choice: `draft`, `submitted`, `posted`, `reversed` | Y | Vendor payments need CFO approval (R59) |
| wht_amount, wht_gross_up (payments) | Money / Bool snapshot | computed | R61 to R63. Detail rows in `WhtDeduction` |
| bank_account | FK ledger.Account (same entity, asset) | Y | Default 1100 (OQ-20) |
| reference_en / _ar, bank_reference | Char | N | |
| status (receipts) | Choice: `draft`, `posted`, `reversed` | Y | Receipts post at once (R33; OQ-41) |
| journal_entry / reversal_entry | FK JournalEntry | | |
| amount_allocated / amount_unallocated | Money | computed | For payments, AP settled = `amount + wht_amount` unless gross-up (R62, R63) |

**WhtDeduction** (one per payment × allocated bill line category): `entity`, `payment`, `bill`, `wht_category`, `base_amount` (doc currency) and `base_amount_sar`, `rate_applied`, `treaty_certificate` (null), `wht_amount` and `wht_amount_sar`, `gross_up` (Bool), `return_month`. Immutable; reversed with the payment. Source of the monthly WHT return data (R64).

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
    *Amended 2026-10-16 (ledger build):* the ledger sets `JournalLine.requires_purification` at posting, only on **credits** of `normal` lines on accounts flagged `requires_purification` (4900) or on Sharia-prohibited income accounts (possible only in entities not enforcing Sharia). A reversal's lines carry the flags of the lines they mirror. Debits on 4900, so the purification transfer E14, are not flagged. The amount to purify (`ledger.services.purification_amount(entity, user, as_of, date_from)`, needs `ledger.view`) is the signed net of flagged lines: flagged credits minus flagged debits, so a reversed document nets to zero; what was already transferred shows on 2600. **[ledger `_post`, `signed_purification`; test AT-71]**
7. No interest, late-payment penalty or time-value charge can be posted as income in a Sharia-enforcing entity. No late-payment charge feature is built until OQ-15 is decided. **[svc: no such feature; account checks per R5]**

### Money and VAT
8. *Withdrawn — see Decision D13 (replaced by R56).*
9. Line `net_amount` = `quantity × unit_price − line_discount`, rounded to 2 dp. Header VAT is computed **per VAT category and rate at document level**: category VAT = round(sum of line net amounts in that category (after document discount) × rate, 2). `total_vat` = sum of category VAT. It is **not** the sum of rounded line VAT ("rounded on document level and not as a summation of rounded Invoice line VAT amounts", XML v1.2 §10 p.29; BR-CO-17 / BR-S-09). Line `vat_amount` = round(net × rate, 2) is not used for the header, but is still emitted as KSA-11/KSA-12 on standard documents (BR-KSA-52/53). ZATCA publishes no line-vs-document tolerance, so none is checked (zatca.md R-31). *Note 2026-10-16:* SDK 2.0.3's schematron accepts a category VAT difference < 1.00 (BR-CO-17 / BR-S-09; zatca SDK findings §8); the exact round(…, 2) category VAT here and in zatca.md R-31 is a house rule, stricter than ZATCA. Rounding mode ROUND_HALF_UP (OQ-11; XML v1.2 §10 p.29; VAT rounded to the nearest halala, TIR §4.2.4 p.21). *Amended 2026-10-16 (verification I7).* The 2200 / 1310 JE line equals `total_vat`. **[svc; test AT-1, AT-35]**
    *Amended 2026-10-17 (as built):* R9 does not yet say how a document-level discount is split across VAT categories, so a sales document with a non-zero `document_discount` is refused at issue; line discounts are used instead (OQ-55). **[svc `_issue_problems`; test AT-77]**
10. The VAT rate is taken from `VatRate` effective on the supply date (or the issue date if no supply date). Lines with category Z, E or O need an exemption reason. **[svc; test AT-15]**
11. An entity with an empty `vat_number` cannot issue sales documents with category S lines. **[svc; D19 (OQ-17); test AT-16]**
    *Amended 2026-10-17 (D30b):* kept; and such an entity charges no VAT on any line (R94).

### Sales document lifecycle
12. *Withdrawn — see Decision D17 (replaced by R57).*
13. Only drafts can be edited or deleted. Issued, final and rejected documents are immutable except for fields written by `zatca.services` and the computed payment fields. **[model `save` guard like `JournalEntry.save`; svc; test AT-5]**
14. On issue, the system assigns the number from `DocumentSequence(entity, doc_type, year of issue_date)`. The number is gap-free and never reused. Assigning the number and changing the status happen in one transaction. A failure rolls back both. **[svc `select_for_update`; DB unique `(entity, doc_type, number)`; test AT-6]**
15. On issue, the buyer and seller snapshots are frozen. Later edits to the Customer or Entity do not change issued documents. **[svc; test AT-7]**
16. A standard (B2B) document needs a buyer name in Arabic and the buyer address of §3.1: street, city, country (BR-KSA-10); for SA buyers also building number, postal code (5 digits) and district (BR-KSA-63, BR-KSA-67); buyer building number 4 digits as a house rule (OQ-45). If the customer is VAT-registered, the buyer VAT number is mandatory; otherwise an `other_id` is mandatory, chosen by the §3.1 priority order (BR-KSA-81, XML v1.2 p.61; BR-KSA-14; DTG p.79). Export invoices carry no buyer VAT number (BR-KSA-46). *Amended 2026-10-16 (verification I1, I2, I3, I9).* **[svc validation at issue; test AT-8, AT-36, AT-61, AT-65]**
    *Amended 2026-10-17:* buyer IDs come from `CustomerIdentifier` rows (§3.1 note). Export to a VAT-registered buyer: see R82 (pending fix).
17. Issued invoices are never cancelled or deleted. A final invoice is corrected only by a credit note or debit note that references it. **[svc: no cancel path; ZATCA requirement; test AT-9]**
    *Amended 2026-10-17 (D25):* agreed with ZATCA; an issued document also never returns to draft (R96).
18. A credit note must reference a `final` invoice of the same entity and customer (one original per note, default, OQ-43), and it needs a reason in Arabic (`reason_ar` required; English optional; KSA-10 mandatory on 381, BR-KSA-17, XML v1.2 p.52; visible fields in Arabic, DTG p.79) and a `payment_means_code` (BR-49, p.38). The `reason_code` pick list offers the five statutory reasons in en/ar (DD KSA-10). Its gross amount cannot exceed the invoice gross amount minus credit notes already issued against that invoice. **[svc; test AT-10, AT-11, AT-63]**
19. A debit note must reference a `final` invoice of the same entity and customer (one original, OQ-43), and it needs a reason in Arabic (`reason_ar` required; English optional; KSA-10 mandatory on 383, BR-KSA-17) and a `payment_means_code` (BR-49). Same pick list as R18. **[svc; test AT-11, AT-63]**
20. A rejected document keeps its number (the number stays used) and has no posted GL effect. The correction is a new document under a new number (confirmed by D12). **[svc; test AT-19]**

### Purchase document lifecycle
21. *Withdrawn — see Decision D17 (replaced by R58).*
22. A document created by an agent (`source=agent:*`) stays a draft until a human submits it. **[svc; D8; test AT-22]**
23. `(entity, vendor, vendor_document_number)` is unique among non-reversed purchase documents (duplicate-bill guard, default pending OQ-10). **[DB partial unique + svc; test AT-23]**
24. A posted bill is never edited. It is corrected by a vendor credit note, or (when it has no allocations) by reversal through `ledger.services.reverse_entry`, which sets the status to `reversed`. **[svc; ledger lock; test AT-24]**
    *Amended 2026-10-16 (ledger build; access R36):* one booking per document. A reversed bill (or receipt, payment, sales document) is never booked again; the corrected one is entered as a **new document** with its own number and entry. This matches zatca D-4 / OQ-6 for rejected sales documents (new document, new number) and R20. **[ledger DB partial unique + svc; test AT-69]**
25. A bill with allocations cannot be reversed until those allocations are un-allocated (R36). **[svc]**
26. The internal number is assigned at posting from `DocumentSequence`, gap-free. **[svc]**

### Posting (all documents)
27. Every journal entry is created with `ledger.services.create_entry(..., reference=document.number)` and posted only with `post_entry`. Invoicing never sets `JournalEntry.status`. **[svc; code-reviewer; test AT-1]**
    *Amended 2026-10-17 (as built):* every invoicing entry is created and posted in one step with `create_entry(..., lines=None, post=True, grant=..., document=...)`; invoicing never creates a draft JE and never calls `post_entry` (the ledger refuses `post_entry` on document-linked entries). Lines come from the document type's registered hooks (R75). **[ledger; svc; test AT-72]**
28. The JE date is `issue_date` for sales documents, `accounting_date` for purchase documents and `date` for receipts and payments. Posting fails when no open period covers that date. The document stays in its previous status and no number is consumed. **[ledger `_find_open_period`; svc single transaction; test AT-17]**
29. Input VAT recovery follows VAT Implementing Regulations Art. 51 **[Needs expert sign-off: tax]** (the Input Tax Deduction guideline was not downloaded on 2026-10-16; unverified). Each bill line has `vat_attribution`: `taxable` (used for taxable supplies), `exempt` (used for exempt supplies) or `mixed`. For a resident vendor, VAT goes to 1310 only when attribution is `taxable` **and** the vendor document has a vendor VAT number. `exempt` VAT is added to the line's expense or asset account. `mixed`: once OQ-9 is signed off, the recoverable share uses the entity's `VatRecoveryRatio` for the year, with an annual true-up; **until then, mixed VAT is posted fully to the line account (cost)**. This is the conservative default: it never claims more than allowed, and posting is not blocked. **[Needs expert sign-off: tax]**. **[svc; test AT-25, AT-39]**
    *Amended 2026-10-17 (D24):* "and the vendor document has a vendor VAT number" no longer sends VAT to cost: a resident vendor without a VAT number charges **no VAT**, so its bill has 0 VAT and no 1310 line (R92). R29 attribution applies only to bills that carry VAT from a vendor with a VAT number.
30. Reverse charge applies only to **standard-rated services imported** from a non-resident vendor (`supply_kind=services`). It does not apply to goods (import VAT on goods is paid at customs, outside this module; OQ-29) **[Needs expert sign-off: tax]** (the RCM circular was not downloaded on 2026-10-16; TIR p.13 only notes that no tax invoice is issued for VAT due under reverse charge). RC lines post Dr 1310 and Cr 2200 for the same amount, and AP holds net only. R30 takes precedence over R29's vendor-VAT-number requirement (a non-resident has none), but R29 attribution still applies to the 1310 side: `exempt` → cost, `mixed` → cost by default. VAT-return box mapping: OQ-29. **[svc; KSA VAT reverse-charge; test AT-26, AT-40]**
31. *Withdrawn — see Decision D14 (replaced by R61 to R64).*
32. Reversals use `reverse_entry(entry, date=..., user=..., reason=...)`. If the original period is closed, the service passes a date in the current open period. It never relies on the default `date=None`. **[svc; test AT-24]**
    *Amended 2026-10-16 (ledger build):* actual signature `reverse_entry(entry, grant=None, date=None, reason="")`; the grant is `invoicing.reverse_document` issued for the document (or `ledger.reverse_entry`). `invoicing.reverse_document` only reverses; it never books an entry (access R37). Document postings use `create_entry(..., post=True, grant=..., document=...)` with the document's type registered by this app in `ledger.documents` (access R34, R35). **[ledger; test AT-69]**

### Receipts, payments and allocation
33. A receipt or payment posts at once (`create_entry(..., post=True)`). It can be reversed only while it has no allocations. **[svc; test AT-28]**
    *Amended 2026-10-16 (ledger build; access R35):* the ledger refuses a document posting by the document's creator, submitter or last modifier, checked on the stored document. So the person who records a receipt cannot post it: a **second person** with `invoicing.post_receipt` posts it. This conflicts with the earlier wording "posted at once by accounting staff" (R33, R59, OQ-41 default, §7 "Post receipt"): a clerk can no longer record and post a receipt alone. Not decided by the owner: OQ-49 (default: a second person posts). **[ledger; test AT-70]**
    *Amended 2026-10-17 (as built; OQ-49 default applied):* receipts run `draft → posted → reversed`. Recording a receipt (`create_receipt`) and planning its applications (`add_receipt_application`) need `invoicing.allocate` **and** the user must also hold `invoicing.post_receipt` in the entity. Posting (`post_receipt`) needs an `invoicing.post_receipt` grant for that receipt, held by a **different person** from the one who recorded or last changed it, also when `segregation_required = False`. Receipts with `purpose` `advance` or `deposit` are refused in slice 1 (billing contracts not built; R65 to R67). **[svc `_require_receipt_staff`; ledger; test AT-70, AT-73]**
    *Amended 2026-10-17 (D26; supersedes the "different person" part of the two notes above; resolves OQ-49):* the person who records a customer receipt (`kind = receipt`) may post it; a second person is not required, whatever `segregation_required` says. Posting still needs an `invoicing.post_receipt` grant for that receipt (access R46). Customer refunds and every other document keep creator ≠ poster (access OQ-26). Recording keeps needing `invoicing.allocate` + `invoicing.post_receipt`. **[ledger registry option + svc; test AT-97]**
34. An allocation needs source and target to be the same entity and the same party, both posted/final, and an amount no higher than the open amount on either side. **[svc with row locks; DB check amount ≠ 0; test AT-29]**
    *Amended 2026-10-17 (as built):* source and target must also be in the same currency. Foreign-currency items are not allocated later: they are settled when the receipt, payment or credit note posts (R76). **[svc `_allocation_problems`; test AT-74]**
35. Allocation has no GL effect (both sides sit on the same control account). It changes `payment_state` only. **[svc]**
36. Allocations are never edited or deleted. An un-allocation is a new row with negative amount and `reverses` set. **[model `save/delete` guard; test AT-30]**
37. On reaching `final`, a credit note is automatically allocated to its original invoice up to the invoice's open amount. Any remainder stays as customer credit. **[svc; test AT-10]**
38. Unallocated receipts with `purpose=settlement` (overpayments, unidentified cash awaiting allocation) are allowed. They stay as a credit balance on the customer and appear on the unallocated-cash report. Advances and deposits: R65 to R67. **[svc + report]**
39. The open amounts in the sub-ledger tie out to the GL: the sum of open customer balances per entity equals the trial balance of 1200, plus 1210 per counterparty for intercompany customers. The same holds for AP against 2100 and 2110. **[svc `reconcile_subledger(entity, as_of)`; test AT-31]**

### ZATCA hand-off (details in `docs/specs/zatca.md`; service names provisional, OQ-2)
40. When a sales document is issued, invoicing calls `zatca.services.submit_document(document, user)` in the same request after the issue transaction commits. Standard documents are submitted for clearance and simplified ones for reporting. **[svc; test AT-18 with zatca stub]**
    *Amended 2026-10-17 (as built):* until zatca exists the call goes through `invoicing.zatca_port` (`submit_document`, `is_issued`; setting `INVOICING_ZATCA_PORT`; default `PendingZatcaPort` sends nothing and leaves documents `pending`, so nothing posts without a ZATCA result). A hand-off that fails after commit is retried by R79 (pending fix). **[svc; port; test AT-18]**
41. At issue, invoicing builds the GL entry with `create_entry(..., post=False)`. That draft JE is posted with `post_entry` only when `zatca.services` reports `cleared`, `reported` or `*_with_warnings`. The document then becomes `final`. **[svc callback `on_zatca_result(document, result)`; test AT-18]**
    *Amended 2026-10-17 (as built; supersedes the draft-JE wording):* **no journal entry is created at issue.** `on_zatca_result(document, result, user)` (ZATCA queue system user, `zatca.process_queue`) records the `zatca_*` fields; on `cleared`, `reported` or `*_with_warnings` it calls `post_cleared_document`, which posts the document in **one step** with `create_entry(..., lines=None, post=True, grant=<invoicing.post_on_clearance grant of the queue user>, posted_by=document.approved_by, document=document)`, dated `issue_date`. The ledger accepts `posted_by` only if it is a human whose `invoicing.approve_sales_document` grant for this document was used after its last change, and who is not its creator, submitter or modifier; the post is access-logged (`post_on_clearance`). The document then becomes `final` (credit notes are allocated per R37). If posting fails, the result stays recorded, the document stays `issued`, and `post_cleared_document` retries it. **[svc `on_zatca_result`, `post_cleared_document`; ledger `_clearance_approver`; test AT-18, AT-72]**
42. On ZATCA `rejected`, the document becomes `rejected` and its draft JE is deleted (drafts are deletable per `JournalEntry.delete`). **[svc; test AT-19]**
    *Amended 2026-10-17 (as built):* there is no draft JE to delete. On `rejected` the document becomes `rejected`, **nothing is booked**, and its number stays used (R20, D12). **[svc; test AT-19]**
43. A period cannot close while any sales document dated inside it waits for ZATCA. This follows from the draft JE, which `close_period` refuses. **[ledger `close_period`; test AT-32]**
    *Amended 2026-10-17 (as built):* without a draft JE, the rule is kept by the ledger's period hook: invoicing registers `pending_documents` for `SalesDocument` (`_pending_sales_documents`: documents in `issued` status with `issue_date` in the period), and `close_period(period, user=...)` refuses with `documents_pending`, listing up to 10 document numbers (en/ar). **[ledger `close_period` + `documents.pending_in_period`; svc hook; test AT-32]**
44. A document is not final, cannot be allocated, and cannot be credited until it is cleared or reported. A standard invoice is not shared with the buyer before clearance. **[svc; test AT-18]**
45. Fields `zatca_*` are written only by `zatca.services`. Invoicing never computes the hash, ICV, PIH or QR. **[model guard / code-reviewer]**
46. A purchase document is never sent to ZATCA. **[svc]**

### Added 2026-10-16 (compliance review)
47. The printed and PDF title is "Tax Invoice" / "فاتورة ضريبية" for standard documents and "Simplified Tax Invoice" / "فاتورة ضريبية مبسطة" for simplified ones; credit and debit notes use their bilingual titles "Credit Note" / "Debit Note" (TIR §4.2.3 p.17, §7 p.37, annex). **[svc rendering; test AT-37]**
    *Amended 2026-10-17 (D30b):* these titles are only for entities with a VAT number. An entity without one never renders "Tax Invoice" / "فاتورة ضريبية" (or the simplified title); its title and document type: R94, OQ-59 **[Needs expert sign-off: tax]**.
48. *Amended 2026-10-16 (verification I11; scope widened, D19/OQ-19 default unchanged).* A simplified tax invoice may be issued only for a supply of less than SAR 1,000, or to a natural person not subject to VAT regardless of value (TIR §4.1.1 p.12–13, §4.3.1 p.23; DG §2.3 p.8, §4.2.1 p.17). So a document to any customer whose `customer_kind` is not `individual` (every business, VAT-registered or not, and every `government` customer) with a value of SAR 1,000.00 or more cannot be `simplified`; 1,000.00 itself must be standard ("less than"). Documents to `individual` customers may be simplified at any value. The value tested is the gross amount in SAR (default) until it is settled whether the threshold is gross or net of VAT **[Needs expert sign-off: tax]** (OQ-44). **[svc at issue; test AT-38, AT-62]**
    *Amended 2026-10-17:* an `individual` customer with `is_vat_registered=True` counts as a business for this test (R83, pending fix). Notes keep their original's profile, so a note of SAR 1,000 or more to a non-individual whose original was simplified cannot be issued as built (OQ-52).
    *Amended 2026-10-17 (as built, interim default; supersedes the previous sentence):* only `doc_type = invoice` is tested against the SAR 1,000 threshold. Credit and debit notes are not tested: a note "is subject to the same issuing requirements as the type of invoice on which it is based" (XML v1.2 §5.2) and keeps its original's profile (zatca R-19), so a note of SAR 1,000 or more on a simplified invoice is issuable as simplified. Tax sign-off pending (OQ-52). **[svc `_simplified_problems`; test AT-86]**

### Personal data (PDPL)
49. Buyer snapshots and parties of `customer_kind=individual` (and, *added 2026-10-17 (D24)*, vendors of `vendor_kind=individual`) are personal data. The lawful basis recorded for them is legal obligation (tax invoicing and record-keeping) **[Needs expert sign-off: legal]** (PDPL sources not downloaded on 2026-10-16). **[data register; svc]**
50. Simplified documents do not collect or store a buyer ID (`other_id`) unless a rule requires it. **[svc: fields cleared/blocked at issue; test AT-42]**
    *Amended 2026-10-17:* one such rule exists: lines with exemption reason VATEX-SA-EDU or VATEX-SA-HEA need the buyer's national ID (`NAT`) (R81, pending fix).
51. Access to individuals' personal data (customer record, buyer snapshot, `other_id`, IBAN) is limited to roles that need it, and each view/export is logged with user, entity and time. **[permission + access log; test AT-44]**
52. After `retention_until`, personal data of individuals on documents and parties is anonymised (names, IDs, address replaced by a fixed token); amounts, VAT, numbers and ZATCA hashes are kept. `retention_until` follows zatca.md R-38 per class: `standard` 6 years from the end of the tax period; `capital_asset_real_estate` 15 years; `movable_capital_asset` Art. 52 adjustment period + 5 years from acquisition (TIR §8.3.3 p.42; OQ-46). WHT payment records are kept at least 10 years after payment (WHT p.31; OQ-8). No job runs before the retention rules are confirmed (zatca.md OQ-8, OQ-31). **[svc job; test AT-43]**
    *Amended 2026-10-17 (slice-2 review finding 2, as built; the job stays off until OQ-8 / OQ-31 are confirmed):* individual vendors (D24, R49) are covered too: once their retention date (`_vendor_retention_until`) has passed, their name, address, VAT number and IBAN are replaced by the token (and the vendor is made inactive), and the `vendor_vat_number` on their posted or reversed bills is blanked. A vendor's retention date is the latest of: each posted or reversed bill or vendor credit note, 6 years after the end of the tax period of its accounting date (15 years if a line books to an asset account, conservative default for capital-asset records); each posted or reversed payment, 6 years after the end of its tax period; each WHT deduction, **10 years after its payment date**. A vendor with no bill or payment, or with one still a draft or submitted, is left alone. The PostgreSQL lock lets a posted or reversed bill change once more, only for this (`vendor_vat_number` blanked, `anonymised_at` set) and never within 6 years of the end of its quarter or 10 years of a WHT payment on it. Not covered: scans attached to bills and treaty certificates (files), and the ZATCA XML (OQ-63). **[svc job; test AT-43 vendor cases]**
53. Any processor outside KSA (hosting, backup, e-mail, OCR, AI) needs a transfer assessment before it receives personal data. **[Needs expert sign-off: legal]** (OQ-31). Independently of PDPL, VAT already requires invoice records of KSA-resident taxpayers to be kept on servers in the Kingdom (TIR §8.3.1 p.41; zatca.md OQ-20). **[process; code-reviewer]**

### Purification and advances
54. Purification moves the flagged amount from 4900 to 2600 Charity payable, and the payout clears 2600 against a bank account (E14, E15). Both go through `create_entry` + `post_entry`, carry `reference`, and need a human user; never an agent. Amount and approval: OQ-30. **[svc; ledger; test AT-45]**
    *Amended 2026-10-16 (ledger build):* the amount offered for transfer is the signed net of flagged lines (R6 amendment); the E14 debit on 4900 is not flagged, so it does not reduce the flagged net; the transferred total is read from 2600. Which amount is transferred and when stays OQ-30. **[ledger `purification_amount`; test AT-71]**
    *Amended 2026-10-17 (slice-2 review findings 1 and 8):* the limits (transferred ≤ flagged, paid out ≤ transferred) hold on the row's date **and on every later booking date** (so all-time), so a backdated transfer or payout cannot push a later date over its limit; posts and reversals of one entity are serialised (lock on the entity row). A posted transfer or payout is corrected only by `reverse_purification_transfer` (`invoicing.reverse_document`, a person, a reason; through `ledger.services.reverse_entry`; dated as R32); the row becomes `reversed` and no longer counts. A transfer is not reversed while the payouts after it would then exceed 2600 (reverse the payout first). **[svc; tests `test_purification.py`]**
55. *Withdrawn — see Decision D15 (replaced by R65 to R67).*

    *Amended 2026-10-17 (D31b, to build):* purification money is kept apart in a Saudi bank account. (1) Each Sharia-enforcing entity designates one active `banking.BankAccount` as its **purification account** (Saudi IBAN, `SA` + 22 digits with a valid check digit, SAR); its GL account is a separate proposed bank account (1140 Bank – purification account, proposed per D29). Changing the designation follows the bank-account change control (banking R7). (2) **Approved charity recipients** per entity (`CharityRecipient`: name_en/name_ar, Saudi IBAN, bank, optional platform reference such as Ihsan, active flag), added by the accountant and approved by the Managing Partner (approver ≠ creator); Sharia board sign-off of the list stays OQ-30. (3) After E14 (4900 → 2600), a **funding transfer** moves the same cash from an operating bank account to the purification account (Dr 1140 / Cr operating bank), so 2600 is backed by cash in that account. (4) The payout E15 is Dr 2600 / Cr 1140 only, from the purification account to one approved, active charity recipient (named on the row with its IBAN, masked in lists); payouts to any other account are refused (`purification_recipient_not_approved`, `purification_account_required`). Payouts are capped by 2600 and by the purification account's GL balance. (5) A payout produces a bank instruction (amount, recipient IBAN, reference) for the bank file/banking module; no direct bank connection. **[svc; tests AT-45 amended, AT-101 to AT-103 to add]**

### Added 2026-10-16 (project-owner decisions D10 to D18)

**Currency (D13)**
56. Every amount is `Decimal` with 2 dp; a float input is rejected, not converted. Every document, receipt and payment stores its currency (any active ISO 4217 code) and, when it differs from `entity.base_currency`, the rate, rate date and source defined in `ledger-fx.md`. The JE is created per ledger-fx.md (document and functional amounts). For ZATCA, document VAT is also expressed in SAR: `total_vat_sar` and `vat_breakdown.vat_sar` = round(category VAT × rate to SAR, 2), using the SAMA daily exchange rate on the date the tax becomes due (TIR §4.2.2 p.17; XML v1.2 BR-KSA-68, BR-KSA-EN16931-02/09). zatca.md R-40 emits the SAR tax total on every document, SAR ones included. Non-SAR documents cannot be issued or posted until ledger-fx.md is approved and built. **[svc; DB decimal fields; test AT-46, AT-47]**
    *Amended 2026-10-17 (as built):* ledger-fx is built and foreign-currency documents are issued and posted. The rate to the functional currency is fixed at approval (sales, bills) or posting (receipts, payments); the SAR rate for `total_vat_sar` is taken on `issue_date` from the ledger rate table (source and tax-point date: OQ-51). Settlement rules: R76. Booking at the pinned rate: R77 (pending fix).
    *Amended 2026-10-17 (D27):* "with 2 dp" now means SAR and functional amounts; transaction-currency amounts follow R93 (up to 3 dp per the currency's `minor_units`).

**Approval and posting (D17)**
57. Sales documents run `draft → submitted → issued → final`, or `issued → rejected`; `submitted → draft` returns it for edits. Accounting staff create and submit. CFO approval of a `submitted` document issues it (number, snapshots, ZATCA hand-off R40) in one transaction; posting then follows automatically on ZATCA `cleared` / `reported` / `*_with_warnings` (R41). The approver must differ from the creator when `InvoicingSettings.segregation_required` (D19/OQ-5). No other transitions. **[svc state machine; test AT-2, AT-51]**
58. Vendor bills run `draft → submitted → posted`, then `posted → reversed`. CFO approval of a `submitted` bill posts it in the same transaction (`create_entry` + `post_entry`); if posting fails (e.g. closed period) the approval rolls back and the bill stays `submitted`. Creator ≠ approver per R57. **[svc; test AT-52]**
59. Vendor payments run `draft → submitted → posted` with CFO approval posting at once, as R58. Customer receipts keep R33 (OQ-41). **[svc; test AT-52]**
60. `invoicing.services.create_from_billing(billing_item, user)` creates a sales document with `source="system"` and `billing_item` set, using the same validation, numbering, ZATCA and posting rules. If the item's contract is CFO-approved and `InvoicingSettings.auto_issue_from_approved_contract` is True, the document is issued without per-invoice approval (`approved_by` = the contract approver); otherwise it waits as `submitted` for the CFO (D20 confirms no per-invoice approval; the switch stays, default True). Credit and debit notes from billing never use this path (R73). Only the billing system user may call it; agent-gateway users cannot (D8). One document per billing item (idempotent). **[svc; DB unique `billing_item`; test AT-53]**
    *Cross-reference 2026-10-17 (billing interface, billing.md R52 to R55; to be applied when this hook is built):* `billing_item` is billing's `BillingPeriod`; the lines come from its frozen `invoice_lines`. "One document per billing item" means one **non-rejected** document (partial unique), because billing re-generates a ZATCA-rejected period under a new number (billing R12). `approved_by` = the approver of `billed_version` (the contract approver for version 1). The call is all-or-nothing and raises distinct error codes (`rate_required`, open-period, a new code for the OQ-62 third-decimal refusal, `entity_not_vat_registered`) that billing maps to `failed` or `held`. Posting on clearance must accept that contract/version approval as the recorded approval for these documents (billing R53; needs ledger `_clearance_approver` and access changes). The billing system user needs `ledger.view_rates` for the rate lookup.
    *Amended 2026-10-17 (slice-2 review finding 9, default chosen: refuse):* if the contract's `requires_purification` disagrees with the purification flag of the invoice lines' income accounts (an account flagged `requires_purification`, or Sharia-prohibited income where Sharia is not enforced), `create_from_billing` refuses with `purification_flag_mismatch` and creates nothing; billing treats it as a failure (billing R54, "any other code"). Recording a warning instead was not chosen, because the invoice's purification flag drives rule 5. **[svc; test `test_r60_purification_flag_must_agree_with_the_line_accounts`]**

**Withholding tax (D14; reference rates in OQ-8 (WHT §3 p.28) + [Needs expert sign-off: tax])**
61. Every bill line from a non-resident vendor carries a `wht_category`. A payment allocated to a non-resident bill with a line lacking a category is rejected. **[svc; test AT-57]**
    *Amended 2026-10-17 (as built; conservative default):* R61 to R64 are not built in slice 1. Until they are, creating or approving a vendor payment (`kind=payment`) to a non-resident vendor is refused (`wht_not_built`, en/ar). Bills from non-resident vendors (reverse charge, R30) and refunds received from them are allowed. **[svc `_no_payment_without_wht`; test AT-75]**
62. On a payment to a non-resident vendor, WHT = round(allocated base × rate, 2) per category, where rate = `WhtCategory.rate` effective on the payment date, or the `treaty_rate` of a verified `TreatyCertificate` valid on the payment date for that vendor and category. One `WhtDeduction` row per category. Without gross-up, cash paid = settled amount − WHT; the AP settled is the full amount. WHT is recognised at payment **[Needs expert sign-off: tax]** (not stated in the verified excerpts). **[svc; test AT-54, AT-56]**
    *Amended 2026-10-17 (slice-2 review findings 4 and 6, conservative defaults):* treaty certificates are written only through `add_treaty_certificate` / `update_treaty_certificate` (bulk updates and deletes refused); every change of an unverified certificate records `modified_by`, and neither the creator nor the last modifier verifies it. The scan is a PDF, PNG or JPG of at most 10 MB (name and content checked). On entry, change and verification: the treaty country equals the vendor's `residence_country`, and `treaty_rate` ≤ the statutory rate (the category's rate effective on `valid_from`; for an all-categories certificate, the lowest category rate). At payment the rate applied is never above the statutory rate. Whether the residence rule fits dual-resident vendors, and the all-categories cap, need tax-adviser confirmation (OQ-8). **[svc + model + PG trigger; tests `test_treaty_certificates.py`]**
63. With `wht_gross_up` (vendor or contract, OQ-8), the vendor receives the full amount and WHT = round(amount × rate / (1 − rate), 2) **[Needs expert sign-off: tax]** (gross-up not covered by the WHT guideline read), booked as an expense of the entity (E21). **[svc; test AT-55]**
64. `wht_return_data(entity, month)` returns, per category and vendor, base, rate, WHT (SAR), treaty reference and country, from `WhtDeduction`; it ties to the 2220 movements for the month. Remittance (E22) is a CFO-approved payment. **[svc report; test AT-58]**
    *Amended 2026-10-17 (slice-2 review finding 7):* WHT is computed, recorded and returned only for entities whose functional currency is SAR (the SAR amounts are the functional amounts that tie to 2220). For any other functional currency, payments to non-resident vendors and `wht_return_data` are refused with `wht_requires_sar_functional` until the owner decides how such an entity accounts for and files KSA WHT (not specified). **[svc; test `test_r64_wht_needs_a_sar_functional_currency`]**

**Advances and security deposits (D15)**
65. An advance is invoiced with a prepayment invoice (`doc_type=prepayment_invoice`, ZATCA 386, zatca.md R-39) referencing a contract that states an advance applies. VAT is due on the advance: the supply occurs on receipt, to the extent of the amount paid, and the tax invoice is due within fifteen days from the end of the month of receipt (TIR §4.5.1 p.26; PSG §6.1 p.26; DG §8 p.40). The 386 posts to 2310 Deferred income (E16). The final 388 lists its `prepayment_deductions` (same entity, customer and contract) and cannot deduct more than the 386 net and VAT not yet deducted (E17); its XML reference to the 386 (KSA-26/28/29/30, KSA-31 to KSA-34, BT-113) is built by zatca.md R-39. *Note 2026-10-16:* the SDK 2.0.3 downloaded from ZATCA (2021 rules) rejects type 386; XML v1.2 stays the target, and ZATCA issuance of 386 waits on a current SDK or sandbox check (zatca.md R-39, OQ-33) **[SDK-confirm: needs current SDK or sandbox]**. **[svc; test AT-59]**
66. A receipt with `purpose=deposit` needs a contract stating a deposit applies and posts to 2320 Security deposits received (E18). Deposit receipts are blocked while `InvoicingSettings.deposit_vat_treatment` is null; it is set only after tax-adviser sign-off (OQ-16). Application or forfeiture of a deposit is not built (OQ-16). **[svc; test AT-60]**
67. A receipt with `purpose=advance` or `deposit` without a qualifying contract is rejected. **[svc; test AT-60]**

**Intercompany and identity (D10, D11)**
68. An invoice to a customer with `group_entity` (subsidiary, associate or minority holding) is a normal standard VAT invoice: VAT per R9 to R11, ZATCA clearance, buyer snapshot from the buyer entity's §3.0 fields, counterparty on 1210 (R4). The buyer entity books its own bill in its own books; invoicing never creates or posts records in another entity, and no automatic mirror bill or draft is created (CLAUDE.md rule 4; D23, was OQ-37). *(Amended 2026-10-16.)* **[svc; test AT-50]**
69. Issuing needs the seller's §3.0 fields complete (legal names en/ar, CR, VAT if S lines, full national address). **[svc at issue; test AT-48]**
70. Real Almiras identifiers (D10) exist only in setup data, never in test fixtures or sample data. **[test AT-49 scans fixtures]**

### Added 2026-10-16 (ZATCA verification)
71. *Withdrawn 2026-10-17 — see Decision D30(a) (replaced by R95): the note's own date counts; no warning is based on a backdated event date.* Former text: A credit or debit note should be issued no later than 15 days of the month following the event that triggered the adjustment (TIR §7.2 p.39). When `adjustment_event_date` is set and the issue date is later than the 15th of the following month, the CFO sees a bilingual warning at approval; issue is not blocked (OQ-47). **[svc warning; test AT-64]**

### Added 2026-10-16 (project-owner decisions D20 to D23)
72. Seller identification (D22). The ZATCA XML carries exactly one seller ID, chosen by zatca.md R-48 (`CRN` = `cr_number` when present). The human-readable invoice, credit and debit note (PDF and print) shows **both** the CR number and the unified national number when the entity has them, each labelled in Arabic and English: "رقم السجل التجاري / Commercial Registration No." and "الرقم الوطني الموحد / Unified National No.". Values come from the frozen seller snapshot (R15). A `unified_number` that is not all digits or does not start with `7` is rejected at entity save. **[svc rendering + `Entity.clean`; test AT-66]**
73. `invoicing.services.create_adjustment_from_billing(adjustment, user)` (billing R43) creates a **draft** credit note or debit note with `source="system"`, `billing_adjustment` set, `original_document` = the adjustment's invoice, supply period and lines from the adjustment, `reason_ar`/`reason_en` from the contract version and `adjustment_event_date` from billing (*amended 2026-10-17, D30a:* billing no longer sets it to the version's effective date; the note is dated and taxed on its own issue date, R95). It never issues: `auto_issue_from_approved_contract` and R60 do not apply. A human (AR clerk or accountant) reviews, completes required fields (`payment_means_code`, R18/R19) and submits; the CFO approves under R57 (not the submitter); R18 credit caps, R28 open-period dating and R71 warnings apply unchanged. Only the billing system user may call it; one non-terminal document per adjustment (idempotent). Drafts superseded by billing (billing R46) are deleted through this module (drafts only, R13). **[svc; DB unique `billing_adjustment` among non-deleted; test AT-67]**
    *Amended 2026-10-17 (slice-2 review finding 5):* "read-only for every user" is enforced in the database too: the PostgreSQL line lock refuses any insert, update or delete of a line whose document has a `billing_item` or `billing_adjustment` unless the transaction-local setting `invoicing.billing_lines` is on, which only `invoicing.models.billing_line_writes()` sets (around the billing hooks, the approval's re-pricing and `discard_adjustment_draft`). In Python the same flag gates `save`, `delete`, queryset `update` / `delete` and `bulk_create` (which also refuses lines on non-draft or other-entity documents). **[model + PG trigger 0020; tests `test_r56_*`]**
    *Cross-reference 2026-10-17 (billing.md R43, R46, R56):* lines of a draft with `billing_adjustment` set are read-only for every user (header fields only); deletion of a superseded draft is a system-user service (name proposed `discard_adjustment_draft(document, user)`, `invoicing.create_adjustment_from_billing`), which first returns a `submitted` note to `draft`; issued, final and rejected notes are never touched.
74. *Withdrawn 2026-10-17 — see Decision D27 (replaced by R93).* Former text: Until OQ-48 is decided, a document, receipt or payment in a currency whose `minor_units` ≠ 2 is rejected at save with a bilingual message (default). **[svc; test AT-68]**

### Added 2026-10-17 (invoicing slice 1 as built)
75. **Document registration.** `InvoicingConfig.ready()` registers each invoicing document type in `ledger.documents`; no other code registers them. Each type registers `build_lines` (the document builds its own journal lines from its stored data, so a caller cannot book other lines) and `validate_lines` (`_check_document_rate`: foreign lines must carry the rate stored on the document). `SalesDocument` also registers `pending_documents` (R43). The build hooks run only inside an `invoicing.services` posting (a posting context set by the service); a `create_entry(post=True)` for an invoicing document from anywhere else is refused (`post_through_services`). **[ledger registry; svc; test AT-72]**

    | Model | Post permission | Reverse permission | Lines |
    |---|---|---|---|
    | `SalesDocument` | `invoicing.post_on_clearance` | none (R17, OQ-28) | E1 to E4 |
    | `PurchaseDocument` | `invoicing.approve_bill` | `invoicing.reverse_document` | E7 to E10 |
    | `CustomerReceipt` | `invoicing.post_receipt` | `invoicing.reverse_document` | E5, E6 |
    | `VendorPayment` | `invoicing.approve_vendor_payment` | `invoicing.reverse_document` | E11 |

76. **Foreign-currency settlement.** For documents in a currency other than `entity.base_currency`: (a) a receipt or payment settles the items in its applications (`add_receipt_application` / `add_payment_application`, planned on the draft) **when it posts**, clearing each item's control line at its carrying rate via `ledger.services.fx_settlement_lines`, with the difference on 4700 / 5750 (ledger-fx R18); a credit note settles its original the same way when it posts on clearance. The allocation rows these postings create are marked `via_settlement`. (b) A later `allocate` of a foreign-currency item is refused. (c) `unallocate` of a `via_settlement` allocation is refused; the receipt, payment or note is reversed instead (receipts/payments, R33) and a new one is entered. (d) An unapplied foreign-currency balance on a posted receipt or payment cannot be allocated later (known limitation, OQ-56). (e) Applications must be in the receipt's or payment's currency; cross-currency receipts and payments are not built. **[svc `_allocation_problems`, `unallocate`, `_application_problems`, `_with_settlements`; test AT-74]**

### Added 2026-10-17 (pending fixes, being built; from the slice-1 compliance report)
77. **Pinned rate.** A foreign-currency document books at the `fx_rate` stored on it at approval (sales documents, bills) or posting (receipts, payments), also when the sales document posts later on clearance; `build_lines` passes that rate and `validate_lines` refuses any other. As built, a rate-table change between approval and clearance makes the posting fail and the document must be returned to draft and re-approved; the fix removes that failure. **[svc `build_lines` + `_check_document_rate`; test AT-76]**
    *Amended 2026-10-17 (D25):* "returned to draft" is not possible: an issued document never returns to draft (R96; `return_to_draft` accepts only `submitted`, as built). Until the fix lands, such a document stays `issued` and `post_cleared_document` keeps failing; the fix is required, not optional.
78. **Issue date.** On a draft, `issue_date` is only the requested date. CFO approval sets `issue_date` to today in Asia/Riyadh and `issue_time` to the approval time; the number year, the open-period check (R28), VAT rate lookup when there is no supply date (R10) and the R71 warning use that date. An `issue_date` later than today (Asia/Riyadh) is never stored on an issued document (BR-KSA-04, XML v1.2 p.47; TIR §4.2.3, §4.5). Compliance report row 6b. **[svc `approve_sales_document`; test AT-78]**
79. **Resubmit to ZATCA.** A service (name provisional, e.g. `invoicing.services.resubmit_to_zatca(document, *, user)`) hands an `issued` document whose ZATCA hand-off failed after the issue transaction committed (`zatca_status = not_submitted`) to the port again. It changes no number, UUID, date, snapshot or amount, and refuses documents in any other state. Who may call it: OQ-57. **[svc; test AT-79]**
    *Amended 2026-10-17 (as built, interim default until the owner decides OQ-57):* built as `invoicing.services.resubmit_to_zatca(document, *, user)`. Allowed callers: (a) the ZATCA queue system user, with `zatca.process_queue` in the entity (automatic retry); (b) a human holding `zatca.retry_failed` **through a `zatca_admin` membership in that entity**. A human holding `zatca.retry_failed` through any other role (e.g. `accountant`, which the access catalogue seeds with it) is refused (`no_membership`, en/ar). Agents are always refused (`zatca.retry_failed` is not **Ag**). Every resubmission, including one whose port call then fails, writes an `AccessLog` row (`action = zatca_resubmit`, document) inside the locking transaction before the port is called; refusals are logged as `permission_denied`. **[svc; test AT-79, AT-87]**
    *Confirmed 2026-10-17 (D28; resolves OQ-57):* the interim default is the decision. An accountant or CFO who must resubmit is given the `zatca_admin` add-on membership in that entity (access R50).
80. **VATEX-SA-OOS reason.** A line with exemption reason `VATEX-SA-OOS` needs a free-text reason (`vat_exemption_text_ar` required, `_en` optional, §3.3) given by the taxpayer case by case (XML v1.2 §11.2.4 p.33; BR-KSA-83). Compliance report row 1c. **[svc at line save and issue; test AT-80]**
    *Amended 2026-10-17 (as built):* the fields are `exemption_text_ar` / `exemption_text_en` (§3.3), max 300 characters, stripped. `_ar` is required with `VATEX-SA-OOS` (English alone is refused: the text is visible on the invoice); either text with any other reason, or with no reason, is refused. The texts are carried in the VAT breakdown of category `O` (`exemption_texts`, one {en, ar} pair per distinct text). **[svc `_sales_line_values`, line problems, `vat_breakdown`; test AT-80]**
81. **NAT ID for EDU/HEA.** On a simplified document with a line whose exemption reason is `VATEX-SA-EDU` or `VATEX-SA-HEA`, the buyer's national ID (`NAT`, from `CustomerIdentifier`) is kept in the snapshot and is required, and the buyer name is required (BR-KSA-49 p.57; BR-KSA-25 p.52). This is the R50 exception. Compliance report row 1d. **[svc `_buyer_snapshot`, issue check; test AT-81]**
82. **Export to a VAT-registered buyer.** On an export document (`flag_export`) the buyer VAT number is removed first (BR-KSA-46), then the buyer ID is chosen by priority; a standard document whose snapshot has no buyer VAT number must carry another buyer ID (BR-KSA-81 p.61). So an export to a VAT-registered buyer needs another ID on the customer, or issue is refused. Compliance report row 4b. **[svc `_buyer_snapshot` + `_buyer_problems`; test AT-82]**
83. **VAT-registered individuals.** For the SAR 1,000 test of R48, a customer with `customer_kind = individual` and `is_vat_registered = True` counts as a business: a simplified document to them is allowed only below SAR 1,000. Compliance report row 8a; gross vs net stays OQ-44. **[svc `_simplified_problems`; test AT-83]**
84. **Late standard invoice warning.** When a standard invoice is issued more than 15 days after the end of the month of `supply_date`, the CFO sees a bilingual warning at approval; issue is not blocked (TIR §4.5 p.25; same pattern as R71). Compliance report row 6f. **[svc warning; test AT-84]**
85. **Residential rent exemption reason.** `IncomeType rent_residential` gets `default_exemption_reason = VATEX-SA-30` so its lines prefill the reason. **[Needs expert sign-off: tax]** (OQ-39, OQ-53); the user can still change it. Compliance report row 10c. **[reference data migration; test AT-85]**

### Added 2026-10-17 (pending fixes, being built; from the slice-1 code review)
86. **Personal names in admin labels.** The name of a customer or vendor with `customer_kind = individual` (and the buyer name in an individual's snapshot) never appears in an admin label (`__str__`, related-field choices, autocomplete, list columns, change-history text) unless the user holds `invoicing.view_personal_data` in that document's entity; otherwise a masked label (party code / document number) is shown. Business customers' names are not masked. Extends R51 to labels. **[admin + model labels; test AT-88]**
    *Amended 2026-10-17 (D24):* vendors now have `vendor_kind`; the same masking applies to vendors with `vendor_kind = individual` (R91). **[admin + `Vendor.__str__`; test AT-94]**
87. **Stale approvals by version counter.** Each sales document carries a version counter incremented by every service write to the document or its lines. An approval records the version it approved; a posting on clearance (access R42) or any later step that relies on that approval is refused when the stored version differs. Clock values (`modified_at` vs grant time) are no longer used for this test. Field name: builder's choice. **[svc + model; test AT-89]**
88. **Purification flag fixed at posting.** A purchase document's `requires_purification` is set in the transaction that posts it and never changes afterwards; on PostgreSQL the document lock trigger (migration after `invoicing/0007_document_lock_triggers`) refuses any later change, replacing the current "posted -> posted (its entry, set once, and the purification flag)" allowance. **[svc + DB trigger; test AT-90]**
89. **Exchange-rate row only from `build_lines`.** A document posting's journal lines may carry an `exchange_rate` (ledger `ExchangeRate` row) only as set by that document type's registered `build_lines` (the document's pinned `fx_exchange_rate`, R77). A line with an `exchange_rate` supplied any other way is refused with `pinned_rate_not_allowed`, for every registered document type (lines passed by the caller may not carry `exchange_rate` at all). **[ledger `_document_lines`; test AT-91]**
90. **Settlement helper not public.** The ledger helper that computes a document's FX settlement without a rights check (renamed `ledger.services._settlement_for_document`; it refuses outside a registered type's `build_lines` with `settlement_outside_posting` and refuses another entity's item with `settlement_other_entity`) is no longer a public service function; `fx_settlement_lines` keeps its `ledger.view` check: views, APIs and agents cannot call it, and it is used only from the posting paths that build settlement lines (R76). The exact internal call sites are the builder's choice and are checked in review. **[ledger services + access R1 public-function scan; test AT-92]**

### Added 2026-10-17 (project-owner decisions D24 to D30)
91. **Vendor kind (D24).** Every vendor has `vendor_kind` (`business`, `individual`, `government`). Individual vendors are personal data (R49): their records and bill snapshots are shown only to holders of `invoicing.view_personal_data` (each view logged, R51) and their names are masked in admin labels (R86). Applies in every entity. **[model + svc + admin; test AT-93, AT-94]**
92. **VAT-free bills from unregistered vendors (D24).** A bill or vendor credit note from a **resident** vendor whose document has no `vendor_vat_number` carries **0 VAT**: every line has VAT 0.00, the document `total_vat` is 0.00, and the entry has **no 1310 line** and no VAT added to cost (E8a). Such bills are allowed and expected (no warning). A vendor total VAT other than 0.00 on such a bill is refused at submit with a bilingual message (default, OQ-60). Which VAT category code the lines record: OQ-60 **[Needs expert sign-off: tax]**. Non-resident vendors keep R30 (reverse charge on services), as they have no KSA VAT number by nature. **[svc `_purchase_amounts` + submit check; test AT-95, AT-96]**
93. **3-decimal currencies (D27; replaces R74; default for the XML side in OQ-62).** Documents, receipts, payments and allocations may be in any active currency with `minor_units` 2 or 3 (KWD, BHD, OMR, JOD). Transaction amounts are stored with up to 3 dp and must not have more decimals than the currency allows (ledger-fx R8); line net, discounts and category VAT in the document currency are rounded half-up to the currency's `minor_units` (R9 method unchanged). SAR amounts (`total_vat_sar`, `vat_breakdown.vat_sar`, functional JE amounts) are rounded to 2 dp. Unit price keeps D19 (OQ-12) at the currency's decimals. Currencies with `minor_units` 0 (JPY) stay refused (OQ-58, default). ZATCA hand-off of 3-dp documents: OQ-62 **[SDK-confirm]**. **[svc + model fields + migration; test AT-98, AT-99]**
94. **Entity without a VAT number (D30b; resolves OQ-17, OQ-50).** An entity whose `vat_number` is empty charges no VAT: every sales line has VAT 0.00, no 2200 line is booked, S lines stay refused (R11). Its documents are never titled "Tax Invoice" / "فاتورة ضريبية" or "Simplified Tax Invoice" (R47). Its documents are not handed to ZATCA (zatca R-4: no profile without a VAT number). Until the title/type and the posting path are signed off (OQ-59 **[Needs expert sign-off: tax]**), such documents can be drafted but approval (issue) is refused with a bilingual message (default). **[svc `_issue_problems` + rendering; test AT-100]**
95. **Note tax date (D30a; replaces R71).** The date that counts for VAT on a credit or debit note is the note's own `issue_date` (set at approval, R78). A backdated event or contract effective date never moves the note's tax date or period, and no 15-day warning is computed from `adjustment_event_date` (information only). Backdated billing corrections are dated on the note date (billing R43, R45). **[svc; test AT-101]**
96. **Issued documents never return to draft (D25).** A sales document in `issued`, `final` or `rejected` never goes back to `draft` or `submitted`; only `submitted → draft` exists (R57). Corrections of an issued document are credit or debit notes (R17 to R19); a rejected one is replaced by a new document (R20). **[svc state machine + PostgreSQL lock trigger `invoicing/0007`; test AT-102]**

## 5. Accounting

Example amounts are SAR. Account codes come from `ledger/coa_template.py`. The income or expense account comes from each document line. The tables show one line for clarity.

*Amended 2026-10-17 (as built).* When each entry is booked:
| Event | Entry | How |
|---|---|---|
| Sales document approved (issued) | none | No draft JE (R41) |
| ZATCA `cleared` / `reported` / `*_with_warnings` | E1 to E4, E16, E17 | One step: `create_entry(post=True)`, `invoicing.post_on_clearance`, `posted_by` = approver, dated `issue_date` (R41) |
| ZATCA `rejected` | none | Nothing booked; number stays used (R42) |
| Bill / vendor credit note approved | E7 to E10 | One step at CFO approval (R58) |
| Receipt / refund posted by a second person | E5, E6 | One step (R33). *Amended 2026-10-17 (D26):* a receipt may be posted by its recorder; refunds by a second person |
| Vendor payment approved | E11 | One step; non-resident payments refused (R61 note) |
| Foreign-currency receipt, payment or credit note posted | plus settlement lines and 4700 / 5750 realised FX | At posting, not at allocation (R76) |
| Reversal of bill, receipt or payment | E12 | `reverse_entry` with `invoicing.reverse_document` |

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
| 4110 / 4210 / 4310 (per line) | | 10,000.00 | |
| 2200 VAT output | | 1,500.00 | |
VAT is charged on intercompany invoices (no VAT group, D11). 4310 is proposed (D18).

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
VAT on bank charges: none split (D19, OQ-23).

**E6. Customer refund**: Dr 1200 (or 1210 + counterparty) / Cr 1100.

**E7. Vendor bill, resident, recoverable VAT**
| Account | Dr | Cr | Counterparty |
|---|---|---|---|
| 5xxx expense or 15xx/1320 asset (per line) | 10,000.00 | | |
| 1310 VAT input | 1,500.00 | | |
| 2100 Trade payables (or 2110) | | 11,500.00 | (SUB) |

**E8. Vendor bill, non-recoverable VAT** (no vendor VAT number, or attribution `exempt`, or `mixed` by default; R29, OQ-9): Dr expense or asset 11,500.00 / Cr 2100 11,500.00. *Amended 2026-10-17 (D24):* "no vendor VAT number" is withdrawn from E8 (see E8a); E8 stays for `exempt` and `mixed` VAT charged by a registered vendor.

**E8a. Vendor bill from a resident vendor not registered for VAT** (added 2026-10-17, D24, R92; net 10,000.00, VAT 0.00)
| Account | Dr | Cr | Counterparty |
|---|---|---|---|
| 5xxx expense or 15xx asset (per line) | 10,000.00 | | |
| 2100 Trade payables (or 2110) | | 10,000.00 | (SUB) |
No 1310 line.

**E3a. Sales document of an entity without a VAT number** (added 2026-10-17, D30b, R94): Dr 1200 (or 1210 + counterparty) / Cr income per line; no 2200 line. When and how it posts: OQ-59.

**E9. Vendor bill, non-resident, standard-rated services, reverse charge, attribution `taxable`** (for `exempt`/`mixed` the 1310 debit goes to the expense line instead; goods: no RC lines, R30)
| Account | Dr | Cr |
|---|---|---|
| 5xxx expense (per line) | 10,000.00 | |
| 1310 VAT input | 1,500.00 | |
| 2100 Trade payables | | 10,000.00 |
| 2200 VAT output | | 1,500.00 |
Withholding tax is booked at payment (E20, E21).

**E10. Vendor credit note**: Dr 2100 (or 2110 + counterparty) / Cr expense or asset / Cr 1310 (mirror of E7).

**E11. Vendor payment**: Dr 2100 (or 2110 + counterparty) / Cr 1100. **Vendor refund received**: Dr 1100 / Cr 2100.

**E12. Reversal of a bill, receipt or payment**: `reverse_entry` swaps debit and credit on every line and keeps the counterparty. It is posted with its own JE number.

**E13. Allocation**: no journal entry.

**E14. Purification transfer** (R54; amount and approval per OQ-30; example 1,200.00)
| Account | Dr | Cr |
|---|---|---|
| 4900 Non-compliant income (to purify) | 1,200.00 | |
| 2600 Charity payable (purification) | | 1,200.00 |

**E15. Purification payout**
| Account | Dr | Cr |
|---|---|---|
| 2600 Charity payable (purification) | 1,200.00 | |
| 1100 Cash and bank | | 1,200.00 |

Both accounts exist in `ledger/coa_template.py` (4900 with `purify`, 2600). If a late-payment charge is ever built (OQ-15), it is Dr 1200 / Cr 2600, never income.

**E16. Prepayment invoice 386, final** (advance net 10,000.00, VAT 1,500.00; R65): Dr 1200 11,500.00 / Cr 2310 10,000.00 / Cr 2200 1,500.00. The receipt is E5, allocated to the 386.

**E17. Final invoice 388 deducting that prepayment** (total net 40,000.00, VAT 6,000.00; XML presentation per zatca.md R-39: BT-113 = 11,500.00, payable = 46,000.00 − 11,500.00 = 34,500.00, XML v1.2 §9.5; DG §8)
| Account | Dr | Cr |
|---|---|---|
| 1200 Trade receivables (balance due) | 34,500.00 | |
| 2310 Deferred income | 10,000.00 | |
| 4100 / 4200 / … (per line) | | 40,000.00 |
| 2200 VAT output (6,000.00 − 1,500.00 already declared) | | 4,500.00 |

**E18. Security deposit received** (R66, only once `deposit_vat_treatment` is set; VAT lines per that sign-off): Dr 1100 5,000.00 / Cr 2320 5,000.00. **E19. Deposit refund**: Dr 2320 / Cr 1100.

**E20. Vendor payment with WHT, vendor bears it** (bill 10,000.00, example rate 5%, e.g. technical and consulting services (WHT §3 p.28); R62)
| Account | Dr | Cr |
|---|---|---|
| 2100 Trade payables | 10,000.00 | |
| 1100 Cash and bank | | 9,500.00 |
| 2220 Withholding tax payable | | 500.00 |

**E21. Vendor payment with WHT gross-up** (R63; 10,000 × 5/95 = 526.32; gross-up method **[Needs expert sign-off: tax]**)
| Account | Dr | Cr |
|---|---|---|
| 2100 Trade payables | 10,000.00 | |
| 5920 Withholding tax borne (gross-up) | 526.32 | |
| 1100 Cash and bank | | 10,000.00 |
| 2220 Withholding tax payable | | 526.32 |

**E22. WHT remittance**: Dr 2220 / Cr 1100.

**E23. Foreign-currency documents**: every entry above is built in document currency with functional amounts at the document rate; realised FX differences on allocation and revaluation are per `ledger-fx.md` (accounts defined there). *Amended 2026-10-17:* realised FX is booked when the settling receipt, payment or credit note posts (4700 gain / 5750 loss), not on a later allocation (R76).

**Proposed accounts (D18; resolves OQ-22 at decision level; proposed, accountant to review before `coa_template.py` changes merge)**. *Note 2026-10-17 (D29):* still **proposed**; the accountant/CFO reviews them in a Word document being prepared. Codes already in `ledger/coa_template.py` (migration `ledger/0013_invoicing_income_accounts`) stay marked proposed until that review.
| Code | English | Arabic | Type | Flags | Used by |
|---|---|---|---|---|---|
| 1330 | Advances to vendors | دفعات مقدمة للموردين | asset | | OQ-40 |
| 2220 | Withholding tax payable | ضريبة الاستقطاع المستحقة | liability | | E20 to E22 |
| 2310 | Deferred income (customer advances) | إيرادات مؤجلة (دفعات مقدمة من العملاء) | liability | | E16, E17 |
| 2320 | Security deposits received | تأمينات مستلمة | liability | | E18, E19 |
| 4120 | Residential rental income (VAT-exempt) | إيرادات الإيجار السكني (معفاة) | income | | D16; 4100 used for commercial rent |
| 4220 | Management fee income | إيرادات رسوم الإدارة | income | | D16 |
| 4230 | Service charge income | إيرادات رسوم الخدمات المشتركة | income | | D16 |
| 4240 | Service fee income | إيرادات أتعاب الخدمات | income | | D16 |
| 4310 | Intercompany IP and licence income | إيرادات ملكية فكرية من شركات المجموعة | income | | E2 |
| 5910 | Rounding differences | فروقات التقريب | expense | | OQ-11 vendor-total differences, if allowed |
| 5920 | Withholding tax borne (gross-up) | ضريبة استقطاع محملة على الشركة | expense | | E21 |
Plus an `is_control` flag on 1200, 2100, 1210, 2110 (and 2310, 2320) to block manual journals. FX gain/loss codes are left to `ledger-fx.md`. Still not proposed (features not in scope): retention, ECL / bad debt, split reverse-charge VAT accounts. Intercompany variants of 4220 to 4240 for management fees between group entities use 4210.

## 6. Integrations

| System | Direction | What flows | Status |
|---|---|---|---|
| ZATCA (via `zatca` module only) | Out | Issued sales documents (all fields in 3.3) for clearance or reporting | Hand-off R40 to R46. XML, hash, QR and CSID belong to zatca.md |
| ZATCA (via `zatca`) | In | Status, ICV, PIH, hash, QR, cleared XML, warnings and errors | Written to the `zatca_*` fields |
| Banks | Out | Vendor payment file (format such as SARIE/WPS undecided) | Not in scope until OQ-21 |
| Banks | In | Statement lines matched to receipts and payments | README step 4 (bank module), not built here |
| GOSI / Mudad | none | Payroll is out of scope | n/a |
| SAP B1 / Daftra | In (one-off) | Open AR/AP items at cutover as `source=import` documents. No ZATCA resubmission. They must tie to the opening balances in 1200/2100 | migration-agent. Method in OQ-24 |
| Agent gateway | In | Draft bills from the "invoice reader" agent (`source=agent:invoice-reader`) | Drafts only (R22). If the agent's model runs outside KSA, bill attachments with personal data need the R53 transfer assessment |
| ZATCA VAT return | Out (report only) | Box totals per entity and period | Not built (D19, OQ-29) |
| ZATCA WHT return | Out (report only) | Monthly WHT data per entity (R64); filed manually | D14 |
| `billing` (billing.md) | In | Billing items from approved contracts (rent, annual car rental, secondments until HR ends them) → `create_from_billing` (R60); contract terms for advances/deposits (R65 to R67) | D17, D15 |
| `ledger-fx` (ledger-fx.md) | Both | Rates in; document-currency JEs and FX differences out | D13 |
| Hosting | n/a | DB, backups, logs in KSA | D12 |

## 7. Permissions

Business roles decided by D17: accounting staff create, the CFO approves, posting is automatic. Roles are granted **per entity** (a user who is AP Clerk in ALM and Viewer in SUB has exactly those rights in each). The `core` user × entity × role model is still to be built (OQ-25). "CFO" = the entity's approver role (OQ-42 for delegation).

| Action | AR Clerk | AP Clerk | Accountant | CFO (approver) | Viewer / Auditor | Billing system user | Agent service user |
|---|---|---|---|---|---|---|---|
| Create/edit customer or vendor | C (customer) | C (vendor) | Y | | | | |
| Create/edit/submit draft sales document | Y | | Y | | | | Draft only, if granted |
| Approve (= issue) sales invoice, debit note, credit note, 386 (R57) | | | | Y (not own) | | | No |
| Create sales document from billing item (R60) | | | | | | Y | No |
| Create draft CN/DN from billing adjustment (R73) | | | | | | Y | No |
| Review, complete and submit a billing correction draft (R73) | Y | | Y | | | No | No |
| Create/submit draft bill or vendor payment | | Y | Y | | | | Draft only (bills) |
| Approve (= post) bill or vendor payment (R58, R59) | | | | Y (not own) | | | No |
| Reverse bill, receipt or payment | | | Y | | | | No |
| Post receipt (R33) | Y | | Y | | | | No |
| Allocate / un-allocate | Y | Y | Y | | | | No |
| Maintain WHT categories, treaty certificates (verify) | | Y (enter) | Y (verify) | | | | No |
| Set `InvoicingSettings` (incl. `deposit_vat_treatment` after sign-off) | | | | Y | | | No |
| View documents and reports | Y | Y | Y | Y | Y | | Scoped |
| View/export individuals' personal data (R51) | Y (logged) | Y (vendor, logged) | Y (logged) | Y (logged) | Masked unless granted | | No |
| Post purification transfer / payout (R54) | | | Y | Y (payout) | | | No |

Segregation of duties (creator ≠ approver) is on by default and configurable per entity (D19, OQ-5).
*Amended 2026-10-16 (ledger build):* "Post receipt" is by a person other than the receipt's creator, submitter or last modifier (R33 note, OQ-49); the ledger enforces this for every document posting, also when `segregation_required = False` (access OQ-19).
*Amended 2026-10-17 (as built):* "Record receipt" needs `invoicing.allocate` and the user must also hold `invoicing.post_receipt` in that entity; posting is by another holder of `invoicing.post_receipt` (R33). "Post sales document on clearance" is done by the ZATCA queue system user (`zatca.process_queue` + `invoicing.post_on_clearance`), recording the CFO approver as `posted_by` (R41); no person posts sales documents directly. Vendor payments to non-resident vendors: nobody, until WHT is built (R61 note). Resubmit to ZATCA (R79): OQ-57.
*Amended 2026-10-17 (as built, interim default for OQ-57):* resubmit to ZATCA (R79) = the ZATCA queue system user (`zatca.process_queue`) or a `zatca_admin` of that entity (`zatca.retry_failed` through that role). Accountants hold `zatca.retry_failed` in the access catalogue but are refused by the service; agents never; every resubmission logged in `AccessLog`. Admin labels of individuals: masked without `invoicing.view_personal_data` (R86).
*Amended 2026-10-17 (D26, D28, D24):* "Post receipt" may be done by the person who recorded it (R33 note, access R46); customer refunds and all other documents keep a second person. Resubmit to ZATCA: `zatca_admin` holders (an add-on membership any accountant or CFO may be given) and the ZATCA queue system user (resolves OQ-57). Individual vendors' data: as individual customers (R91).

## 8. Acceptance tests

Fixtures: entities ALM (Sharia enforced, VAT number set) and SUB (child of ALM, Sharia enforced), each with the template chart and 2027 periods, made-up VAT/CR numbers and national addresses (never the D10 values), users per role. Customers and vendors are anonymised. All amounts are Decimal. From D17, "Issue" in AT-1 to AT-45 means CFO approval of a submitted document, and "Post" of a bill or payment means CFO approval.

| # | Given | When | Then |
|---|---|---|---|
| AT-1 | ALM draft invoice, 2 lines: 3 × 1,000.00 at S 15%, and 1 × 333.33 at S 15% | Issue, then ZATCA stub returns `cleared` | Category S taxable 3,333.33; category VAT = round(3,333.33 × 15%) = round(499.9995) = 500.00 (document level, half-up). Line VAT shown 450.00 and 50.00 (display only). Totals net 3,333.33, VAT 500.00, gross 3,833.33. JE posted: Dr 1200 3,833.33 / Cr 4100 3,333.33 / Cr 2200 500.00. Reference = invoice number. Status `final`. |
| AT-2 | Final invoice | Try to set status to `draft` or `issued` | Error. Status unchanged. |
| AT-3 | ALM invoice line uses SUB's account 4100 | Save line / issue | Rejected with "belongs to another entity". No number consumed. |
| AT-4 | User with rights on ALM only | List invoices, or create an invoice for a SUB customer | Sees only ALM documents. Create rejected. |
| AT-5 | Issued invoice | Edit a line's quantity, or delete the document | Rejected. |
| AT-6 | Two ALM invoices issued concurrently in 2027 | Both issue | Consecutive numbers, no gap or duplicate. A third issue that fails validation consumes no number. |
| AT-7 | Final invoice; the customer's address is then changed | Read the invoice | The invoice still shows the old address snapshot. |
| AT-8 | Standard draft invoice, customer (not VAT-registered) with no Arabic name / no address / no VAT number and no other ID | Issue | Rejected, listing each missing field. |
| AT-9 | Final invoice | Call cancel or delete; call `reverse_entry` through invoicing services | No cancel path exists. The invoicing service refuses. The only correction is a credit note. |
| AT-10 | Final invoice gross 11,500.00, open 11,500.00 | Credit note 4,600.00 (incl. VAT) cleared, then a second credit note of 7,000.00 | First: JE as E4 for 4,000/600, auto-allocated, invoice open 6,900.00, `partial`. Second rejected (exceeds 6,900.00 creditable). |
| AT-11 | Credit or debit note without reason, with an English-only reason, or referencing another customer's invoice | Issue | Rejected. With `reason_ar` only: accepted. |
| AT-12 | ALM (Sharia enforced) bill line on 5610 conventional interest | Save line / post | Rejected "Sharia-prohibited". Same bill in an entity with `sharia_enforced=False`: posts. |
| AT-13 | Invoice line on 4900 non-compliant income | Final | Posts. Document `requires_purification=True`. Appears on the purification report. |
| AT-14 | *Withdrawn — see Decision D13 (float case now AT-46; USD now AT-47).* | | |
| AT-15 | VatRate S 15% valid from 2020-07-01; line category E with no exemption reason | Save | Rejected. With a reason: VAT 0.00 and no 2200 line in the JE. |
| AT-16 | Entity with blank `vat_number`, invoice with an S line | Issue | Rejected. |
| AT-17 | Invoice dated in a closed period (2027-01 closed) | Issue | Rejected "Period … is closed". Document stays draft, no number used, no JE. |
| AT-18 | Issued standard invoice, ZATCA stub `pending` | Try to allocate a receipt, or post the JE | Allocation rejected. JE is still draft. After the stub returns `cleared_with_warnings`: JE posted, status `final`. *Amended 2026-10-17:* while `pending` the document has **no** JE (`journal_entry` empty); `post_cleared_document` is refused (`not_cleared`). After `cleared_with_warnings`: one posted JE, `posted_by` = the CFO approver, status `final`. |
| AT-19 | Issued invoice, ZATCA stub `rejected` | Callback | Status `rejected`. Draft JE deleted. Number not reused. Trial balance unchanged. *Amended 2026-10-17:* no JE ever exists for the document (none to delete). |
| AT-20 | ALM customer with `group_entity`=SUB | Invoice final | JE has 1210 with counterparty SUB. `intercompany_balances` shows (ALM, SUB). A customer with `group_entity`=ALM in ALM: rejected. |
| AT-21 | *Withdrawn — see Decision D17 (now AT-52).* | | |
| AT-22 | Bill created with `source=agent:invoice-reader` | Agent calls submit or post | Rejected. Stays draft. |
| AT-23 | Posted bill, vendor X, vendor number "A-17" | Second bill with the same vendor and number | Rejected as duplicate. Allowed after the first is reversed. |
| AT-24 | Posted bill in 2027-02 (now closed), no allocations | Reverse | `reverse_entry` called with a date in the open 2027-03. Bill `reversed`. Balances net to zero. A bill with an allocation: reversal rejected. |
| AT-25 | *Withdrawn 2026-10-17 — see Decision D24 (now AT-95).* Bill from a resident vendor with no VAT number, S line 1,000 + 150 | Post | E8: Dr expense 1,150.00 / Cr 2100 1,150.00. No 1310 line. |
| AT-26 | Bill from a non-resident vendor, services 10,000 S | Post | JE as E9. AP open 10,000.00. |
| AT-27 | *Withdrawn — see Decision D14 (now AT-54 to AT-58).* | | |
| AT-28 | Posted receipt with an allocation | Reverse | Rejected. After un-allocation: reversal posts, status `reversed`. |
| AT-29 | Receipt 5,000 for customer A; invoice for customer B; invoice of SUB | Allocate | Both rejected. Allocation above the open amount rejected. |
| AT-30 | Allocation row | Edit or delete | Rejected. Un-allocate creates a negative row. Open amounts are restored. |
| AT-31 | Fixture set of invoices, credit notes, receipts and bills in ALM and SUB | `reconcile_subledger(entity, as_of)` | The sub-ledger equals the TB for 1200, 2100, 1210 and 2110 per counterparty, for each entity separately. |
| AT-32 | Issued invoice in 2027-03 awaiting ZATCA | `close_period(2027-03)` | Rejected "Draft entries exist". *Amended 2026-10-17:* rejected with code `documents_pending`, message (en/ar) listing the invoice number; no draft JE is involved. After `cleared` and posting: the close succeeds. An issued invoice of SUB does not block ALM's period. |
| AT-33 | Any document, customer, vendor or VAT category | Render the label in the `ar` locale | Arabic text shown. Missing Arabic on a required Bi field is rejected at save. |
| AT-34 | New entity NEW added as data (chart + periods) | Run AT-1 for NEW | Passes with no code change. Numbers use the `NEW` prefix. |
| AT-35 | ALM invoice, 3 lines × net 10.03 at S 15% | Issue, stub `cleared` | Line VAT shown 1.50 each (sum 4.50). Category VAT = round(30.09 × 15%) = round(4.5135) = 4.51. Totals net 30.09, VAT 4.51, gross 34.60. JE Dr 1200 34.60 / Cr 4100 30.09 / Cr 2200 4.51 (R9). |
| AT-36 | Customer `is_vat_registered=True`, no VAT number, `other_id` CRN | Issue a standard invoice (or save the customer) | Rejected: VAT number required. Same customer with `is_vat_registered=False` and CRN: accepted (R16). |
| AT-37 | Standard and simplified final invoices; a credit note | Render PDF in `ar` and `en` | Titles "Tax Invoice"/"فاتورة ضريبية", "Simplified Tax Invoice"/"فاتورة ضريبية مبسطة", and the note's bilingual title (R47). |
| AT-38 | VAT-registered business customer | Issue as `simplified` with gross 1,000.00; then 999.99 | First rejected; second allowed (R48; gross vs net pending OQ-44). |
| AT-39 | Resident vendor with VAT number; bill lines each net 1,000 + VAT 150: `taxable`, `exempt`, `mixed`; no ratio approved | Post | Dr expense 1,000 + Dr 1310 150; Dr expense 1,150; Dr expense 1,150; Cr 2100 3,450.00 (R29). |
| AT-40 | Non-resident vendor, no VAT number: line A services S 10,000 `taxable`; line B goods 5,000 | Post | Line A as E9 (Dr 1310 1,500 / Cr 2200 1,500) despite no VAT number. Line B: Dr expense 5,000, no RC lines. AP 15,000.00 (R30). |
| AT-41 | *Withdrawn — see Decision D15 (now AT-59, AT-60).* | | |
| AT-42 | Simplified invoice to an individual; user enters `other_id` | Issue | Snapshot has no `other_id`; the field is not stored (R50). |
| AT-43 | Test setting with anonymisation job enabled; final simplified invoice to an individual with `retention_until` yesterday; another with it tomorrow | Run job | First: names, address, ID replaced by token; number, amounts, VAT, ZATCA hash unchanged; JE untouched. Second unchanged. Default settings: job disabled (R52). |
| AT-44 | Individual customer in ALM | Viewer opens it; accountant opens and exports it | Viewer sees masked data. Accountant sees it; one access-log row per view/export with user, entity, time (R51). |
| AT-45 | ALM final invoice with 1,200.00 on 4900 | Post purification transfer, then payout; agent user tries the same | E14 then E15 posted with references; 4900 and 2600 net to zero for that amount. Agent rejected (R54). |
| AT-46 | Line `unit_price` given as float `0.1` | Save | Rejected (R56). |
| AT-47 | ledger-fx built; ALM (SAR) invoice in USD, rate 3.75, net 1,000.00 S 15%; second USD invoice with no rate | Issue, stub `cleared` | First: totals USD 1,000.00 / 150.00 / 1,150.00; `total_vat_sar` 562.50; JE per ledger-fx with 2200 = SAR 562.50. Second rejected "rate required" (en/ar). Before ledger-fx is built: any non-SAR issue rejected (R56). |
| AT-48 | Entity with blank `building_no` or `street_ar` | Issue a standard invoice | Rejected, listing missing seller fields. Complete fields: seller snapshot holds the national address en/ar (R69, R15). |
| AT-49 | Repository fixtures and test data | Scan for `310960118600003`, `7023761708`, `4030419505` | None found; the setup-data file is not loaded by any test (R70). |
| AT-50 | ALM customer with `group_entity`=SUB (and one with `group_entity`=an equity-method associate ASC) | Issue S-rated invoice, stub `cleared` | Standard profile, VAT 15% charged, buyer snapshot = SUB's legal names/address/VAT, Dr 1210 counterparty SUB. Same for ASC. No record created in SUB or ASC (R68). |
| AT-51 | Sales draft created and submitted by staff user U | U approves; CFO C approves; stub `cleared` | U rejected (segregation). C: number assigned, status `issued`, `approved_by`=C; on clearance JE posted automatically, `final` (R57). |
| AT-52 | Bill submitted by U; second bill dated in closed 2027-01 | U approves; CFO approves both | U rejected. First: posted, JE as E7. Second: approval rolled back, stays `submitted`, no number used (R58). Same for a vendor payment (R59). |
| AT-53 | Billing item on a CFO-approved contract; item on an unapproved contract; agent user | `create_from_billing` each; call twice for the first; agent calls it | First: `source=system`, issued without per-invoice approval. Second: `submitted`, awaits CFO. Repeat call: no duplicate. Agent: rejected (R60). |
| AT-54 | Non-resident vendor, bill 10,000.00 category with rate 5% (fixture value), no treaty | CFO approves payment | E20; one `WhtDeduction` 500.00; bill `paid` (R62). |
| AT-55 | Same vendor with `wht_gross_up=True` | Pay | E21: cash 10,000.00, WHT 526.32 to 2220, 5920 526.32 (R63). |
| AT-56 | Verified treaty certificate 0% valid to 2027-06-30 | Pay on 2027-06-30, then on 2027-07-01 | First WHT 0.00 with certificate referenced; second at the statutory rate (R62). |
| AT-57 | Non-resident bill line with no `wht_category` | Pay | Rejected (R61). |
| AT-58 | Payments in 2027-03 with WHT in two categories | `wht_return_data(ALM, 2027-03)` | Rows per category/vendor; total equals 2220 credits for March; SUB data excluded (R64). |
| AT-59 | Contract with advance terms; 386 for net 10,000.00 S 15% cleared; final 388 net 40,000.00 deducting it | Issue both; try a second 388 deducting the same 386 | E16, then E17 (AR 34,500.00). Second deduction rejected (R65). |
| AT-60 | Contract with deposit terms; `deposit_vat_treatment` null; receipt `purpose=advance` with no contract | Post deposit receipt; set treatment, post again; post the advance receipt | First rejected (en/ar "pending tax adviser sign-off"); second E18; third rejected (R66, R67). |
| AT-61 | Customer not VAT-registered with `other_id_scheme` `TIN`; second customer holding both a TIN and a CRN | Issue a standard invoice to each | First accepted with scheme `TIN`. Second: snapshot uses `TIN` (first in priority order) (R16, §3.1). |
| AT-62 | Customers: business not VAT-registered; `government`; `individual` | Issue `simplified` with gross 1,000.00 to the first two; 5,000.00 to the individual; 999.99 to the business | First two rejected; individual accepted; 999.99 accepted (R48). |
| AT-63 | Credit note on a final invoice, `reason_code` picked, `payment_means_code` blank; then with code `30` | Issue | First rejected ("payment means code required", en/ar). Second accepted; `reason_ar`/`reason_en` prefilled from `NoteReason`; ZATCA hand-off carries the original's `number` as the billing reference (R18). |
| AT-64 | *Withdrawn 2026-10-17 — see Decision D30(a) (now AT-101).* Credit note with `adjustment_event_date` 2027-03-02 | CFO approves on 2027-04-15, then another on 2027-04-16 | First: no warning. Second: bilingual warning shown, issue proceeds (R71). |
| AT-65 | Standard invoice: SA buyer with blank district; non-SA buyer (country `AE`) with street, city, country only | Issue | First rejected listing district. Second accepted (R16). |
| AT-66 | *Added 2026-10-16 (D22).* Fixture entity with made-up CR `1010000001` and unified number `7000000001`; a second with unified number only; saving unified number `6000000001` or `70000A0001` | Issue a standard invoice for each, render PDF in `ar` and `en`; save | First: PDF shows both numbers with the bilingual labels of R72; XML seller ID `CRN` = `1010000001` only. Second: XML scheme `700`; PDF shows the unified number. Invalid unified numbers rejected (R72, zatca R-48). |
| AT-67 | *Added 2026-10-16 (D21).* ALM `auto_issue_from_approved_contract=True`; billing adjustment `credit` 2,000.00 on a final invoice | Billing system user calls `create_adjustment_from_billing` twice; it then calls submit; AR clerk U submits without, then with, `payment_means_code`; U approves; CFO approves; stub `cleared` | One draft credit note (`source=system`, `billing_adjustment` set). System submit rejected. U's first submit rejected (R18); second accepted. U's approval rejected; CFO's issues it; JE as E4 posted on clearance, dated issue date (R73, R57). |
| AT-68 | *Withdrawn 2026-10-17 — see Decision D27 (now AT-98, AT-99).* *Added 2026-10-16.* Draft invoice in KWD | Save | Rejected (R74, OQ-48 default) with en/ar message. |
| AT-69 | *Added 2026-10-16 (R24, R32 notes).* Posted bill B, no allocations; accountant A holds `invoicing.reverse_document` | A reverses B; tries to book B again; tries to book a new entry with the reversal grant; enters corrected bill B2 | Reversal posts, B `reversed`; re-booking B refused (`document_booked`); booking with the reversal grant refused; B2 posts with a new number |
| AT-70 | *Withdrawn 2026-10-17 — see Decision D26 (now AT-97).* *Added 2026-10-16 (R33 note, OQ-49).* Clerk K records receipt R in ALM; clerk L in ALM; ALM `segregation_required` True, then False | K posts R; L posts R | K refused (`self_approval`) in both settings; L posts R |
| AT-71 | *Added 2026-10-16 (R6, R54 notes).* ALM final invoice with 1,200.00 credit on 4900; a second such invoice later reversed | Read `purification_amount(ALM)`; post E14 for 1,200.00; read it again | 1,200.00 (reversed pair nets to 0); the reversal's lines carry the flags; E14's 4900 debit is not flagged, amount still 1,200.00; 2600 shows 1,200.00 transferred |
| AT-72 | *Added 2026-10-17 (R27 note, R41, R75).* ALM invoice approved by CFO C; ZATCA queue user Q; accountant A | Q calls `on_zatca_result(cleared)`; separately A calls `ledger.create_entry(post=True)` for the document with own lines; A calls `post_entry` on its entry; Q posts with `posted_by` = the creator | Q: one posted JE, lines built by the hook, `posted_by` = C, access-log row `post_on_clearance`. A's own lines refused (`document_builds_lines`); a call outside invoicing services refused (`post_through_services`); `post_entry` refused (`draft_has_links`); `posted_by` = creator refused (`posted_by_not_approver`) |
| AT-73 | *Added 2026-10-17 (R33 note).* User U with `invoicing.allocate` only; user K with `allocate` + `post_receipt`; user L with `post_receipt` | U records a receipt; K records it; K posts; L posts; K records a `purpose=advance` receipt | U refused; K's draft saved; K refused (`self_approval`); L posts E5; advance refused. *Amended 2026-10-17 (D26):* "K refused" becomes "K posts E5" (L then finds it posted); see AT-97 |
| AT-74 | *Added 2026-10-17 (R34 note, R76).* ALM (SAR) USD invoice final at 3.75; USD receipt with an application to it, posted when the rate is 3.76; second USD receipt with no application | Post the first; un-allocate its allocation; `allocate` the second receipt to the invoice; plan a SAR application on a USD receipt | First: settlement at carrying rate 3.75, realised FX gain on 4700, allocation `via_settlement`. Un-allocation refused (`unallocate_settled`). Later allocation refused (foreign currency). SAR application refused (cross-currency) |
| AT-75 | *Added 2026-10-17 (R61 note).* Non-resident vendor N; resident vendor V | Create payment to N; create refund received from N; payment to V | First refused (`wht_not_built`, en/ar); refund and V payment allowed |
| AT-76 | *Added 2026-10-17 (R77, pending fix).* USD invoice approved at 3.75; rate table for that day corrected to 3.76 before clearance | Stub `cleared` | JE booked at 3.75 (the pinned rate); no failure, no return to draft |
| AT-77 | *Added 2026-10-17 (R9 note).* Submitted invoice with `document_discount` 100.00 | CFO approves | Refused (en/ar "discount the lines instead"); no number used |
| AT-78 | *Added 2026-10-17 (R78, pending fix).* Draft with requested `issue_date` 3 days in the future; another 5 days in the past; approval on D (Asia/Riyadh) | CFO approves each | Both issued with `issue_date` = D; number year and period from D; no issued document has a date after D |
| AT-79 | *Added 2026-10-17 (R79, pending fix).* Issued invoice with `zatca_status = not_submitted` (port raised after commit); a `final` invoice | Resubmit each | First handed to the port, number/UUID/date unchanged, `pending`. Second refused |
| AT-80 | *Added 2026-10-17 (R80, pending fix).* Line with reason `VATEX-SA-OOS`, no free text; then with `vat_exemption_text_ar` | Save / issue | First refused (en/ar); second accepted, text kept on the line. *Amended 2026-10-17 (as built):* fields are `exemption_text_ar` / `_en`; also: OOS with English text only refused; an `exemption_text_ar` on a `VATEX-SA-30` line refused; the issued document's category `O` breakdown has `exemption_texts` = [{en, ar}] |
| AT-81 | *Added 2026-10-17 (R81, pending fix).* Simplified invoice to an individual, line reason `VATEX-SA-EDU`; customer with no `NAT` ID; then with one | Issue | First refused; second: snapshot keeps scheme `NAT` and its value. Same document with a non-EDU/HEA reason: no buyer ID stored (R50) |
| AT-82 | *Added 2026-10-17 (R82, pending fix).* Export invoice to a VAT-registered customer with no other ID; then with a `CRN` identifier | Issue | First refused (buyer ID required); second: no buyer VAT number, buyer ID `CRN` |
| AT-83 | *Added 2026-10-17 (R83, pending fix).* Customer `individual`, `is_vat_registered=True` | Issue simplified at gross 1,000.00; then 999.99 | First refused; second allowed |
| AT-84 | *Added 2026-10-17 (R84, pending fix).* Standard invoice with `supply_date` 2027-03-10 | Approve on 2027-04-15; another on 2027-04-16 | First: no warning. Second: bilingual warning, issue proceeds |
| AT-85 | *Added 2026-10-17 (R85, pending fix).* New line with income type `rent_residential` | Add the line | Category `E`, reason prefilled `VATEX-SA-30`; the user may change it |
| AT-86 | *Added 2026-10-17 (R48 note, OQ-52 interim default).* Simplified invoice of SAR 900 to a business customer, issued; credit note and debit note on it of SAR 1,200 gross each | Approve each note | Both issued as simplified (not tested against SAR 1,000). A simplified *invoice* of SAR 1,200 to the same customer is still refused |
| AT-87 | *Added 2026-10-17 (R79 note, OQ-57 interim default).* Issued invoice with `zatca_status = not_submitted` in ALM; users: queue system user, `zatca_admin` in ALM, `zatca_admin` in SUB only, `accountant` in ALM (holds `zatca.retry_failed`), agent user | Each calls `resubmit_to_zatca`; then the ALM `zatca_admin` resubmits with a port that raises | Queue user and ALM `zatca_admin` allowed; SUB-only `zatca_admin`, accountant (`no_membership`) and agent refused with `permission_denied` rows; each allowed call, including the one whose port raised, leaves an `AccessLog` row with `action = zatca_resubmit` |
| AT-88 | *Added 2026-10-17 (R86, pending fix).* Individual customer "Test Person"; business customer; user V without `invoicing.view_personal_data` in ALM, user P with it | V and P open the admin lists and choice fields that show customers and sales documents | V never sees "Test Person" (masked label); P sees it, view logged; the business customer's name is shown to both |
| AT-89 | *Added 2026-10-17 (R87, pending fix).* Submitted invoice approved by CFO at version n; then (fixture) a line is changed so the version becomes n+1 with `modified_at` set earlier than the approval time | Queue posts on clearance with `posted_by` = that CFO | Refused (stale approval) despite the clock order; after re-approval at n+1 it posts |
| AT-90 | *Added 2026-10-17 (R88, pending fix; PostgreSQL).* Posted bill with `requires_purification = True` | Change the flag through the service, `QuerySet.update` and raw SQL | All refused; flag unchanged |
| AT-91 | *Added 2026-10-17 (R89, pending fix).* Foreign-currency bill approved at a pinned rate row R | Post with `build_lines` output; post with lines that carry another `exchange_rate` row, or R supplied by the caller | First posts; the others refused |
| AT-92 | *Added 2026-10-17 (R90, pending fix).* Public-function scan (access AT-26) | Run the scan; call the settlement helper from a view/API test client and from an agent user | Helper not in the public API; external calls impossible or refused |
| AT-93 | *Added 2026-10-17 (D24, R91).* ALM and SUB | Create a vendor without `vendor_kind`; with `individual`; migrate an existing vendor row | First refused (required); second saved; migrated row `business`. Same in SUB with no code change |
| AT-94 | *Added 2026-10-17 (R86, R91).* Individual vendor "Test Supplier"; business vendor; user V without `invoicing.view_personal_data`, user P with it | V and P open the vendor and bill admin lists, choice fields and a bill | V sees masked labels (vendor code / bill number) for the individual only; P sees the name, view logged |
| AT-95 | *Added 2026-10-17 (D24, R92; replaces AT-25).* Resident vendor with no VAT number; bill line net 1,000.00, vendor totals VAT 0.00, gross 1,000.00 | Submit, CFO approves | Posts E8a: Dr expense 1,000.00 / Cr 2100 1,000.00; no 1310 line; `total_vat` 0.00; no warning |
| AT-96 | *Added 2026-10-17 (R92, OQ-60 default).* Same vendor; bill with vendor total VAT 150.00 | Submit | Refused with en/ar message (unregistered vendor cannot charge VAT); a non-resident services bill (no VAT number) still reverse-charges as E9 |
| AT-97 | *Added 2026-10-17 (D26; replaces AT-70).* Clerk K records receipt R and refund F in ALM; `segregation_required` True, then False | K posts R; K posts F; clerk L posts F | R posts E5 with `posted_by` = K (both settings); K's refund refused (`self_approval`); L posts F (E6) |
| AT-98 | *Added 2026-10-17 (D27, R93).* ALM (SAR); fixture KWD→SAR 12.2000; KWD invoice: 3 × 333.333 at S 15% | Approve; stub `cleared` | Net KWD 999.999; category VAT round_half_up(999.999 × 15%, 3) = 150.000; gross 1,149.999; `total_vat_sar` = round(150.000 × 12.2000, 2) = 1,830.00; JE functional amounts 2 dp; txn amounts read back with 3 dp |
| AT-99 | *Added 2026-10-17 (R93).* Lines: KWD unit price 1.2345; USD 1.005; JPY invoice; BHD receipt 10.125 allocated to a BHD invoice | Save / post | KWD 4 dp and USD 3 dp refused (en/ar, naming the allowed decimals); JPY refused (OQ-58 default); BHD receipt posts and settles 10.125 |
| AT-100 | *Added 2026-10-17 (D30b, R94).* Entity NOV with empty `vat_number`; draft invoice with one E line and one Z line; another with an S line | Save; submit; CFO approves; render | Lines VAT 0.00; S line refused (R11); approval refused (OQ-59 default, en/ar); no "Tax Invoice" / "فاتورة ضريبية" title in any rendering; nothing handed to ZATCA |
| AT-101 | *Added 2026-10-17 (D30a, R95; replaces AT-64).* Billing correction credit note for a supply in 2027-01 (contract effective date 2027-01-15), approved on 2027-04-16; manual note with `adjustment_event_date` 2027-03-02 approved on 2027-04-16 | Approve each | Both dated 2027-04-16, VAT in the period of 2027-04-16; no 15-day warning for either; supply period of the first stays 2027-01 |
| AT-102 | *Added 2026-10-17 (D25, R96).* Issued, final and rejected documents | Call `return_to_draft` on each; on PostgreSQL raw UPDATE `status='draft'` on each | All refused (en/ar `not_submitted`; trigger error); documents unchanged |

## 9. Open questions

**BLOCKING** means the builder cannot build that part without an answer. **Default** means a safe default exists that the builder may use (shown in the rules) and that is easy to change later.

| # | Question | Owner | Status |
|---|---|---|---|
| OQ-1 | Where do seller addresses (street, building no., additional no., district, city, postal code) live: new fields on `core.Entity` or a zatca config? `Entity` has none today. | Dev + zatca.md | **Resolved by D10**: fields on `core.Entity` (§3.0). |
| OQ-2 | Exact zatca service names, the callback contract, and which model owns ICV/PIH/hash/QR (invoice fields vs `zatca.Submission`). | zatca.md author | **BLOCKING** for R40 to R45. Build against a stub interface. |
| OQ-3 | Payment terms: standard terms per customer/vendor, due-date rules, early-payment discounts. | Accountant | Resolved by D19. |
| OQ-4 | Document number format and prefixes (INV, CN, DN, BILL, VCN, RCPT, PAY), and whether CN/DN share the invoice sequence. ZATCA requires uniqueness. | Accountant | Resolved by D19. Still open: whether 386 (`PRE`) may have its own sequence **[Needs expert sign-off: tax]** (not covered by the 2026-10-16 verification); default separate. |
| OQ-5 | Approval thresholds for bills, payments and invoices (amounts, who approves, the Managing Partner's limit). Is segregation of duties mandatory for small entities? | Managing Partner | Resolved by D19 and D17 (approver = CFO). Amount thresholds: none. |
| OQ-6 | Multi-currency: foreign-currency invoices and bills, FX rate source, realised/unrealised FX. `JournalLine` has no currency field and the template has no FX account. | Accountant + Dev | **Resolved by D13** (in scope). Rates, FX accounts and JE currency are owned by `ledger-fx.md`; non-SAR issue/posting **BLOCKED until ledger-fx.md is approved and built** (R56). For when it is unblocked (verification I14): tax must be stated in SAR, converted at the SAMA daily rate on the date tax becomes due (TIR §4.2.2 p.17); the XML shape is zatca.md R-40. |
| OQ-7 | Do credit notes (and debit notes) need approval, and above what amount? | Managing Partner | Resolved by D17: CFO approval, no threshold. |
| OQ-8 | Withholding tax on payments to non-resident vendors. **In scope by D14** (R61 to R64, E20 to E22, 2220/5920 proposed). Reference rate list for `WhtCategory` (non-residents; WHT §3 p.28, p.31, verification I18): management fees 20%; royalties 15%; dividends 5%; rent 5%; loan charges 5%; insurance/reinsurance 5%; technical and consulting services 5%; air tickets / air or sea freight 5%; international telecom 5%; land transport within KSA 15%; any other payments 15%. Payment due by the 10th of the following month; late payment 1% per 30 days; annual beneficiary list required. WHT payment records kept at least 10 years after payment (WHT p.31; R52). Sharia note: "loan charges" must never arise in a Sharia-enforcing entity (R5, R7). Still open: recognition at payment vs at bill; gross-up per vendor or per contract and its method; treaty relief and certificate evidence; related-party variants **[Needs expert sign-off: tax]**. | Accountant / tax adviser | Buildable now with `WhtCategory` as data (load the list above as fixture-independent reference data). **BLOCKING for go-live** of non-resident payments until rates/categories are signed off. |
| OQ-9 | Input VAT recovery per Art. 51 (source not downloaded on 2026-10-16; unverified): which entities make exempt supplies (e.g. residential rent), how `mixed` lines are apportioned (`VatRecoveryRatio` per entity per year, provisional vs final), and the annual true-up method and account. **[Needs expert sign-off: tax]**. | Accountant / tax adviser | Default resolved by D19 (R29). Exempt supplies now known (D16: residential rent, via `IncomeType.art51_class`). Ratio method and true-up account still need tax adviser sign-off. |
| OQ-10 | Duplicate-bill rule and whether a vendor credit note must reference an original bill. | Accountant | Resolved by D19. |
| OQ-11 | Rounding policy (per line vs per document, half-up vs half-even) and how to absorb a 0.01 difference against vendor totals. There is no rounding account. | Accountant | Sales: decided by compliance input, not open (R9: document-level VAT per category, ROUND_HALF_UP, confirmed by XML v1.2 §10 p.29 and TIR §4.2.4 p.21). No line-vs-header tolerance is published by ZATCA, so none is applied; line VAT is still emitted (KSA-11/12). SDK 2.0.3 tolerates a category VAT difference < 1.00; our exact check is a house rule (R9; zatca.md R-31). Resolved by D19 (half-up; bills match exactly; 5910 proposed if a tolerance is ever allowed). |
| OQ-12 | Unit price precision beyond 2 dp (e.g. hourly rates). | Accountant | Resolved by D19 (2 dp). Note (verification I8): ZATCA sets no limit on unit-price decimals (only line net is 2 dp), so a later change to more dp has no ZATCA blocker. |
| OQ-13 | Are any group entities in a KSA VAT group (no VAT on intra-group supplies)? | Accountant / tax adviser | **Resolved by D11**: no VAT group; VAT charged on intercompany invoices (R68). |
| OQ-14 | Shared party master across entities (one customer for ALM and SUB) or per entity. | Managing Partner | Resolved by D19. |
| OQ-15 | Late payment: are any late charges contracted, and what is their VAT treatment? Sharia allows no penalty income. If built later: Dr 1200 / Cr 2600, never income. **[Needs expert sign-off: Sharia]** (no ZATCA document addresses it). | Managing Partner + Sharia board | Default resolved by D19; Sharia board sign-off still open if a feature is ever requested. |
| OQ-16 | Customer advances and deposits. **In scope by D15** (R65 to R67, E16 to E19). 386 VAT timing confirmed (VAT due on receipt, tax invoice within 15 days from the end of the month; TIR §4.5.1 p.26; R65) and 388 deduction presentation per zatca.md R-39. Still open: deposit VAT treatment at receipt, on application to unpaid amounts and on forfeiture **[Needs expert sign-off: tax]**. | Accountant / tax adviser | Advances buildable now. Deposits: build; posting blocked per entity until `deposit_vat_treatment` is set after sign-off (R66). Application/forfeiture not built. |
| OQ-17 | Can a non-VAT-registered group entity issue invoices, and in what form? | Accountant | Resolved by D19. *Note 2026-10-17:* reopened for the tax adviser as OQ-50. **Resolved by D30(b) (2026-10-17):** no VAT, not titled "Tax Invoice" (R94); title/type and posting path: OQ-59. |
| OQ-18 | Must every sales line description be in Arabic (ZATCA/VAT regulations), or is English-only acceptable on B2B? | compliance-checker | Resolved by D19. **Closed 2026-10-16** (verification I15): every visible field, line descriptions and notes included, must be in Arabic on all documents, B2B included; bilingual allowed; Latin script allowed for proper names (DTG p.79; DG p.62; TIR §4.2.1). D19's default stands; `notes_ar` now required when `notes_en` is set. |
| OQ-19 | Which entities issue simplified (B2C) invoices, e.g. rentals to individuals? | Managing Partner | Resolved by D19. |
| OQ-20 | Bank accounts: one sub-account per bank or IBAN under 1100? | Accountant | Resolved by D19. |
| OQ-21 | Bank payment file format and bank (SARIE, bulk upload, H2H). | Accountant | Resolved by D19: not in scope. |
| OQ-22 | Missing accounts. | Accountant | **Resolved by D18** at decision level: proposed codes in §5, accountant to review before the `coa_template.py` change merges. |
| OQ-23 | VAT on bank charges deducted from receipts. | Accountant | Resolved by D19. |
| OQ-24 | Migration: how open Daftra/SAP B1 AR/AP items are loaded (per-document vs per-party balance), and whether their ZATCA data (hash chain) carries over. | Accountant + migration-agent | Resolved by D19. |
| OQ-25 | Entity-scoped role/permission model (user × entity × role) is missing from `core`. Role names and who holds them. | Managing Partner + Dev | **Resolved by D17** at business level (staff create, CFO approves, per entity). The `core` user × entity × role model still needs a spec: **BLOCKING** for views/APIs; services can be built now. |
| OQ-26 | Retention on contracts (percentage held, release terms). | Managing Partner | Resolved by D19: not built. |
| OQ-27 | Bad-debt write-off and ECL policy. | Accountant | Resolved by D19: not built. |
| OQ-28 | Ledger protection: should `ledger.services.reverse_entry` refuse entries owned by a sub-ledger (needs a `source_module` field on `JournalEntry`), so a final invoice can't be reversed outside invoicing? | Dev + code-reviewer | Resolved by D19. |
| OQ-29 | VAT-return box mapping per line type **[Needs expert sign-off: tax]** (not in the downloaded docs); how customs import VAT on goods is recorded. | Accountant / tax adviser | Resolved by D19: no VAT-return report for now. |
| OQ-30 | Purification: who decides the amount and timing, approval before payout, recipient charities. | Managing Partner + Sharia board | Default resolved by D19; amount/charity choice still open **[Needs expert sign-off: Sharia]**. |
| OQ-31 | PDPL: offshore processors (hosting, backup, e-mail, OCR, AI models) and the transfer assessment for each; privacy notice text en/ar; data-subject request handling. **[Needs expert sign-off: legal]**. | Managing Partner + legal | Hosting in KSA decided (D12); for invoice records this is also a VAT requirement (TIR §8.3.1 p.41; zatca.md OQ-20). Default "no offshore processor receives personal data" stands. PDPL sources were not downloaded on 2026-10-16 **[Needs expert sign-off: legal]**. **BLOCKING** for go-live with real personal data until legal sign-off. |
| OQ-32 | Can an entity issue S-rated lines dated before `vat_effective_date` **[Needs expert sign-off: tax]** (not covered by the 2026-10-16 verification)? | Accountant | Default: rejected, as R11. |
| OQ-33 | Which seller ID goes on the invoice: branch CR (`CRN`) or unified national number (`700`)? | compliance-checker | **Answered 2026-10-16** (verification Z2): BR-KSA-08 emits one ID in the order CRN, MOM, MLS, 700, SAG, OTH (XML v1.2 p.48–49), so `CRN` = `Entity.cr_number` whenever present (zatca.md OQ-24). **Resolved by D22**: XML `CRN`; human-readable invoice shows CR and unified number, bilingual labels (R72). |
| OQ-34 | *Withdrawn — see Decision D11* (each subsidiary has its own VAT number and EGS unit(s): confirmed by the project owner). | | Resolved by D11. |
| OQ-35 | Entities with more than one branch (CR per branch, EGS per branch): is a `Branch` model needed? | Managing Partner | Default: one branch per entity, CR on `Entity`. |
| OQ-36 | Do auto-generated invoices from an approved contract need per-invoice CFO approval? | Managing Partner / CFO | **Resolved by D20**: no (R60); per-entity switch `auto_issue_from_approved_contract` kept, default True. Billing correction notes always need CFO approval (D21, R73). |
| OQ-37 | Should an intercompany sales invoice create a mirror draft bill in the buyer entity? | Accountant | **Resolved by D23**: no; buyer's staff enter the bill; `intercompany_balances` reconciles (R68). |
| OQ-38 | Consolidation treatment of intercompany balances with associates/minority holdings (not fully eliminated). | Accountant | Out of invoicing scope; consolidation spec. Default here: same 1210/2110 + counterparty. |
| OQ-39 | Exemption reason code for residential rent and VAT category per `IncomeType` **[Needs expert sign-off: tax]** (not covered by the 2026-10-16 verification). | compliance-checker | Default: residential rent `E`, others `S`. *Note 2026-10-17:* default reason VATEX-SA-30 (R85); document type OQ-53. |
| OQ-40 | Vendor-side advances and deposits paid by group entities: in scope? | Accountant | Default: not built; 1330 proposed. |
| OQ-41 | Do customer receipts need CFO approval? | CFO | Default: R33, posted at once by accounting staff. *Note 2026-10-16:* as built, posted at once by a second person, not the creator (R33 note); see OQ-49. **Resolved by D26 (2026-10-17):** no CFO approval; the recorder may post (R33). |
| OQ-42 | Who approves when the CFO is the creator or absent (delegate per entity)? | Managing Partner | Default: a named delegate approver per entity; Managing Partner otherwise. |
| OQ-43 | *Added 2026-10-16 (verification I6).* May one credit/debit note adjust several invoices of the same customer? VAT allows it ("one single credit or debit note reflecting adjustments to multiple tax invoices issued to the same customer", TIR §7.2 p.39; BT-25 is 1..n and may be a range, DG p.19, 62). Business decision; shared with zatca.md OQ-27. | Accountant / CFO | Default: one original invoice per note (single `original_document` FK, R18, R19). |
| OQ-44 | *Added 2026-10-16 (verification I11).* R48 threshold: is SAR 1,000 measured gross or net of VAT, and is the boundary "less than 1,000" (TIR) or "exceeding 1,000" (one passage of the guidance)? **[Needs expert sign-off: tax]** | Tax adviser | Default: gross in SAR; 1,000.00 and above must be standard. |
| OQ-45 | *Added 2026-10-16 (verification I3).* Keep the 4-digit `building_no` check for SA buyers as a house rule, or relax it (ZATCA requires 4 digits for the seller only)? | Accountant | Default: keep the house rule. |
| OQ-46 | *Added 2026-10-16 (verification I16).* Movable capital assets: which sales documents get `retention_class=movable_capital_asset`, the Art. 52 adjustment period, and where the acquisition date comes from. **[Needs expert sign-off: tax]** Shared with zatca.md OQ-31. | Accountant / tax adviser | Default: set only by an explicit flag on the document (no automatic trigger); no anonymisation or purge job runs (R52). |
| OQ-47 | *Added 2026-10-16 (verification I6).* 15-day note deadline (R71): is a warning enough or should late notes be blocked; should `adjustment_event_date` be mandatory on CN/DN? | Accountant / CFO | Default: optional field, warning only. Backdated billing corrections (D21) will often trigger the warning; event date choice is billing OQ-25. **Resolved by D30(a) (2026-10-17):** the note's own date counts; R71 withdrawn, R95; `adjustment_event_date` information only. |
| OQ-48 | *Added 2026-10-16 (CLAUDE.md rule 7 amended).* Documents in 3-decimal (KWD, BHD, OMR, JOD) or 0-decimal (JPY) currencies: document amounts are `Decimal(18,2)`, and EN 16931 decimal rules in ZATCA XML may limit document amounts to 2 decimals **[SDK-confirm]**. Store document amounts with the currency's decimals, or block such currencies for sales/purchase documents? | Accountant + Dev + compliance-checker | Default: rejected (R74). **Resolved by D27 (2026-10-17)** for 3-decimal currencies: supported (R93). Remaining: 0-decimal currencies (OQ-58) and ZATCA XML decimals (OQ-62). |
| OQ-49 | *Added 2026-10-16 (ledger build).* With segregation enforced by the ledger, the creator of a customer receipt cannot post it (R33 note). This conflicts with the earlier wording "receipts post at once by accounting staff" (R33, OQ-41 default, §7). Is a second person required to post every receipt, also in small entities, or should the receipt's creator be allowed to post it? Linked: access OQ-19. | Project owner (Managing Partner / CFO) | Default: a second person posts (as built). *Note 2026-10-17:* default applied in slice 1 (R33 note, AT-73). **Resolved by D26 (2026-10-17):** the recorder may post a receipt; no second person (R33, access R46). |
| OQ-50 | *Added 2026-10-17 (compliance report row 6a).* BR-KSA-39 (XML v1.2 p.56) requires the seller VAT number on every invoice, and TIR §4.1.3 p.16 says a person not registered for VAT may not issue a tax invoice. As built (D19 / OQ-17 default), an entity without a VAT number may issue Z/E/O-only documents. Should any sales document be refused when the entity has no VAT number? This would reverse D19 for OQ-17. **[Needs expert sign-off: tax]** | Tax adviser, then project owner | Default: D19 stands (Z/E/O allowed) until sign-off. **Resolved by D30(b) (2026-10-17):** an entity without a VAT number issues documents without VAT and never titled "Tax Invoice" (R94); the title/type stays open as OQ-59. |
| OQ-51 | *Added 2026-10-17 (compliance report row 7).* SAR VAT conversion: must the rate be the SAMA rate (`source=sama`) rather than the ledger's preference order (manual, then file, then SAMA)? Which date: the tax point (earliest of supply, invoice and receipt; receipt date for advances), not `issue_date`? Same question for the line VAT rate (supply date used even when the issue date is earlier) and for reverse-charge VAT on bills (`accounting_date`). Stale-rate policy: reject rates older than the ledger's 5-day limit, or record why one was used? **[Needs expert sign-off: tax]** | Tax adviser | Default: as built (ledger rate table on `issue_date`; 5-day maximum age). |
| OQ-52 | *Added 2026-10-17 (compliance report row 8).* (a) SAR 1,000 test gross or net of VAT: still OQ-44. (b) A credit or debit note keeps its original's profile; a note of SAR 1,000 or more to a non-individual whose original was simplified can therefore never be issued. May such a note stay simplified, or must it be standard? **[Needs expert sign-off: tax]** | Tax adviser | Default: as built (refused). VAT-registered individuals: R83. *Amended 2026-10-17 (interim default as built):* notes are **not** refused: credit and debit notes are not tested against the threshold, because a note "is subject to the same issuing requirements as the type of invoice on which it is based" (XML v1.2 §5.2), so a note of SAR 1,000 or more on a simplified invoice stays simplified (R48 note, AT-86). Tax sign-off still pending. |
| OQ-53 | *Added 2026-10-17 (compliance report row 10c).* Residential rent: is VATEX-SA-30 the right default reason (R85), and should an exempt-only residential rent document be issued as a "Tax Invoice" at all (TIR p.14 note 4: exempt real-estate supplies need no tax invoice)? Also whether rent documents count as real-estate capital asset records for 15-year retention. Linked: OQ-39. **[Needs expert sign-off: tax]** | Tax adviser | Default: issued as a tax invoice, reason VATEX-SA-30 prefilled, 15-year retention (as built). |
| OQ-54 | *Added 2026-10-17 (compliance report row 5).* Historical VAT rates: confirm 5% from 2018-01-01 to 2020-06-30 and 15% from 2020-07-01 from the ZATCA rate-change notice, and whether transitional rules for contracts signed before the change must be modelled (relevant to migrated history, not to Almiras from 2024-04-01). **[Needs expert sign-off: tax]** | Tax adviser | Default: rates as seeded; transitional rules not modelled. |
| OQ-55 | *Added 2026-10-17 (as built).* Document-level discount: how is it split across VAT categories and income accounts (pro rata to line net, or entered per category), and how is it shown in ZATCA XML (BT-92 per category)? | Accountant + compliance-checker | Default: refused at issue (R9 note); discount the lines. |
| OQ-56 | *Added 2026-10-17 (as built; known limitation).* An unapplied foreign-currency balance on a posted receipt or payment cannot be allocated later (R76 (d)); the receipt must be reversed and re-entered with applications. Is a later settlement entry (FX settlement JE at allocation) needed? Cross-currency receipts and payments are also not built. | Accountant + Dev | Default: as built (refused). |
| OQ-57 | *Added 2026-10-17 (R79).* Who may resubmit a document whose ZATCA hand-off failed after commit (the approving CFO, an accountant, the ZATCA queue system user), and is it automatic? Linked: OQ-2. | Project owner + zatca.md | Open; no default chosen. *Amended 2026-10-17 (interim default as built; owner decision pending):* the ZATCA queue system user (automatic retry, `zatca.process_queue`) and humans holding `zatca_admin` in that entity (`zatca.retry_failed` through that role); accountants refused although the catalogue gives them `zatca.retry_failed` (access §3.6 note); agents never; every resubmission, including failed ones, logged in `AccessLog` (R79 note, AT-87). Still open: whether the approving CFO may resubmit, and when the automatic retry runs. **Resolved by D28 (2026-10-17):** `zatca_admin` holders and the ZATCA queue system user; a CFO resubmits only if given the `zatca_admin` add-on. Automatic retry timing follows zatca R-24 (zatca spec). |
| OQ-58 | *Added 2026-10-17 (D27).* 0-decimal currencies (JPY): may documents be issued in them? | Accountant / CFO | Default: refused (R93). |
| OQ-59 | *Added 2026-10-17 (D30b).* Entity without a VAT number: which document title and type does it issue (it must not be "Tax Invoice"; ZATCA ties tax invoices to VAT registration, TIR §4.1.3 p.16; BR-KSA-39), which VAT category code its lines record (O, or none), is it ever handed to ZATCA (zatca R-4 says no profile), and when does it post (sales documents post only on ZATCA clearance today, R41)? **[Needs expert sign-off: tax]** | Tax adviser / CFO (Word document) | Default: drafts allowed; approval refused until answered (R94). **BLOCKING** for issuing by such entities. |
| OQ-60 | *Added 2026-10-17 (D24).* Bills from resident vendors without a VAT number (R92): which VAT category code the lines record, and what to do when such a vendor's document shows VAT anyway (refuse, book the shown VAT to cost, or pay net). **[Needs expert sign-off: tax]** | Accountant / CFO (Word document) | Default: lines VAT 0.00, category `O`; a non-zero vendor VAT total is refused at submit. |
| OQ-61 | *Added 2026-10-17 (D30b).* An entity without a VAT number cannot recover input VAT: should its bills from registered vendors send the VAT to cost always (R29 today depends only on the vendor's VAT number and the line attribution)? **[Needs expert sign-off: tax]** | Accountant / CFO (Word document) | Default: as built (R29 unchanged); flagged for review. |
| OQ-62 | *Added 2026-10-17 (D27).* ZATCA XML for 3-decimal currencies: do the KSA/EN 16931 rules accept amounts with 3 decimals in the document currency? BR-CO-17 / BR-S-09 say category VAT is "rounded to two decimals" (XML v1.2 p.38–40; zatca verification Z35), which suggests 2 dp in the XML. **[SDK-confirm]** If ZATCA requires 2 dp: how is a KWD document with a non-zero third decimal reported (round in XML and differ from the stored document, or restrict such documents to amounts whose third decimal is 0)? | compliance-checker + Dev, then project owner | Default until confirmed: 3-dp documents are stored and booked (R93); handing to ZATCA a document whose amounts have a non-zero third decimal is refused locally with a bilingual message (zatca R-51). |
| OQ-63 | *Added 2026-10-17 (slice-2 review finding 2).* The anonymisation job (R52) replaces buyer data on `SalesDocument` but does **not** touch the signed and cleared XML stored by the zatca module (`EInvoice.signed_xml` / `cleared_xml`), which still holds the buyer's name, ID and address: that XML is the record ZATCA requires to be kept unchanged for its retention period (zatca R-17, R-38), and it is never rewritten. Please confirm that keeping the buyer data inside the stored XML until the ZATCA retention ends is acceptable under PDPL, or say what should happen to it then (e.g. purge the XML after `retention_until`). **[Needs expert sign-off: legal]** | Project owner / legal | **Resolved by D31c (2026-10-17):** the XML is kept unchanged for the ZATCA retention period; only the invoicing fields are anonymised. |

**Tax questions for the CFO (D30c, 2026-10-17).** OQ-44 (SAR 1,000 gross vs net), OQ-51 (SAMA rate source and tax-point date), OQ-52 (notes following the original's type), OQ-53 (residential rent document type), OQ-54 (historical 5%), R30 / OQ-29 (reverse charge), OQ-9 (Art. 51), OQ-8 (WHT treaty), OQ-16 (deposits), and the new OQ-59 to OQ-61 stay open and go to the CFO in the Word document being prepared. Their defaults stand until then.

**Blocking (build):** OQ-2 (zatca hand-off contract), OQ-25 (core role model, views/APIs only), non-SAR documents until `ledger-fx.md` is approved (OQ-6), `billing.md` contract/billing-item models for R60 and R65 to R67, and `BillingAdjustment` for R73. **Blocking (go-live):** OQ-8 WHT rates/categories sign-off (non-resident payments), OQ-16 deposit VAT sign-off (deposit posting), OQ-31 legal. *Added 2026-10-17:* access.md R45 (application PostgreSQL role without trigger-bypass or DDL privileges): the invoicing lock triggers (`invoicing/0007_document_lock_triggers`) accept raw SQL that makes an allowed transition with plausible values and do not check every referenced row (e.g. approver, number), so the role's privileges are the final guard. *Added 2026-10-17 (open; defaults stand, blocking status not decided):* tax adviser OQ-50 (no VAT number), OQ-51 (SAMA rate, tax point, stale rates), OQ-52 (notes vs simplified threshold), OQ-53 (residential rent), OQ-54 (5% history); accountant/dev OQ-55 (document discount), OQ-56 (unapplied foreign balances); project owner OQ-57 (resubmit). **Review before merge:** D18 proposed accounts (accountant). **Expert sign-off:** tax adviser OQ-4 (386 sequence), OQ-8, OQ-9 / R29, OQ-16, OQ-29 / R30, OQ-32, OQ-39, OQ-44 (R48 threshold), OQ-46, R62/R63 (WHT recognition and gross-up); legal OQ-31 / R49 / R53; Sharia board OQ-15, OQ-30. Technical [SDK-confirm] items live in zatca.md; in this spec only OQ-48 (decimals) and the R65 386 note (zatca.md OQ-33) remain. *Updated 2026-10-17:* OQ-48 resolved by D27; the decimals [SDK-confirm] is now OQ-62 (zatca OQ-35). **Blocking (build, added 2026-10-17):** OQ-59 blocks issuing by entities without a VAT number (drafts allowed). D29: proposed accounts stay proposed until the accountant/CFO Word-document review.
