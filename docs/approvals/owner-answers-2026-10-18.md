# Project owner answers, 2026-10-18

Answers given in the Claude Code session on 2026-10-18 to the open-question list of 2026-10-17.
Wording is the owner's, lightly condensed; "Interpretation" lines are the team's reading, to be confirmed if wrong.

## A. Reconciliation of `main` and the development branch

Note: in questions 1 and 13, "branch" meant the code branch (`claude/funny-davinci-7yd88s`), not a company branch. The owner's answers describe company governance, which is recorded here as stated.

| # | Answer |
| --- | --- |
| A1 | Authority in each company lies with that company's CEO or General Manager. The Managing Partner of Almiras sits on the board or acts as shareholder representative only. *Interpretation:* the project owner's current answers prevail over earlier decisions on either code line. |
| A2 | The Managing Partner does not approve everything. Each company needs its own **authority matrix** for every task and activity. Example: contracted payroll needs no Managing Partner approval; overtime in Almiras needs the Managing Partner; other companies have their own approval structures. |
| A3 | Self-approval is allowed only to the highest authority of each entity: the Managing Partner in Almiras, the CEO in each other company. No other role may ever self-approve. Every self-approval by these two is logged and listed in the monthly governance / internal-audit report. |
| A4 | Each company is granted separately (no cascade of group roles to subsidiaries). |
| A5 | The CEO can approve and post, and can also act as accountant. Roles also needed: (internal) audit, personal assistant, business analyst, external auditor. |
| A6 | Yes: system users may issue and post after a person's prior approval. |
| A7 | Auto-generated invoices: one approval per contract. |
| A8 | Intercompany: automatic mirror bill in the buying company. Cost-plus, timesheets and cost pools are needed at go-live. |
| A9 | Invoice date: last day of the service month (owner's preference), unless ZATCA issues require otherwise (team to advise); services ending earlier are invoiced on that earlier end date. |
| A10 | All approvable documents go through the same approval engine. |
| A11 | Retention confirmed: keep everything; data older than 10 years is nominated for destruction (see B7). |
| A12 | User model: follow best practice for large groups and large ERP systems. *Interpretation:* a custom user model (Django's recommended practice for new projects). |
| A13 | Option C confirmed. The CEO of the company approves and reviews; the CEO may delegate this to another user. *Interpretation:* the project owner (or their delegate) reviews and approves the integration before it replaces `main`. |

## B. Decision-pack follow-ups

| # | Answer |
| --- | --- |
| B1 | Exchange rate for VAT in SAR: the **invoice date** rate. |
| B2 | Goal: load opening balances into the new ERP accounts. *Interpretation:* allow the migration opening entry to post to control accounts as the one exception to policy A; customer/vendor open items are loaded via the migration template. |
| B3 | Yes: tax penalties get their own account (separate from 5930 GOSI late-payment fines). |
| B4 | Yes: omitting "land transport inside KSA 15%" from the WHT list was intentional. |
| B5 | Yes: if GOSI rejects a maternity claim, the company pays full salary. |
| B6 | Yes: GOSI contributions continue during the GOSI maternity benefit. |
| B7 | Destruction of records older than 10 years is decided by an **internal audit committee approval minute**. |
| B8 | Bai' ajel employee sale: VAT is charged to the employee by law on the sale price (cost + profit margin), e.g. cost 100 + margin 10 = 110 net, VAT 15% of 110 = 16.50 paid by the employee. |

## C. Purification

| # | Answer |
| --- | --- |
| C1 | Rules apply to **all companies**. |
| C2 | Yes: funding the purification account needs the CFO's approval. |
| C3 | Charities are re-approved every **12 months** from the last approval; reactivating a switched-off charity is allowed. |
| C4 | An accounting user marks instructions as sent; open banking connection later if possible. |
| C5 | The **board** approves the charity list. Purification accounts should not be profit-generating; **any return on a purification account goes to charity**. |
| C6 | The transfer from a current account to the purification account must be booked. Under IFRS the purification account is a restricted / escrow-like account that cannot be returned to the company. |
| C7 | Account 1140 approved. |

## D. Migration

Migration later. The journal export already sent is close to the final one. The team prepares fillable templates to simplify the process.

## E. Files

| # | Answer |
| --- | --- |
| E1 | SDK R4.0.0 jar re-uploaded (now 37 MB, received 2026-10-18). |
| E2 | uqn.gov.sa allowed (still refused by this session's network proxy on 2026-10-18). |
| E3 | Bank WPS and statement formats come later. |
| E4 | Egypt rules: the team researches them. |

## F. Other

| # | Answer |
| --- | --- |
| F1 | Yes: "Daftra" replaced by "Falcon" in `CLAUDE.md` and the migration agent. |
| F4 | Build the VAT filing module. Zakat certificate validity checked against the certificate: valid until 23/11/1448 AH = 30/04/2027 AD (correct). |
| F5 | Yes: add pytest-xdist. |
| F6 | Hosting, domain and key holders: team to propose. |
| F7 | ZATCA sandbox: team to explain how. |

## G. Decisions taken on the team's recommendations (owner, 2026-10-18: "go with your recommendations on all open decisions")

| # | Decision |
| --- | --- |
| G1 | Standard invoices to government customers (B2G, Art. 20(5)): add a **payment order date** field to sales documents; it fills ZATCA TaxPointDate (BR-KSA-92). Until it is entered the document stays held. The tax adviser confirms which government customers fall under Art. 20(5). |
| G2 | QR tag 5 on foreign-currency simplified invoices: the **VAT total in SAR**, following ZATCA's SDK sample (SAR TaxTotal first). To be confirmed in the sandbox. |
| G3 | VAT returns keep **three different people** (preparer, reviewer, approver); the top-authority self-approval (A3) does **not** apply to VAT returns. |
| G4 | VAT returns in the ERP start from each entity's **VAT go-live date**; earlier periods are treated as filed outside the ERP (no overdue alerts, no import required). |
| G5 | Purification choices confirmed as built: 12 months run from the board approval date; the Managing Partner is the fallback funding approver; an instruction may be marked sent without being exported first; reactivation starts with an accountant's request. |
| G6 | VAT return period of a sale = its **tax point** (date of supply / earliest of supply, invoice, payment), per PSG §11.3. |
| G7 | SAR exchange rate for VAT = **SAMA rate on the tax point date** (VAT IR Art. 61; TIR §4.2.2). This replaces B1 (invoice date) and needs no separate adviser opinion; when the tax point is the invoice date the two coincide. |
| G8 | Group-level settings (global exchange rates, the Sharia switch under dual control) stay group-level; everything else is granted per company (A4). |

## H. Further decisions on the team's recommendations (owner, 2026-10-19: "go with your recommendations")

| # | Decision |
| --- | --- |
| H1 | The team prepares the 2026–2027 list of SAMA non-publication days (Fridays, Saturdays and KSA public holidays), loaded as unverified data for the accountant to review and confirm. |
| H2 | When the tax point is earlier than the issue date, the whole sales document is booked at the SAMA rate on the tax point, so VAT account 2200 ties to the VAT in SAR without a rate difference. |
| H3 | VAT return box verification: the accountant proposes, the CFO (approver) verifies. |
| H4 | A correction where the ledger is already right and only the filed return was wrong may be approved by the reviewer with a written reason (no correcting entry). |
| H5 | HT Rakik web fonts (WOFF2) were supplied, but only with Hadi Type's Desktop Font License, which does not cover web use. The ERP keeps IBM Plex Sans Arabic until Almiras provides the Hadi Type web licence; the files are not committed. |
| H6 | 2026-10-19: Almiras bought the Hadi Type **webfont** licence for HT Rakik Light and Medium (invoice HT2610260140, 20,000 pageviews each). The ERP uses HT Rakik on its screens, served from host-only files (`deploy/brand-fonts/`), never committed. The pageview counting period is to be confirmed with Hadi Type. Supersedes H5. |
