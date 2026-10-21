"""Minimal Arabic shaping and right-to-left ordering for PDF text (reportlab draws glyphs in logical order and has no
shaping of its own without extra libraries).

Letters take their contextual presentation forms (Unicode "Arabic Presentation Forms-B", looked up by name), lam-alef
ligatures are joined, and each line is reordered for display: Arabic runs right to left, Latin letters and digits
kept left to right. Enough for the governance report's labels and names; not a full bidi implementation.
"""
import re
import unicodedata

# Letters that join only to the preceding letter (never to the following one).
RIGHT_JOINING = set("اأإآءؤةدذرزوىٱ")
TRANSPARENT = re.compile(r"[ً-ٰٟۖ-ۭ]")
ARABIC = re.compile(r"[؀-ۿ]")
LTR_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .,:;/\-+%()#'_@]*[A-Za-z0-9)%]|[A-Za-z0-9]")
LAM_ALEF = {"ا": "ALEF", "أ": "ALEF WITH HAMZA ABOVE", "إ": "ALEF WITH HAMZA BELOW", "آ": "ALEF WITH MADDA ABOVE"}


def _form(ch, form):
    try:
        return unicodedata.lookup(f"{unicodedata.name(ch)} {form} FORM")
    except (KeyError, ValueError):
        return ch


def _joins(ch):
    return bool(ch) and ARABIC.match(ch) is not None and not TRANSPARENT.match(ch) and ch not in "ءـ،؛؟٠١٢٣٤٥٦٧٨٩"


def shape(text):
    """Contextual forms and lam-alef ligatures, still in logical order."""
    chars = [c for c in text if not TRANSPARENT.match(c)]
    out, i = [], 0
    while i < len(chars):
        ch = chars[i]
        prev = chars[i - 1] if i else ""
        joins_prev = _joins(prev) and prev not in RIGHT_JOINING
        if ch == "ل" and i + 1 < len(chars) and chars[i + 1] in LAM_ALEF:
            name = f"ARABIC LIGATURE LAM WITH {LAM_ALEF[chars[i + 1]]} {'FINAL' if joins_prev else 'ISOLATED'} FORM"
            try:
                out.append(unicodedata.lookup(name))
            except KeyError:
                out.extend(chars[i:i + 2])
            i += 2
            continue
        if not _joins(ch):
            out.append(ch)
            i += 1
            continue
        nxt = chars[i + 1] if i + 1 < len(chars) else ""
        joins_next = ch not in RIGHT_JOINING and _joins(nxt)
        if joins_prev and joins_next:
            out.append(_form(ch, "MEDIAL"))
        elif joins_prev:
            out.append(_form(ch, "FINAL"))
        elif joins_next:
            out.append(_form(ch, "INITIAL"))
        else:
            out.append(_form(ch, "ISOLATED"))
        i += 1
    return "".join(out)


def visual(text):
    """``text`` shaped and in display order for a right-to-left line (Latin and digit runs stay left to right)."""
    if not ARABIC.search(text or ""):
        return text
    shaped = shape(text)
    parts, pos = [], 0
    for m in LTR_RUN.finditer(shaped):
        if m.start() > pos:
            parts.append(shaped[pos:m.start()][::-1])
        parts.append(m.group(0))
        pos = m.end()
    if pos < len(shaped):
        parts.append(shaped[pos:][::-1])
    mirrored = {"(": ")", ")": "(", "[": "]", "]": "[", "«": "»", "»": "«"}
    return "".join("".join(mirrored.get(c, c) for c in p) if ARABIC.search(p) else p for p in reversed(parts))
