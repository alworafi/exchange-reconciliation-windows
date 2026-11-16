"""خدمات العمل: الحسابات، الاستيراد، المطابقة، التصحيح اليدوي، وسجل العمليات.

كل عملية تغيّر البيانات تتم داخل معاملة واحدة وتُسجَّل في audit_log.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from . import db
from .db import Tx, get_setting, now_iso
from .engine import (EngineConfig, Item, Proposal, allocate, kind_for, linked_party, make_signature,
                     names_conflict, propose)
from .importers.base import Mapping, ParseResult
from .money import from_minor, to_minor
from .textnorm import normalize, normalize_ref, numbers_in, similarity

SIDE_LABEL = {"system": "النظام", "exchanger": "الصراف"}
KIND_LABEL = {
    "one_to_one": "حركة مقابل حركة",
    "one_to_many": "حركة نظام مقابل عدة حركات صراف",
    "many_to_one": "عدة حركات نظام مقابل حركة صراف",
    "many_to_many": "عدة مقابل عدة",
}
STATUS_LABEL = {"proposed": "مقترحة", "approved": "معتمدة", "rejected": "مرفوضة", "unmatched": "مفكوكة"}


class ReconError(Exception):
    """خطأ عمل يُعرض للمستخدم كما هو."""


# ------------------------------------------------------------------ audit

def audit(conn, action: str, user: str | None = None, entity: str | None = None,
          entity_id: int | None = None, account_id: int | None = None, currency: str | None = None,
          **details) -> None:
    conn.execute(
        "INSERT INTO audit_log(ts, user, action, entity, entity_id, account_id, currency, details)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (now_iso(), user or get_setting(conn, "user_name"), action, entity, entity_id, account_id,
         currency, json.dumps(details, ensure_ascii=False, default=str) if details else None),
    )


def current_user(conn) -> str:
    return get_setting(conn, "user_name") or "المحاسب"


# ------------------------------------------------------------------ accounts

def create_account(conn, name: str, code: str = "", mirror: bool = True, notes: str = "") -> int:
    name = (name or "").strip()
    if not name:
        raise ReconError("اسم الصراف مطلوب")
    with Tx(conn):
        try:
            cur = conn.execute(
                "INSERT INTO accounts(name, code, mirror, notes, created_at) VALUES (?,?,?,?,?)",
                (name, code, 1 if mirror else 0, notes, now_iso()))
        except sqlite3.IntegrityError:
            raise ReconError(f"يوجد حساب بنفس الاسم: {name}")
        audit(conn, "إنشاء حساب", entity="account", entity_id=cur.lastrowid, account_id=cur.lastrowid,
              name=name, mirror=mirror)
    return cur.lastrowid


def update_account(conn, account_id: int, name: str, code: str, mirror: bool, notes: str) -> None:
    with Tx(conn):
        has_matches = conn.execute(
            "SELECT 1 FROM matches WHERE account_id=? AND status='approved' LIMIT 1", (account_id,)).fetchone()
        old = conn.execute("SELECT mirror FROM accounts WHERE id=?", (account_id,)).fetchone()
        if has_matches and old and bool(old["mirror"]) != bool(mirror):
            raise ReconError("لا يمكن تغيير اتجاه كشف الصراف بعد وجود مطابقات معتمدة. فك المطابقات أولًا.")
        conn.execute("UPDATE accounts SET name=?, code=?, mirror=?, notes=? WHERE id=?",
                     (name, code, 1 if mirror else 0, notes, account_id))
        audit(conn, "تعديل حساب", entity="account", entity_id=account_id, account_id=account_id,
              name=name, mirror=mirror)


def accounts(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM accounts WHERE active=1 ORDER BY name").fetchall()


def currencies(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM currencies ORDER BY sort, code").fetchall()


def ensure_currency(conn, code: str) -> None:
    conn.execute("INSERT OR IGNORE INTO currencies(code, name, sort) VALUES (?,?,99)", (code, code))


# ------------------------------------------------------------------ import

def fingerprint(currency: str, d: date, direction: str, amount_minor: int, ref: str, desc: str,
                party: str) -> str:
    key = "|".join([currency, d.isoformat(), direction, str(amount_minor), normalize_ref(ref),
                    normalize(desc), normalize(party)])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def import_statement(conn, account_id: int, side: str, file_path: str | Path, parsed: ParseResult,
                     mapping: Mapping, data_dir: str | Path, allow_same_file: bool = False,
                     notes: str = "") -> dict:
    """يحفظ الكشف: نسخة الملف الأصلي + الحركات الجديدة فقط + الأرصدة الواردة.

    الحركات الموجودة مسبقًا (كشف متداخل الفترة) لا تُضاف مرة ثانية، بل تُسجَّل كظهور
    إضافي في batch_sightings. الحركات المتطابقة داخل الملف نفسه تُحسب بعدد مرات ظهورها.
    """
    if side not in ("system", "exchanger"):
        raise ReconError("نوع الكشف غير صحيح")
    file_path = Path(file_path)
    sha = file_sha256(file_path)
    prev_same = conn.execute(
        "SELECT id, imported_at FROM import_batches WHERE account_id=? AND side=? AND sha256=?",
        (account_id, side, sha)).fetchone()
    if prev_same and not allow_same_file:
        raise ReconError(f"هذا الملف نفسه استُورد سابقًا (دفعة {prev_same['id']} بتاريخ "
                         f"{prev_same['imported_at']}). لن يُستورد مرة أخرى.")
    originals = Path(data_dir) / "originals" / sha[:2]
    originals.mkdir(parents=True, exist_ok=True)
    stored = originals / f"{sha}{file_path.suffix.lower()}"
    if not stored.exists():
        shutil.copy2(file_path, stored)

    user = current_user(conn)
    txns = parsed.txns
    stats = Counter()
    with Tx(conn):
        dates = [r.date for r in txns]
        cur = conn.execute(
            "INSERT INTO import_batches(account_id, side, file_name, stored_path, sha256, imported_at,"
            " imported_by, period_from, period_to, total_rows, mapping_json, warnings_json, notes)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (account_id, side, file_path.name, str(stored), sha, now_iso(), user,
             min(dates).isoformat() if dates else None, max(dates).isoformat() if dates else None,
             len(txns), json.dumps(mapping.to_dict(), ensure_ascii=False),
             json.dumps(parsed.warnings, ensure_ascii=False), notes))
        batch_id = cur.lastrowid

        # آخر تاريخ مغطى سابقًا لكل عملة (لاكتشاف الحركات بأثر رجعي)
        covered = {r["currency"]: r["mx"] for r in conn.execute(
            "SELECT currency, MAX(txn_date) mx FROM transactions WHERE account_id=? AND side=?"
            " GROUP BY currency", (account_id, side))}

        seen_in_file: Counter = Counter()
        for r in txns:
            ensure_currency(conn, r.currency)
            amt = to_minor(r.amount)
            fp = fingerprint(r.currency, r.date, r.direction, amt, r.reference, r.description, r.counterparty)
            seen_in_file[fp] += 1
            occ = seen_in_file[fp]
            existing = conn.execute(
                "SELECT id FROM transactions WHERE account_id=? AND side=? AND fingerprint=? AND occurrence=?",
                (account_id, side, fp, occ)).fetchone()
            if existing:
                conn.execute("INSERT OR IGNORE INTO batch_sightings(batch_id, txn_id, source_row) VALUES (?,?,?)",
                             (batch_id, existing["id"], r.source_row))
                stats["dup"] += 1
                continue
            retro = 1 if covered.get(r.currency) and r.date.isoformat() <= covered[r.currency] else 0
            ref_n = normalize_ref(r.reference)
            suspect = _find_suspect(conn, account_id, side, r, amt, ref_n, fp)
            if suspect is None and occ > 1:
                first = conn.execute(
                    "SELECT id FROM transactions WHERE account_id=? AND side=? AND fingerprint=? AND occurrence=1",
                    (account_id, side, fp)).fetchone()
                if first:
                    suspect = first["id"]
            cur = conn.execute(
                "INSERT INTO transactions(account_id, side, currency, txn_date, description, reference, ref_norm,"
                " counterparty, debit, credit, direction, amount, stmt_balance, fingerprint, occurrence, batch_id,"
                " source_row, dup_suspect_of, is_retroactive, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (account_id, side, r.currency, r.date.isoformat(), r.description, r.reference, ref_n,
                 r.counterparty, to_minor(r.debit), to_minor(r.credit), r.direction, amt,
                 to_minor(parsed.balance_sign * r.balance) if r.balance is not None else None,
                 fp, occ, batch_id, r.source_row, suspect, retro, now_iso()))
            conn.execute("INSERT INTO batch_sightings(batch_id, txn_id, source_row) VALUES (?,?,?)",
                         (batch_id, cur.lastrowid, r.source_row))
            stats["new"] += 1
            stats["retro"] += retro
            stats["suspect"] += 1 if suspect else 0

        _store_statement_balances(conn, account_id, side, batch_id, parsed, user)
        conn.execute(
            "UPDATE import_batches SET new_rows=?, duplicate_rows=?, skipped_rows=?, retro_rows=? WHERE id=?",
            (stats["new"], stats["dup"], len([r for r in parsed.rows if r.kind == "skip"]), stats["retro"],
             batch_id))
        audit(conn, "استيراد كشف", entity="batch", entity_id=batch_id, account_id=account_id,
              side=SIDE_LABEL[side], file=file_path.name, new=stats["new"], already_imported=stats["dup"],
              retroactive=stats["retro"], suspected_duplicates=stats["suspect"])
    return {"batch_id": batch_id, "new": stats["new"], "already_imported": stats["dup"],
            "retroactive": stats["retro"], "suspected_duplicates": stats["suspect"],
            "skipped": len([r for r in parsed.rows if r.kind == "skip"]), "warnings": parsed.warnings}


def _find_suspect(conn, account_id, side, r, amt, ref_n, fp) -> int | None:
    """تكرار مشتبه: نفس المبلغ والاتجاه والعملة مع نفس المرجع، أو نفس التاريخ وبيان شبه متطابق."""
    rows = conn.execute(
        "SELECT id, txn_date, ref_norm, description, counterparty, fingerprint FROM transactions"
        " WHERE account_id=? AND side=? AND currency=? AND direction=? AND amount=? AND status='active'",
        (account_id, side, r.currency, r.direction, amt)).fetchall()
    for x in rows:
        if x["fingerprint"] == fp:
            continue
        if ref_n and x["ref_norm"] == ref_n:
            return x["id"]
        if x["txn_date"] == r.date.isoformat() and similarity(
                f"{x['description']} {x['counterparty']}", f"{r.description} {r.counterparty}") >= 0.85:
            return x["id"]
    return None


def _store_statement_balances(conn, account_id, side, batch_id, parsed: ParseResult, user):
    sign = parsed.balance_sign
    by_cur: dict[str, list] = defaultdict(list)
    for r in parsed.rows:
        if r.kind in ("txn", "opening", "closing"):
            by_cur[r.currency].append(r)
    for cur_code, rows in by_cur.items():
        txns = [r for r in rows if r.kind == "txn"]
        first_date = min((r.date for r in txns), default=None)
        last_date = max((r.date for r in txns), default=None)
        for r in rows:
            if r.kind == "opening" and r.balance is not None:
                as_of = min(d for d in (r.date, first_date) if d) if (r.date or first_date) else None
                if as_of:
                    _ins_bal(conn, account_id, side, cur_code, "opening", as_of, to_minor(sign * r.balance),
                             "statement", batch_id, user)
            elif r.kind == "closing" and r.balance is not None:
                as_of = r.date or last_date
                if as_of:
                    _ins_bal(conn, account_id, side, cur_code, "closing", as_of, to_minor(sign * r.balance),
                             "statement", batch_id, user)
        has_opening = any(r.kind == "opening" for r in rows)
        with_bal = [r for r in txns if r.balance is not None]
        if not has_opening and with_bal:
            f = with_bal[0]
            if f is txns[0]:
                derived = sign * f.balance - (f.debit - f.credit)
                _ins_bal(conn, account_id, side, cur_code, "opening", f.date, to_minor(derived),
                         "derived", batch_id, user, note="مستنتج من رصيد أول حركة في الكشف")
        if with_bal and not any(r.kind == "closing" for r in rows):
            lst = with_bal[-1]
            if lst is txns[-1]:
                _ins_bal(conn, account_id, side, cur_code, "closing", lst.date, to_minor(sign * lst.balance),
                         "derived", batch_id, user, note="رصيد آخر حركة في الكشف")


def _ins_bal(conn, account_id, side, currency, kind, as_of, balance, source, batch_id, user, note=None):
    conn.execute(
        "INSERT INTO statement_balances(account_id, side, currency, kind, as_of, balance, source, batch_id,"
        " created_at, created_by, note) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (account_id, side, currency, kind, as_of.isoformat(), balance, source, batch_id, now_iso(), user, note))


def set_manual_opening(conn, account_id: int, side: str, currency: str, as_of: date, balance_dc,
                       note: str = "") -> None:
    """رصيد افتتاحي يدوي باصطلاح الكشف نفسه (مدين − دائن)."""
    with Tx(conn):
        ensure_currency(conn, currency)
        _ins_bal(conn, account_id, side, currency, "opening", as_of, to_minor(balance_dc), "manual", None,
                 current_user(conn), note)
        audit(conn, "إدخال رصيد افتتاحي يدوي", entity="balance", account_id=account_id, currency=currency,
              side=SIDE_LABEL[side], as_of=as_of, balance=str(balance_dc), note=note)


# ------------------------------------------------------------------ open amounts / items

def approved_alloc(conn, account_id: int, currency: str | None = None) -> dict[int, int]:
    sql = ("SELECT mi.txn_id, SUM(mi.amount) a FROM match_items mi JOIN matches m ON m.id=mi.match_id"
           " WHERE m.status='approved' AND m.account_id=?")
    params: list = [account_id]
    if currency:
        sql += " AND m.currency=?"
        params.append(currency)
    sql += " GROUP BY mi.txn_id"
    return {r["txn_id"]: r["a"] for r in conn.execute(sql, params)}


def eff_dir(side: str, direction: str, mirror: bool) -> str:
    if side == "exchanger" and mirror:
        return "C" if direction == "D" else "D"
    return direction


def load_items(conn, account_id: int, currency: str) -> tuple[list[Item], list[Item]]:
    acc = conn.execute("SELECT mirror FROM accounts WHERE id=?", (account_id,)).fetchone()
    mirror = bool(acc["mirror"])
    alloc = approved_alloc(conn, account_id, currency)
    sys_items, ex_items = [], []
    for t in conn.execute(
            "SELECT * FROM transactions WHERE account_id=? AND currency=? AND status='active'"
            " ORDER BY txn_date, id", (account_id, currency)):
        it = Item(id=t["id"], side=t["side"], eff_dir=eff_dir(t["side"], t["direction"], mirror),
                  open=t["amount"] - alloc.get(t["id"], 0), amount=t["amount"],
                  date=date.fromisoformat(t["txn_date"]), ref=t["reference"] or "",
                  desc=t["description"] or "", party=t["counterparty"] or "",
                  nums=numbers_in(t["description"]) | numbers_in(t["counterparty"]))
        (sys_items if t["side"] == "system" else ex_items).append(it)
    return sys_items, ex_items


def engine_config(conn, account_id: int | None = None) -> EngineConfig:
    links = [(normalize(r["name_a"]), normalize(r["name_b"]), f"{r['name_a']} ↔ {r['name_b']}")
             for r in party_links(conn, account_id)]
    return EngineConfig(
        party_links=links,
        date_window=int(get_setting(conn, "date_window_days")),
        max_lookback=int(get_setting(conn, "max_lookback_days") or 120),
        min_score=int(get_setting(conn, "min_proposal_score")),
        auto_approve=get_setting(conn, "auto_approve") == "1",
        auto_min_score=int(get_setting(conn, "auto_approve_min_score")),
        max_group=int(get_setting(conn, "max_group_size")),
    )


# ------------------------------------------------------------------ matching operations

def _insert_match(conn, account_id, currency, kind, allocations, score, reasons, method, is_partial,
                  user) -> int:
    matched = sum(a for _, s, a in allocations if s == "system")
    cur = conn.execute(
        "INSERT INTO matches(account_id, currency, kind, is_partial, status, score, reasons, method, signature,"
        " matched_amount, created_at, created_by) VALUES (?,?,?,?, 'proposed', ?,?,?,?,?,?,?)",
        (account_id, currency, kind, 1 if is_partial else 0, score, json.dumps(reasons, ensure_ascii=False),
         method, make_signature(allocations), matched, now_iso(), user))
    mid = cur.lastrowid
    conn.executemany("INSERT INTO match_items(match_id, txn_id, side, amount) VALUES (?,?,?,?)",
                     [(mid, tid, side, amt) for tid, side, amt in allocations])
    return mid


def _check_available(conn, match_id: int) -> None:
    rows = conn.execute(
        "SELECT mi.txn_id, mi.amount, t.amount total, t.status,"
        " (SELECT COALESCE(SUM(mi2.amount),0) FROM match_items mi2 JOIN matches m2 ON m2.id=mi2.match_id"
        "   WHERE mi2.txn_id=mi.txn_id AND m2.status='approved' AND m2.id<>mi.match_id) used"
        " FROM match_items mi JOIN transactions t ON t.id=mi.txn_id WHERE mi.match_id=?", (match_id,)).fetchall()
    for r in rows:
        if r["status"] != "active":
            raise ReconError(f"الحركة {r['txn_id']} لم تعد فعالة (مكررة أو مستبعدة)")
        if r["used"] + r["amount"] > r["total"]:
            raise ReconError(
                f"الحركة {r['txn_id']}: المتاح {from_minor(r['total'] - r['used']):,.2f} فقط، "
                f"والمطابقة تحتاج {from_minor(r['amount']):,.2f}. ربما اعتُمدت في مطابقة أخرى.")


def run_matching(conn, account_id: int, currency: str | None = None) -> dict:
    """يعيد توليد المقترحات للحركات المفتوحة فقط. المطابقات المعتمدة لا تُمس."""
    user = current_user(conn)
    cfg = engine_config(conn, account_id)
    curs = [currency] if currency else [r["currency"] for r in conn.execute(
        "SELECT DISTINCT currency FROM transactions WHERE account_id=?", (account_id,))]
    rejected = {r["signature"] for r in conn.execute(
        "SELECT signature FROM rejected_signatures WHERE account_id=?", (account_id,))}
    summary = Counter()
    with Tx(conn):
        for cur_code in curs:
            old = [r["id"] for r in conn.execute(
                "SELECT id FROM matches WHERE account_id=? AND currency=? AND status='proposed' AND method='auto'",
                (account_id, cur_code))]
            if old:
                conn.execute(f"DELETE FROM match_items WHERE match_id IN ({','.join('?' * len(old))})", old)
                conn.execute(f"DELETE FROM matches WHERE id IN ({','.join('?' * len(old))})", old)
            sys_items, ex_items = load_items(conn, account_id, cur_code)
            proposals = propose(sys_items, ex_items, cfg, rejected)
            for p in proposals:
                mid = _insert_match(conn, account_id, cur_code, p.kind, p.allocations, p.score, p.reasons,
                                    "auto", p.is_partial, user)
                if p.auto_ok:
                    conn.execute("UPDATE matches SET status='approved', decided_at=?, decided_by=?,"
                                 " decision_note=? WHERE id=?",
                                 (now_iso(), "اعتماد تلقائي", f"درجة الثقة {p.score}", mid))
                    summary["auto_approved"] += 1
                else:
                    summary["proposed"] += 1
        audit(conn, "تشغيل المطابقة", account_id=account_id, currency=currency or "الكل",
              auto_approved=summary["auto_approved"], proposed=summary["proposed"])
    return dict(summary)


def approve_match(conn, match_id: int, note: str = "") -> None:
    with Tx(conn):
        m = conn.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
        if not m or m["status"] != "proposed":
            raise ReconError("يمكن اعتماد المطابقات المقترحة فقط")
        _check_available(conn, match_id)
        conn.execute("UPDATE matches SET status='approved', decided_at=?, decided_by=?, decision_note=? WHERE id=?",
                     (now_iso(), current_user(conn), note, match_id))
        audit(conn, "اعتماد مطابقة", entity="match", entity_id=match_id, account_id=m["account_id"],
              currency=m["currency"], score=m["score"], note=note)


def reject_match(conn, match_id: int, note: str = "") -> None:
    with Tx(conn):
        m = conn.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
        if not m or m["status"] != "proposed":
            raise ReconError("يمكن رفض المطابقات المقترحة فقط")
        conn.execute("UPDATE matches SET status='rejected', decided_at=?, decided_by=?, decision_note=? WHERE id=?",
                     (now_iso(), current_user(conn), note, match_id))
        conn.execute("INSERT OR IGNORE INTO rejected_signatures(signature, account_id, created_at) VALUES (?,?,?)",
                     (m["signature"], m["account_id"], now_iso()))
        audit(conn, "رفض مطابقة", entity="match", entity_id=match_id, account_id=m["account_id"],
              currency=m["currency"], note=note)


def unmatch(conn, match_id: int, reason: str) -> None:
    if not (reason or "").strip():
        raise ReconError("سبب فك المطابقة مطلوب")
    with Tx(conn):
        m = conn.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
        if not m or m["status"] != "approved":
            raise ReconError("يمكن فك المطابقات المعتمدة فقط")
        conn.execute("UPDATE matches SET status='unmatched', decided_at=?, decided_by=?, decision_note=? WHERE id=?",
                     (now_iso(), current_user(conn), reason, match_id))
        audit(conn, "فك مطابقة", entity="match", entity_id=match_id, account_id=m["account_id"],
              currency=m["currency"], reason=reason)


def manual_match(conn, txn_ids: list[int], note: str = "", allow_partial: bool = True,
                 diff_reason: str | None = None) -> int:
    """مطابقة يدوية لأي عدد من حركات النظام والصراف. تُعتمد مباشرة."""
    if not txn_ids:
        raise ReconError("اختر حركات للمطابقة")
    with Tx(conn):
        rows = conn.execute(
            f"SELECT * FROM transactions WHERE id IN ({','.join('?' * len(txn_ids))})", list(txn_ids)).fetchall()
        if len(rows) != len(set(txn_ids)):
            raise ReconError("بعض الحركات غير موجودة")
        accs = {r["account_id"] for r in rows}
        curs = {r["currency"] for r in rows}
        if len(accs) != 1:
            raise ReconError("لا يمكن مطابقة حركات من حسابات مختلفة")
        if len(curs) != 1:
            raise ReconError("لا يمكن مطابقة حركات بعملات مختلفة")
        account_id, currency = accs.pop(), curs.pop()
        sys_items, ex_items = load_items(conn, account_id, currency)
        by_id = {i.id: i for i in sys_items + ex_items}
        sel = [by_id.get(t) for t in txn_ids]
        if any(i is None for i in sel):
            raise ReconError("بعض الحركات ليست فعالة (مكررة أو مستبعدة)")
        s_sel = [i for i in sel if i.side == "system"]
        e_sel = [i for i in sel if i.side == "exchanger"]
        if not s_sel or not e_sel:
            raise ReconError("اختر حركة واحدة على الأقل من النظام وواحدة من الصراف")
        dirs = {i.eff_dir for i in sel}
        if len(dirs) != 1:
            raise ReconError("اتجاه الحركات غير متوافق: مدين النظام يقابل دائن الصراف (والعكس). "
                             "لا تُطابق حركات باتجاهات مختلفة.")
        closed = [i.id for i in sel if i.open <= 0]
        if closed:
            raise ReconError(f"حركات مطابقة بالكامل مسبقًا: {closed}")
        allocs, matched, partial = allocate(s_sel, e_sel)
        if partial and not allow_partial:
            raise ReconError("المجموعان غير متساويين")
        user = current_user(conn)
        kind = kind_for(len(s_sel), len(e_sel))
        reasons = ["مطابقة يدوية"] + (["جزئية: يبقى فرق مفتوح"] if partial else [])
        mid = _insert_match(conn, account_id, currency, kind, allocs, 100, reasons, "manual", partial, user)
        # المقترحات التلقائية التي تتعارض مع هذه الحركات تُحذف لأنها لم تعد صالحة
        _drop_conflicting_proposals(conn, [i.id for i in sel], keep=mid)
        _check_available(conn, mid)
        conn.execute("UPDATE matches SET status='approved', decided_at=?, decided_by=?, decision_note=? WHERE id=?",
                     (now_iso(), user, note, mid))
        audit(conn, "مطابقة يدوية", entity="match", entity_id=mid, account_id=account_id, currency=currency,
              txns=list(txn_ids), matched=str(from_minor(matched)), partial=partial, note=note)
        if partial and diff_reason:
            alloc_by = defaultdict(int)
            for tid, _, a in allocs:
                alloc_by[tid] += a
            for i in sel:
                if i.open - alloc_by.get(i.id, 0) > 0:
                    _set_reason(conn, i.id, diff_reason, note, account_id, currency)
    return mid


def _set_reason(conn, txn_id, reason, note, account_id, currency):
    if reason and reason not in db.DIFF_REASONS:
        raise ReconError("سبب فرق غير معروف")
    conn.execute("UPDATE transactions SET diff_reason=?, diff_note=? WHERE id=?", (reason or None, note, txn_id))
    audit(conn, "تعليل فرق", entity="txn", entity_id=txn_id, account_id=account_id, currency=currency,
          reason=db.DIFF_REASONS.get(reason, "إلغاء التعليل"), note=note)


def set_diff_reason(conn, txn_id: int, reason: str | None, note: str = "") -> None:
    """تعليل المتبقي المفتوح لحركة (خصم، عمولة، ...). لا يغيّر الأرصدة ولا يُنشئ قيدًا."""
    with Tx(conn):
        t = conn.execute("SELECT account_id, currency FROM transactions WHERE id=?", (txn_id,)).fetchone()
        if not t:
            raise ReconError("الحركة غير موجودة")
        _set_reason(conn, txn_id, reason, note, t["account_id"], t["currency"])


def _drop_conflicting_proposals(conn, txn_ids, keep):
    ids = [r["match_id"] for r in conn.execute(
        f"SELECT DISTINCT mi.match_id FROM match_items mi JOIN matches m ON m.id=mi.match_id"
        f" WHERE m.status='proposed' AND m.method='auto' AND mi.txn_id IN ({','.join('?' * len(txn_ids))})",
        list(txn_ids)) if r["match_id"] != keep]
    if ids:
        ph = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM match_items WHERE match_id IN ({ph})", ids)
        conn.execute(f"DELETE FROM matches WHERE id IN ({ph})", ids)


def name_differences(conn, txn_ids: list[int]) -> list[str]:
    """يعيد وصفًا لكل زوج (نظام، صراف) اسماه مختلفان أو مرتبطان، حتى تطلب الواجهة تأكيد المستخدم.
    قائمة فارغة = لا يوجد اختلاف واضح في الأسماء."""
    rows = conn.execute(f"SELECT * FROM transactions WHERE id IN ({','.join('?' * len(txn_ids))})",
                        list(txn_ids)).fetchall()
    if not rows:
        return []
    cfg = engine_config(conn, rows[0]["account_id"])

    def item(t):
        return Item(id=t["id"], side=t["side"], eff_dir="D", open=0, amount=0, date=date.fromisoformat(t["txn_date"]),
                    ref=t["reference"] or "", desc=t["description"] or "", party=t["counterparty"] or "")

    out = []
    sys_i = [item(t) for t in rows if t["side"] == "system"]
    ex_i = [item(t) for t in rows if t["side"] == "exchanger"]
    for a in sys_i:
        for b in ex_i:
            link = linked_party(a, b, cfg)
            if link:
                out.append(f"اسم مرتبط ({link}): «{a.text}» ↔ «{b.text}»")
            elif names_conflict(a, b, cfg) or (a.text and b.text and similarity(a.text, b.text) < 0.3):
                out.append(f"النظام: «{a.text}»  ↔  الصراف: «{b.text}»")
    return out


def match_name_warning(conn, match_id: int) -> list[str]:
    reasons = json.loads(conn.execute("SELECT reasons FROM matches WHERE id=?", (match_id,)).fetchone()[0] or "[]")
    if not any(r.startswith("اسم مرتبط") or r.startswith("الاسم مختلف") for r in reasons):
        return []
    ids = [r["txn_id"] for r in conn.execute("SELECT txn_id FROM match_items WHERE match_id=?", (match_id,))]
    return name_differences(conn, ids)


# ------------------------------------------------------------------ party links

def party_links(conn, account_id: int | None = None):
    """الأسماء المرتبطة العامة + الخاصة بالحساب."""
    if account_id:
        return conn.execute("SELECT l.*, a.name account_name FROM party_links l LEFT JOIN accounts a"
                            " ON a.id=l.account_id WHERE l.account_id IS NULL OR l.account_id=? ORDER BY l.id",
                            (account_id,)).fetchall()
    return conn.execute("SELECT l.*, a.name account_name FROM party_links l LEFT JOIN accounts a"
                        " ON a.id=l.account_id ORDER BY l.id").fetchall()


def add_party_link(conn, name_a: str, name_b: str, account_id: int | None = None, note: str = "") -> int:
    """يربط اسمين (مثلًا: الأخ يحوّل عن أخيه) حتى يعاملهما محرك المطابقة كطرف واحد."""
    a, b = (name_a or "").strip(), (name_b or "").strip()
    if not a or not b:
        raise ReconError("أدخل الاسمين")
    if normalize(a) == normalize(b):
        raise ReconError("الاسمان متطابقان، لا حاجة للربط")
    if len(normalize(a)) < 3 or len(normalize(b)) < 3:
        raise ReconError("الاسم قصير جدًا. اكتب الاسم كما يظهر في الكشف (مثلًا: علي سعيد)")
    with Tx(conn):
        cur = conn.execute("INSERT INTO party_links(account_id, name_a, name_b, note, created_at, created_by)"
                           " VALUES (?,?,?,?,?,?)", (account_id, a, b, note, now_iso(), current_user(conn)))
        audit(conn, "ربط أسماء", entity="party_link", entity_id=cur.lastrowid, account_id=account_id,
              name_a=a, name_b=b, note=note)
    return cur.lastrowid


def delete_party_link(conn, link_id: int) -> None:
    with Tx(conn):
        r = conn.execute("SELECT * FROM party_links WHERE id=?", (link_id,)).fetchone()
        if not r:
            raise ReconError("الربط غير موجود")
        conn.execute("DELETE FROM party_links WHERE id=?", (link_id,))
        audit(conn, "حذف ربط أسماء", entity="party_link", entity_id=link_id, account_id=r["account_id"],
              name_a=r["name_a"], name_b=r["name_b"])


# ------------------------------------------------------------------ duplicates

def mark_duplicate(conn, txn_id: int, of_id: int | None, note: str = "") -> None:
    with Tx(conn):
        used = conn.execute(
            "SELECT 1 FROM match_items mi JOIN matches m ON m.id=mi.match_id WHERE mi.txn_id=? AND m.status='approved'",
            (txn_id,)).fetchone()
        if used:
            raise ReconError("الحركة داخلة في مطابقة معتمدة. فك المطابقة أولًا.")
        t = conn.execute("SELECT * FROM transactions WHERE id=?", (txn_id,)).fetchone()
        conn.execute("UPDATE transactions SET status='duplicate', duplicate_of=?, dup_reviewed=1, note=? WHERE id=?",
                     (of_id, note, txn_id))
        _drop_conflicting_proposals(conn, [txn_id], keep=None)
        audit(conn, "تأكيد تكرار", entity="txn", entity_id=txn_id, account_id=t["account_id"],
              currency=t["currency"], duplicate_of=of_id, note=note)


def mark_not_duplicate(conn, txn_id: int, note: str = "") -> None:
    with Tx(conn):
        t = conn.execute("SELECT * FROM transactions WHERE id=?", (txn_id,)).fetchone()
        conn.execute("UPDATE transactions SET status='active', duplicate_of=NULL, dup_reviewed=1 WHERE id=?",
                     (txn_id,))
        audit(conn, "ليست مكررة", entity="txn", entity_id=txn_id, account_id=t["account_id"],
              currency=t["currency"], note=note)


def set_txn_note(conn, txn_id: int, note: str) -> None:
    with Tx(conn):
        t = conn.execute("SELECT account_id, currency, note FROM transactions WHERE id=?", (txn_id,)).fetchone()
        conn.execute("UPDATE transactions SET note=? WHERE id=?", (note, txn_id))
        audit(conn, "ملاحظة على حركة", entity="txn", entity_id=txn_id, account_id=t["account_id"],
              currency=t["currency"], old=t["note"], new=note)


# ------------------------------------------------------------------ queries for UI / reports

def match_rows(conn, status: str | None = None, account_id: int | None = None, currency: str | None = None):
    sql = ("SELECT m.*, a.name account_name FROM matches m JOIN accounts a ON a.id=m.account_id WHERE 1=1")
    params: list = []
    if status:
        sql += " AND m.status=?"
        params.append(status)
    if account_id:
        sql += " AND m.account_id=?"
        params.append(account_id)
    if currency:
        sql += " AND m.currency=?"
        params.append(currency)
    sql += " ORDER BY m.score DESC, m.id" if status == "proposed" else " ORDER BY m.id DESC"
    return conn.execute(sql, params).fetchall()


def match_items(conn, match_id: int):
    return conn.execute(
        "SELECT mi.*, t.txn_date, t.description, t.reference, t.counterparty, t.direction, t.amount total"
        " FROM match_items mi JOIN transactions t ON t.id=mi.txn_id WHERE mi.match_id=? ORDER BY mi.side DESC, t.txn_date",
        (match_id,)).fetchall()


def txn_rows(conn, account_id: int | None = None, currency: str | None = None, side: str | None = None,
             open_only: bool = False, retro_only: bool = False, suspects_only: bool = False,
             statuses: tuple = ("active",)):
    sql = ("SELECT t.*, a.name account_name, a.mirror,"
           " COALESCE((SELECT SUM(mi.amount) FROM match_items mi JOIN matches m ON m.id=mi.match_id"
           "   WHERE mi.txn_id=t.id AND m.status='approved'),0) matched"
           " FROM transactions t JOIN accounts a ON a.id=t.account_id WHERE 1=1")
    params: list = []
    if statuses:
        sql += f" AND t.status IN ({','.join('?' * len(statuses))})"
        params += list(statuses)
    for col, val in (("t.account_id", account_id), ("t.currency", currency), ("t.side", side)):
        if val:
            sql += f" AND {col}=?"
            params.append(val)
    if retro_only:
        sql += " AND t.is_retroactive=1"
    if suspects_only:
        sql += " AND t.dup_suspect_of IS NOT NULL AND t.dup_reviewed=0"
    sql += " ORDER BY t.txn_date, t.id"
    rows = conn.execute(sql, params).fetchall()
    if open_only:
        rows = [r for r in rows if r["amount"] - r["matched"] > 0]
    return rows


def audit_rows(conn, account_id: int | None = None, limit: int = 5000):
    sql = "SELECT l.*, a.name account_name FROM audit_log l LEFT JOIN accounts a ON a.id=l.account_id"
    params: list = []
    if account_id:
        sql += " WHERE l.account_id=?"
        params.append(account_id)
    sql += " ORDER BY l.id DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def batches(conn, account_id: int | None = None):
    sql = "SELECT b.*, a.name account_name FROM import_batches b JOIN accounts a ON a.id=b.account_id"
    params = []
    if account_id:
        sql += " WHERE b.account_id=?"
        params.append(account_id)
    return conn.execute(sql + " ORDER BY b.id DESC", params).fetchall()


def open_database(path=None) -> tuple[sqlite3.Connection, Path]:
    data_dir = Path(path).parent if path else db.default_data_dir()
    db_path = Path(path) if path else data_dir / "sarraf_recon.db"
    return db.connect(db_path), data_dir
