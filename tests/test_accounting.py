"""اختبارات محاسبية إلزامية: العلاقة بين فرق الافتتاحي وفرق الحركة وفرق الختامي.

الأرقام 400,850 و 633,700 و 1,034,550 ليست مكتوبة في البرنامج. هي ناتجة هنا
من حركات كشفين، والبرنامج يحسبها من البيانات.
"""
from decimal import Decimal

from conftest import D, do_import, write_statement
from sarraf_recon import reconciliation as rc, services
from sarraf_recon.report import build_report

PERIOD = (D(2026, 10, 1), D(2026, 10, 31))

SYSTEM_ROWS = [  # (التاريخ، المرجع، البيان، مدين، دائن) من دفاترنا
    (D(2026, 9, 10), "9001", "حوالة واردة عبر الصراف - أحمد سعيد", 1_200_000, None),
    (D(2026, 9, 20), "9002", "حوالة واردة - محمد علي", 400_850, None),
    (D(2026, 10, 5), "1001", "حوالة واردة - علي ناصر", 1_000_000, None),
    (D(2026, 10, 10), "1002", "حوالة واردة - سالم صالح", 250_000, None),
    (D(2026, 10, 15), "1003", "حوالة واردة - خالد عمر", 583_700, None),
    (D(2026, 10, 20), "1004", "سداد للصراف نقدا", None, 300_000),
]
# كشف الصراف من منظوره: ما هو مدين عندنا يظهر دائنًا عنده
EXCHANGER_ROWS = [
    (D(2026, 9, 10), "9001", "حواله صادره احمد سعيد", None, 1_200_000),
    (D(2026, 10, 5), "1001", "حواله صادره علي ناصر", None, 1_000_000),
    (D(2026, 10, 11), "1002", "حواله سالم صالح (دفعة اولى)", None, 200_000),
    (D(2026, 10, 22), "1004", "استلام نقدية من الشركة", 300_000, None),
]


def _setup_full_history(env):
    acc = services.create_account(env["conn"], "صراف الأمانة")
    s = write_statement(env["dir"] / "system.xlsx", SYSTEM_ROWS)
    e = write_statement(env["dir"] / "exchanger.xlsx", EXCHANGER_ROWS)
    do_import(env, acc, "system", s)
    do_import(env, acc, "exchanger", e)
    return acc


def test_difference_identity_full_history(env):
    conn = env["conn"]
    acc = _setup_full_history(env)
    rec = rc.compute(conn, acc, "YER", *PERIOD)
    assert rec.opening_diff == Decimal("400850.00")
    assert rec.movement_diff == Decimal("633700.00")
    assert rec.closing_diff == Decimal("1034550.00")
    # العلاقة بحسب الاصطلاح: الفرق = النظام − الصراف، والختامي = الافتتاحي + الحركة
    assert rec.closing_diff == rec.opening_diff + rec.movement_diff
    assert rec.identity_ok
    # قبل أي مطابقة: كل الفرق مفسَّر بحركات مفتوحة، ولا شيء غير مفسَّر
    assert rec.explained == rec.closing_diff
    assert rec.unexplained == 0


def test_difference_explained_after_matching(env):
    conn = env["conn"]
    acc = _setup_full_history(env)
    services.run_matching(conn, acc)
    # اعتماد كل المقترحات (بما فيها الجزئية) كما سيفعل المراجع
    for m in services.match_rows(conn, "proposed", acc):
        services.approve_match(conn, m["id"], "مراجعة الاختبار")
    rec = rc.compute(conn, acc, "YER", *PERIOD)
    assert rec.closing_diff == Decimal("1034550.00")
    tot = rec.category_totals()
    assert tot["system_only"]["D"] == Decimal("984550.00")      # 400,850 + 583,700
    assert tot["system_partial"]["D"] == Decimal("50000.00")    # 250,000 − 200,000
    assert tot["exchanger_only"]["count"] == 0
    assert rec.explained == Decimal("1034550.00")
    assert rec.unexplained == 0
    split = rec.explained_split()
    assert split["before_period"] == Decimal("400850.00")       # يفسر فرق الافتتاحي
    assert split["in_period"] == Decimal("633700.00")           # يفسر فرق الحركة
    path = build_report(conn, acc, *PERIOD, env["dir"] / "report.xlsx")
    assert path.exists()


def test_difference_with_statement_opening_balances(env):
    """عند وجود رصيد سابق في الكشوفات فقط (بدون حركات قديمة) يبقى فرق الافتتاحي غير مفسَّر بحركات،
    ويظهر صريحًا كفرق غير مفسَّر بدل أن يُصفَّر."""
    conn = env["conn"]
    acc = services.create_account(conn, "صراف النجم")
    period_sys = [r for r in SYSTEM_ROWS if r[0] >= PERIOD[0]]
    period_ex = [r for r in EXCHANGER_ROWS if r[0] >= PERIOD[0]]
    s = write_statement(env["dir"] / "s.xlsx", period_sys, with_balance=True, opening=1_600_850)
    # حسابنا في دفتر الصراف دائن 1,200,000: أي أن الصراف مدين لنا = +1,200,000 بمنظور النظام
    e = write_statement(env["dir"] / "e.xlsx", period_ex, with_balance=True, opening=-1_200_000)
    do_import(env, acc, "system", s)
    do_import(env, acc, "exchanger", e)
    services.run_matching(conn, acc)
    for m in services.match_rows(conn, "proposed", acc):
        services.approve_match(conn, m["id"])
    rec = rc.compute(conn, acc, "YER", *PERIOD)
    assert rec.opening_diff == Decimal("400850.00")
    assert rec.movement_diff == Decimal("633700.00")
    assert rec.closing_diff == Decimal("1034550.00")
    assert rec.explained == Decimal("633700.00")
    assert rec.unexplained == Decimal("400850.00")
    assert rec.base_diff == Decimal("400850.00")
    # أرصدة الكشوفات الختامية تتطابق مع المحسوب من الحركات
    assert all(b["diff"] == 0 for b in rec.balance_checks)


def test_currencies_are_fully_separated(env):
    conn = env["conn"]
    acc = services.create_account(conn, "صراف العملات")
    s = write_statement(env["dir"] / "s_yer.xlsx", [(D(2026, 10, 1), "77", "حوالة", 1000, None)])
    e = write_statement(env["dir"] / "e_sar.xlsx", [(D(2026, 10, 1), "77", "حوالة", None, 1000)],
                        currency_title="ريال سعودي")
    do_import(env, acc, "system", s)
    do_import(env, acc, "exchanger", e)
    services.run_matching(conn, acc)
    assert services.match_rows(conn, None, acc) == []
    yer = rc.compute(conn, acc, "YER", *PERIOD)
    sar = rc.compute(conn, acc, "SAR", *PERIOD)
    assert yer.closing_diff == Decimal("1000.00")
    assert sar.closing_diff == Decimal("-1000.00")
    assert yer.exchanger.closing == 0 and sar.system.closing == 0
