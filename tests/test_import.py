"""اختبارات الاستيراد: التعرف على الأعمدة، التداخل، التكرار، الأثر الرجعي، حفظ الأصل."""
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import Workbook

from conftest import D, do_import, write_statement
from sarraf_recon import reconciliation as rc, services
from sarraf_recon.importers import detect_mapping, parse_table, read_file
from sarraf_recon.money import to_decimal
from sarraf_recon.services import ReconError


@pytest.mark.parametrize("raw,expected", [
    ("1,234.50", "1234.50"), ("١٬٢٣٤٫٥", "1234.50"), ("(500)", "-500.00"), ("500-", "-500.00"),
    (1234.1, "1234.10"), (0.1 + 0.2, "0.30"), ("  ", None), ("12,000 ر.ي", "12000.00"),
])
def test_amount_parsing_uses_decimal(raw, expected):
    v = to_decimal(raw)
    assert (v is None and expected is None) or v == Decimal(expected)


def test_column_detection_and_original_file_kept(env):
    conn = env["conn"]
    acc = services.create_account(conn, "صراف")
    p = write_statement(env["dir"] / "s.xlsx", [(D(2026, 10, 1), "1", "حوالة", 100, None)])
    table = read_file(p)
    m = detect_mapping(table)
    assert m.columns["date"] == 0 and m.columns["reference"] == 1 and m.columns["debit"] == 3
    assert m.columns["credit"] == 4 and m.default_currency == "YER"
    res = do_import(env, acc, "system", p)
    stored = conn.execute("SELECT stored_path FROM import_batches").fetchone()[0]
    assert Path(stored).exists() and res["new"] == 1
    with pytest.raises(ReconError, match="استُورد سابقًا"):
        do_import(env, acc, "system", p)


def test_overlapping_periods_are_not_counted_twice(env):
    conn = env["conn"]
    acc = services.create_account(conn, "صراف")
    sep = [(D(2026, 9, 25), "1", "حوالة", 100, None), (D(2026, 9, 30), "2", "حوالة", 200, None)]
    octo = [(D(2026, 9, 30), "2", "حوالة", 200, None), (D(2026, 10, 3), "3", "حوالة", 300, None)]
    do_import(env, acc, "system", write_statement(env["dir"] / "sep.xlsx", sep))
    res = do_import(env, acc, "system", write_statement(env["dir"] / "oct.xlsx", octo))
    assert res == {**res, "new": 1, "already_imported": 1}
    rec = rc.compute(conn, acc, "YER", D(2026, 9, 1), D(2026, 10, 31))
    assert rec.system.closing == Decimal("600.00")


def test_identical_rows_in_same_file_are_kept_but_flagged(env):
    conn = env["conn"]
    acc = services.create_account(conn, "صراف")
    rows = [(D(2026, 10, 1), "7", "حوالة", 100, None), (D(2026, 10, 1), "7", "حوالة", 100, None)]
    res = do_import(env, acc, "system", write_statement(env["dir"] / "a.xlsx", rows))
    assert res["new"] == 2 and res["suspected_duplicates"] == 1
    # إعادة استيراد كشف يغطي نفس اليوم بنفس الصفين لا يضيف شيئًا
    res2 = do_import(env, acc, "system", write_statement(env["dir"] / "b.xlsx", rows + [
        (D(2026, 10, 2), "8", "حوالة", 50, None)]))
    assert res2["new"] == 1 and res2["already_imported"] == 2
    suspect = services.txn_rows(conn, acc, suspects_only=True)[0]
    services.mark_duplicate(conn, suspect["id"], suspect["dup_suspect_of"], "قيد مكرر في النظام")
    rec = rc.compute(conn, acc, "YER", D(2026, 10, 1), D(2026, 10, 31))
    assert rec.system.closing == Decimal("150.00")


def test_duplicate_in_approved_match_cannot_be_excluded(env):
    conn = env["conn"]
    acc = services.create_account(conn, "صراف")
    do_import(env, acc, "system", write_statement(env["dir"] / "s.xlsx", [(D(2026, 10, 1), "9", "x", 100, None)]))
    do_import(env, acc, "exchanger", write_statement(env["dir"] / "e.xlsx", [(D(2026, 10, 1), "9", "x", None, 100)]))
    ids = [r["id"] for r in conn.execute("SELECT id FROM transactions")]
    services.manual_match(conn, ids)
    with pytest.raises(ReconError):
        services.mark_duplicate(conn, ids[0], None)


def test_retroactive_entries_are_flagged(env):
    conn = env["conn"]
    acc = services.create_account(conn, "صراف")
    do_import(env, acc, "exchanger", write_statement(env["dir"] / "a.xlsx", [(D(2026, 10, 10), "1", "x", None, 100)]))
    res = do_import(env, acc, "exchanger", write_statement(env["dir"] / "b.xlsx", [
        (D(2026, 10, 3), "2", "قيد بأثر رجعي", None, 70), (D(2026, 10, 20), "3", "عادي", None, 30)]))
    assert res["retroactive"] == 1
    retro = services.txn_rows(conn, acc, retro_only=True)
    assert [r["reference"] for r in retro] == ["2"]


def test_single_amount_column_and_balance_sign_detection(env):
    """كشف بعمود مبلغ واحد + عمود نوع، ورصيد بمنظور دائن − مدين، وأرقام عربية."""
    wb = Workbook()
    ws = wb.active
    ws.append(["التاريخ", "البيان", "نوع الحركة", "المبلغ", "الرصيد", "العملة"])
    ws.append(["٠١/١٠/٢٠٢٦", "رصيد سابق", None, None, "١٬٠٠٠", "ريال سعودي"])
    ws.append(["02/10/2026", "حوالة", "دائن", "٥٠٠", "1,500", "ريال سعودي"])
    ws.append(["03/10/2026", "صرف", "مدين", "200", "1,300", "ريال سعودي"])
    ws.append(["", "الإجمالي", "", "700", "", ""])
    p = env["dir"] / "x.xlsx"
    wb.save(p)
    table = read_file(p)
    m = detect_mapping(table)
    parsed = parse_table(table, m)
    assert parsed.balance_sign == -1
    assert [(r.kind, r.currency) for r in parsed.rows if r.kind != "skip"] == [
        ("opening", "SAR"), ("txn", "SAR"), ("txn", "SAR")]
    assert parsed.txns[0].credit == Decimal("500.00") and parsed.txns[0].date == D(2026, 10, 2)
    assert not [w for w in parsed.warnings if "المتوقع" in w]
    conn = env["conn"]
    acc = services.create_account(conn, "صراف")
    services.import_statement(conn, acc, "exchanger", p, parsed, m, env["dir"])
    bal = conn.execute("SELECT balance FROM statement_balances WHERE kind='opening'").fetchone()[0]
    assert bal == -100000          # 1,000 دائن بمنظور الكشف (مدين − دائن = −1000)


def test_running_balance_gap_is_reported(env):
    rows = [(D(2026, 10, 1), "1", "a", 100, None), (D(2026, 10, 2), "2", "b", 50, None)]
    p = write_statement(env["dir"] / "g.xlsx", rows, with_balance=True, opening=0)
    from openpyxl import load_workbook
    wb = load_workbook(p)
    wb.active.cell(row=7, column=6, value=999)   # رصيد خاطئ في الصف الأخير
    wb.save(p)
    table = read_file(p)
    parsed = parse_table(table, detect_mapping(table))
    assert any("المتوقع" in w for w in parsed.warnings)
