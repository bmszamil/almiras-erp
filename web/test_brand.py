"""Brand rules in the web UI (docs/design/brand.md): logo files, fonts and licences, contrast, and HT Rakik never in
the repository."""
import hashlib
import re
import subprocess
from pathlib import Path

import pytest

from web.brand_assets import TARGET, derived

WEB = Path(__file__).resolve().parent
REPO = WEB.parent
STATIC = WEB / "static" / "web"
CSS = re.sub(r"/\*.*?\*/", "", (STATIC / "css" / "almiras.css").read_text(encoding="utf-8"), flags=re.S)
FONTS_CSS = (STATIC / "css" / "fonts.css").read_text(encoding="utf-8")

# Upstream files, byte for byte (npm: bootstrap 5.3.8, @fontsource/bricolage-grotesque 5.3.0,
# @ibm/plex-sans-arabic 1.1.0). A changed hash means the vendored file is no longer the released one.
VENDORED = {
    "vendor/bootstrap-5.3.8/css/bootstrap.min.css": "d85327d99c7a3ee1f9b5d0500d1370acea3ad2db39c163c2f51f232baedbdede",
    "vendor/bootstrap-5.3.8/css/bootstrap.rtl.min.css": "b9048c83571ab73da54395b6b307219f9eb9c5447f4f8edb17a069eb0f19465a",
    "fonts/bricolage-grotesque/bricolage-grotesque-latin-700-normal.woff2":
        "4c373ce3c1cca41c864eb3e27c059a59fc6310547ab9c9b6cd780d387ba24206",
    "fonts/bricolage-grotesque/bricolage-grotesque-latin-ext-700-normal.woff2":
        "25d714d702deab603819c7fbc619d6d63d86a0d055fa61b3b9f0453f9d5e697a",
    "fonts/ibm-plex-sans-arabic/IBMPlexSansArabic-Light.woff2":
        "a9f0b2eda9dda69757567065818a78dd2c578866ca6f7ed45089f8ca532a8a4d",
    "fonts/ibm-plex-sans-arabic/IBMPlexSansArabic-Regular.woff2":
        "74112e1b7bf2e2c547989a4e947a60cba6cd1f9156e0065670134636e717199d",
    "fonts/ibm-plex-sans-arabic/IBMPlexSansArabic-Medium.woff2":
        "c72f6986f50ded745d06e71e508096209c52a55a6b3a81029954e33c52ba1dd2",
    "fonts/ibm-plex-sans-arabic/IBMPlexSansArabic-Bold.woff2":
        "db6650a05969c144ab5ab9bb6c3beca6ca4dbb3537bb5f2311c878dd293875b4",
}


@pytest.mark.parametrize("name,digest", VENDORED.items())
def test_vendored_files_are_the_released_ones(name, digest):
    assert hashlib.sha256((STATIC / name).read_bytes()).hexdigest() == digest


def test_licence_files_ship_with_the_vendored_assets():
    assert "MIT License" in (STATIC / "vendor/bootstrap-5.3.8/LICENSE").read_text(encoding="utf-8")
    for folder in ("bricolage-grotesque", "ibm-plex-sans-arabic"):
        assert "SIL OPEN FONT LICENSE Version 1.1" in (STATIC / "fonts" / folder / "OFL.txt").read_text(encoding="utf-8")


def test_ht_rakik_only_through_the_brand_font_face_rules():
    rules = re.findall(r"@font-face\s*{[^}]*'HT Rakik'[^}]*}", FONTS_CSS)
    assert len(rules) == 2
    assert "url('/brand-fonts/HT%20Rakik%20-%20Light.woff2')" in rules[0] and "font-weight: 300" in rules[0]
    assert "url('/brand-fonts/HT%20Rakik%20-%20Medium.woff2')" in rules[1] and "font-weight: 500" in rules[1]
    # The fallback (IBM Plex Sans Arabic, self-hosted) follows HT Rakik in every stack that names it.
    assert "--alm-font-ar: 'HT Rakik', 'IBM Plex Sans Arabic', sans-serif;" in CSS
    assert "--alm-font-display: 'Bricolage Grotesque', 'IBM Plex Sans Arabic', sans-serif;" in CSS


FONT_SUFFIXES = (".woff2", ".woff", ".otf", ".ttf")


def _repository_files():
    """Tracked files; without version control (an exported tree, a container), every file on disk except the
    host-only font folder deploy/brand-fonts/, which is where the licensed files are allowed to live."""
    try:
        listed = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True).stdout
        return listed.splitlines()
    except (OSError, subprocess.CalledProcessError):
        skip = {".git", "__pycache__", "node_modules"}
        return [p.relative_to(REPO).as_posix() for p in REPO.rglob("*")
                if p.is_file() and not skip & set(p.relative_to(REPO).parts)
                and not p.relative_to(REPO).as_posix().startswith("deploy/brand-fonts/")]


def test_no_ht_rakik_font_file_anywhere_in_the_repository():
    assert not [f for f in _repository_files() if "rakik" in f.lower()]
    assert not [p for p in WEB.rglob("*") if "rakik" in p.name.lower()]  # also not untracked in the app


def test_the_fallback_file_scan_works_without_version_control(monkeypatch):
    def unavailable(*args, **kwargs):
        raise FileNotFoundError("no version control here")

    monkeypatch.setattr(subprocess, "run", unavailable)
    files = _repository_files()
    assert "web/test_brand.py" in files and not [f for f in files if f.startswith("deploy/brand-fonts/")]
    assert not [f for f in files if "rakik" in f.lower() and f.endswith(FONT_SUFFIXES)]


def test_logo_files_are_the_brand_files_or_their_defined_variants():
    for name, content in derived().items():
        assert (TARGET / name).read_text(encoding="utf-8") == content, name
    assert derived()["almiras-logo-ar.svg"] == (REPO / "docs/design/brand/almiras-logo-ar.svg").read_text(encoding="utf-8")
    on_navy = derived()["almiras-logo-ar-on-navy.svg"]
    assert 'fill="#FFFFFF"' in on_navy and "url(#gar1)" in on_navy  # name white, symbol keeps its gradient


def test_the_mislabelled_brand_shape_is_not_served():
    """docs/design/brand/almiras-brand-shape.svg draws another company's logo ("Rabeez"), so the UI never uses it."""
    assert not (TARGET / "almiras-brand-shape.svg").exists()
    for path in [*(WEB / "templates").rglob("*.html"), *(STATIC / "css").glob("*.css")]:
        assert "brand-shape" not in path.read_text(encoding="utf-8"), path


def test_logo_size_rules_in_the_layout():
    assert re.search(r"\.alm-logo--full \{ inline-size: 176px;", CSS)  # full logo at 140 px or more
    assert re.search(r"\.alm-auth__logo img \{[^}]*min-inline-size: 140px", CSS)
    assert re.search(r"@media \(max-width: 991\.98px\) \{[^@]*\.alm-logo--full \{ display: none; \}[^@]*"
                     r"\.alm-logo--symbol \{ display: block; \}", CSS)  # symbol where there is less room


def test_orange_is_used_for_text_only_in_the_large_title():
    text_rules = re.findall(r"([^{}]+)\{[^}]*(?<![-\w])color:\s*var\(--alm-orange\)[^}]*\}", CSS)
    assert [r.strip() for r in text_rules] == [".alm-title"]
    assert re.search(r"\.alm-title \{ font-size: clamp\(1\.75rem", CSS)  # ≥ 24 px: large text
    assert "--bs-link-color: var(--alm-orange-4);" in CSS  # small accent text: orange 4


def _luminance(hex_colour):
    channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a, b):
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


TOKENS = dict(re.findall(r"(--alm-[\w-]+):\s*(#[0-9A-Fa-f]{6})", CSS))


@pytest.mark.parametrize("fg,bg", [
    ("--alm-navy", "--alm-sand"), ("--alm-navy", "--alm-white"), ("--alm-sand", "--alm-navy"),
    ("--alm-white", "--alm-navy"), ("--alm-white", "--alm-navy-4"), ("--alm-navy-1", "--alm-navy"),
    ("--alm-orange-4", "--alm-white"), ("--alm-orange-4", "--alm-sand"), ("--alm-orange-5", "--alm-orange-1"),
    ("--alm-navy", "--alm-sand-2"), ("--alm-navy", "--alm-sand-3"), ("--alm-navy", "--alm-navy-1"),
    ("--alm-white", "--alm-orange-4"), ("--alm-navy-2", "--alm-white"),
])
def test_r35_small_text_colour_pairs_meet_aa(fg, bg):
    assert contrast(TOKENS[fg], TOKENS[bg]) >= 4.5, (fg, bg)


def test_contrast_matches_the_brand_table():
    assert round(contrast("#0B2A3C", "#FFEEE8"), 2) == 13.22
    assert round(contrast("#9B3317", "#FFFFFF"), 2) == 7.29
    assert contrast("#DF5323", "#FFEEE8") < 4.5  # why orange is never small text
