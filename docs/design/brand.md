# Almiras brand rules for the ERP web screens

Source: *Almiras Visual Identity Guide* (دليل الهوية البصرية), version 1.0, October 2026, supplied by the project owner on 2026-10-18.
This page turns the guide into rules for the web UI (`docs/specs/web.md`). The guide itself is not stored in the repository; the logo files below were taken from it unchanged.

## Logo

| File | Use |
| --- | --- |
| `brand/almiras-logo-ar.svg` | Arabic logo (horse-head symbol + Arabic name). Default in the RTL interface and in bilingual material. |
| `brand/almiras-logo-en.svg` | English logo. Used in the LTR (English) interface. |
| `brand/almiras-brand-shape.svg` | Supporting brand shape (graphic element), e.g. login screen and empty states. |

- Never place the Arabic and English logos together in one lock-up.
- Clear space on every side = half the height of the symbol.
- Minimum on screen: **140 px** wide for either full logo, **24 px** for the symbol alone (use the symbol below 140 px, e.g. a collapsed sidebar or favicon).
- On white or sand backgrounds: full-colour logo. On navy or dark images: name in white, symbol keeps its gradient. On busy images: all-white logo.
- Do not stretch, rotate, recolour, add shadows/effects, re-set the name in a font, frame it, or apply the symbol gradient to the name.

## Colour

Primary palette and reference balance **55 / 30 / 10 / 5** (navy / sand / orange / white):

| Token | Name | Hex | Role |
| --- | --- | --- | --- |
| `--alm-navy` | Navy · الكحلي | `#0B2A3C` | Primary: navigation, headers, primary text on light |
| `--alm-sand` | Sand · الرملي | `#FFEEE8` | Calm surfaces, page and panel backgrounds |
| `--alm-orange` | Orange · البرتقالي | `#DF5323` | Highlight only: large titles, key graphic elements, focus/accent |
| `--alm-white` | White · الأبيض | `#FFFFFF` | Supporting surface (cards, inputs, tables) |

Supporting tints (charts and secondary UI only, never replacing the primaries):

| Family | 1 | 2 | 3 ★ | 4 | 5 |
| --- | --- | --- | --- | --- | --- |
| Orange | `#F9E4DD` | `#EFA78B` | `#DF5323` | `#9B3317` | `#54170A` |
| Navy | `#A7B4B9` | `#5E7580` | `#0B2A3C` | `#081C26` | `#030F17` |
| Sand | `#FFEEE8` | `#FFD0BE` | `#FFC1A7` | `#FFB193` | `#BE8670` |

Contrast rules from the guide, with WCAG 2.1 AA ratios checked for the UI (web.md R35 requires ≥ 4.5:1 for body text):

| Text / element on background | Ratio | UI rule |
| --- | --- | --- |
| Navy on sand | 13.22 | Body text: yes |
| Navy on white | 14.89 | Body text: yes |
| Sand on navy | 13.22 | Body text on navy panels: yes |
| Orange on white | 3.89 | **Large text (≥ 24 px, or ≥ 19 px bold) and icons only** |
| Orange on sand | 3.45 | Large text and graphics only |
| White on orange | 3.89 | Large symbols and large labels only |
| Navy on orange | 3.83 | Large symbols and large labels only |
| Orange 4 `#9B3317` on white | 7.29 | Use for small "accent" text, links and error text instead of orange |
| Navy 2 `#5E7580` on white | 4.85 | Secondary / muted text on white |
| Navy 2 `#5E7580` on sand | 4.30 | **Not** for small text on sand; use navy |

So: orange is never used for small text. Primary buttons are **navy with white or sand text**; orange marks emphasis (page title first line, active tab underline, focus ring, key figures). Status colours must not rely on colour alone (web.md R35).

## Typography

| Use | Arabic | Latin |
| --- | --- | --- |
| Headings, labels | HT Rakik Medium | Bricolage Grotesque Bold (headlines, large figures) |
| Body text | HT Rakik Light | HT Rakik Light |
| Short labels | HT Rakik Medium | HT Rakik Medium |
| Large numbers / KPIs | — | Bricolage Grotesque Bold |

- Arabic text right-aligned; line height 130 % for headings, 185 % for body text; natural letter spacing (no tracking).
- The first line of a major title may be orange; the rest navy on light, sand on dark.
- **Licensing:** Bricolage Grotesque is under the SIL Open Font License and is self-hosted with the app's static files. **HT Rakik** is licensed from Hadi Type: a Desktop Font License (design work, up to 5 devices) and, from 2026-10-19, a **webfont licence** for Light and Medium (invoice HT2610260140, 20,000 pageviews per weight). The web licence lets the ERP serve the two WOFF2 files to browsers. The licence still forbids distributing, sharing, renaming or modifying the font files, so:
  - the WOFF2 files live **only on the ERP host** in `deploy/brand-fonts/` (served by the proxy at `/brand-fonts/`), never in git, the Docker image or backups shared outside Almiras; `.gitignore` blocks them;
  - file names and font data stay exactly as delivered;
  - **pageview limit:** 20,000 pageviews per weight. Hadi Type's invoice does not say whether this is per month or in total. An internal ERP can pass it quickly (e.g. 50 users × 100 page loads a day ≈ 100,000 a month); fonts are cached by the browser for a year, but the licence counts pageviews, not downloads. Confirm the counting period with Hadi Type and upgrade the tier before go-live if needed;
  - if the files are absent, the UI falls back to IBM Plex Sans Arabic (OFL).
  - Generated PDFs (invoices, reports) use IBM Plex Sans Arabic: embedding HT Rakik in server-generated PDFs needs a server/app licence, which the web licence does not cover.

```css
@font-face { font-family: 'HT Rakik'; font-weight: 300; font-style: normal; font-display: swap;
  src: url('/brand-fonts/HT%20Rakik%20-%20Light.woff2') format('woff2'); }
@font-face { font-family: 'HT Rakik'; font-weight: 500; font-style: normal; font-display: swap;
  src: url('/brand-fonts/HT%20Rakik%20-%20Medium.woff2') format('woff2'); }
```

## Layout and shape

- Outer margin 4 % of the shorter side of the screen; content aligned to a column grid (12 columns on desktop).
- Corner radius from the guide's components: 20 px for cards/panels, 16 px for small elements; tables and inputs keep square or small (4 px) corners for density.
- Concept icons (Order, Leadership, Growth …) and photography rules apply to the login screen, dashboards' empty states and reports, not to dense data screens.

## Tokens for the web app

```css
:root {
  --alm-navy: #0B2A3C;   --alm-sand: #FFEEE8;   --alm-orange: #DF5323;  --alm-white: #FFFFFF;
  --alm-orange-1: #F9E4DD; --alm-orange-2: #EFA78B; --alm-orange-4: #9B3317; --alm-orange-5: #54170A;
  --alm-navy-1: #A7B4B9;   --alm-navy-2: #5E7580;   --alm-navy-4: #081C26;   --alm-navy-5: #030F17;
  --alm-sand-2: #FFD0BE;   --alm-sand-3: #FFC1A7;   --alm-sand-4: #FFB193;   --alm-sand-5: #BE8670;
  --alm-text: var(--alm-navy);        --alm-text-muted: var(--alm-navy-2);
  --alm-accent-text: var(--alm-orange-4);
  --alm-surface: var(--alm-white);    --alm-page: var(--alm-sand);
  --alm-radius: 20px; --alm-radius-sm: 16px; --alm-radius-input: 4px;
  --alm-font-ar: 'HT Rakik', 'IBM Plex Sans Arabic', sans-serif;
  --alm-font-display: 'Bricolage Grotesque', 'IBM Plex Sans Arabic', sans-serif;
  --alm-lh-heading: 1.3; --alm-lh-body: 1.85;
}
```

Bootstrap 5.3 (web.md OQ-5) is themed through these tokens: `--bs-primary` = navy, `--bs-body-bg` = sand, `--bs-body-color` = navy, `--bs-link-color` = orange 4, focus ring = orange.
