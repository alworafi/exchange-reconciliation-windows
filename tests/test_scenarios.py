"""سيناريوهات العمل كما وصفها المستخدم، بأرقامه."""
from decimal import Decimal

from conftest import D, do_import, write_statement
from sarraf_recon import reconciliation as rc, services
from sarraf_recon.report import build_report

P = (D(2026, 10, 1), D(2026, 11, 30))


def _setup(env, sys_rows, ex_rows, name="صراف"):
    acc = services.create_account(env["conn"], name)
    do_import(env, acc, "system", write_statement(env["dir"] / f"s_{name}.xlsx", sys_rows))
    do_import(env, acc, "exchanger", write_statement(env["dir"] / f"e_{name}.xlsx", ex_rows))
    return acc


def _approve_all(conn, acc):
    for m in services.match_rows(conn, "proposed", acc):
        services.approve_match(conn, m["id"])


def test_partial_500k_paid_200k_leaves_300k(env):
    conn = env["conn"]
    acc = _setup(env, [(D(2026, 10, 1), "F-100", "فاتورة حوالة - عبدالله", 500_000, None)],
                 [(D(2026, 10, 2), "F-100", "سداد فاتورة عبدالله", None, 200_000)])
    services.run_matching(conn, acc)
    _approve_all(conn, acc)
    rec = rc.compute(conn, acc, "YER", *P)
    [item] = rec.open_items
    assert item.category == "system_partial" and item.open == Decimal("300000.00")
    assert rec.closing_diff == Decimal("300000.00") and rec.unexplained == 0


def test_invoice_paid_by_several_transfers_on_different_days(env):
    conn = env["conn"]
    acc = _setup(env, [(D(2026, 10, 1), "F-200", "فاتورة", 500_000, None)],
                 [(D(2026, 10, 3), "", "تحويل 1", None, 150_000),
                  (D(2026, 10, 12), "", "تحويل 2", None, 250_000),
                  (D(2026, 10, 25), "", "تحويل 3", None, 100_000)])
    services.run_matching(conn, acc)
    [p] = services.match_rows(conn, "proposed", acc)
    assert p["kind"] == "one_to_many" and len(services.match_items(conn, p["id"])) == 4
    services.approve_match(conn, p["id"])
    assert rc.compute(conn, acc, "YER", *P).closing_diff == 0


def test_one_transfer_for_several_invoices(env):
    conn = env["conn"]
    acc = _setup(env, [(D(2026, 10, 1), "F1", "فاتورة", 120_000, None), (D(2026, 10, 2), "F2", "فاتورة", 80_000, None),
                       (D(2026, 10, 4), "F3", "فاتورة", 50_000, None)],
                 [(D(2026, 10, 6), "T9", "حوالة مجمعة", None, 250_000)])
    services.run_matching(conn, acc)
    [p] = services.match_rows(conn, "proposed", acc)
    assert p["kind"] == "many_to_one"


def test_unrecorded_and_not_transferred_are_classified(env):
    conn = env["conn"]
    acc = _setup(env, [(D(2026, 10, 1), "F5", "فاتورة لم تحول", 70_000, None)],
                 [(D(2026, 10, 1), "X5", "حوالة غير مسجلة لديكم", None, 40_000)])
    services.run_matching(conn, acc)
    assert services.match_rows(conn, None, acc) == []
    t = rc.compute(conn, acc, "YER", *P).category_totals()
    assert t["system_only"]["D"] == Decimal("70000.00")       # فاتورة لم يُحوّلها الصراف
    assert t["exchanger_only"]["D"] == Decimal("40000.00")    # حوالة عند الصراف لم تُسجّل بالنظام


def test_discount_is_explained_with_reason_not_zeroed(env):
    conn = env["conn"]
    acc = _setup(env, [(D(2026, 10, 1), "F7", "فاتورة", 100_000, None)],
                 [(D(2026, 10, 1), "F7", "سداد بعد الخصم", None, 98_000)])
    ids = [r["id"] for r in conn.execute("SELECT id FROM transactions ORDER BY side DESC")]
    services.manual_match(conn, ids, "خصم 2% متفق عليه", diff_reason="discount")
    rec = rc.compute(conn, acc, "YER", *P)
    [item] = rec.open_items
    assert item.open == Decimal("2000.00") and item.reason == "discount"
    assert rec.closing_diff == Decimal("2000.00")                # الفرق باقٍ، لم يُصفّر
    assert rec.reason_totals()["خصم متفق عليه"] == Decimal("2000.00")
    actions = [r["action"] for r in services.audit_rows(conn, acc)]
    assert "تعليل فرق" in actions


def test_old_invoice_paid_a_month_later(env):
    conn = env["conn"]
    acc = _setup(env, [(D(2026, 9, 5), "", "فاتورة قديمة - مؤسسة الريان", 333_250, None)],
                 [(D(2026, 10, 20), "", "حوالة مؤسسة الريان", None, 333_250)])
    services.run_matching(conn, acc)
    [p] = services.match_rows(conn, "proposed", acc)    # لا يُعتمد تلقائيًا (بلا مرجع)
    assert "المبلغ لا يتكرر في الحركات المفتوحة" in p["reasons"]


def test_overlapping_exchanger_statements_do_not_duplicate(env):
    conn = env["conn"]
    acc = services.create_account(conn, "صراف")
    a = [(D(2026, 10, 1), "T1", "حوالة", None, 100), (D(2026, 10, 15), "T2", "حوالة", None, 200)]
    b = [(D(2026, 10, 15), "T2", "حوالة", None, 200), (D(2026, 10, 20), "T3", "حوالة", None, 300)]
    do_import(env, acc, "exchanger", write_statement(env["dir"] / "a.xlsx", a))
    r = do_import(env, acc, "exchanger", write_statement(env["dir"] / "b.xlsx", b))
    assert r["new"] == 1 and r["already_imported"] == 1
    assert rc.compute(conn, acc, "YER", *P).exchanger.closing == Decimal("600.00")


def test_balance_difference_cause_analysis(env):
    """فاتورة قديمة مفتوحة في بداية الفترة سُددت خلال الفترة: تظهر في تحليل فرق الافتتاحي
    وتُطفأ في فرق الحركات، وكل جزء من الفرق مربوط بحركاته."""
    conn = env["conn"]
    acc = _setup(env, [(D(2026, 9, 20), "OLD", "فاتورة سبتمبر", 400_000, None),
                       (D(2026, 10, 10), "NEW", "فاتورة أكتوبر", 90_000, None)],
                 [(D(2026, 10, 5), "OLD", "سداد فاتورة سبتمبر", None, 400_000)])
    services.run_matching(conn, acc)
    _approve_all(conn, acc)
    rec = rc.compute(conn, acc, "YER", *P)
    assert rec.opening_diff == Decimal("400000.00")
    assert rec.movement_diff == Decimal("-310000.00")
    assert rec.closing_diff == Decimal("90000.00")
    assert [i.reference for i in rec.opening_open_items] == ["OLD"]
    assert rec.opening_explained == rec.opening_diff
    settled = rec.settled_during_period()
    assert [i.reference for i in settled] == ["OLD"]
    path = build_report(conn, acc, *P, env["dir"] / "r.xlsx")
    from openpyxl import load_workbook
    ws = load_workbook(path)["تحليل أسباب الفرق"]
    rows = {r[1]: r[6] for r in ws.iter_rows(min_row=4, values_only=True) if r[1]}
    assert rows["  غير مفسَّر في حركات الفترة"] == 0
    assert rows["  غير مفسَّر في الافتتاحي بعد ما سبق"] == 0
    assert rows["  غير مفسَّر بحركات"] == 0
