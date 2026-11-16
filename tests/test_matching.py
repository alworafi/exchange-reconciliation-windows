"""اختبارات محرك المطابقة وإدارة المطابقات."""
import sqlite3
from decimal import Decimal

import pytest

from conftest import D, do_import, write_statement
from sarraf_recon import reconciliation as rc, services
from sarraf_recon.services import ReconError


def _acc(env, name="صراف"):
    return services.create_account(env["conn"], name)


def _imp(env, acc, sys_rows, ex_rows, tag=""):
    s = write_statement(env["dir"] / f"s{tag}.xlsx", sys_rows)
    e = write_statement(env["dir"] / f"e{tag}.xlsx", ex_rows)
    do_import(env, acc, "system", s)
    do_import(env, acc, "exchanger", e)


def _txn_id(conn, side, ref):
    return conn.execute("SELECT id FROM transactions WHERE side=? AND reference=? ORDER BY id",
                        (side, ref)).fetchone()["id"]


def _matches(conn, acc, status):
    return services.match_rows(conn, status, acc)


def test_direction_is_respected_not_amount_only(env):
    conn = env["conn"]
    acc = _acc(env)
    # نفس المبلغ والتاريخ والمرجع، لكن الاتجاه نفسه في الكشفين (أي متعاكس فعليًا) => لا مطابقة
    _imp(env, acc, [(D(2026, 10, 1), "500", "حوالة", 5000, None)],
         [(D(2026, 10, 1), "500", "حوالة", 5000, None)])
    services.run_matching(conn, acc)
    assert services.match_rows(conn, None, acc) == []
    s, e = _txn_id(conn, "system", "500"), _txn_id(conn, "exchanger", "500")
    with pytest.raises(ReconError, match="اتجاه"):
        services.manual_match(conn, [s, e])


def test_one_to_one_auto_approves_only_with_strong_evidence(env):
    conn = env["conn"]
    acc = _acc(env)
    _imp(env, acc,
         [(D(2026, 10, 1), "A1", "حوالة علي", 1000, None),       # مرجع + مبلغ + تاريخ => تلقائي
          (D(2026, 10, 2), None, "حوالة", 2000, None)],          # مبلغ وتاريخ فقط => مقترحة
         [(D(2026, 10, 1), "A1", "حوالة علي", None, 1000),
          (D(2026, 10, 2), None, "تحويل", None, 2000)])
    res = services.run_matching(conn, acc)
    assert res == {"auto_approved": 1, "proposed": 1}
    prop = _matches(conn, acc, "proposed")[0]
    assert prop["score"] < 85


def test_ambiguous_candidates_are_not_auto_approved(env):
    conn = env["conn"]
    acc = _acc(env)
    _imp(env, acc,
         [(D(2026, 10, 1), "X9", "حوالة", 1000, None)],
         [(D(2026, 10, 1), "X9", "حوالة", None, 1000),
          (D(2026, 10, 1), "X9", "حوالة مكررة؟", None, 1000)])
    services.run_matching(conn, acc)
    assert _matches(conn, acc, "approved") == []
    assert len(_matches(conn, acc, "proposed")) == 1


def test_one_to_many_and_many_to_one(env):
    conn = env["conn"]
    acc = _acc(env)
    _imp(env, acc,
         [(D(2026, 10, 1), "G1", "حوالات مجمعة", 3000, None),
          (D(2026, 10, 5), "H1", "دفعة", 400, None),
          (D(2026, 10, 5), "H2", "دفعة", 600, None)],
         [(D(2026, 10, 1), None, "حوالة 1", None, 1000),
          (D(2026, 10, 2), None, "حوالة 2", None, 2000),
          (D(2026, 10, 6), None, "دفعة مجمعة", None, 1000)])
    services.run_matching(conn, acc)
    props = _matches(conn, acc, "proposed")
    kinds = sorted(p["kind"] for p in props)
    assert kinds == ["many_to_one", "one_to_many"]
    for p in props:
        services.approve_match(conn, p["id"])
    rec = rc.compute(conn, acc, "YER", D(2026, 10, 1), D(2026, 10, 31))
    assert rec.open_items == [] and rec.closing_diff == 0


def test_many_to_many_by_reference(env):
    conn = env["conn"]
    acc = _acc(env)
    _imp(env, acc,
         [(D(2026, 10, 1), "R77", "قسط", 700, None), (D(2026, 10, 2), "R77", "قسط", 800, None)],
         [(D(2026, 10, 3), "R77", "قسط", None, 500), (D(2026, 10, 3), "R77", "قسط", None, 1000)])
    services.run_matching(conn, acc)
    props = _matches(conn, acc, "proposed")
    assert [p["kind"] for p in props] == ["many_to_many"]
    services.approve_match(conn, props[0]["id"])
    assert rc.compute(conn, acc, "YER", D(2026, 10, 1), D(2026, 10, 31)).open_items == []


def test_partial_payment_keeps_remainder_open(env):
    conn = env["conn"]
    acc = _acc(env)
    _imp(env, acc, [(D(2026, 10, 1), "P1", "حوالة سالم", 1000, None)],
         [(D(2026, 10, 2), "P1", "حوالة سالم", None, 600)])
    services.run_matching(conn, acc)
    p = _matches(conn, acc, "proposed")[0]
    assert p["is_partial"] == 1 and p["score"] <= 70
    services.approve_match(conn, p["id"])
    # الدفعة الثانية تصل في كشف لاحق ويُطابق المتبقي فقط
    e2 = write_statement(env["dir"] / "e2.xlsx", [(D(2026, 10, 9), "P1", "حوالة سالم باقي", None, 400)])
    do_import(env, acc, "exchanger", e2)
    services.run_matching(conn, acc)
    p2 = _matches(conn, acc, "proposed")[0]
    services.approve_match(conn, p2["id"])
    sid = _txn_id(conn, "system", "P1")
    assert services.approved_alloc(conn, acc)[sid] == 100000   # 1000.00 بالكامل
    assert rc.compute(conn, acc, "YER", D(2026, 10, 1), D(2026, 10, 31)).open_items == []


def test_different_dates_within_window_and_beyond(env):
    conn = env["conn"]
    acc = _acc(env)
    _imp(env, acc,
         [(D(2026, 10, 1), None, "حوالة فهد", 1234, None), (D(2026, 10, 1), None, "حوالة", 999, None),
          (D(2026, 10, 1), "LONG1", "حوالة متأخرة", 555, None)],
         [(D(2026, 10, 8), None, "حوالة فهد", None, 1234), (D(2026, 12, 25), None, "حوالة", None, 999),
          (D(2026, 12, 20), "LONG1", "حوالة", None, 555)])
    services.run_matching(conn, acc)
    props = _matches(conn, acc, "proposed")
    amounts = sorted(p["matched_amount"] for p in props)
    # 1234 داخل نافذة التاريخ، 555 خارجها لكن المرجع متطابق، 999 خارجها بلا مرجع => لا تُقترح
    assert amounts == [55500, 123400]


def test_over_allocation_is_blocked(env):
    conn = env["conn"]
    acc = _acc(env)
    _imp(env, acc, [(D(2026, 10, 1), "Z1", "حوالة", 1000, None)],
         [(D(2026, 10, 1), "Z1", "حوالة", None, 1000), (D(2026, 10, 2), "Z2", "حوالة", None, 1000)])
    s, e1, e2 = _txn_id(conn, "system", "Z1"), _txn_id(conn, "exchanger", "Z1"), _txn_id(conn, "exchanger", "Z2")
    services.manual_match(conn, [s, e1])
    with pytest.raises(ReconError):
        services.manual_match(conn, [s, e2])
    # الحماية داخل قاعدة البيانات أيضًا (لو تجاوز أحد طبقة الخدمات)
    conn.execute("BEGIN")
    cur = conn.execute("INSERT INTO matches(account_id,currency,kind,status,method,signature,matched_amount,created_at)"
                       " VALUES (?, 'YER','one_to_one','proposed','manual','x',100000,'now')", (acc,))
    conn.execute("INSERT INTO match_items(match_id,txn_id,side,amount) VALUES (?,?, 'system',100000)",
                 (cur.lastrowid, s))
    with pytest.raises(sqlite3.IntegrityError, match="OVER_ALLOCATION"):
        conn.execute("UPDATE matches SET status='approved' WHERE id=?", (cur.lastrowid,))
    conn.execute("ROLLBACK")


def test_unmatch_requires_reason_and_logs(env):
    conn = env["conn"]
    acc = _acc(env)
    _imp(env, acc, [(D(2026, 10, 1), "U1", "حوالة", 1000, None)], [(D(2026, 10, 1), "U1", "حوالة", None, 1000)])
    services.run_matching(conn, acc)
    m = _matches(conn, acc, "approved")[0]
    with pytest.raises(ReconError):
        services.unmatch(conn, m["id"], "")
    services.unmatch(conn, m["id"], "المرجع صحيح لكن المستفيد مختلف")
    assert rc.compute(conn, acc, "YER", D(2026, 10, 1), D(2026, 10, 31)).closing_diff == 0
    assert len(rc.compute(conn, acc, "YER", D(2026, 10, 1), D(2026, 10, 31)).open_items) == 2
    actions = [r["action"] for r in services.audit_rows(conn, acc)]
    assert "فك مطابقة" in actions and "تشغيل المطابقة" in actions
    # المطابقة المفكوكة تبقى محفوظة كسجل ولا تُحذف
    assert services.match_rows(conn, "unmatched", acc)[0]["decision_note"].startswith("المرجع")


def test_rejected_proposal_is_not_proposed_again(env):
    conn = env["conn"]
    acc = _acc(env)
    _imp(env, acc, [(D(2026, 10, 1), None, "حوالة", 1000, None)], [(D(2026, 10, 1), None, "حوالة", None, 1000)])
    services.run_matching(conn, acc)
    p = _matches(conn, acc, "proposed")[0]
    services.reject_match(conn, p["id"], "ليست نفس العملية")
    services.run_matching(conn, acc)
    assert _matches(conn, acc, "proposed") == []


def test_new_import_keeps_approved_matches_and_matches_only_new(env):
    conn = env["conn"]
    acc = _acc(env)
    rows_s = [(D(2026, 10, 1), "N1", "حوالة", 1000, None)]
    rows_e = [(D(2026, 10, 1), "N1", "حوالة", None, 1000)]
    _imp(env, acc, rows_s, rows_e, "1")
    services.run_matching(conn, acc)
    first = _matches(conn, acc, "approved")[0]["id"]
    # كشف جديد متداخل: يحتوي الحركة القديمة + حركة جديدة
    _imp(env, acc, rows_s + [(D(2026, 10, 5), "N2", "حوالة", 2000, None)],
         rows_e + [(D(2026, 10, 5), "N2", "حوالة", None, 2000)], "2")
    services.run_matching(conn, acc)
    approved = _matches(conn, acc, "approved")
    assert first in [m["id"] for m in approved] and len(approved) == 2
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 4


def test_manual_composite_partial_match(env):
    conn = env["conn"]
    acc = _acc(env)
    _imp(env, acc, [(D(2026, 10, 1), None, "أ", 300, None), (D(2026, 10, 2), None, "ب", 200, None)],
         [(D(2026, 10, 3), None, "ج", None, 450)])
    ids = [r["id"] for r in conn.execute("SELECT id FROM transactions ORDER BY id")]
    mid = services.manual_match(conn, ids, "دفعة مجمعة ناقصة 50")
    m = conn.execute("SELECT * FROM matches WHERE id=?", (mid,)).fetchone()
    assert m["is_partial"] == 1 and m["kind"] == "many_to_one" and m["matched_amount"] == 45000
    rec = rc.compute(conn, acc, "YER", D(2026, 10, 1), D(2026, 10, 31))
    assert [(i.category, i.open) for i in rec.open_items] == [("system_partial", Decimal("50.00"))]
    assert rec.unexplained == 0
