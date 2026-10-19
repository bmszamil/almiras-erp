# Brand web fonts (host only)

Put the licensed HT Rakik web fonts here on the production host; they are served at `/brand-fonts/` by the proxy.

| File name (exactly) | Weight |
| --- | --- |
| `HT Rakik - Light.woff2` | 300 (Light, body text) |
| `HT Rakik - Medium.woff2` | 500 (Medium, headings and labels) |

- Licence: Hadi Type webfont licence, invoice HT2610260140 (2026-10-19), 20,000 pageviews per weight. Keep the invoice with Almiras's licence records.
- Never commit these files, copy them into the image, or share them outside the ERP host (Hadi Type licence: no distribution). `.gitignore` blocks them.
- Keep the supplier's file names and font data exactly as delivered (the licence forbids renaming or modifying the font).
- If the files are missing, the UI falls back to IBM Plex Sans Arabic automatically.
