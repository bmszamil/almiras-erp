"""Every UI text in English and Arabic, and no literal text in templates (web.md R28, WT-13)."""
import re
import unicodedata
from pathlib import Path

from core.models import label_part
from web.texts import TEXTS, text

WEB = Path(__file__).resolve().parent
TEMPLATES = sorted((WEB / "templates").rglob("*.html"))
ARABIC = re.compile(r"[؀-ۿ]")
TEXT_ATTRS = ("alt", "title", "aria-label", "placeholder")


def test_wt13_every_text_has_english_and_arabic():
    for key, pair in TEXTS.items():
        assert isinstance(pair, tuple) and len(pair) == 2, key
        en, ar = pair
        assert en.strip() and ar.strip(), key
        if key not in {"switch_to_en", "switch_to_ar"}:  # a language's own name is shown in that language
            assert ARABIC.search(ar), key
            assert not ARABIC.search(en), key


def test_text_placeholders_match_in_both_languages():
    for key, (en, ar) in TEXTS.items():
        assert set(re.findall(r"{(\w+)}", en)) == set(re.findall(r"{(\w+)}", ar)), key


def _strip_template(source):
    source = re.sub(r"{#.*?#}", " ", source, flags=re.S)
    source = re.sub(r"{%.*?%}", " ", source, flags=re.S)
    source = re.sub(r"{{.*?}}", " ", source, flags=re.S)
    source = re.sub(r"<!doctype[^>]*>", " ", source, flags=re.I)
    return source


def test_wt13_templates_have_no_literal_text_outside_the_text_tag():
    for path in TEMPLATES:
        stripped = _strip_template(path.read_text(encoding="utf-8"))
        for attr in TEXT_ATTRS:  # user-facing attribute values come from the text tag as well
            for value in re.findall(rf'\s{attr}="([^"]*)"', stripped):
                assert not any(unicodedata.category(c).startswith("L") for c in value), (path.name, attr, value)
        between_tags = re.sub(r"<[^>]*>", " ", stripped)
        letters = [c for c in between_tags if unicodedata.category(c).startswith("L")]
        assert not letters, (path.name, "".join(letters)[:80])


def test_every_text_key_used_in_templates_exists():
    used = set()
    for path in TEMPLATES:
        used |= set(re.findall(r"""{%\s*t\s+["']([\w.]+)["']""", path.read_text(encoding="utf-8")))
    missing = used - set(TEXTS)
    assert not missing, missing


def test_wt13_a_model_label_renders_only_the_active_half():
    assert label_part("Draft / مسودة", "ar") == "مسودة" and label_part("Draft / مسودة", "en") == "Draft"


def test_text_formats_values():
    assert text("page_of", "ar", page=2, pages=5) == "الصفحة 2 من 5"
    assert text("page_of", "en", page=2, pages=5) == "Page 2 of 5"
