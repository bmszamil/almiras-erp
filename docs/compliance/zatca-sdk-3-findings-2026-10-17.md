# ZATCA SDK R3.4.8 / R4.0.0 findings — 2026-10-17

Follows `zatca-sdk-findings-2026-10-16.md` (SDK 2.0.3; "2.0.3 findings §n" below). Settles, or re-scopes, the open
**[SDK-confirm]** items of `docs/specs/zatca.md`: OQ-33 (current SDK), OQ-28 remainder (QR tags 7–9, TLV length, OU/O,
BR-KSA-50, X509IssuerName), OQ-29 / R-39 (386), OQ-35 / R-51 (3-decimal amounts), R-37 (QR length cap), OQ-26
(simulation template name).

**Run conditions.** Offline only: every SDK call ran inside `unshare -n` (no network); no ZATCA API was called. Only
fake data: the kit's fake invoices, new fake invoices in `zatca-sdk-kit/inv3/`, the SDK's own fake samples, and
documents produced by our zatca code in a clean `git archive HEAD` export (HEAD `fb3cdbf`) with the fixture entities
(PAR/TST1/TST2, made-up VAT numbers). Signing used the SDK's bundled dummy key and certificate. Nothing inside the
SDK folders was changed; copies were used. SDK files are not committed (LGPL-3; reference only).

**Builds under test** (shared by the project owner, who accepted the ZATCA SDK terms):

| Build | Jar | Rules (`Data/Rules/…`) | Usable |
| --- | --- | --- | --- |
| `zatca-einvoicing-sdk-Java-238-R3.4.8` | `zatca-einvoicing-sdk-238-R3.4.8.jar`, sha256 `48abeb82…5d530`, 23.9 MB; manifest `Build-Jdk-Spec: 11`, `Main-Class com.zatca.sdk.MainApp`; `application.properties` `sdk.version=3.4.8` (file dated 2025-12-21; ZATCA classes 2024-10 to 2025-03) | `20210819_ZATCA_E-invoice_Validation_Rules.xsl` sha256 `3312e193…0c4` (152 assertions), `CEN-EN16931-UBL.xsl` `378a7a4d…5b0` (105) | fully: validate, sign, generateHash, qr, invoiceRequest, csr |
| `zatca-einvoicing-sdk-238-R4.0.0` | `zatca-einvoicing-sdk-238-R4.0.0.jar` is **0 bytes** in the share | same file names, sha256 `7dbcd013…8b3` (143) and `9a25eb15…d23` (105) | rules, XSD, samples, readme, config only. R4.0.0 rules were run **with the R3.4.8 jar** (config pointing at the R4.0.0 XSLs); that checks R4.0.0's schematron, not R4.0.0's Java code (QR, signature, hash, PIH checks) |

Tools: OpenJDK 21.0.12, Python 3.13, lxml, cryptography; `javap` for reading the jar's ZATCA classes
(`com.zatca.sdk.*`, `com.gazt.einvoicing.*`).

**Java provider order (changes the 2.0.3 advice).** On Java 21 SunEC has no secp256k1, so the JDK default fails every
signature ("wrong signature Value"). BouncyCastle first (the 2.0.3 workaround) now fails the SDK's **own** samples with
`wrong X509IssuerName`, because the samples carry the SUN form. **SUN first, BouncyCastle second** (`bc-second.security`)
passes everything: SUN's CertificateFactory gives the SUN issuer string and BC supplies secp256k1 ECDSA. CI must use
that order (or Java 11, which the readme still asks for: "≥11 and <15").

## 1. What changed: R4.0.0 vs R3.4.8 vs 2.0.3

**Readme, XSD, config.** `Readme/Java/readme.md` of R4.0.0 is byte-identical to R3.4.8's `Readme/readme.md` (only moved
into `Java/`). It says nothing about QR tags, signing, hashing, PIH, prepayment or currencies. The UBL 2.1 XSD set is
byte-identical in 2.0.3, R3.4.8 and R4.0.0. `Configuration/defaults.json` identical; R4.0.0 ships a `config.json`
written by `install.bat` with `C:\SDK\…` paths (unusable on Linux, harmless). `usage.txt` (both): adds
`-sim` ("use the csr and private key on simulation server") to 2.0.3's list. R3.4.8 rule folder is `Schematrons`
(capital S) while `defaults.json` says `schematrons`: on Linux `install.sh` writes a broken path; the kit writes its
own config. Certificates/key/PIH identical between R3.4.8 and R4.0.0; the dummy certificate is new
(issuer `CN=PRZEINVOICESCA4-CA, DC=extgazt, DC=gov, DC=local`, valid 2024-01-11 to 2029-01-09; 2.0.3's expired 2024).
`pih.txt` is still the R-12 seed literal. CSR examples now come in EN/AR and VAT-group variants.

**Jar signing templates (R3.4.8 vs 2.0.3).** `xml/signature.xml`, `xml/qr.xml`, `invoice.xsl`, `removeElements.xsl`,
`addQRElement.xsl` identical. Changed: `addSignatureElement.xsl` now inserts `cac:Signature` **before
`cac:AccountingSupplierParty`** (2.0.3: after the last `AdditionalDocumentReference`). Same position for documents
whose last ADR precedes the supplier party, so no module change.

**Schematron (ZATCA KSA rules), assertion ids** (extracted with `scripts/sdk3/xsl_rules.py`; the *executed* test was
compared, not the `test` attribute, which is stale in places — e.g. BR-KSA-05's attribute still lists `388 383 381`
while the executed test accepts 386):

| | 2.0.3 → R3.4.8 | R3.4.8 → R4.0.0 |
| --- | --- | --- |
| KSA rules | +86, −54, 52 changed | +2 (BR-KSA-86 buyer e-mail format, BR-KSA-91 KSA-40 legal form `GOV`), −11 (BR-KSA-97, -98, -99, F-06-C40, F-07 … F-13), 10 changed (BR-KSA-04, -06, -08, -14, -31, -81, -92, CL-04, EN16931-06, F-01) |
| EN rules | +32, −49, 45 changed | +BR-10 (as warning), −BR-KSA-F-C-01, changed BR-KSA-F-06-C19, BR-S-08 |

Main rule changes relevant to us (R3.4.8 and R4.0.0 agree unless stated):

- **BR-KSA-05: 386 accepted** (`388 383 381 386`). 2.0.3 rejected it (2.0.3 findings §9).
- **Prepayment rules present:** BR-KSA-73 to -80, -82, BR-KSA-CL-07, BR-KSA-DEC-06, BR-KSA-F-05, BR-KSA-93 to -96.
- **BR-KSA-50 (line VAT = net × rate, exact) removed.** BR-KSA-51 is now a *warning* comparing at 2 dp.
- **BR-CO-17 / BR-S-09** are *warnings* with tolerance `0.01 × number of invoice lines` (2.0.3: error, < 1.00).
  BR-CO-14 and BR-CO-15 stay *errors* and round the sum to 2 dp.
- **BR-CL-KSA-14: QR ≤ 1000 characters** (2.0.3: 500). **BT-CL-27 (seller name ≤ 127) removed**; seller name is
  BR-KSA-F-06-C10, 1 to 1000 characters, warning.
- **BR-DEC-xx and BR-KSA-DEC-03/-04 (max 2 decimals) are warnings** (2.0.3: errors).
- BR-KSA-26/-27/-60/-61 became warnings; BR-CUSTOM-VALIDATION-01 (seller VAT ≠ buyer VAT) added, error.
- R3.4.8 only: KSA-2 may have 9 positions `NNPNESBCG` (C continuous supply, G B2G; BR-KSA-06, BR-KSA-92 contract ID
  for B2G), exemption codes VATEX-SA-ROYALDECREE / -32(bis), BR-KSA-98 (simplified submitted > 24 h: warning),
  BR-KSA-F-07 … F-13 (ID format warnings), BR-KSA-97 (BT-110 ≠ BT-111 for non-SAR). R4.0.0 has the 7-position
  `NNPNESB` form, B2G via KSA-40 `GOV` + tax point date (BR-KSA-91/-92), and allows a price-level charge
  (BR-KSA-EN16931-06 relaxed; its samples use one). **R4.0.0 is therefore not a strict superset of R3.4.8**: either a
  different branch or a build whose rules differ in both directions. Which one ZATCA's servers run is unverified.

**Samples.** Both builds: the same 19 valid samples (standard 388 incl. three "Advance Payment adjustments", export,
exempt, out of scope, self-billing, summary, third party, document charge, payable rounding; simplified 388, nominal,
zero rated; notes). R4.0.0 adds "Credit Note of a Simplified Tax Invoice" and seven `*_Error` samples. **No sample is a
386 itself**; the advance-payment samples are the final 388 that deducts a 386. PDF-A3 copies differ for five samples.

## 2. Validation results (R3.4.8 jar, SUN+BC; "R4 rules" = R3.4.8 jar with R4.0.0 XSLs)

Logs and per-file summaries: work dir `…/scratchpad/sdk3-work/out/` (`v348-*`, `v400r-*`). W = warnings only.

| Set | R3.4.8 | R4 rules |
| --- | --- | --- |
| R3.4.8 samples (19) | 19 PASSED (simplified: W BR-KSA-98) | 19 PASSED |
| R4.0.0 samples (26) | 14 PASSED; 5 fail BR-KSA-EN16931-06 (price-level charge, allowed only in R4 rules); 7 `_Error` fail as intended (BR-CO-04, BR-KSA-84) | 19 PASSED, 7 `_Error` fail as intended (BR-CO-04, BR-KSA-84) |
| Kit 2.0.3 fake invoices, SDK-signed (20) | 18 PASSED. Fail: `std-381-noreason` BR-KSA-17 (expected), `std-388-pih2` KSA-13 (expected). `std-381-cnroot` (CreditNote root) fails XSD before signing. `std-386`/`simp-386` **PASS**. `std-tol-5.51` **PASS** with W BR-CO-17/BR-S-09 (2.0.3: error) | same |
| New fake invoices `inv3/`, SDK-signed (12) | 9 PASSED (386 std/simp, final 388 std/simp, KWD 2-dp std/simp, seller name 127/128/200 bytes). Fail: `std-388-kwd3`, `simp-388-kwd3`, `std-388-kwd3-vat3`: **errors BR-CO-14, BR-CO-15** (+ W BR-DEC-*, BR-KSA-DEC-03/-04, BR-CO-10/-13/-16) | same |
| **Our zatca code**, signed by the module with the SDK dummy key/cert, default QR layout (DTG), `rfc2253` issuer style (9) | **9 PASSED**: simplified 388, simplified 388 with 2 lines, simplified KWD, simplified with 127-byte Arabic seller name, standard 388, standard KWD, chained 388 → 381 → 383 (PIH checked against each document's own PIH; the SDK only knows one `pih.txt`) | 9 PASSED |
| QR variants of an SDK-signed simplified final 388 (QR is outside hash and signature) | see §3.1 | — |

**Hash (R-13, AT-13).** SDK `-sign`/`-generateHash` hash = `zatca-sdk-kit/scripts/pyhash.py` on **31/31** unsigned
documents (kit 19 + inv3 12; `std-381-cnroot` not signable). Hash of signed output = hash of unsigned input. R-13
stays confirmed. PIH stage: R3.4.8 still compares against `pih.txt` only (`KSA-13 PIH is inValid` otherwise).

**Module notes.** Without the 2.0.3 workarounds in `zatca/test_sdk_offline.py` (no seller additional number, no buyer
region) everything passes: BR-KSA-09/-10/-63/-64 no longer require them. Module QR is 524 chars for a short name and
656 for a 127-byte name (< 1000).

## 3. Settled points

### 3.1 QR tags 6–9 and tags 8/9 for simplified (R-15, R-33, AT-14, OQ-28) — settled

Evidence: R3.4.8 `QrGenerationService` / `QRCodeGeneratorServiceImpl` (generator) and `QrCodeValidator` (bytecode),
every SDK sample, SDK-signed kit invoices, and QR variants.

- **Tag 6** = invoice hash, base64 text, 44 bytes (unchanged).
- **Tag 7** = `ds:SignatureValue` **base64 text** (96 bytes); raw DER in tag 7 fails ("invoice signature value … in
  tag 7").
- **Tag 8** = public key, DER SubjectPublicKeyInfo (`getPublicKey().getEncoded()`, 88 bytes).
- **Tag 9** = `X509Certificate.getSignature()`, the CA's DER ECDSA signature on the certificate (71 bytes); written
  **only when KSA-2 starts with `02`** (simplified and its notes). Standard samples carry tags 1–8 only.
- The **DTG layout of R-15 is the SDK's layout**. The 2.0.3 layout (7 SPKI, 8 r, 9 s) now fails (`digital signature`,
  `publicKey`, `certificate signature`). → Default `ZATCA_QR_TAGS_7_9_LAYOUT=dtg` is correct; the `sdk_2_0_3` layout
  is obsolete.
- **Are 8/9 required for simplified?** The generator always writes 1–9 for simplified and every simplified sample has
  9 tags, so: **yes, emit tags 1–9** (R-15 unchanged). Note: the R3.4.8 *validator* does not enforce presence — a
  missing tag throws inside a try/catch that records an error without failing the stage, so QRs with tags 1–6 or 1–8
  still "PASS". The SDK cannot be used as a test for missing tags; AT-14 must assert presence itself.
- **Tag 4 = BT-115 PayableAmount, not BT-112.** Generator puts `LegalMonetaryTotal/PayableAmount` in tag 4; the
  validator accepts tag 4 = BT-115 and, where BT-112 ≠ BT-115 and tag 4 = BT-112, warns "Please provide the value in
  Tag 4 as per Amount due payment (BT-115)". Samples: advance payment 388 tag 4 = `1035` (BT-112 2185); our
  `simp-388-final` tag 4 `3450.00` (BT-112 4600.00). R-15 says "total incl. VAT": **change to BT-115**. Same for every
  document today (prepaid 0, no rounding), different once R-39 / payable rounding exist.
- **Tag 3.** Generator: `IssueDate + "T" + IssueTime` reformatted `yyyy-MM-dd'T'HH:mm:ss`, **no `Z`** for an IssueTime
  without suffix (samples: `2022-09-07T15:23:55`, 19 bytes). The validator's mismatch goes to a warnings map that is
  only reported when another QR check fails, so the module's `…T10:00:00Z` and the SDK's no-`Z` form both pass.
  Not settled by the SDK; R-15 / OQ-18: align with the SDK form (no `Z`) unless the sandbox says otherwise.
- **Tag 5** = first `/Invoice/TaxTotal/TaxAmount` (generator XPath). In the foreign-currency advance sample the SAR
  TaxTotal comes first, so tag 5 = 1068.75 SAR. The module puts the document-currency TaxTotal first (R-40), which
  the SDK accepts (`module-simp-388-kwd` PASSED). Which total ZATCA *intends* for FX simplified documents is open.
- **TLV length = BER.** Generator `BerTlvBuilder`: 128–255-byte values get `0x81 LL` (SDK-signed seller names of 128
  and 200 bytes: `long(0x81)`, QR PASSED). The validator parses BER: the same 200-byte name with a **1-byte length
  (`C8`) fails `QRCODE_INVALID`**. → OQ-28 TLV point settled: BER. The interim 127-byte cap (R-15, AT-56) can go;
  BT-27 has no 127-character rule any more.
- **QR length cap (R-37):** BR-CL-KSA-14 = **1000** characters (2.0.3: 500). A 200-byte name gives 752 chars.

### 3.2 ECDSA signature and certificate-signature encoding (R-14, AT-65, OQ-28) — settled, unchanged

`DigitalSignatureServiceImpl` = `SHA256withECDSA` over `Base64.decode(invoiceHash)` (raw 32 bytes); `SignatureValue`
= base64 of DER SEQUENCE(r, s), 70–72 bytes, secp256k1. Verified in Python on every SDK sample, every SDK-signed kit
document and every module document (83/83; the recomputed R-13 hash also equals the `invoiceSignedData` DigestValue on 83/83, comments dropped as the SDK does): verifies over the raw 32-byte hash only (not the base64/hex text, not
prehashed). Tag 9 = DER of the certificate's own signature (§3.1). Curve still hard-coded `secp256k1`.

### 3.3 SignedProperties digest and XAdES details (R-14, AT-66) — settled, unchanged

On all 83 signed documents checked with `scripts/sdk3/analyse3.py` (45 SDK samples of both builds, 29 SDK-signed kit/`inv3` documents, 9 module documents): **SignedProperties digest = base64(hex text(SHA-256(the element
as dom4j `asXML()` writes it: `xmlns:xades` on SignedProperties, `xmlns:ds` on each `ds:` child)))**, whitespace as in
the document. The SDK writes it indented; the module writes it without whitespace and digests the same text with the
declarations added — accepted (SIGNATURE PASSED on all module documents). CertDigest = base64(hex(SHA-256(base64
certificate text))). Reference `Type="…xmldsig#SignatureProperties"`, `URI="#xadesSignedProperties"`; templates
identical to 2.0.3. X509SerialNumber decimal.

- **X509IssuerName = the SUN / RFC 2253 form `CN=…, DC=extgazt, DC=gov, DC=local`** in every R3.4.8/R4.0.0 sample
  and accepted by the validator under SUN-first. → R-14's target (`rfc2253` style, the module default) settled; the
  `bc` style is only an artefact of running the SDK with BouncyCastle first.
- **SigningTime:** SDK `-sign` writes `yyyy-MM-dd'T'HH:mm:ss` **without `Z`** (JVM local time); all samples have no
  `Z`. The validator does not check SigningTime (not among its checks: invoice digest, SignedProperties digest,
  CertDigest, issuer name, serial, signature value, certificate). The module's UTC `…Z` passes. R-14 can keep UTC; the
  `Z` suffix is optional as far as the SDK goes.
- Validator checks only simplified documents' signature and QR; standard documents get XSD/EN/KSA/PIH only (clearance
  re-signs them server-side).

### 3.4 Prepayment / advance payment (386) structure (R-39, R-8, OQ-29, AT-41, AT-52) — rules settled; compliance-sample question open

- **386 is accepted** (BR-KSA-05, both rule sets): `std-386` and `simp-386` (one line, 1,000.00 + 15 %) pass all
  stages, simplified one including QR and signature. A 386 is a normal invoice with type code 386.
- **Final 388 deducting a 386**, as the R4.0.0 "Advance Payment adjustments" samples (and our `inv3/*-388-final`,
  PASSED) build it:
  - real lines as usual; header `TaxTotal/TaxSubtotal` covers the **real lines only** (sample: taxable 1900 = 2000 −
    100 allowance; the prepaid 1000 is not deducted);
  - one extra `cac:InvoiceLine` per prepaid VAT category: `InvoicedQuantity 0`, `LineExtensionAmount 0.00`,
    `Price/PriceAmount 0.00`, line `TaxTotal/TaxAmount 0` and `RoundingAmount 0` (BR-KSA-82, error), and inside that
    line `TaxTotal/TaxSubtotal` with `TaxableAmount` = KSA-31, `TaxAmount` = KSA-32, `TaxCategory` ID/Percent =
    KSA-33/-34 (BR-KSA-75, -79: KSA-32 = round(KSA-31 × KSA-34 / 100, 2));
  - in that line `cac:DocumentReference` with `cbc:ID` (KSA-26, the 386 number), **`cbc:UUID` (the 386's UUID; all
    three samples carry it)**, `cbc:IssueDate` (KSA-28), `cbc:IssueTime` (KSA-29), `cbc:DocumentTypeCode` `386`
    (KSA-30, BR-KSA-74 error) — placed after `LineExtensionAmount`, before the line `TaxTotal`;
  - `PrepaidAmount` (BT-113) = Σ(KSA-31 + KSA-32) (BR-KSA-80), `PayableAmount` = BT-112 − BT-113 (BR-CO-16), QR tag 4
    = BT-115 (§3.1);
  - rate-change sample: prepayment at 5 % deducted on a 15 % invoice — one deduction line per prepaid rate; FX
    sample: same structure in USD with BT-111 in SAR.
  - Z/E/O prepayments need KSA-41/-42 exemption code/text on the line (BR-KSA-CL-07, -93 to -96).
- R-39 as written matches, plus the UUID. **Whether a 386 is a required compliance sample (OQ-29) is not answered by
  the SDK** (no 386 sample, no compliance-check list in the jar) → sandbox.

### 3.5 3-decimal currency amounts in XML (R-51, D-14, OQ-35, AT-72) — settled: 2 decimals in XML

KWD documents with 3-decimal amounts (`*-kwd3`, `std-388-kwd3-vat3`) **fail with errors BR-CO-14 and BR-CO-15**
(both round their sums to 2 dp and compare exactly), plus BR-DEC-*/BR-KSA-DEC-* warnings. The same documents with
2-decimal amounts (`*-kwd2`) and the module's KWD documents (3-dp stored, emitted 2-dp, amounts ending in 0) pass.
No rule looks at the currency's own minor units. → OQ-35 [SDK-confirm] closed: **ZATCA XML amounts are 2 decimals
for every currency**; R-51 (refuse a non-zero third decimal; emit 2 dp) is correct and stays. Unit price (BT-146)
has no decimals rule.

### 3.6 Other points

- **R-31 / BR-KSA-50:** BR-KSA-50 is gone; category VAT tolerance is now `±0.01 × lines` and only a warning. The
  module's exact `round(…, 2)` category check remains a stricter house rule (intended). OQ-28 BR-KSA-50 item closed.
- **§3.2 OU/O (OQ-28):** R3.4.8 `CsrGenerationService` requires OU = "the 10-digit TIN … of the individual group
  member" **only for a VAT group** (11th digit of the organization identifier = 1; example
  `csr-config-example-EN-VAT-group`); otherwise OU = branch name, O = company name. A fake CSR with
  `OU=Test Branch, O=Fake Seller Co` was generated without error. → docs' mapping confirmed.
- **Template name (OQ-26):** `-csr` → `ZATCA-Code-Signing`, `-nonprod` → `TSTZATCA-Code-Signing`, **`-sim` →
  `PREZATCA-Code-Signing`** (extension 1.3.6.1.4.1.311.20.2, decoded from fake CSRs).
- **OQ-30:** `-invoiceRequest` body still `{"invoiceHash", "uuid", "invoice"}`; jar holds no ZATCA URL.
- **PIH (R-12, AT-10):** seed literal unchanged in both builds.

## 4. Code changes needed (for module-builder; zatca app code not changed here)

| File | Change | Reason (spec) |
| --- | --- | --- |
| `zatca/services.py` (`_sign_xml`) | QR tag 4 = `ubl.amount(payable)` (BT-115), not `total_gross` | §3.1; R-15 |
| `zatca/testing.py` (`FakeZatcaClient.cleared`) | tag 4 from `LegalMonetaryTotal/PayableAmount` | same, keeps the fake ZATCA consistent |
| `zatca/qr.py` | `tlv()`: BER length (`0x81 LL` for 128–255, `0x82` above); drop the 127-byte cap and `ZATCA_QR_TLV_LENGTH_FORM` interim; keep `decode()` BER-aware; `DEFAULT_MAX_CHARS` 700 → 1000 | §3.1; R-15, R-37, AT-39, AT-56 |
| `zatca/qr.py` | remove (or mark test-only/obsolete) `LAYOUT_SDK_203`; DTG layout is the SDK layout | §3.1; OQ-28 |
| `zatca/rules.py` | drop the BT-27 ≤ 127-character / 127-byte checks (keep ≤ 1000 chars as BR-KSA-F-06-C10) | §3.1; R-15 |
| `zatca/ubl.py` (`qr_timestamp`) | decide tag 3 suffix: SDK form has no `Z`; both pass the SDK | §3.1; R-15, OQ-18 (owner/sandbox decision) |
| `zatca/ubl.py`, `zatca/services.py` (`_invoice_data`), `zatca/rules.py` | build 386 and the prepayment-deduction lines per §3.4 (incl. `cbc:UUID` of the 386, nested line TaxSubtotal, BT-113, BT-115 = BT-112 − BT-113); local checks BR-KSA-73/-74/-75/-79/-80/-82 | §3.4; R-39, AT-41, AT-52 (now buildable) |
| `zatca/crypto.py` (`template_name`) | simulation → `PREZATCA-Code-Signing`; non-production test → `TSTZATCA-…`; production → `ZATCA-…` | §3.6; §3.2, OQ-26 |
| `zatca/test_sdk_offline.py` | target SDK R3.4.8 layout: DTG QR layout, `rfc2253` issuer style, run with SUN first + BC second (`scripts/sdk3/bc-second.security`), remove the seller additional-number / buyer-region workarounds, make QR gating (not "recorded") | §2, §3.1, §3.3; AT-69 |
| `zatca/crypto.py` (`issuer_name` docstring), settings | `rfc2253` confirmed; `bc` style only for a BC-first JVM | §3.3; R-14 |
| `zatca/rules.py` (R-51) | no change; keep refusing a non-zero third decimal and emitting 2 dp | §3.5; R-51, AT-72 |
| optional: `zatca/ubl.py` (`subtype_code`) | R3.4.8 allows 9-position KSA-2 (continuous supply, B2G); R4.0.0 rules allow only 7 — keep 7 until the server version is known | §1 |

Spec edits (for spec-writer): R-14 (issuer name settled; SigningTime `Z` optional), R-15 (BER; tag 4 = BT-115; tag 3
form; cap removed), R-37 (1000), R-31 (BR-KSA-50 gone, tolerance warning), R-39 (+UUID, structure §3.4), R-51/OQ-35
(closed: 2 dp), OQ-28 (closed except tag 3 `Z`), OQ-26 (`PREZATCA-` for simulation), OQ-33 (R3.4.8 used; R4.0.0 jar
missing), §6 (Java provider order SUN, BC).

## 5. Not verified

- **R4.0.0 Java code**: its jar is 0 bytes. Its QR generator/validator, signature validator, hashing, PIH and CSR
  checks are unknown; only its schematron/XSD/samples were checked (with the R3.4.8 jar). Get a complete R4.0.0
  jar (sha256 to record) and re-run `scripts/sdk3/` against it.
- Which rule set ZATCA's sandbox/production runs (R3.4.8's 9-position KSA-2, BR-KSA-98 etc. vs R4.0.0's KSA-40 `GOV`,
  price-level charges).
- QR tag 3 with or without `Z`; tag 5 for foreign-currency simplified documents; presence of tags 8/9 (SDK validator
  does not enforce it) — sandbox.
- OQ-29 (386 as a required compliance sample), OQ-30 paths, OQ-18 — sandbox / Swagger.
- Whether ZATCA's servers accept the module's whitespace-free SignedProperties (the SDK does).

## 6. Reproduce

Kit: `docs/compliance/zatca-sdk-kit/scripts/sdk3/` (see the kit README) and `inv3/`. Copy the SDK into a work dir
(`$SDK3_WORK/r348`), write `r348/Configuration/config.json` with absolute paths (note the `Schematrons` folder name),
then `SDK3_WORK=… scripts/sdk3/validate_all.sh OUT files…`. Module documents: copy
`scripts/sdk3/sdk3_export_generator.py` as `zatca/test_sdk3_export.py` into a `git archive HEAD` export and run it with `ZATCA_SDK_DIR` and
`SDK3_OUT` set; validate with `validate_ownpih.sh`.
