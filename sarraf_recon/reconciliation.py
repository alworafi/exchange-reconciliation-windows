"""حساب الأرصدة والفروقات لكل حساب ولكل عملة على حدة.

اصطلاح الإشارة (موحّد في كل البرنامج):
- كل الأرصدة تُعرض من منظور النظام: موجب = مدين (الصراف عليه لنا)، سالب = دائن (له).
- رصيد الصراف يُحوَّل لمنظور النظام: إذا كان كشفه من منظوره (عكسي) نضربه في −1.
- الفرق = رصيد النظام − رصيد الصراف (بعد التحويل).

المعادلات التي يتحقق منها البرنامج في كل تقرير:
    فرق الختامي = فرق الافتتاحي + فرق حركات الفترة
    فرق الختامي = الفروقات المفسَّرة + الفروقات غير المفسَّرة
    الفروقات المفسَّرة = Σ المتبقي المفتوح في النظام − Σ المتبقي المفتوح لدى الصراف (بالإشارة)
لا يُنشئ البرنامج أي قيد تسوية. غير المفسَّر يبقى ظاهرًا كما هو.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .money import ZERO, from_minor
from .services import eff_dir


@dataclass
class SideBalance:
    base_as_of: str | None = None       # تاريخ الرصيد الأساسي (أول رصيد افتتاحي معروف)
    base_balance: Decimal = ZERO        # من منظور النظام
    opening: Decimal = ZERO
    period_debit: Decimal = ZERO        # من منظور النظام (اتجاه فعلي)
    period_credit: Decimal = ZERO
    closing: Decimal = ZERO
    own_opening: Decimal = ZERO         # كما في الكشف نفسه (مدين − دائن)
    own_closing: Decimal = ZERO
    excluded_before_base: int = 0       # حركات أقدم من الرصيد الأساسي (لا تدخل الرصيد)

    @property
    def movement(self) -> Decimal:
        return self.period_debit - self.period_credit


@dataclass
class OpenItem:
    txn_id: int
    side: str
    date: str
    description: str
    reference: str
    counterparty: str
    direction: str          # كما في الكشف
    eff_dir: str            # من منظور النظام
    amount: Decimal
    matched: Decimal
    open: Decimal
    category: str           # system_only / exchanger_only / system_partial / exchanger_partial
    in_period: bool
    is_retroactive: bool
    age_days: int

    @property
    def signed(self) -> Decimal:
        return self.open if self.eff_dir == "D" else -self.open


CATEGORY_LABEL = {
    "system_only": "موجود بالنظام فقط (لم يسجله الصراف / لم يحوّل)",
    "exchanger_only": "موجود بالصراف فقط (لم يُسجّل بالنظام)",
    "system_partial": "متبقي مطابقة جزئية - طرف النظام",
    "exchanger_partial": "متبقي مطابقة جزئية - طرف الصراف",
}


@dataclass
class CurrencyRecon:
    account_id: int
    account_name: str
    currency: str
    date_from: str
    date_to: str
    system: SideBalance
    exchanger: SideBalance
    open_items: list[OpenItem] = field(default_factory=list)
    balance_checks: list[dict] = field(default_factory=list)

    @property
    def opening_diff(self) -> Decimal:
        return self.system.opening - self.exchanger.opening

    @property
    def movement_diff(self) -> Decimal:
        return self.system.movement - self.exchanger.movement

    @property
    def closing_diff(self) -> Decimal:
        return self.system.closing - self.exchanger.closing

    @property
    def explained(self) -> Decimal:
        s = sum((i.signed for i in self.open_items if i.side == "system"), ZERO)
        e = sum((i.signed for i in self.open_items if i.side == "exchanger"), ZERO)
        return s - e

    @property
    def unexplained(self) -> Decimal:
        return self.closing_diff - self.explained

    @property
    def base_diff(self) -> Decimal:
        """جزء غير مفصّل بحركات: فرق الرصيدين الأساسيين المستوردين من الكشوفات."""
        return self.system.base_balance - self.exchanger.base_balance

    @property
    def identity_ok(self) -> bool:
        return self.closing_diff == self.opening_diff + self.movement_diff

    def category_totals(self) -> dict[str, dict[str, Decimal]]:
        out = {k: {"D": ZERO, "C": ZERO, "count": 0} for k in CATEGORY_LABEL}
        for i in self.open_items:
            out[i.category][i.eff_dir] += i.open
            out[i.category]["count"] += 1
        return out

    def explained_split(self) -> dict[str, Decimal]:
        """تفصيل المفسَّر: ما يخص ما قبل الفترة وما يخص الفترة."""
        def part(pred):
            s = sum((i.signed for i in self.open_items if i.side == "system" and pred(i)), ZERO)
            e = sum((i.signed for i in self.open_items if i.side == "exchanger" and pred(i)), ZERO)
            return s - e
        return {"before_period": part(lambda i: not i.in_period), "in_period": part(lambda i: i.in_period)}


def balance_label(v: Decimal) -> str:
    if v > 0:
        return "مدين (عليه)"
    if v < 0:
        return "دائن (له)"
    return "متوازن"


def _base(conn, account_id, side, currency):
    return conn.execute(
        "SELECT * FROM statement_balances WHERE account_id=? AND side=? AND currency=? AND kind='opening'"
        " ORDER BY as_of, CASE source WHEN 'manual' THEN 0 WHEN 'statement' THEN 1 ELSE 2 END, id LIMIT 1",
        (account_id, side, currency)).fetchone()


def compute(conn, account_id: int, currency: str, date_from: date | str, date_to: date | str) -> CurrencyRecon:
    df = date_from.isoformat() if isinstance(date_from, date) else date_from
    dt = date_to.isoformat() if isinstance(date_to, date) else date_to
    acc = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    mirror = bool(acc["mirror"])
    conv = {"system": 1, "exchanger": -1 if mirror else 1}

    txns = conn.execute(
        "SELECT * FROM transactions WHERE account_id=? AND currency=? AND status='active' AND txn_date<=?"
        " ORDER BY txn_date, id", (account_id, currency, dt)).fetchall()

    # المطابقات المعتمدة التي كل حركاتها ضمن تاريخ نهاية الفترة فقط تُحتسب في ذلك التاريخ
    alloc: dict[int, int] = {}
    for m in conn.execute(
            "SELECT id FROM matches WHERE account_id=? AND currency=? AND status='approved'", (account_id, currency)):
        items = conn.execute(
            "SELECT mi.txn_id, mi.amount, t.txn_date FROM match_items mi JOIN transactions t ON t.id=mi.txn_id"
            " WHERE mi.match_id=?", (m["id"],)).fetchall()
        if all(i["txn_date"] <= dt for i in items):
            for i in items:
                alloc[i["txn_id"]] = alloc.get(i["txn_id"], 0) + i["amount"]

    sides = {}
    open_items: list[OpenItem] = []
    end = date.fromisoformat(dt)
    for side in ("system", "exchanger"):
        sb = SideBalance()
        base = _base(conn, account_id, side, currency)
        base_date = base["as_of"] if base else None
        own_base = from_minor(base["balance"]) if base else ZERO
        sb.base_as_of = base_date
        sb.base_balance = conv[side] * own_base
        own_open = own_base
        own_mov_d, own_mov_c = ZERO, ZERO
        for t in (x for x in txns if x["side"] == side):
            if base_date and t["txn_date"] < base_date:
                sb.excluded_before_base += 1
                continue
            amt = from_minor(t["amount"])
            signed_own = amt if t["direction"] == "D" else -amt
            if t["txn_date"] < df:
                own_open += signed_own
            else:
                if t["direction"] == "D":
                    own_mov_d += amt
                else:
                    own_mov_c += amt
            matched = from_minor(alloc.get(t["id"], 0))
            remaining = amt - matched
            if remaining > 0:
                cat = f"{side}_only" if matched == 0 else f"{side}_partial"
                open_items.append(OpenItem(
                    txn_id=t["id"], side=side, date=t["txn_date"], description=t["description"] or "",
                    reference=t["reference"] or "", counterparty=t["counterparty"] or "",
                    direction=t["direction"], eff_dir=eff_dir(side, t["direction"], mirror), amount=amt,
                    matched=matched, open=remaining, category=cat, in_period=t["txn_date"] >= df,
                    is_retroactive=bool(t["is_retroactive"]),
                    age_days=(end - date.fromisoformat(t["txn_date"])).days))
        sb.own_opening = own_open
        sb.own_closing = own_open + own_mov_d - own_mov_c
        sb.opening = conv[side] * sb.own_opening
        sb.closing = conv[side] * sb.own_closing
        if conv[side] == 1:
            sb.period_debit, sb.period_credit = own_mov_d, own_mov_c
        else:
            sb.period_debit, sb.period_credit = own_mov_c, own_mov_d
        sides[side] = sb

    rec = CurrencyRecon(account_id, acc["name"], currency, df, dt, sides["system"], sides["exchanger"], open_items)
    rec.balance_checks = balance_checks(conn, account_id, currency, mirror)
    return rec


def balance_checks(conn, account_id: int, currency: str, mirror: bool) -> list[dict]:
    """يقارن كل رصيد ورد في الكشوفات بالرصيد المحسوب من الحركات المخزنة.

    أي اختلاف يعني غالبًا صفوفًا ناقصة في الاستيراد أو رصيدًا خاطئًا في الكشف.
    """
    out = []
    for side in ("system", "exchanger"):
        base = _base(conn, account_id, side, currency)
        if not base:
            continue
        rows = conn.execute(
            "SELECT * FROM statement_balances WHERE account_id=? AND side=? AND currency=? ORDER BY as_of, id",
            (account_id, side, currency)).fetchall()
        for r in rows:
            if r["id"] == base["id"]:
                continue
            op = "<" if r["kind"] == "opening" else "<="
            s = conn.execute(
                f"SELECT COALESCE(SUM(CASE direction WHEN 'D' THEN amount ELSE -amount END),0) s FROM transactions"
                f" WHERE account_id=? AND side=? AND currency=? AND status='active' AND txn_date>=? AND txn_date {op} ?",
                (account_id, side, currency, base["as_of"], r["as_of"])).fetchone()["s"]
            computed = from_minor(base["balance"] + s)
            reported = from_minor(r["balance"])
            out.append({"side": side, "kind": r["kind"], "as_of": r["as_of"], "source": r["source"],
                        "reported": reported, "computed": computed, "diff": reported - computed})
    return out


def account_currencies(conn, account_id: int) -> list[str]:
    return [r["currency"] for r in conn.execute(
        "SELECT DISTINCT currency FROM transactions WHERE account_id=? UNION"
        " SELECT DISTINCT currency FROM statement_balances WHERE account_id=? ORDER BY 1", (account_id, account_id))]


def date_bounds(conn, account_id: int | None = None) -> tuple[str | None, str | None]:
    sql = "SELECT MIN(txn_date) a, MAX(txn_date) b FROM transactions"
    params = []
    if account_id:
        sql += " WHERE account_id=?"
        params.append(account_id)
    r = conn.execute(sql, params).fetchone()
    return r["a"], r["b"]
