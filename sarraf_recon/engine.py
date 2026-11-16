"""محرك المطابقة: يولّد مقترحات مطابقة مع درجة ثقة وأسباب واضحة.

القواعد الصارمة (لا تُكسر أبدًا):
- نفس الحساب ونفس العملة.
- نفس الاتجاه الفعلي (بعد عكس كشف الصراف إن كان من منظوره هو).
- طرف واحد على الأقل من كل جانب.
- لا يتجاوز التخصيص المبلغ المتبقي (المفتوح) للحركة.

التسجيل: المبلغ وحده لا يكفي للاعتماد التلقائي. أقصى درجة ممكنة بدون تطابق
المرجع هي 70، وحد الاعتماد التلقائي الافتراضي 85. ما دون ذلك يُعرض للمراجعة.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from itertools import combinations

from .textnorm import normalize_ref, numbers_in, similarity


@dataclass
class Item:
    id: int
    side: str                 # system | exchanger
    eff_dir: str              # الاتجاه من منظور النظام: D / C
    open: int                 # المبلغ المتبقي بالوحدة الصغرى
    amount: int               # المبلغ الكامل
    date: date
    ref: str = ""
    desc: str = ""
    party: str = ""
    nums: set = field(default_factory=set)

    @property
    def text(self) -> str:
        return f"{self.desc} {self.party}".strip()


@dataclass
class Proposal:
    kind: str                           # one_to_one / one_to_many / many_to_one / many_to_many
    allocations: list[tuple[int, str, int]]   # (txn_id, side, amount)
    score: int
    reasons: list[str]
    is_partial: bool = False
    auto_ok: bool = False

    @property
    def signature(self) -> str:
        return make_signature(self.allocations)


def make_signature(allocations) -> str:
    return "|".join(f"{s[0]}:{i}:{a}" for i, s, a in sorted(allocations, key=lambda x: (x[1], x[0])))


@dataclass
class EngineConfig:
    date_window: int = 30
    max_lookback: int = 120          # عمليات قديمة: مسموح فقط إذا كان المبلغ وحيدًا في الجانبين
    min_score: int = 45
    auto_approve: bool = True
    auto_min_score: int = 85
    ambiguity_gap: int = 10
    max_group: int = 4
    max_candidates: int = 16
    max_many_side: int = 6


# ------------------------------------------------------------------ scoring

def ref_relation(a: Item, b: Item) -> tuple[int, str | None]:
    ra, rb = normalize_ref(a.ref), normalize_ref(b.ref)
    if ra and rb:
        if ra == rb:
            return 30, "تطابق المرجع"
        if len(ra) >= 4 and len(rb) >= 4 and (ra in rb or rb in ra):
            return 22, "المرجع متشابه جزئيًا"
    if ra and len(ra) >= 5 and ra in b.nums:
        return 25, "مرجع النظام موجود في بيان الصراف"
    if rb and len(rb) >= 5 and rb in a.nums:
        return 25, "مرجع الصراف موجود في بيان النظام"
    common = a.nums & b.nums
    if common:
        return 22, f"رقم مشترك في البيان ({sorted(common)[0]})"
    if ra and rb:
        return -10, "مرجعان مختلفان"
    return 0, None


def date_score(days: int, window: int) -> int:
    if days == 0:
        return 15
    if days <= 1:
        return 12
    if days <= 3:
        return 9
    if days <= 7:
        return 6
    if days <= window:
        return 3
    return 0


def text_score(a: Item, b: Item) -> tuple[int, str | None]:
    sim = similarity(a.text, b.text)
    pts = round(15 * sim)
    if pts >= 9:
        return pts, f"تشابه الاسم/البيان {round(sim * 100)}%"
    if pts > 0:
        return pts, None
    return 0, None


def score_pair(a: Item, b: Item, cfg: EngineConfig, exact_amount: bool = True,
               unique_amount: bool = False) -> tuple[int, list[str]] | None:
    """يرجع (الدرجة، الأسباب) أو None إذا كان الزوج غير مقبول."""
    if a.eff_dir != b.eff_dir:
        return None
    days = abs((a.date - b.date).days)
    rpts, rmsg = ref_relation(a, b)
    if days > cfg.date_window and rpts < 20:
        # عملية قديمة (حوالة اليوم مقابل فاتورة قبل شهر): نقبلها فقط إذا كان المبلغ وحيدًا في الجانبين
        if not (unique_amount and days <= cfg.max_lookback):
            return None
    reasons = []
    score = 0
    if unique_amount:
        score += 5
        reasons.append("المبلغ لا يتكرر في الحركات المفتوحة")
    if exact_amount:
        score += 40
        reasons.append("المبلغ متطابق")
    score += rpts
    if rmsg:
        reasons.append(rmsg)
    dpts = date_score(days, cfg.date_window)
    score += dpts
    reasons.append("نفس التاريخ" if days == 0 else f"فرق التاريخ {days} يوم")
    tpts, tmsg = text_score(a, b)
    score += tpts
    if tmsg:
        reasons.append(tmsg)
    return max(0, min(100, score)), reasons


def group_score(target: Item, group: list[Item], cfg: EngineConfig) -> tuple[int, list[str]]:
    days = max(abs((target.date - g.date).days) for g in group)
    refs = [ref_relation(target, g)[0] for g in group]
    texts = [text_score(target, g)[0] for g in group]
    score = 45 + date_score(days, cfg.date_window) + round(max(0, sum(refs) / len(refs))) + round(
        sum(texts) / len(texts))
    score -= 2 * (len(group) - 2)
    reasons = [f"مجموع {len(group)} حركات يساوي المبلغ", f"أقصى فرق تاريخ {days} يوم"]
    if max(refs) >= 20:
        reasons.append("ارتباط بالمرجع")
    return max(0, min(80, score)), reasons


# ------------------------------------------------------------------ engine

def propose(system: list[Item], exchanger: list[Item], cfg: EngineConfig,
            rejected: set[str] | None = None) -> list[Proposal]:
    rejected = rejected or set()
    sys_items = [i for i in system if i.open > 0]
    ex_items = [i for i in exchanger if i.open > 0]
    used: set[tuple[str, int]] = set()
    out: list[Proposal] = []

    def free(i: Item) -> bool:
        return (i.side, i.id) not in used

    def take(p: Proposal) -> bool:
        if p.signature in rejected:
            return False
        out.append(p)
        for tid, side, _ in p.allocations:
            used.add((side, tid))
        return True

    # 1) حركة مقابل حركة بنفس المبلغ
    pairs = []
    by_amount: dict[tuple[str, int], list[Item]] = {}
    for e in ex_items:
        by_amount.setdefault((e.eff_dir, e.open), []).append(e)
    sys_count: dict[tuple[str, int], int] = {}
    for s in sys_items:
        sys_count[(s.eff_dir, s.open)] = sys_count.get((s.eff_dir, s.open), 0) + 1
    for s in sys_items:
        cands = by_amount.get((s.eff_dir, s.open), [])
        unique = len(cands) == 1 and sys_count[(s.eff_dir, s.open)] == 1
        for e in cands:
            r = score_pair(s, e, cfg, exact_amount=True, unique_amount=unique)
            if r and r[0] >= cfg.min_score:
                pairs.append((r[0], s, e, r[1]))
    # للتحقق من الغموض: أفضل درجتين لكل حركة
    best_for: dict[tuple[str, int], list[int]] = {}
    for sc, s, e, _ in pairs:
        best_for.setdefault(("system", s.id), []).append(sc)
        best_for.setdefault(("exchanger", e.id), []).append(sc)
    pairs.sort(key=lambda x: (-x[0], abs((x[1].date - x[2].date).days), x[1].id, x[2].id))
    for sc, s, e, reasons in pairs:
        if not (free(s) and free(e)):
            continue
        rivals = sorted(best_for[("system", s.id)], reverse=True)[1:2] + \
            sorted(best_for[("exchanger", e.id)], reverse=True)[1:2]
        ambiguous = any(sc - r < cfg.ambiguity_gap for r in rivals)
        reasons = list(reasons)
        if ambiguous:
            reasons.append("يوجد مرشح آخر قريب - يحتاج مراجعة")
        p = Proposal("one_to_one", [(s.id, "system", s.open), (e.id, "exchanger", e.open)], sc, reasons)
        p.auto_ok = cfg.auto_approve and sc >= cfg.auto_min_score and not ambiguous
        take(p)

    # 2) حركة مقابل عدة حركات (وبالعكس) بمجموع مساوٍ
    def group_pass(targets: list[Item], pool: list[Item], kind: str):
        for t in sorted(targets, key=lambda x: (-x.open, x.date)):
            if not free(t):
                continue
            cands = [c for c in pool if free(c) and c.eff_dir == t.eff_dir and c.open < t.open
                     and (abs((c.date - t.date).days) <= cfg.date_window or ref_relation(t, c)[0] >= 20)]
            if len(cands) < 2:
                continue
            cands.sort(key=lambda c: (-ref_relation(t, c)[0], abs((c.date - t.date).days)))
            cands = cands[: cfg.max_candidates]
            best = None
            for size in range(2, min(cfg.max_group, len(cands)) + 1):
                for combo in combinations(cands, size):
                    if sum(c.open for c in combo) != t.open:
                        continue
                    sc, reasons = group_score(t, list(combo), cfg)
                    if best is None or sc > best[0]:
                        best = (sc, combo, reasons)
            if best and best[0] >= cfg.min_score:
                sc, combo, reasons = best
                allocs = [(t.id, t.side, t.open)] + [(c.id, c.side, c.open) for c in combo]
                take(Proposal(kind, allocs, sc, reasons))

    group_pass(sys_items, ex_items, "one_to_many")
    group_pass(ex_items, sys_items, "many_to_one")

    # 3) عدة حركات مقابل عدة حركات: نفس المرجع أو نفس اليوم، بمجموعين متساويين
    def many_pass(key_fn, label: str, base: int):
        groups: dict = {}
        for i in sys_items + ex_items:
            if not free(i):
                continue
            k = key_fn(i)
            if k is None:
                continue
            groups.setdefault((i.eff_dir, k), []).append(i)
        for (_, k), items in groups.items():
            ss = [i for i in items if i.side == "system" and free(i)]
            es = [i for i in items if i.side == "exchanger" and free(i)]
            if len(ss) + len(es) < 3 or not ss or not es:
                continue
            if len(ss) > cfg.max_many_side or len(es) > cfg.max_many_side:
                continue
            if sum(i.open for i in ss) != sum(i.open for i in es):
                continue
            allocs = [(i.id, i.side, i.open) for i in ss + es]
            take(Proposal("many_to_many", allocs, base, [label, "المجموعان متساويان"]))

    many_pass(lambda i: normalize_ref(i.ref) or None, "مجموعة بنفس المرجع", 75)
    many_pass(lambda i: i.date, "مجموعة بنفس التاريخ", 50)

    # 4) سداد جزئي: نفس المرجع/البيان بمبالغ مختلفة
    partials = []
    for s in sys_items:
        if not free(s):
            continue
        for e in ex_items:
            if not free(e) or e.eff_dir != s.eff_dir or e.open == s.open:
                continue
            rpts, rmsg = ref_relation(s, e)
            days = abs((s.date - e.date).days)
            tpts, tmsg = text_score(s, e)
            strong_text = tpts >= 10 and days <= 3
            if rpts < 20 and not strong_text:
                continue
            sc = 20 + max(rpts, 0) + date_score(days, cfg.date_window) + tpts
            sc = min(sc, 70)
            reasons = ["مطابقة جزئية: المبلغان مختلفان"] + [m for m in (rmsg, tmsg) if m]
            big, small = max(s.open, e.open), min(s.open, e.open)
            pct = (big - small) * 100 / big
            if pct <= 3:
                reasons.append(f"الفرق صغير ({pct:.1f}%) — قد يكون خصمًا أو عمولة، حدّد السبب")
            partials.append((sc, s, e, reasons))
    partials.sort(key=lambda x: -x[0])
    for sc, s, e, reasons in partials:
        if sc < cfg.min_score or not (free(s) and free(e)):
            continue
        amt = min(s.open, e.open)
        p = Proposal("one_to_one", [(s.id, "system", amt), (e.id, "exchanger", amt)], sc, reasons,
                     is_partial=True)
        take(p)
    return out


def allocate(sys_items: list[Item], ex_items: list[Item]) -> tuple[list[tuple[int, str, int]], int, bool]:
    """تخصيص مطابقة يدوية: الطرف الأصغر بالكامل، والأكبر بالترتيب الزمني حتى ينفد المبلغ."""
    ts, te = sum(i.open for i in sys_items), sum(i.open for i in ex_items)
    matched = min(ts, te)
    allocs: list[tuple[int, str, int]] = []
    for group in (sys_items, ex_items):
        remaining = matched
        for i in sorted(group, key=lambda x: (x.date, x.id)):
            if remaining <= 0:
                break
            a = min(i.open, remaining)
            allocs.append((i.id, i.side, a))
            remaining -= a
    return allocs, matched, ts != te


def kind_for(n_sys: int, n_ex: int) -> str:
    if n_sys == 1 and n_ex == 1:
        return "one_to_one"
    if n_sys == 1:
        return "one_to_many"
    if n_ex == 1:
        return "many_to_one"
    return "many_to_many"
