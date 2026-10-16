# ZATCA SDK verification kit (2026-10-16)

Scripts and fake test invoices used to check the ZATCA SDK 2.0.3 offline.
Findings: `../zatca-sdk-findings-2026-10-16.md`.

- `inv/` – fake UBL test documents (dummy VAT numbers); candidates for the AT-13 fixtures.
- `scripts/pyhash.py` – invoice hash with lxml (matched SDK `-generateHash` on all test documents).
- `scripts/pysign.py` – proof that lxml + cryptography produce a signed simplified invoice the SDK validates.
- `scripts/fatbc.sh` – runs the SDK offline with BouncyCastle first (needed on Java 17+ for secp256k1).

The SDK itself (LGPL-3) is not stored here; download it from ZATCA's DownloadSDK page.
Paths inside the scripts point to the build session's scratch area; adjust before reuse.
