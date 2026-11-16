"""المبالغ المالية: تحليل دقيق بـ Decimal وتخزين كأعداد صحيحة بالوحدة الصغرى.

لا يُستخدم float في أي حساب مالي. تُخزَّن المبالغ في SQLite كعدد صحيح
(المبلغ × 10^SCALE) حتى تكون الجمع داخل قاعدة البيانات دقيقًا أيضًا.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

SCALE = 2
_Q = Decimal(1).scaleb(-SCALE)          # 0.01
_FACTOR = 10 ** SCALE
ZERO = Decimal("0.00")

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


class AmountError(ValueError):
    pass


def q(value: Decimal) -> Decimal:
    """تقريب إلى خانتين عشريتين (تقريب محاسبي نصف لأعلى)."""
    return value.quantize(_Q, rounding=ROUND_HALF_UP)


def to_decimal(value) -> Decimal | None:
    """يحوّل أي قيمة خلية إلى Decimal. يرجع None للخلية الفارغة.

    يدعم: الأرقام العربية/الفارسية، الفواصل (، ٬ ,)، الفاصلة العشرية العربية (٫)،
    الأقواس للسالب، الإشارة اللاحقة (123-)، ورموز العملات المكتوبة بجانب الرقم.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise AmountError(f"قيمة غير رقمية: {value!r}")
    if isinstance(value, Decimal):
        return q(value)
    if isinstance(value, int):
        return q(Decimal(value))
    if isinstance(value, float):
        # الخلايا الرقمية في Excel تصل float؛ نمرّ عبر repr لتجنب أخطاء التمثيل الثنائي
        return q(Decimal(repr(value)))
    text = str(value).strip()
    if not text or text in {"-", "—", "–"}:
        return None
    text = text.translate(_ARABIC_DIGITS)
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative, text = True, text[1:-1]
    text = text.replace("٫", ".").replace("٬", "").replace("،", "").replace(",", "")
    text = text.replace("‏", "").replace("‎", "").replace("\xa0", "").replace(" ", "")
    if text.endswith("-"):
        negative, text = True, text[:-1]
    if text.startswith("-"):
        negative, text = (not negative), text[1:]
    text = text.lstrip("+")
    m = re.search(r"\d+(?:\.\d+)?", text)
    if not m:
        raise AmountError(f"لا يمكن قراءة المبلغ: {value!r}")
    rest = (text[: m.start()] + text[m.end():])
    if re.search(r"\d", rest):
        raise AmountError(f"المبلغ يحتوي أكثر من رقم: {value!r}")
    try:
        d = Decimal(m.group(0))
    except InvalidOperation as exc:  # pragma: no cover
        raise AmountError(f"مبلغ غير صالح: {value!r}") from exc
    return q(-d if negative else d)


def to_minor(value: Decimal) -> int:
    """Decimal -> عدد صحيح بالوحدة الصغرى (للتخزين)."""
    return int(q(value) * _FACTOR)


def from_minor(value: int | None) -> Decimal:
    if value is None:
        return ZERO
    return q(Decimal(int(value)) / _FACTOR)


def fmt(value: Decimal | int | None, minor: bool = False) -> str:
    """تنسيق للعرض: 1,234,567.50"""
    if value is None:
        return ""
    d = from_minor(value) if minor else q(Decimal(value))
    return f"{d:,.2f}"
