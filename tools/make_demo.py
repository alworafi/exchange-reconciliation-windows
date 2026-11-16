"""ينشئ كشفين تجريبيين (نظام + صراف) في مجلد demo لتجربة البرنامج.

البيانات وهمية بالكامل لأغراض التجربة. الاستخدام:
    python tools/make_demo.py
"""
from datetime import date
from pathlib import Path

from openpyxl import Workbook

OUT = Path(__file__).resolve().parent.parent / "demo"

SYSTEM = [
    (date(2026, 9, 10), "9001", "حوالة واردة عبر الصراف - أحمد سعيد", 1_200_000, None, "ريال يمني"),
    (date(2026, 9, 20), "9002", "حوالة واردة - محمد علي", 400_850, None, "ريال يمني"),
    (date(2026, 10, 5), "1001", "حوالة واردة - علي ناصر", 1_000_000, None, "ريال يمني"),
    (date(2026, 10, 10), "1002", "حوالة واردة - سالم صالح", 250_000, None, "ريال يمني"),
    (date(2026, 10, 15), "1003", "حوالة واردة - خالد عمر", 583_700, None, "ريال يمني"),
    (date(2026, 10, 20), "1004", "سداد للصراف نقدا", None, 300_000, "ريال يمني"),
    (date(2026, 10, 3), "S10", "حوالة - فهد الحربي", 5_000, None, "ريال سعودي"),
    (date(2026, 10, 9), "S11", "حوالة مجمعة", 3_000, None, "ريال سعودي"),
]
EXCHANGER = [
    (date(2026, 9, 10), "9001", "حواله صادره احمد سعيد", None, 1_200_000, "يمني"),
    (date(2026, 10, 5), "1001", "حواله صادره علي ناصر", None, 1_000_000, "يمني"),
    (date(2026, 10, 11), "1002", "حواله سالم صالح دفعه اولى", None, 200_000, "يمني"),
    (date(2026, 10, 22), "1004", "استلام نقدية من الشركة", 300_000, None, "يمني"),
    (date(2026, 10, 4), "S10", "حواله فهد الحربي", None, 5_000, "سعودي"),
    (date(2026, 10, 9), "", "حواله 1", None, 1_000, "سعودي"),
    (date(2026, 10, 10), "", "حواله 2", None, 2_000, "سعودي"),
    (date(2026, 10, 25), "S99", "عمولة غير مسجلة لديكم", 150, None, "سعودي"),
]


def write(path, title, rows):
    wb = Workbook()
    ws = wb.active
    ws.sheet_view.rightToLeft = True
    ws.append([title])
    ws.append([])
    ws.append(["التاريخ", "رقم المستند", "البيان", "مدين", "دائن", "العملة"])
    for r in rows:
        ws.append(list(r))
    wb.save(path)


if __name__ == "__main__":
    import sys
    try:  # نافذة أوامر Windows قد لا تدعم العربية بترميزها الافتراضي
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    if len(sys.argv) > 1:
        OUT = Path(sys.argv[1])
    OUT.mkdir(parents=True, exist_ok=True)
    write(OUT / "كشف_النظام_تجريبي.xlsx", "كشف حساب الصراف في النظام (تجريبي)", SYSTEM)
    write(OUT / "كشف_الصراف_تجريبي.xlsx", "كشف حسابكم لدينا (تجريبي)", EXCHANGER)
    print("تم إنشاء الملفات في", OUT)
