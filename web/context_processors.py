"""Template context of every page: language and direction (web.md R27)."""
from django.utils import translation


def ui(request):
    lang = getattr(request, "web_lang", None) or ("ar" if (translation.get_language() or "").startswith("ar") else "en")
    return {"ui": {"lang": lang, "dir": "rtl" if lang == "ar" else "ltr", "other_lang": "en" if lang == "ar" else "ar"}}
