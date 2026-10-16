# ZATCA SDK findings — 2026-10-16

Settles (or re-scopes) the **[SDK-confirm]** items in `docs/specs/zatca.md`: OQ-28, §3.2 CSR fields, §3.4 381 root, R-31 schematron, OQ-29, OQ-30. Offline run only: no ZATCA API was called, the SDK ran with no network (`unshare -n`), and only the SDK's bundled dummy key/certificate and fake data were used (seller VAT 300000000000003, buyer 399999999900003). Nothing was committed.

**SDK under test:** "zatca-envoice-sdk-203" (SDK 2.0.3) from ZATCA's DownloadSDK page. `sdk.zip` sha256 `1a7df6d91fd34968ad59a97087f637f56504fdf92c42050916b838be20fc5ae3`. Licence LGPL-3 (`LICENSE.txt`). CLI/SDK jar `cli-3.0.8-jar-with-dependencies.jar` (sha256 `e0a250bf…bf403bf2`), built 2022-05-11 with JDK 11.0.13. Rules: `20210819_ZATCA_E-invoice_Validation_Rules.xsl` and `CEN-EN16931-UBL.xsl`. **Java used:** OpenJDK 21.0.12 (the SDK asks for >=11 and <15). Python 3.13.16, lxml 6.1.3, cryptography 50.0.1.

**Main caveat: this SDK is older than the documents the spec follows.** It is a May 2022 build with August 2021 rules. In four places it disagrees with XML v1.2 and the DTG: 386 is rejected, the QR tag 7–9 layout differs, the QR limit is 500 characters, and OU must be a 10-character TIN (items 5, 6, 9). Its `fatoora -help` has no `validateqr` command, which the spec mentions. That suggests a newer SDK exists. Where this SDK conflicts with newer documents, the spec should keep the newer documents and re-check against a current SDK or the sandbox. Do **not** copy the old SDK behaviour into the spec.

**Java 21 issue.** SunEC no longer supports secp256k1, so SDK signature validation fails with `ECDSAUtil - Curve not supported: secp256k1 (1.3.132.0.10)` → "wrong signature Value". It also fails on the SDK's own signed output. Workaround used for all results below: register BouncyCastle (already inside the jar) as JCA provider 1 with `-Djava.security.properties==<file>`. CI should use this, or pin Java 11.

Work dir (scripts, inputs, outputs): `/tmp/claude-0/-home-user-almiras-erp/ea5f57b4-d147-5a57-9a1e-1750b3cc3f70/scratchpad/sdk-work/` (`scripts/make_invoices.py`, `pyhash.py`, `pysign.py`, `analyse_signed.py`, `fatbc.sh`).

## Results per item

**1. Curve: secp256k1.** The bundled key `ec-secp256k1-priv-key.pem` and certificate both use OID 1.3.132.0.10. `ECDSAUtil.getKeyPair` hard-codes `"secp256k1"`. A CSR generated with `-csr` has `ASN1 OID: secp256k1` (openssl). No P-256 anywhere. → **R-14:** curve settled as secp256k1. Drop the "[SDK-confirm] (OQ-28)" curve clause and keep the SFIS "P-256" note as superseded. **§6:** note that the SDK in CI needs BouncyCastle first on Java ≥16, or Java 11.

**2. What is signed: the raw 32-byte invoice hash, using SHA256withECDSA (so the curve signs SHA-256(hash)). It is not SignedInfo.** Code: `DigitalSignatureServiceImpl.getDigitalSignature` does `Base64.decode(invoiceHash)` → `Signature.getInstance("SHA256withECDSA").update(raw32)`. The verifier `ECDSAUtil.verifyECDSA` does the same. Python proof (`analyse_signed.py` on SDK-signed `simp-388`), using the cert key and `ec.ECDSA(SHA256)`: `VERIFY OK: raw 32-byte invoice hash`. These fail: the base64 text, the hex text, C14N SignedInfo, the canonical invoice, and Prehashed(raw hash). `SignatureValue` = base64 of DER SEQUENCE(r,s) (`0x30…`, 70–72 bytes). → **R-14 (a):** "SignatureValue = base64(DER ECDSA-secp256k1 over SHA-256 of the raw 32-byte invoice hash, i.e. `sign(raw_hash, ECDSA(SHA256))`). SignedInfo is **not** canonicalised or signed." Add AT: verify SignatureValue against `base64decode(invoice_hash)` with the cert key.

**3. XAdES structure.** It comes from the jar template `xml/signature.xml` (wrapper `xml/ubl.xml`, `xml/qr.xml`; XSLT `xslt/add*.xsl`) and matches the DD/DTG target in R-14 exactly:
- `ds:Signature Id="signature"`; `SignedInfo`: C14N11 `CanonicalizationMethod`, `ecdsa-sha256`.
- Reference 1, `Id="invoiceSignedData" URI=""`: 3 XPath transforms (`REC-xpath-19991116`) plus a c14n11 transform, `xmlenc#sha256`; DigestValue = the invoice hash.
- Reference 2: `Type="http://www.w3.org/2000/09/xmldsig#SignatureProperties" URI="#xadesSignedProperties"`, so the **DD form, not ETSI**.
- `KeyInfo/X509Data/X509Certificate`; `Object/xades:QualifyingProperties Target="signature"` (xades 1.3.2) `/SignedProperties Id="xadesSignedProperties"/SignedSignatureProperties/{SigningTime, SigningCertificate/Cert/{CertDigest, IssuerSerial}}`.
- **No** SigningCertificateV2, SignaturePolicyIdentifier or DataObjectFormat.
- The UBL `cac:Signature` (ID `…signature:Invoice`, method `…dsig:enveloped:xades`) is inserted after the last `AdditionalDocumentReference`. The QR ADR is inserted before `AccountingSupplierParty`.

Encodings were proven in Python:
- **CertDigest** = base64(hex text(SHA-256(**the base64 certificate text** as ASCII))). It is not the DER bytes: `sha256(base64 cert text): b64(hex)=True`, `sha256(DER): False`.
- **SignedProperties digest** = base64(**hex text**(SHA-256(dom4j `asXML()` of the SignedProperties element))). That serialisation is the element as written in the document, whitespace kept, `/>` empty tags, plus `xmlns:xades` on SignedProperties and `xmlns:ds` on every `ds:` child. It is **not** C14N, **not** linearised, and **not** the raw digest. Candidates tested: as-is, C14N, exc-C14N and whitespace-stripped all fail; only that form matched.
- **SigningTime** = `yyyy-MM-dd'T'HH:mm:ss'Z'`. The SDK uses `LocalDateTime.now()` (local time with a literal Z), so emit UTC.
- **X509IssuerName** = `getIssuerDN().getName()`, which depends on the JCA provider. SUN gives `CN=TSZEINVOICE-SubCA-1, DC=extgazt, DC=gov, DC=local`; BC gives `DC=local,DC=gov,DC=extgazt,CN=…`. The validator compares strings exactly, so a document signed under one provider fails under the other.
- **X509SerialNumber** = decimal.

→ **R-14 (b), (c):** SFIS-only elements are **not used**. The Reference Type is `xmldsig#SignatureProperties`. **Change R-14:** SignedProperties digest = base64 of the **hex text** (not raw). Define the serialisation as: SignedProperties with no inter-element whitespace, `xmlns:xades` on it and `xmlns:ds` on each ds element. Emit the same element without whitespace in the document. X509IssuerName in RFC 2253 order with `", "` separators (the SUN/Java 11 form, matching DTG examples; re-confirm in the sandbox). CertDigest stays "base64 of hex text", with "of the SHA-256 of the base64 certificate string" added.

**4. Invoice hash.**
- Code: `HashingGenerationServiceImpl` applies the XSLT `invoice.xsl`, which drops `UBLExtensions`, the QR `AdditionalDocumentReference` and any `Signature` under `Invoice`, and omits the XML declaration. It then applies Santuario `Canonicalizer("…/2006/12/xml-c14n11")`, then SHA-256, then base64 of the raw digest (44 chars).
- Python: lxml removes the 3 nodes (keeping surrounding whitespace text) and serialises with `method="c14n"` (C14N **1.0**). This matched SDK `-generateHash` byte-for-byte on **18/18** documents. They include all type codes, signed and unsigned versions, CRLF line ends, comments, `&amp;`/`&#x41;`, single-quoted attributes, a tab and Arabic text.
- C14N 1.0 and 1.1 differ only for document subsets with `xml:*` attributes, which this whole-document process never meets. lxml `c14n2` does **not** match.
- The hash of the signed output equals the hash of the unsigned input (`hash of unsigned input == signed hash: True`).
- `[PIH]` validation compares against `Data/PIH/pih.txt`, which holds exactly the R-12 seed literal. Another PIH → `KSA-13 PIH is inValid`.

→ **R-13:** confirmed. Note "lxml `c14n` (1.0) is byte-identical for full UBL documents; proven against the SDK (OQ-11 settled for hashing)". Remove the cac:Signature only as a direct child of Invoice, which matches the SDK's `removeElements.xsl` (the hash XSLT drops any `Signature`; identical for valid UBL). **AT-13:** use these SDK-generated fixtures (`inv/*.xml` + the `-generateHash` value). **AT-10:** the SDK's own default PIH equals the literal.

**5. QR TLV (SDK output, `simp-388`, 317 bytes / 424 base64 chars).**
- Tags 1–5: UTF-8 text. Tag 3 = `IssueDate + "T" + IssueTime + "Z"`. Tag 5 = the first `cac:TaxTotal/cbc:TaxAmount`.
- **Tag 6** = the base64 hash **as text, L=44** (`tag6 == b64 text: True; == raw 32: False`).
- **This SDK uses an older layout for tags 7–9:** **7** = DER SubjectPublicKeyInfo (88 bytes), **8** = ECDSA **r** and **9** = ECDSA **s**, as Java `BigInteger.toByteArray()` (32 or 33 bytes).
- Its QR validator rejects the DTG layout used in R-15 (7 = base64 signature, 8 = SPKI, 9 = CA signature) with "R value … doesn't match the R value in tag 8".
- Lengths use **BER** (`BerTlvBuilder`). A 156-byte seller name was encoded as `01 81 9c …`, not 1 byte.
- The schematron caps the QR at 500 characters (`BR-CL-KSA-14`) and the seller name at 127 characters (`BT-CL-27`).
- `-qr` output equals the embedded QR. There is no `validateqr` command; QR is checked inside `-validate` for simplified documents only.

→ **R-15:** tag 6 settled as base64 text (44 bytes); remove that [SDK-confirm]. **Tags 7–9 stay per DTG**, marked "SDK 2.0.3 uses the pre-v1.2 layout (7 SPKI, 8 r, 9 s); confirm with a current SDK/sandbox" (OQ-28 remainder). Add to R-15 that values of 128–255 bytes are encoded by SDK 2.0.3 with BER long-form length (`0x81 LL`). The DTG 1-byte form must be confirmed (OQ-28 remainder). Until then cap each value at **127** bytes, which is safe under both readings, and add BT-27 ≤ 127 characters. **AT-56** changes accordingly. R-37 (700 characters) stays; the SDK's 500 is the 2021 limit.

**6. CSR (`fatoora -csr -pem -csrConfig fake-csr.properties`; `openssl req -text`, asn1parse).**
- Subject, in order: `C=SA` (PrintableString), `OU`, `O`, `CN` (UTF8String). No organizationIdentifier in the subject.
- Extension 1.3.6.1.4.1.311.20.2 (Microsoft certificate template name) = UTF8String `ZATCA-Code-Signing`, or `TSTZATCA-Code-Signing` with `-nonprod`.
- SAN = one DirectoryName containing:
  - **SN 2.5.4.4** = `1-FakeERP|2-1.0|3-<uuid>`, the EGS serial
  - **UID 0.9.2342.19200300.100.1.1** = the 15-digit VAT number
  - **title 2.5.4.12** = the invoice-type flag `1100`
  - **registeredAddress 2.5.4.26** = location
  - **businessCategory 2.5.4.15** = industry
- Checks: invoice type `^[0-1]{4}$`; organization identifier length 15; country required. **OU and O must each be 10 characters** ("please provide a valid 10 digit of your tin number").
- Key/CSR signature: secp256k1 / ecdsa-with-SHA256. The key file says `EC PRIVATE KEY` but holds PKCS#8.
- The bundled ZATCA test certificate has the same SAN layout (`title=1100`). It expired on 2024-04-18.

→ **§3.2:** serial OID = **2.5.4.4 (SN) in the SAN DirName**. Invoice-type OID = **2.5.4.12 (title)**, not 2.5.4.15 (that settles the SFIS duplicate). Add a VAT-number row: UID 0.9.2342.19200300.100.1.1 in the SAN. Add a template-name row: 1.3.6.1.4.1.311.20.2 = `ZATCA-Code-Signing`; the `TSTZATCA-` prefix is for non-production, and simulation is unknown (OQ-26). CN = free text; the ZATCA example is `TST-886431145-<VAT>`. **OU/O conflict:** SDK 2.0.3 forces 10-character TIN values, while SFIS/DTG say OU = branch name and O = name. Keep the docs' mapping, with OQ-28 remainder: "confirm with the current SDK/sandbox".

**7. 381/383 root.** The `Invoice` root with `InvoiceTypeCode` 381 or 383 passes XSD+EN+KSA (`std-381`, `std-383`, and both signed simplified versions). A `CreditNote` root fails XSD ("XML does not comply with UBL 2.1"). Only `UBL-Invoice-2.1.xsd` is shipped (Data and jar), and `BR-KSA-05` tests `InvoiceTypeCode`. 381/383 without BT-25 → BR-KSA-56. Without the reason (`PaymentMeans/cbc:InstructionNote`, KSA-10) → BR-KSA-17. → **§3.4:** root = `Invoice` for 381/383; remove the tag. **R-19:** reason goes in `cbc:InstructionNote` (BR-KSA-17); add an AT.

**8. Line vs header VAT tolerance (R-31).**
- There is **no** rule comparing the sum of line VAT with BT-110 (grep of both XSLs).
- BR-CO-14 (BT-110 = Σ BT-117) is exact.
- **BR-CO-17 / BR-S-09 in the bundled CEN XSL allow a tolerance:** `abs(BT-117) - 1 < round(taxable×rate) and abs(BT-117) + 1 > …`, i.e. |difference| **< 1.00**. Tests: AT-33(b), 4.50 vs 4.51 → **PASS**; 5.50 → PASS; 5.51 → BR-CO-17/BR-S-09 fail.
- BR-KSA-51 (line total = net + line VAT) is exact. BR-KSA-50 (line VAT = net × rate) is **exact with no rounding and applies to simplified documents only** in these 2021 rules. A simplified 10.03 line with 1.50 VAT fails BR-KSA-50.

→ **R-31:** keep the stricter local check (exact `round(…,2)`). Record that the SDK schematron accepts up to ±0.99, so local rejection is stricter than ZATCA; this is intended. AT-33(b) is a local-only rejection. Mark BR-KSA-50 "exact in SDK 2.0.3 rules (simplified); confirm rounding behaviour with the current rules", because exact equality cannot hold for most prices. OQ-28 remainder; it blocks simplified issuance tests only.

**9. 386 / OQ-29.** `std-386` and `simp-386` fail `BR-KSA-05` (allowed: `' 388 383 381 '`). The SDK has no compliance samples and no mention of prepayment. → **OQ-29:** **cannot be settled with SDK 2.0.3** because its rules predate 386. Keep the default (3/6 samples). Settle in the sandbox or with a current SDK. R-39 stays.

**10. API endpoints (OQ-30).**
- The jar and config hold **no** ZATCA URL or path. `strings` and `grep` find only W3C/ETSI namespace URIs. Config keys are only file paths and `certPassword`.
- `-invoiceRequest` (local) writes `{"invoiceHash", "uuid", "invoice"}` (the invoice is base64 of the signed XML). → **§6/OQ-30:** the request body **includes `uuid`**; settle that part ("body = `invoiceHash`, `uuid`, `invoice`").
- Paths and base URLs are still not settled from the SDK; keep them Swagger/config.
- `-nonprod` is the only environment switch, and it only changes the CSR template name. That is no evidence for or against "simulation" (OQ-26).

**11. Python feasibility: yes.**
- `pysign.py` uses only lxml and cryptography (no signxml or xmlsec). From the unsigned `simp-388.xml` and the SDK dummy key/cert it produces a signed invoice. SDK `-validate` gives **PASSED** for XSD, EN, KSA, QR, SIGNATURE and PIH (global PASSED). The hash is byte-identical to the SDK (item 4).
- The same script with the DTG QR layout fails only on the SDK's old QR rules (item 5). With the other provider's issuer-name style it fails `wrong X509IssuerName` (item 3).
- signxml/xmlsec are not needed and would not reproduce the custom digests (hex-text CertDigest and SignedProperties, the signature over the hash). → **§6 libraries:** "XML signature: hand-built on lxml + cryptography (proven against SDK 2.0.3). signxml/python-xmlsec not used." Close OQ-11 for C14N/hash.

## Spec edit list (for the spec owner)
1. R-14: curve settled; (a) signs raw hash via SHA256withECDSA; (b) no SFIS-only elements; (c) Type = `xmldsig#SignatureProperties`. Change the SignedProperties digest to base64-of-hex. CertDigest = SHA-256 of the base64 certificate text, hex, base64. Issuer-name format and SigningTime in UTC. Add ATs for the signature and the two digests.
2. R-13 / AT-13 / AT-10 / OQ-11: confirmed; lxml c14n matches; fixtures from this run.
3. R-15 / AT-14 / AT-56: tag 6 = base64 text. Tags 7–9 per DTG but open against a current SDK. TLV long-length open; cap values at 127 bytes meanwhile.
4. §3.2: OIDs SN 2.5.4.4, UID, title 2.5.4.12, registeredAddress, businessCategory; template-name extension; OU/O conflict open.
5. §3.4 / R-19: Invoice root for 381/383; reason in `InstructionNote`.
6. R-31 / AT-33: no Σline check in the SDK; the SDK allows <1.00 category tolerance; the local rule stays stricter; BR-KSA-50 open.
7. OQ-29: not settleable with 2.0.3 (386 rejected). OQ-30: `uuid` is in the body; paths still open.
8. OQ-28: close items 1, 2, 3(a–c), tag 6 and the CSR OIDs. Open remainder: QR tags 7–9 layout, TLV length form, OU/O content and BR-KSA-50 rounding. Re-run this kit (`sdk-work/scripts`) against the newest SDK on ZATCA's DownloadSDK page before building R-15.
9. §6: the SDK in CI needs Java 11, or BouncyCastle as provider 1 on newer JVMs. Licence: LGPL-3, stated in `LICENSE.txt` (update the "licence not stated" note; legal sign-off still applies).
