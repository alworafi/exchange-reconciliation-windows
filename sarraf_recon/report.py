"""تقرير Excel منظم (من اليمين لليسار) للمطابقة والفروقات."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from . import reconciliation as rc
from .money import from_minor
from .services import KIND_LABEL, SIDE_LABEL, STATUS_LABEL, audit, match_items, match_rows, txn_rows
from .db import Tx

NUM = '#,##0.00;[Red]-#,##0.00'
HEAD_FILL = PatternFill("solid", fgColor="1F4E5A")
HEAD_FONT = Font(bold=True, color="FFFFFF", name="Tahoma")
TITLE_FONT = Font(bold=True, size=14, name="Tahoma")
BOLD = Font(bold=True, name="Tahoma")
BAD_FILL = PatternFill("solid", fgColor="F8D7DA")
OK_FILL = PatternFill("solid", fgColor="D4EDDA")
THIN = Border(*(Side(style="thin", color="BBBBBB"),) * 4)


def _sheet(wb, title):
    ws = wb.create_sheet(title[:31])
    ws.sheet_view.rightToLeft = True
    return ws


def _table(ws, start_row: int, headers: list[str], rows: list[list], widths: list[int] | None = None) -> int:
    for j, h in enumerate(headers, 1):
        c = ws.cell(row=start_row, column=j, value=h)
        c.fill, c.font, c.border = HEAD_FILL, HEAD_FONT, THIN
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    r = start_row
    for r_i, row in enumerate(rows, start_row + 1):
        r = r_i
        for j, v in enumerate(row, 1):
            c = ws.cell(row=r_i, column=j, value=v)
            c.border = THIN
            if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
                c.number_format = NUM
    if widths:
        for j, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(j)].width = w
    ws.freeze_panes = ws.cell(row=start_row + 1, column=1)
    if rows:
        ws.auto_filter.ref = f"A{start_row}:{get_column_letter(len(headers))}{r}"
    return r + 1


def build_report(conn, account_id: int, date_from: date, date_to: date, path: str | Path,
                 currencies: list[str] | None = None) -> Path:
    acc = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    curs = currencies or rc.account_currencies(conn, account_id)
    recs = [rc.compute(conn, account_id, c, date_from, date_to) for c in curs]
    wb = Workbook()
    wb.remove(wb.active)

    # ---------------- الملخص
    ws = _sheet(wb, "الملخص")
    ws["A1"] = f"تقرير مطابقة حساب الصراف: {acc['name']}"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = f"الفترة من {date_from} إلى {date_to}"
    ws["A3"] = ("اصطلاح الإشارة: الأرصدة من منظور النظام. موجب = مدين (الصراف عليه)، سالب = دائن (له). "
                "الفرق = النظام − الصراف. لم تُنشأ أي قيود تسوية.")
    ws["A3"].alignment = Alignment(wrap_text=True)
    ws.merge_cells("A3:H3")
    ws.row_dimensions[3].height = 32
    headers = ["البند"] + [r.currency for r in recs]
    lines = [
        ("رصيد النظام الافتتاحي", lambda r: r.system.opening),
        ("رصيد الصراف الافتتاحي (بمنظور النظام)", lambda r: r.exchanger.opening),
        ("فرق الرصيد الافتتاحي", lambda r: r.opening_diff),
        ("حركات النظام في الفترة (صافي)", lambda r: r.system.movement),
        ("  منها مدين", lambda r: r.system.period_debit),
        ("  منها دائن", lambda r: r.system.period_credit),
        ("حركات الصراف في الفترة (صافي بمنظور النظام)", lambda r: r.exchanger.movement),
        ("فرق حركات الفترة", lambda r: r.movement_diff),
        ("رصيد النظام الختامي", lambda r: r.system.closing),
        ("رصيد الصراف الختامي (بمنظور النظام)", lambda r: r.exchanger.closing),
        ("رصيد الصراف الختامي كما في كشفه (مدين − دائن)", lambda r: r.exchanger.own_closing),
        ("فرق الرصيد الختامي", lambda r: r.closing_diff),
        ("الفروقات المفسَّرة بحركات مفتوحة", lambda r: r.explained),
        ("  منها حركات قبل الفترة (مرحّلة)", lambda r: r.explained_split()["before_period"]),
        ("  منها حركات الفترة", lambda r: r.explained_split()["in_period"]),
        ("الفروقات غير المفسَّرة", lambda r: r.unexplained),
        ("  منها فرق الرصيدين الأساسيين (غير مفصّل بحركات)", lambda r: r.base_diff),
        ("  الباقي بعد ذلك", lambda r: r.unexplained - r.base_diff),
    ]
    rows = [[label] + [fn(r) for r in recs] for label, fn in lines]
    rows.append(["تحقق: فرق الختامي = فرق الافتتاحي + فرق الحركات"] +
                ["صحيح" if r.identity_ok else "خطأ" for r in recs])
    rows.append(["تحقق: فرق الختامي = المفسَّر + غير المفسَّر"] +
                ["صحيح" if r.explained + r.unexplained == r.closing_diff else "خطأ" for r in recs])
    end = _table(ws, 5, headers, rows, [52] + [20] * len(recs))
    for i in range(6, end):
        label = ws.cell(row=i, column=1).value or ""
        if label.startswith("فرق") or label.startswith("الفروقات"):
            for j in range(1, len(headers) + 1):
                ws.cell(row=i, column=j).font = BOLD
    for i, r in enumerate(recs, 2):
        cell = ws.cell(row=5 + lines.index(next(l for l in lines if l[0] == "الفروقات غير المفسَّرة")) + 1, column=i)
        cell.fill = OK_FILL if r.unexplained == 0 else BAD_FILL

    # تصنيف المفتوح
    r0 = end + 2
    ws.cell(row=r0, column=1, value="تصنيف الحركات المفتوحة (المبالغ المتبقية)").font = TITLE_FONT
    cat_rows = []
    for rec in recs:
        tot = rec.category_totals()
        for k, label in rc.CATEGORY_LABEL.items():
            cat_rows.append([rec.currency, label, tot[k]["count"], tot[k]["D"], tot[k]["C"],
                             tot[k]["D"] - tot[k]["C"]])
    _table(ws, r0 + 1, ["العملة", "التصنيف", "العدد", "مدين (عليه)", "دائن (له)", "الصافي"], cat_rows)
    ws.freeze_panes = None

    # ---------------- الحركات المفتوحة
    def open_sheet(title, pred):
        sh = _sheet(wb, title)
        data = []
        for rec in recs:
            for i in rec.open_items:
                if pred(i):
                    data.append([rec.currency, SIDE_LABEL[i.side], i.date, i.reference, i.counterparty,
                                 i.description, "مدين" if i.direction == "D" else "دائن",
                                 "مدين (عليه)" if i.eff_dir == "D" else "دائن (له)", i.amount, i.matched, i.open,
                                 "نعم" if i.in_period else "مرحّلة", "نعم" if i.is_retroactive else "",
                                 i.age_days, i.txn_id])
        _table(sh, 1, ["العملة", "الجانب", "التاريخ", "المرجع", "الاسم", "البيان", "الاتجاه في الكشف",
                       "الأثر على الحساب", "المبلغ", "المطابَق", "المتبقي", "ضمن الفترة", "بأثر رجعي",
                       "العمر (يوم)", "رقم الحركة"], data,
               [8, 8, 11, 14, 20, 40, 10, 12, 15, 15, 15, 10, 9, 9, 9])

    open_sheet("موجود بالنظام فقط", lambda i: i.category == "system_only")
    open_sheet("موجود بالصراف فقط", lambda i: i.category == "exchanger_only")
    open_sheet("متبقي مطابقات جزئية", lambda i: i.category.endswith("partial"))

    # ---------------- المطابقات
    def match_sheet(title, pred, status):
        sh = _sheet(wb, title)
        data = []
        for m in match_rows(conn, status, account_id):
            if m["currency"] not in curs or not pred(m):
                continue
            for it in match_items(conn, m["id"]):
                data.append([m["id"], m["currency"], KIND_LABEL.get(m["kind"], m["kind"]),
                             "جزئية" if m["is_partial"] else "كاملة", m["score"],
                             "يدوي" if m["method"] == "manual" else "تلقائي", SIDE_LABEL[it["side"]],
                             it["txn_date"], it["reference"], it["counterparty"], it["description"],
                             from_minor(it["total"]), from_minor(it["amount"]),
                             "، ".join(json.loads(m["reasons"] or "[]")), m["decided_by"], m["decided_at"],
                             m["decision_note"]])
        _table(sh, 1, ["رقم المطابقة", "العملة", "النوع", "كاملة/جزئية", "الثقة", "الطريقة", "الجانب", "التاريخ",
                       "المرجع", "الاسم", "البيان", "مبلغ الحركة", "المبلغ المخصص", "الأسباب", "بواسطة",
                       "وقت القرار", "ملاحظة"], data,
               [10, 8, 18, 10, 7, 8, 8, 11, 14, 18, 36, 15, 15, 40, 12, 18, 20])

    match_sheet("المطابقات المعتمدة", lambda m: True, "approved")
    match_sheet("مطابقات جزئية", lambda m: m["is_partial"], "approved")
    match_sheet("مطابقات مركبة", lambda m: m["kind"] != "one_to_one", "approved")
    match_sheet("مقترحة للمراجعة", lambda m: True, "proposed")
    match_sheet("مفكوكة ومرفوضة", lambda m: True, "unmatched")

    # ---------------- بأثر رجعي والتكرار
    sh = _sheet(wb, "حركات بأثر رجعي")
    data = [[t["currency"], SIDE_LABEL[t["side"]], t["txn_date"], t["reference"], t["description"],
             "مدين" if t["direction"] == "D" else "دائن", from_minor(t["amount"]),
             from_minor(t["amount"] - t["matched"]), t["created_at"], t["id"]]
            for t in txn_rows(conn, account_id, retro_only=True) if t["currency"] in curs]
    _table(sh, 1, ["العملة", "الجانب", "التاريخ", "المرجع", "البيان", "الاتجاه", "المبلغ", "المتبقي",
                   "تاريخ الإدخال", "رقم"], data, [8, 8, 11, 14, 40, 8, 15, 15, 18, 8])

    sh = _sheet(wb, "التكرارات")
    data = [[t["currency"], SIDE_LABEL[t["side"]], t["txn_date"], t["reference"], t["description"],
             from_minor(t["amount"]), {"duplicate": "مكررة مستبعدة", "active": "مشتبه بها"}.get(t["status"]),
             t["duplicate_of"] or t["dup_suspect_of"], t["id"]]
            for t in txn_rows(conn, account_id, statuses=("active", "duplicate"))
            if t["currency"] in curs and (t["status"] == "duplicate" or (t["dup_suspect_of"] and not t["dup_reviewed"]))]
    _table(sh, 1, ["العملة", "الجانب", "التاريخ", "المرجع", "البيان", "المبلغ", "الحالة", "مكررة من", "رقم"],
           data, [8, 8, 11, 14, 40, 15, 14, 10, 8])

    # ---------------- تحقق الأرصدة
    sh = _sheet(wb, "تحقق أرصدة الكشوفات")
    data = []
    for rec in recs:
        for b in rec.balance_checks:
            data.append([rec.currency, SIDE_LABEL[b["side"]], "افتتاحي" if b["kind"] == "opening" else "ختامي",
                         b["as_of"], b["source"], b["reported"], b["computed"], b["diff"]])
    _table(sh, 1, ["العملة", "الجانب", "النوع", "التاريخ", "المصدر", "الرصيد في الكشف", "المحسوب من الحركات",
                   "الفرق"], data, [8, 8, 9, 11, 10, 18, 18, 15])

    # ---------------- السجل
    sh = _sheet(wb, "سجل العمليات")
    data = [[l["ts"], l["user"], l["action"], l["currency"], l["entity"], l["entity_id"], l["details"]]
            for l in conn.execute("SELECT * FROM audit_log WHERE account_id=? ORDER BY id", (account_id,))]
    _table(sh, 1, ["الوقت", "المستخدم", "العملية", "العملة", "الكيان", "الرقم", "التفاصيل"], data,
           [19, 12, 18, 8, 8, 8, 80])

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    with Tx(conn):
        audit(conn, "تصدير تقرير Excel", account_id=account_id, file=path.name,
              period=f"{date_from} - {date_to}")
    return path
