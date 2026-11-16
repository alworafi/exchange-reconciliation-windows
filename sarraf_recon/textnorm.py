"""توحيد النصوص العربية لأغراض المقارنة (الأسماء، الأوصاف، المراجع)."""
from __future__ import annotations

import re
from difflib import SequenceMatcher

_DIACRITICS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_CHAR_MAP = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
    "ى": "ي", "ئ": "ي", "ؤ": "و", "ة": "ه", "ک": "ك", "ی": "ي",
})
# كلمات شائعة في الكشوفات لا تميّز حركة عن أخرى
STOPWORDS = {
    "ال", "من", "الى", "الي", "عن", "على", "علي", "في", "و", "او", "رقم", "حواله", "حوالات",
    "تحويل", "قيد", "سند", "عمليه", "مبلغ", "لحساب", "حساب", "بواسطه", "عبر", "ر", "ي", "س",
    "ريال", "يمني", "سعودي", "دولار", "صرف", "عموله", "the", "to", "from", "of", "no",
}


def normalize(text: str | None) -> str:
    if not text:
        return ""
    t = str(text).translate(_ARABIC_DIGITS)
    t = _DIACRITICS.sub("", t)
    t = t.translate(_CHAR_MAP).lower()
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def tokens(text: str | None) -> set[str]:
    out = set()
    for tok in normalize(text).split():
        if tok.startswith("ال") and len(tok) > 4:
            tok = tok[2:]
        if tok not in STOPWORDS and len(tok) > 1:
            out.add(tok)
    return out


def normalize_ref(ref: str | None) -> str:
    """المرجع: نحتفظ بالحروف اللاتينية والأرقام فقط، ونحذف الأصفار البادئة."""
    if not ref:
        return ""
    t = str(ref).translate(_ARABIC_DIGITS).upper()
    if re.fullmatch(r"\d+\.0+", t.strip()):        # 12345.0 من Excel
        t = t.split(".")[0]
    t = re.sub(r"[^0-9A-Z]", "", t)
    return t.lstrip("0") or ("0" if t else "")


def numbers_in(text: str | None) -> set[str]:
    """أرقام طويلة (5 خانات فأكثر) داخل البيان، غالبًا أرقام حوالات."""
    if not text:
        return set()
    t = str(text).translate(_ARABIC_DIGITS)
    return {n.lstrip("0") for n in re.findall(r"\d{5,}", t)}


def similarity(a: str | None, b: str | None) -> float:
    """تشابه بين 0 و 1 يجمع تقاطع الكلمات وتشابه الحروف."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    jacc = len(ta & tb) / len(ta | tb)
    seq = SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    return max(jacc, seq * 0.9)
