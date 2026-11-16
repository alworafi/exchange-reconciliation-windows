"""طبقة الاستيراد المرنة: جدول خام -> تعرّف على الأعمدة -> صفوف موحّدة.

لا تفترض هذه الطبقة تنسيقًا محددًا لأي صراف. هي تقترح تحديد الأعمدة
تلقائيًا من العناوين، ويستطيع المستخدم تعديل التحديد يدويًا وحفظه كقالب.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from ..money import AmountError, ZERO, to_decimal
from ..textnorm import normalize

FIELDS = [
    ("date", "التاريخ"),
    ("description", "البيان"),
    ("reference", "المرجع"),
    ("counterparty", "اسم الطرف"),
    ("debit", "مدين"),
    ("credit", "دائن"),
    ("amount", "المبلغ (عمود واحد)"),
    ("type", "نوع الحركة (مدين/دائن)"),
    ("balance", "الرصيد"),
    ("currency", "العملة"),
]
FIELD_LABELS = dict(FIELDS)

# كلمات العناوين لكل حقل (تُقارن بعد التوحيد). الترتيب مهم: الأدق أولًا.
HEADER_SYNONYMS: dict[str, list[str]] = {
    "date": ["التاريخ", "تاريخ", "تاريخ الحركه", "تاريخ القيد", "تاريخ العمليه", "date", "value date"],
    "description": ["البيان", "بيان", "الوصف", "التفاصيل", "تفاصيل", "شرح", "ملاحظات", "description",
                    "details", "narration", "particulars"],
    "reference": ["المرجع", "رقم المرجع", "مرجع", "رقم السند", "رقم القيد", "رقم الحواله", "رقم العمليه",
                  "رقم المستند", "المستند", "السند", "ref", "reference", "voucher", "doc no"],
    "counterparty": ["الاسم", "اسم", "اسم المستفيد", "المستفيد", "المرسل", "المستلم", "اسم العميل",
                     "الطرف", "name", "beneficiary"],
    "debit": ["مدين", "المدين", "عليه", "منه", "سحب", "debit", "dr", "withdrawal"],
    "credit": ["دائن", "الدائن", "له", "اليه", "ايداع", "credit", "cr", "deposit"],
    "amount": ["المبلغ", "مبلغ", "القيمه", "amount", "value"],
    "type": ["النوع", "نوع الحركه", "نوع العمليه", "الاتجاه", "type", "dr cr"],
    "balance": ["الرصيد", "رصيد", "الرصيد التراكمي", "balance", "running balance"],
    "currency": ["العمله", "عمله", "currency", "ccy"],
}

CURRENCY_WORDS: list[tuple[str, list[str]]] = [
    ("SAR", ["ريال سعودي", "سعودي", "ر س", "sar", "sr"]),
    ("YER", ["ريال يمني", "يمني", "ر ي", "yer", "yr"]),
    ("USD", ["دولار", "usd", "$"]),
]

OPENING_WORDS = ["رصيد سابق", "رصيد افتتاحي", "الرصيد السابق", "الرصيد الافتتاحي", "رصيد مرحل",
                 "رصيد اول المده", "رصيد بدايه", "ما قبله", "opening balance", "balance b f",
                 "brought forward"]
CLOSING_WORDS = ["رصيد ختامي", "الرصيد الختامي", "الرصيد الحالي", "رصيد اخر المده", "الرصيد النهائي",
                 "closing balance", "balance c f", "carried forward"]
TOTAL_WORDS = ["الاجمالي", "اجمالي", "المجموع", "مجموع", "الاجماليات", "total", "totals"]

DEBIT_TYPE_WORDS = ["مدين", "عليه", "dr", "debit", "سحب", "d"]
CREDIT_TYPE_WORDS = ["دائن", "له", "cr", "credit", "ايداع", "c"]


@dataclass
class RawTable:
    """جدول خام كما قُرئ من الملف (قيم الخلايا كما هي)."""
    rows: list[list]
    source: str = ""
    sheet: str = ""
    title_text: str = ""        # نصوص ما قبل العناوين (للتعرف على العملة/الفترة)

    @property
    def width(self) -> int:
        return max((len(r) for r in self.rows), default=0)


@dataclass
class Mapping:
    header_row: int | None = None                       # رقم صف العناوين (0-based) أو None
    columns: dict[str, int | None] = field(default_factory=dict)
    default_currency: str = "YER"
    date_order: str = "DMY"                             # DMY أو MDY أو YMD للنصوص الغامضة
    balance_sign: int = 0                                # 0 تلقائي، 1: رصيد = مدين − دائن، -1: العكس
    signed_amount_negative_is: str = "C"                 # في حال عمود مبلغ واحد بإشارة

    def to_dict(self) -> dict:
        return {
            "header_row": self.header_row, "columns": self.columns,
            "default_currency": self.default_currency, "date_order": self.date_order,
            "balance_sign": self.balance_sign,
            "signed_amount_negative_is": self.signed_amount_negative_is,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Mapping":
        m = cls()
        m.header_row = d.get("header_row")
        m.columns = {k: (int(v) if v is not None else None) for k, v in (d.get("columns") or {}).items()}
        m.default_currency = d.get("default_currency", "YER")
        m.date_order = d.get("date_order", "DMY")
        m.balance_sign = int(d.get("balance_sign", 0))
        m.signed_amount_negative_is = d.get("signed_amount_negative_is", "C")
        return m


@dataclass
class ParsedRow:
    source_row: int                       # رقم الصف في الملف (1-based للعرض)
    kind: str                             # txn | opening | closing | skip
    date: date | None = None
    description: str = ""
    reference: str = ""
    counterparty: str = ""
    debit: Decimal = ZERO
    credit: Decimal = ZERO
    balance: Decimal | None = None
    currency: str = ""
    reason: str = ""                      # سبب التخطي أو ملاحظة

    @property
    def direction(self) -> str:
        return "D" if self.debit > 0 else "C"

    @property
    def amount(self) -> Decimal:
        return self.debit if self.debit > 0 else self.credit


@dataclass
class ParseResult:
    rows: list[ParsedRow]
    warnings: list[str]
    balance_sign: int                     # الاصطلاح المكتشف/المستخدم لعمود الرصيد

    @property
    def txns(self) -> list[ParsedRow]:
        return [r for r in self.rows if r.kind == "txn"]


# ---------------------------------------------------------------- helpers

def cell_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d")
    return str(v).strip()


def _contains_word(text_norm: str, word: str) -> bool:
    w = normalize(word)
    if not w:
        return False
    if len(w) <= 2:
        return text_norm == w or text_norm.split() == [w]
    return w in text_norm


def match_header(cell) -> str | None:
    t = normalize(cell_text(cell))
    if not t or len(t) > 40:
        return None
    best, best_len = None, 0
    for fld, words in HEADER_SYNONYMS.items():
        for w in words:
            wn = normalize(w)
            if t == wn or (len(wn) > 2 and (t.startswith(wn) or wn in t.split() or wn in t)):
                if len(wn) > best_len:
                    best, best_len = fld, len(wn)
    return best


def detect_currency(text) -> str | None:
    t = normalize(cell_text(text))
    if not t:
        return None
    raw = cell_text(text)
    for code, words in CURRENCY_WORDS:
        for w in words:
            if w == "$":
                if "$" in raw:
                    return code
            elif _contains_word(t, w):
                return code
    return None


def parse_date(value, order: str = "DMY") -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if 20000 < float(value) < 80000:                   # رقم تسلسلي في Excel
            return date(1899, 12, 30) + timedelta(days=int(value))
        return None
    s = normalize(str(value)).replace(" ", "/")
    s = re.sub(r"[\\.\-]", "/", s)
    m = re.search(r"(\d{1,4})/(\d{1,2})/(\d{1,4})", s)
    if not m:
        return None
    a, b, c = m.groups()
    try:
        if len(a) == 4:
            return date(int(a), int(b), int(c))
        y = int(c) + (2000 if len(c) == 2 else 0)
        if order == "MDY":
            return date(y, int(a), int(b))
        d, mth = int(a), int(b)
        if mth > 12 and d <= 12:                           # تبديل واضح
            d, mth = mth, d
        return date(y, mth, d)
    except ValueError:
        return None


def row_text(row) -> str:
    return normalize(" ".join(cell_text(c) for c in row))


def _has_any(text_norm: str, words) -> bool:
    return any(_contains_word(text_norm, w) for w in words)


# ---------------------------------------------------------------- mapping detection

def detect_mapping(table: RawTable, default_currency: str = "YER", max_scan: int = 40) -> Mapping:
    best_row, best_cols, best_score = None, {}, 0
    for i, row in enumerate(table.rows[:max_scan]):
        cols: dict[str, int] = {}
        for j, cell in enumerate(row):
            fld = match_header(cell)
            if fld and fld not in cols:
                cols[fld] = j
        score = len(cols) + (2 if "date" in cols else 0) + (
            2 if ("debit" in cols and "credit" in cols) or "amount" in cols else 0)
        if score > best_score:
            best_row, best_cols, best_score = i, cols, score
    m = Mapping(default_currency=default_currency)
    if best_row is not None and best_score >= 4:
        m.header_row = best_row
        m.columns = {f: best_cols.get(f) for f, _ in FIELDS}
        title = " ".join(cell_text(c) for r in table.rows[:best_row] for c in r) + " " + table.title_text
        cur = detect_currency(title) or detect_currency(table.sheet)
        if cur:
            m.default_currency = cur
    else:
        m.columns = {f: None for f, _ in FIELDS}
    return m


# ---------------------------------------------------------------- row parsing

def _get(row, idx):
    if idx is None or idx >= len(row):
        return None
    return row[idx]


def parse_table(table: RawTable, mapping: Mapping) -> ParseResult:
    cols = mapping.columns
    warnings: list[str] = []
    out: list[ParsedRow] = []
    if cols.get("date") is None:
        raise ValueError("يجب تحديد عمود التاريخ")
    has_dc = cols.get("debit") is not None or cols.get("credit") is not None
    if not has_dc and cols.get("amount") is None:
        raise ValueError("يجب تحديد عمودي المدين والدائن، أو عمود المبلغ")

    start = (mapping.header_row + 1) if mapping.header_row is not None else 0
    section_currency = None
    last_date = None
    for i in range(start, len(table.rows)):
        row = table.rows[i]
        src = i + 1
        text = row_text(row)
        if not text:
            continue
        pr = ParsedRow(source_row=src, kind="skip")
        pr.description = cell_text(_get(row, cols.get("description")))
        pr.reference = cell_text(_get(row, cols.get("reference")))
        pr.counterparty = cell_text(_get(row, cols.get("counterparty")))
        try:
            if has_dc:
                d = to_decimal(_get(row, cols.get("debit"))) or ZERO
                c = to_decimal(_get(row, cols.get("credit"))) or ZERO
                if d < 0:                                   # مدين سالب = دائن
                    d, c = ZERO, c + (-d)
                if c < 0:
                    c, d = ZERO, d + (-c)
            else:
                a = to_decimal(_get(row, cols.get("amount"))) or ZERO
                typ = normalize(cell_text(_get(row, cols.get("type"))))
                if typ and _has_any(typ, CREDIT_TYPE_WORDS):
                    d, c = ZERO, abs(a)
                elif typ and _has_any(typ, DEBIT_TYPE_WORDS):
                    d, c = abs(a), ZERO
                elif a < 0:
                    if mapping.signed_amount_negative_is == "C":
                        d, c = ZERO, -a
                    else:
                        d, c = -a, ZERO
                else:
                    if mapping.signed_amount_negative_is == "C":
                        d, c = a, ZERO
                    else:
                        d, c = ZERO, a
            bal_raw = _get(row, cols.get("balance"))
            pr.balance = to_decimal(bal_raw) if cols.get("balance") is not None else None
        except AmountError as exc:
            pr.reason = f"مبلغ غير مقروء: {exc}"
            out.append(pr)
            warnings.append(f"الصف {src}: {pr.reason}")
            continue

        cur = None
        if cols.get("currency") is not None:
            cur = detect_currency(_get(row, cols.get("currency"))) or (
                cell_text(_get(row, cols.get("currency"))).upper() or None)
        pr.date = parse_date(_get(row, cols.get("date")), mapping.date_order)

        # صف عنوان قسم عملة (بدون مبالغ): "العملة: ريال سعودي"
        if d == 0 and c == 0 and pr.balance is None and pr.date is None:
            sec = detect_currency(text)
            if sec:
                section_currency = sec
                pr.reason = f"بداية قسم عملة {sec}"
            else:
                pr.reason = "صف بدون مبالغ"
            out.append(pr)
            continue
        pr.currency = cur or section_currency or mapping.default_currency

        if _has_any(text, TOTAL_WORDS) and pr.date is None:
            pr.reason = "صف إجمالي"
            out.append(pr)
            continue
        if _has_any(text, OPENING_WORDS):
            pr.kind = "opening"
            pr.date = pr.date or last_date
            if pr.balance is None:
                pr.balance = d - c
            out.append(pr)
            continue
        if _has_any(text, CLOSING_WORDS) and (pr.balance is not None or d or c):
            pr.kind = "closing"
            pr.date = pr.date or last_date
            if pr.balance is None:
                pr.balance = d - c
            out.append(pr)
            continue
        if d == 0 and c == 0:
            pr.reason = "حركة بمبلغ صفر"
            out.append(pr)
            continue
        if d > 0 and c > 0:
            # حركة فيها مدين ودائن معًا: نأخذ الصافي ونحذّر
            net = d - c
            warnings.append(f"الصف {src}: مدين ودائن في نفس الصف، استُخدم الصافي {net}")
            d, c = (net, ZERO) if net > 0 else (ZERO, -net)
            if net == 0:
                pr.reason = "مدين = دائن في نفس الصف"
                out.append(pr)
                continue
        if pr.date is None:
            pr.reason = "تاريخ غير مقروء"
            warnings.append(f"الصف {src}: {pr.reason} ({cell_text(_get(row, cols.get('date')))})")
            out.append(pr)
            continue
        pr.kind = "txn"
        pr.debit, pr.credit = d, c
        last_date = pr.date
        out.append(pr)

    sign = mapping.balance_sign or detect_balance_sign(out)
    warnings.extend(check_running_balance(out, sign))
    return ParseResult(rows=out, warnings=warnings, balance_sign=sign)


def detect_balance_sign(rows: list[ParsedRow]) -> int:
    """يكتشف هل عمود الرصيد = سابق + مدين − دائن (1) أم سابق − مدين + دائن (-1)."""
    votes = {1: 0, -1: 0}
    prev = None
    for r in rows:
        if r.kind not in ("txn", "opening") or r.balance is None:
            continue
        if prev is not None and r.kind == "txn":
            delta = r.balance - prev
            if delta == r.debit - r.credit:
                votes[1] += 1
            elif delta == r.credit - r.debit:
                votes[-1] += 1
        prev = r.balance
    if votes[1] == 0 and votes[-1] == 0:
        return 1
    return 1 if votes[1] >= votes[-1] else -1


def check_running_balance(rows: list[ParsedRow], sign: int) -> list[str]:
    """يتحقق من تسلسل الرصيد في الكشف ويحذّر من الصفوف المفقودة أو الخاطئة."""
    warnings = []
    prev = None
    prev_cur = None
    bad = 0
    for r in rows:
        if r.kind not in ("txn", "opening") or r.balance is None:
            continue
        if prev is not None and r.kind == "txn" and r.currency == prev_cur:
            expected = prev + sign * (r.debit - r.credit)
            if expected != r.balance:
                bad += 1
                if bad <= 10:
                    warnings.append(
                        f"الصف {r.source_row}: الرصيد في الكشف {r.balance:,.2f} والمتوقع {expected:,.2f}")
        prev, prev_cur = r.balance, r.currency
    if bad > 10:
        warnings.append(f"... و{bad - 10} صفوف أخرى لا يتسلسل فيها الرصيد")
    return warnings
