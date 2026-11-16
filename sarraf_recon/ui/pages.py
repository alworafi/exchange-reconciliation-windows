"""صفحات البرنامج."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QSpinBox, QSplitter, QTextBrowser,
                               QVBoxLayout, QWidget)

from .. import reconciliation as rc
from .. import services
from ..db import DIFF_REASONS, Tx, get_setting, set_setting
from ..money import ZERO, fmt, from_minor, to_decimal
from ..report import build_report
from ..services import KIND_LABEL, SIDE_LABEL, STATUS_LABEL, ReconError
from .import_dialog import MappingDialog
from .widgets import Col, FilterBar, Table, ask_text, button, confirm, date_edit, error, info, pydate


def dir_label(d: str) -> str:
    return "مدين (عليه)" if d == "D" else "دائن (له)"


class Page(QWidget):
    title = ""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.conn = win.conn
        self.lay = QVBoxLayout(self)
        h = QLabel(self.title)
        h.setObjectName("pageTitle")
        self.lay.addWidget(h)

    def refresh(self):
        pass

    def guard(self, fn, *a, ok_msg: str | None = None):
        try:
            r = fn(*a)
        except ReconError as exc:
            error(self, str(exc))
            return None
        except sqlite3.IntegrityError as exc:
            error(self, f"رفضت قاعدة البيانات العملية: {exc}")
            return None
        if ok_msg:
            self.win.status(ok_msg)
        self.win.data_changed()
        return r if r is not None else True


def period_box(page: Page):
    """تاريخا الفترة المشتركان بين اللوحة والتقرير."""
    lo, hi = rc.date_bounds(page.conn)
    today = date.today()
    d_from = date_edit(date(today.year, today.month, 1) if not hi else date.fromisoformat(hi).replace(day=1))
    d_to = date_edit(today if not hi else date.fromisoformat(hi))
    return d_from, d_to


# ====================================================================== اللوحة الرئيسية

class DashboardPage(Page):
    title = "اللوحة الرئيسية — الأرصدة والفروقات لكل حساب وعملة"

    def __init__(self, win):
        super().__init__(win)
        bar = QHBoxLayout()
        self.d_from, self.d_to = period_box(self)
        bar.addWidget(QLabel("الفترة من"))
        bar.addWidget(self.d_from)
        bar.addWidget(QLabel("إلى"))
        bar.addWidget(self.d_to)
        bar.addWidget(button("تحديث", self.refresh, primary=True))
        bar.addStretch(1)
        note = QLabel("الأرصدة من منظور النظام: موجب = مدين (الصراف عليه)، سالب = دائن (له). الفرق = النظام − الصراف.")
        note.setObjectName("hint")
        bar.addWidget(note)
        self.lay.addLayout(bar)
        self.table = Table([
            Col("الحساب", "account", width=150), Col("العملة", "currency", width=60),
            Col("رصيد النظام الختامي", "sys_close", money=True, width=140, color_sign=True),
            Col("رصيد الصراف الختامي", "ex_close", money=True, width=140, color_sign=True),
            Col("فرق الافتتاحي", "d_open", money=True, width=120, color_sign=True),
            Col("فرق الحركات", "d_mov", money=True, width=120, color_sign=True),
            Col("فرق الختامي", "d_close", money=True, width=130, color_sign=True),
            Col("مفسَّر", "explained", money=True, width=120, color_sign=True),
            Col("غير مفسَّر", "unexplained", money=True, width=120, color_sign=True),
            Col("مفتوح (نظام/صراف)", "open_cnt", width=120), Col("مقترحات", "proposed", width=70),
            Col("التحقق", "check")])
        self.table.selectionModel().selectionChanged.connect(self.show_detail)
        self.detail = QTextBrowser()
        sp = QSplitter(Qt.Vertical)
        sp.addWidget(self.table)
        sp.addWidget(self.detail)
        sp.setSizes([420, 260])
        self.lay.addWidget(sp, 1)
        self.recs = {}

    def refresh(self):
        df, dt = pydate(self.d_from.date()), pydate(self.d_to.date())
        rows = []
        self.recs = {}
        proposed = {}
        for r in self.conn.execute(
                "SELECT account_id, currency, COUNT(*) n FROM matches WHERE status='proposed' GROUP BY 1,2"):
            proposed[(r["account_id"], r["currency"])] = r["n"]
        for a in services.accounts(self.conn):
            for cur in rc.account_currencies(self.conn, a["id"]):
                rec = rc.compute(self.conn, a["id"], cur, df, dt)
                key = (a["id"], cur)
                self.recs[key] = rec
                n_s = len([i for i in rec.open_items if i.side == "system"])
                n_e = len(rec.open_items) - n_s
                checks_bad = [b for b in rec.balance_checks if b["diff"] != 0]
                rows.append({
                    "key": key, "account": a["name"], "account_id": a["id"], "currency": cur,
                    "sys_close": rec.system.closing, "ex_close": rec.exchanger.closing,
                    "d_open": rec.opening_diff, "d_mov": rec.movement_diff, "d_close": rec.closing_diff,
                    "explained": rec.explained, "unexplained": rec.unexplained,
                    "open_cnt": f"{n_s} / {n_e}", "proposed": proposed.get(key, 0),
                    "check": ("✔ المعادلات صحيحة" if rec.identity_ok else "✖ خطأ في المعادلة") +
                             (f" — {len(checks_bad)} رصيد كشف لا يطابق الحركات" if checks_bad else "")})
        self.table.set_rows(rows)
        self.detail.setPlainText("اختر صفًا لعرض التفاصيل." if rows else
                                 "لا توجد بيانات بعد. ابدأ بإضافة حساب صراف ثم استيراد كشف النظام وكشف الصراف.")

    def show_detail(self, *_):
        row = self.table.current()
        if not row:
            return
        rec = self.recs[row["key"]]
        t = rec.category_totals()
        sp = rec.explained_split()

        def num(v):
            color = "#1b6e3a" if v > 0 else "#b3261e" if v < 0 else "#1d2a2e"
            return f'<td style="color:{color}" align="left">{fmt(v)}</td>'

        ok = lambda b: '<b style="color:#1b6e3a">✔ صحيح</b>' if b else '<b style="color:#b3261e">✖ خطأ</b>'  # noqa: E731
        h = [f"<h3>{rec.account_name} — {rec.currency} — من {rec.date_from} إلى {rec.date_to}</h3>",
             '<table cellspacing="0" cellpadding="5" border="1" style="border-collapse:collapse">',
             "<tr><th></th><th>النظام</th><th>الصراف (بمنظور النظام)</th><th>الفرق (النظام − الصراف)</th></tr>"]
        for label, a, b, d in (("الافتتاحي", rec.system.opening, rec.exchanger.opening, rec.opening_diff),
                               ("حركات الفترة (صافي)", rec.system.movement, rec.exchanger.movement, rec.movement_diff),
                               ("الختامي", rec.system.closing, rec.exchanger.closing, rec.closing_diff)):
            h.append(f"<tr><th>{label}</th>{num(a)}{num(b)}{num(d)}</tr>")
        h.append("</table><p>")
        h.append(f"فرق الافتتاحي + فرق الحركات = {fmt(rec.opening_diff + rec.movement_diff)} "
                 f"= فرق الختامي: {ok(rec.identity_ok)}<br>")
        h.append(f"المفسَّر بحركات مفتوحة: <b>{fmt(rec.explained)}</b> (قبل الفترة {fmt(sp['before_period'])}، "
                 f"خلال الفترة {fmt(sp['in_period'])})<br>")
        h.append(f"غير المفسَّر: <b>{fmt(rec.unexplained)}</b> — منه فرق الأرصدة الأساسية المستوردة "
                 f"{fmt(rec.base_diff)}</p>")
        h.append('<table cellspacing="0" cellpadding="5" border="1" style="border-collapse:collapse">'
                 "<tr><th>تصنيف المتبقي المفتوح</th><th>العدد</th><th>عليه (مدين)</th><th>له (دائن)</th></tr>")
        for k, label in rc.CATEGORY_LABEL.items():
            h.append(f"<tr><td>{label}</td><td>{t[k]['count']}</td>{num(t[k]['D'])}{num(-t[k]['C'])}</tr>")
        h.append("</table>")
        h.append("<h4>لماذا يختلف الرصيد؟</h4><p>")
        h.append(f"فرق الافتتاحي {fmt(rec.opening_diff)}: تفسّره {len(rec.opening_open_items)} حركة كانت مفتوحة في "
                 f"بداية الفترة بمبلغ {fmt(rec.opening_explained)}، والباقي غير المفسَّر {fmt(rec.opening_unexplained)}.<br>")
        settled = rec.settled_during_period()
        h.append(f"خلال الفترة طوبقت {len(settled)} حركة قديمة من هذه الحركات، والمتبقي المفتوح في نهاية الفترة "
                 f"{len(rec.open_items)} حركة بصافي {fmt(rec.explained)}.</p>")
        rt = rec.reason_totals()
        if rt:
            h.append('<table cellspacing="0" cellpadding="5" border="1" style="border-collapse:collapse">'
                     "<tr><th>سبب الفرق</th><th>الأثر على الفرق</th></tr>")
            for k, v in sorted(rt.items(), key=lambda x: -abs(x[1])):
                h.append(f"<tr><td>{k}</td>{num(v)}</tr>")
            h.append("</table>")
        bad = [b for b in rec.balance_checks if b["diff"] != 0]
        if bad:
            h.append("<p><b>أرصدة في الكشوفات لا تطابق الحركات المستوردة (قد تكون هناك صفوف ناقصة):</b><br>")
            for b in bad:
                h.append(f"{SIDE_LABEL[b['side']]} {b['as_of']}: في الكشف {fmt(b['reported'])} والمحسوب "
                         f"{fmt(b['computed'])} (فرق {fmt(b['diff'])})<br>")
            h.append("</p>")
        for side, sb in (("system", rec.system), ("exchanger", rec.exchanger)):
            if sb.excluded_before_base:
                h.append(f"<p>تنبيه: {sb.excluded_before_base} حركة في {SIDE_LABEL[side]} أقدم من الرصيد "
                         f"الأساسي ({sb.base_as_of}) ولا تدخل في الرصيد.</p>")
        self.detail.setHtml("".join(h))


class ReasonDialog(QDialog):
    """تعليل الفرق المفتوح: لا يغيّر الأرصدة ولا يُنشئ قيدًا، فقط يوضح سبب الفرق في التقارير."""

    def __init__(self, parent, title="تعليل الفرق", allow_none=True):
        super().__init__(parent)
        self.setWindowTitle(title)
        f = QFormLayout(self)
        self.reason = QComboBox()
        if allow_none:
            self.reason.addItem("— بدون تعليل —", None)
        for k, v in DIFF_REASONS.items():
            self.reason.addItem(v, k)
        self.note = QLineEdit()
        self.note.setPlaceholderText("مثال: خصم 2% حسب الاتفاق، أو رقم سند العمولة")
        f.addRow("سبب الفرق", self.reason)
        f.addRow("توضيح", self.note)
        lbl = QLabel("التعليل لا يصفّر الفرق. يبقى المبلغ مفتوحًا حتى يُسجَّل القيد الصحيح في النظام، ويظهر في "
                     "التقرير مصنفًا بسببه.")
        lbl.setWordWrap(True)
        lbl.setObjectName("hint")
        f.addRow(lbl)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)


class LinkDialog(QDialog):
    """ربط اسمين: شخص يحوّل عن صاحب الفاتورة (أخ، قريب، وكيل)."""

    def __init__(self, parent, conn, name_a="", name_b="", account_id=None):
        super().__init__(parent)
        self.setWindowTitle("ربط اسمين")
        self.resize(520, 0)
        f = QFormLayout(self)
        self.a = QLineEdit(name_a)
        self.b = QLineEdit(name_b)
        self.scope = QComboBox()
        if account_id:
            self.scope.addItem("لهذا الصراف فقط", account_id)
        self.scope.addItem("لكل الصرافين", None)
        if not account_id:
            for acc in services.accounts(conn):
                self.scope.addItem(f"للصراف: {acc['name']}", acc["id"])
        self.note = QLineEdit()
        self.note.setPlaceholderText("مثال: أخوه / وكيله / ابن عمه")
        f.addRow("صاحب الفاتورة (كما في النظام)", self.a)
        f.addRow("المحوِّل (كما في كشف الصراف)", self.b)
        f.addRow("النطاق", self.scope)
        f.addRow("العلاقة", self.note)
        lbl = QLabel("اكتب الاسم كما يظهر في البيان أو خانة الاسم، ويُفضّل اسمين أو ثلاثة لتجنّب الخلط. بعد الربط "
                     "يعامل محرك المطابقة الاسمين كطرف واحد ويذكر ذلك في أسباب المقترح، لكنه لا يعتمد تلقائيًا "
                     "بسبب الاسم وحده.")
        lbl.setWordWrap(True)
        lbl.setObjectName("hint")
        f.addRow(lbl)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)


# ====================================================================== الحسابات

class AccountDialog(QDialog):
    def __init__(self, parent, row=None):
        super().__init__(parent)
        self.setWindowTitle("حساب صراف")
        f = QFormLayout(self)
        self.name = QLineEdit(row["name"] if row else "")
        self.code = QLineEdit(row["code"] if row else "")
        self.mirror = QCheckBox("كشف الصراف من منظوره هو (مدين عنده = دائن عندنا)")
        self.mirror.setChecked(bool(row["mirror"]) if row else True)
        self.notes = QLineEdit(row["notes"] if row else "")
        f.addRow("اسم الصراف", self.name)
        f.addRow("رقم الحساب في النظام", self.code)
        f.addRow("", self.mirror)
        f.addRow("ملاحظات", self.notes)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)


class OpeningDialog(QDialog):
    def __init__(self, parent, conn):
        super().__init__(parent)
        self.setWindowTitle("رصيد افتتاحي يدوي")
        f = QFormLayout(self)
        self.side = QComboBox()
        self.side.addItem("النظام", "system")
        self.side.addItem("الصراف", "exchanger")
        self.currency = QComboBox()
        for c in services.currencies(conn):
            self.currency.addItem(c["name"], c["code"])
        self.as_of = date_edit()
        self.amount = QLineEdit()
        self.dir = QComboBox()
        self.dir.addItem("مدين", 1)
        self.dir.addItem("دائن", -1)
        self.note = QLineEdit()
        f.addRow("الكشف", self.side)
        f.addRow("العملة", self.currency)
        f.addRow("الرصيد قبل حركات يوم", self.as_of)
        f.addRow("المبلغ", self.amount)
        f.addRow("طبيعة الرصيد كما في الكشف", self.dir)
        f.addRow("ملاحظة", self.note)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)


class AccountsPage(Page):
    title = "حسابات الصرافين"

    def __init__(self, win):
        super().__init__(win)
        bar = QHBoxLayout()
        bar.addWidget(button("إضافة صراف", self.add, primary=True))
        bar.addWidget(button("تعديل", self.edit))
        bar.addWidget(button("رصيد افتتاحي يدوي", self.opening))
        bar.addStretch(1)
        self.lay.addLayout(bar)
        self.table = Table([Col("رقم", "id", width=50), Col("الاسم", "name", width=200),
                            Col("رقم الحساب", "code", width=120), Col("اتجاه كشف الصراف", "mirror_t", width=260),
                            Col("ملاحظات", "notes")])
        self.lay.addWidget(self.table, 1)
        self.lay.addWidget(QLabel("الأرصدة المسجلة (من الكشوفات أو يدوية):"))
        self.bal = Table([Col("الحساب", "account", width=150), Col("الكشف", "side", width=70),
                          Col("العملة", "currency", width=60), Col("النوع", "kind", width=80),
                          Col("التاريخ", "as_of", width=100), Col("الرصيد (مدين − دائن)", "balance", money=True,
                                                                  width=150, color_sign=True),
                          Col("المصدر", "source", width=90), Col("ملاحظة", "note")])
        self.lay.addWidget(self.bal, 1)
        lb = QHBoxLayout()
        lb.addWidget(QLabel("الأسماء المرتبطة (شخص يحوّل عن صاحب الفاتورة):"))
        lb.addStretch(1)
        lb.addWidget(button("ربط اسمين...", self.add_link))
        lb.addWidget(button("حذف الربط", self.del_link))
        self.lay.addLayout(lb)
        self.links = Table([Col("صاحب الفاتورة", "name_a", width=200), Col("المحوِّل", "name_b", width=200),
                            Col("النطاق", "scope", width=150), Col("العلاقة", "note", width=150),
                            Col("أضيف بواسطة", "created_by", width=100), Col("التاريخ", "created_at")])
        self.lay.addWidget(self.links, 1)

    def add_link(self):
        d = LinkDialog(self, self.conn)
        if d.exec():
            self.guard(services.add_party_link, self.conn, d.a.text(), d.b.text(), d.scope.currentData(),
                       d.note.text(), ok_msg="حُفظ الربط")

    def del_link(self):
        r = self.links.current()
        if r and confirm(self, f"حذف الربط بين {r['name_a']} و{r['name_b']}؟"):
            self.guard(services.delete_party_link, self.conn, r["id"], ok_msg="حُذف الربط")

    def refresh(self):
        self.links.set_rows([{**dict(l), "scope": l["account_name"] or "كل الصرافين"}
                             for l in services.party_links(self.conn)])
        self.table.set_rows([{**dict(a), "mirror_t": "عكسي (من منظور الصراف)" if a["mirror"] else "نفس اتجاه النظام"}
                             for a in services.accounts(self.conn)])
        src = {"statement": "من الكشف", "derived": "مستنتج", "manual": "يدوي"}
        self.bal.set_rows([{
            "account": r["name"], "side": SIDE_LABEL[r["side"]], "currency": r["currency"],
            "kind": "افتتاحي" if r["kind"] == "opening" else "ختامي", "as_of": r["as_of"],
            "balance": from_minor(r["balance"]), "source": src.get(r["source"], r["source"]), "note": r["note"]}
            for r in self.conn.execute("SELECT b.*, a.name FROM statement_balances b JOIN accounts a"
                                       " ON a.id=b.account_id ORDER BY a.name, b.currency, b.as_of")])

    def add(self):
        d = AccountDialog(self)
        if d.exec():
            self.guard(services.create_account, self.conn, d.name.text(), d.code.text(), d.mirror.isChecked(),
                       d.notes.text(), ok_msg="أُضيف الحساب")

    def edit(self):
        row = self.table.current()
        if not row:
            return error(self, "اختر حسابًا")
        d = AccountDialog(self, row)
        if d.exec():
            self.guard(services.update_account, self.conn, row["id"], d.name.text(), d.code.text(),
                       d.mirror.isChecked(), d.notes.text(), ok_msg="حُفظ التعديل")

    def opening(self):
        row = self.table.current()
        if not row:
            return error(self, "اختر حسابًا")
        d = OpeningDialog(self, self.conn)
        if d.exec():
            try:
                amt = to_decimal(d.amount.text()) or ZERO
            except Exception:  # noqa: BLE001
                return error(self, "المبلغ غير صحيح")
            self.guard(services.set_manual_opening, self.conn, row["id"], d.side.currentData(),
                       d.currency.currentData(), pydate(d.as_of.date()), amt * d.dir.currentData(), d.note.text(),
                       ok_msg="حُفظ الرصيد")


# ====================================================================== الاستيراد

class ImportPage(Page):
    title = "استيراد كشف (النظام: Excel — الصراف: Excel أو PDF)"

    def __init__(self, win):
        super().__init__(win)
        f = QHBoxLayout()
        self.account = QComboBox()
        self.side = QComboBox()
        self.side.addItem("كشف النظام المحاسبي", "system")
        self.side.addItem("كشف الصراف", "exchanger")
        self.auto_match = QCheckBox("تشغيل المطابقة بعد الاستيراد")
        self.auto_match.setChecked(True)
        f.addWidget(QLabel("الحساب"))
        f.addWidget(self.account)
        f.addWidget(QLabel("نوع الكشف"))
        f.addWidget(self.side)
        f.addWidget(self.auto_match)
        f.addWidget(button("اختيار ملف واستيراده...", self.pick, primary=True))
        f.addStretch(1)
        self.lay.addLayout(f)
        hint = QLabel("الحركات الموجودة مسبقًا لا تُضاف مرة ثانية عند استيراد كشف متداخل الفترة، والمطابقات المعتمدة "
                      "تبقى كما هي. يُحفظ نسخة من الملف الأصلي في مجلد البيانات.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        self.lay.addWidget(hint)
        self.table = Table([Col("رقم", "id", width=50), Col("الحساب", "account_name", width=140),
                            Col("الكشف", "side_t", width=70), Col("الملف", "file_name", width=220),
                            Col("من", "period_from", width=95), Col("إلى", "period_to", width=95),
                            Col("صفوف", "total_rows", width=60), Col("جديدة", "new_rows", width=60),
                            Col("موجودة سابقًا", "duplicate_rows", width=90), Col("بأثر رجعي", "retro_rows", width=80),
                            Col("وقت الاستيراد", "imported_at", width=150), Col("بواسطة", "imported_by", width=90),
                            Col("النسخة الأصلية", "stored_path")])
        self.lay.addWidget(self.table, 1)
        self.lay.addWidget(button("فتح مجلد الملفات الأصلية", self.open_originals))

    def refresh(self):
        cur = self.account.currentData()
        self.account.clear()
        for a in services.accounts(self.conn):
            self.account.addItem(a["name"], a["id"])
        self.account.setCurrentIndex(max(0, self.account.findData(cur)))
        self.table.set_rows([{**dict(b), "side_t": SIDE_LABEL[b["side"]]} for b in services.batches(self.conn)])

    def pick(self):
        acc = self.account.currentData()
        if not acc:
            return error(self, "أضف حساب صراف أولًا من صفحة الحسابات")
        path, _ = QFileDialog.getOpenFileName(self, "اختر الكشف", "",
                                              "كشوفات (*.xlsx *.xlsm *.xls *.csv *.pdf);;كل الملفات (*)")
        if not path:
            return
        side = self.side.currentData()
        dlg = MappingDialog(self.conn, path, side, self)
        if not dlg.exec():
            return
        try:
            res = services.import_statement(self.conn, acc, side, path, dlg.parsed, dlg.mapping, self.win.data_dir)
        except ReconError as exc:
            if "استُورد سابقًا" in str(exc) and confirm(self, f"{exc}\n\nهل تريد الاستيراد رغم ذلك؟ "
                                                            "(لن تتكرر الحركات الموجودة)"):
                res = self.guard(services.import_statement, self.conn, acc, side, path, dlg.parsed, dlg.mapping,
                                 self.win.data_dir, True)
                if not res:
                    return
            else:
                return error(self, str(exc))
        msg = (f"حركات جديدة: {res['new']}\nموجودة سابقًا (لم تُكرر): {res['already_imported']}\n"
               f"بأثر رجعي: {res['retroactive']}\nتكرار مشتبه للمراجعة: {res['suspected_duplicates']}\n"
               f"صفوف متخطاة: {res['skipped']}\nتنبيهات: {len(res['warnings'])}")
        if self.auto_match.isChecked():
            m = services.run_matching(self.conn, acc)
            msg += f"\n\nالمطابقة: اعتماد تلقائي {m.get('auto_approved', 0)}، مقترحات للمراجعة {m.get('proposed', 0)}"
        self.win.data_changed()
        info(self, msg, "نتيجة الاستيراد")

    def open_originals(self):
        p = Path(self.win.data_dir) / "originals"
        p.mkdir(parents=True, exist_ok=True)
        open_path(p)


def open_path(p: Path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
    except OSError:
        pass


# ====================================================================== المطابقات

def _match_dict(conn, m) -> dict:
    items = services.match_items(conn, m["id"])
    s = [i for i in items if i["side"] == "system"]
    e = [i for i in items if i["side"] == "exchanger"]
    return {
        **dict(m), "date": (items[0]["txn_date"] if items else ""), "amount": from_minor(m["matched_amount"]),
        "kind_t": KIND_LABEL.get(m["kind"], m["kind"]) + (" — جزئية" if m["is_partial"] else ""),
        "status_t": STATUS_LABEL[m["status"]], "method_t": "يدوي" if m["method"] == "manual" else "تلقائي",
        "reasons_t": "، ".join(json.loads(m["reasons"] or "[]")),
        "sys_t": " + ".join(f"{fmt(i['amount'], minor=True)}" for i in s),
        "ex_t": " + ".join(f"{fmt(i['amount'], minor=True)}" for i in e),
        "sys_desc": " | ".join(f"{i['txn_date']} {i['reference'] or ''} {i['description'] or ''}" for i in s),
        "ex_desc": " | ".join(f"{i['txn_date']} {i['reference'] or ''} {i['description'] or ''}" for i in e),
        "confidence": ("عالية" if m["score"] >= 85 else "متوسطة" if m["score"] >= 65 else "منخفضة") + f" ({m['score']})",
    }


ITEM_COLS = [Col("الجانب", "side_t", width=60), Col("التاريخ", "txn_date", width=95),
             Col("المرجع", "reference", width=110), Col("الاسم", "counterparty", width=140),
             Col("البيان", "description", width=300), Col("الاتجاه في الكشف", "dir_t", width=90),
             Col("مبلغ الحركة", "total_d", money=True, width=120), Col("المخصص في المطابقة", "amount_d", money=True)]


def _item_rows(conn, match_id):
    return [{**dict(i), "side_t": SIDE_LABEL[i["side"]], "dir_t": "مدين" if i["direction"] == "D" else "دائن",
             "total_d": from_minor(i["total"]), "amount_d": from_minor(i["amount"])}
            for i in services.match_items(conn, match_id)]


class MatchListPage(Page):
    status = "proposed"

    def __init__(self, win):
        super().__init__(win)
        self.filter = FilterBar(self.conn, self.apply, use_dates=True)
        self.lay.addWidget(self.filter)
        self.bar = QHBoxLayout()
        self.lay.addLayout(self.bar)
        self.table = Table([
            Col("رقم", "id", width=55), Col("الحساب", "account_name", width=120), Col("العملة", "currency", width=55),
            Col("التاريخ", "date", width=95), Col("النوع", "kind_t", width=190), Col("الثقة", "confidence", width=95),
            Col("المبلغ المطابق", "amount", money=True, width=120), Col("النظام", "sys_t", width=130),
            Col("الصراف", "ex_t", width=130), Col("الأسباب", "reasons_t", width=300),
            Col("الطريقة", "method_t", width=60), Col("القرار بواسطة", "decided_by", width=100),
            Col("ملاحظة", "decision_note")], multi=True)
        self.items = Table(ITEM_COLS)
        self.table.selectionModel().selectionChanged.connect(self.show_items)
        sp = QSplitter(Qt.Vertical)
        sp.addWidget(self.table)
        sp.addWidget(self.items)
        sp.setSizes([450, 220])
        self.lay.addWidget(sp, 1)
        self.all_rows = []

    def refresh(self):
        self.filter.reload_lists()
        self.all_rows = [_match_dict(self.conn, m) for m in services.match_rows(self.conn, self.status)]
        self.apply()

    def apply(self):
        self.table.set_rows(self.filter.apply(self.all_rows))
        self.items.set_rows([])

    def show_items(self, *_):
        r = self.table.current()
        self.items.set_rows(_item_rows(self.conn, r["id"]) if r else [])


class ProposedPage(MatchListPage):
    title = "المطابقات المقترحة — تحتاج قرارك"
    status = "proposed"

    def __init__(self, win):
        super().__init__(win)
        self.bar.addWidget(button("تشغيل المطابقة للحساب المحدد", self.run, primary=True))
        self.bar.addWidget(button("اعتماد المحدد", self.approve))
        self.bar.addWidget(button("رفض المحدد", self.reject))
        self.bar.addStretch(1)
        lbl = QLabel("لا يُعتمد تلقائيًا إلا ما فيه تطابق مرجع + مبلغ + تاريخ قريب، وبلا مرشح منافس.")
        lbl.setObjectName("hint")
        self.bar.addWidget(lbl)

    def run(self):
        acc = self.filter.account.currentData()
        targets = [acc] if acc else [a["id"] for a in services.accounts(self.conn)]
        total = {"auto_approved": 0, "proposed": 0}
        for a in targets:
            r = self.guard(services.run_matching, self.conn, a, self.filter.currency.currentData())
            if isinstance(r, dict):
                for k in total:
                    total[k] += r.get(k, 0)
        info(self, f"اعتماد تلقائي: {total['auto_approved']}\nمقترحات للمراجعة: {total['proposed']}")

    def approve(self):
        rows = self.table.selected()
        if not rows:
            return error(self, "اختر مطابقة أو أكثر")
        if any(r["is_partial"] for r in rows) and not confirm(
                self, "بين المحدد مطابقات جزئية. سيبقى الفرق مفتوحًا ولن يُسوّى. متابعة؟"):
            return
        done = 0
        for r in rows:
            names = services.match_name_warning(self.conn, r["id"])
            if names and not confirm(self, f"المطابقة {r['id']} بين اسمين مختلفين:\n\n" + "\n".join(names) +
                                     f"\n\nالمبلغ: {fmt(r['amount'])}\n\nهل أنت متأكد أن هذه الحوالة تخص هذه الفاتورة؟"):
                continue
            try:
                services.approve_match(self.conn, r["id"])
                done += 1
            except ReconError as exc:
                error(self, f"المطابقة {r['id']}: {exc}")
        self.win.data_changed()
        self.win.status(f"اعتُمدت {done} مطابقة")

    def reject(self):
        rows = self.table.selected()
        if not rows:
            return error(self, "اختر مطابقة أو أكثر")
        note = ask_text(self, "رفض", "سبب الرفض (اختياري):")
        if note is None:
            return
        for r in rows:
            self.guard(services.reject_match, self.conn, r["id"], note)
        self.win.status(f"رُفضت {len(rows)} مطابقة")


class ApprovedPage(MatchListPage):
    title = "المطابقات المعتمدة"
    status = "approved"

    def __init__(self, win):
        super().__init__(win)
        self.bar.addWidget(button("فك المطابقة المحددة", self.unmatch))
        self.bar.addStretch(1)

    def unmatch(self):
        r = self.table.current()
        if not r:
            return error(self, "اختر مطابقة")
        reason = ask_text(self, "فك المطابقة", "سبب فك المطابقة (مطلوب، يُحفظ في السجل):", required=True)
        if reason:
            self.guard(services.unmatch, self.conn, r["id"], reason, ok_msg="فُكّت المطابقة")


class HistoryMatchesPage(MatchListPage):
    title = "المطابقات المفكوكة والمرفوضة"
    status = None

    def refresh(self):
        self.filter.reload_lists()
        self.all_rows = [_match_dict(self.conn, m) for m in services.match_rows(self.conn)
                         if m["status"] in ("unmatched", "rejected")]
        self.apply()


# ====================================================================== الحركات المفتوحة والمطابقة اليدوية

TXN_COLS = [Col("رقم", "id", width=55), Col("التاريخ", "date", width=92), Col("المرجع", "reference", width=95),
            Col("المبلغ", "amount", money=True, width=110), Col("المتبقي", "open", money=True, width=110),
            Col("الأثر", "dir_eff", width=85), Col("الحالة", "status", width=80),
            Col("سبب الفرق", "reason_t", width=120), Col("البيان", "description", width=240),
            Col("الاسم", "counterparty", width=110), Col("الحساب", "account_name", width=110),
            Col("العملة", "currency", width=50), Col("في الكشف", "dir_own", width=60), Col("ملاحظة", "note")]


def txn_dict(r) -> dict:
    amount, matched = from_minor(r["amount"]), from_minor(r["matched"])
    eff = services.eff_dir(r["side"], r["direction"], bool(r["mirror"]))
    status = "غير مطابقة" if matched == 0 else ("جزئية" if matched < amount else "مطابقة")
    return {**dict(r), "date": r["txn_date"], "amount": amount, "open": amount - matched,
            "dir_own": "مدين" if r["direction"] == "D" else "دائن", "dir_eff": dir_label(eff), "eff": eff,
            "status": status, "side_t": SIDE_LABEL[r["side"]],
            "reason_t": (DIFF_REASONS.get(r["diff_reason"], "") + (f" — {r['diff_note']}" if r["diff_note"] else ""))
            if r["diff_reason"] else ""}


class OpenItemsPage(Page):
    side = "system"

    def __init__(self, win):
        super().__init__(win)
        self.filter = FilterBar(self.conn, self.apply, statuses=["غير مطابقة", "جزئية"])
        self.lay.addWidget(self.filter)
        bar = QHBoxLayout()
        bar.addWidget(button("ملاحظة على الحركة", self.note))
        bar.addWidget(button("تعليل الفرق (خصم/عمولة/...)", self.reason))
        bar.addWidget(button("مطابقة يدوية...", lambda: self.win.go(ManualMatchPage)))
        bar.addStretch(1)
        self.total = QLabel()
        bar.addWidget(self.total)
        self.lay.addLayout(bar)
        self.table = Table(TXN_COLS, multi=True)
        self.lay.addWidget(self.table, 1)
        self.all_rows = []

    def refresh(self):
        self.filter.reload_lists()
        self.all_rows = [txn_dict(r) for r in services.txn_rows(self.conn, side=self.side, open_only=True)]
        self.apply()

    def apply(self):
        rows = self.filter.apply(self.all_rows)
        self.table.set_rows(rows)
        d = sum((r["open"] for r in rows if r["eff"] == "D"), ZERO)
        c = sum((r["open"] for r in rows if r["eff"] == "C"), ZERO)
        self.total.setText(f"{len(rows)} حركة — متبقي عليه {fmt(d)} — متبقي له {fmt(c)}")

    def note(self):
        r = self.table.current()
        if not r:
            return error(self, "اختر حركة")
        text = ask_text(self, "ملاحظة", "ملاحظة المراجعة على الحركة:")
        if text is not None:
            self.guard(services.set_txn_note, self.conn, r["id"], text, ok_msg="حُفظت الملاحظة")


    def reason(self):
        rows = self.table.selected()
        if not rows:
            return error(self, "اختر حركة أو أكثر")
        d = ReasonDialog(self)
        if d.exec():
            for r in rows:
                self.guard(services.set_diff_reason, self.conn, r["id"], d.reason.currentData(), d.note.text())
            self.win.status("حُفظ التعليل")


class SystemOnlyPage(OpenItemsPage):
    title = "الموجود بالنظام فقط (لم يسجله الصراف / لم يحوّل) — المتبقي المفتوح"
    side = "system"


class ExchangerOnlyPage(OpenItemsPage):
    title = "الموجود بالصراف فقط (لم يُسجّل بالنظام) — المتبقي المفتوح"
    side = "exchanger"


class ManualMatchPage(Page):
    title = "مطابقة يدوية — اختر من الجانبين ثم اضغط مطابقة"

    def __init__(self, win):
        super().__init__(win)
        bar = QHBoxLayout()
        self.account = QComboBox()
        self.currency = QComboBox()
        self.search = QLineEdit()
        self.search.setPlaceholderText("بحث...")
        self.account.currentIndexChanged.connect(self.load)
        self.currency.currentIndexChanged.connect(self.load)
        self.search.textChanged.connect(self.load)
        self.allow_partial = QCheckBox("السماح بمطابقة جزئية (يبقى الفرق مفتوحًا)")
        self.allow_partial.setChecked(True)
        self.other_payer = QCheckBox("حوّلها شخص آخر عن صاحب الفاتورة")
        self.other_payer.setToolTip("بعد المطابقة يمكنك ربط الاسمين حتى يتعرف عليهما المحرك في المرات القادمة")
        for w in (QLabel("الحساب"), self.account, QLabel("العملة"), self.currency, self.search, self.allow_partial,
                  self.other_payer):
            bar.addWidget(w)
        bar.addWidget(button("مطابقة المحدد", self.match, primary=True))
        self.lay.addLayout(bar)
        cols = [Col("رقم", "id", width=50), Col("التاريخ", "date", width=90),
                Col("المتبقي", "open", money=True, width=110), Col("الأثر", "dir_eff", width=80),
                Col("المرجع", "reference", width=90), Col("المبلغ", "amount", money=True, width=105),
                Col("الاسم", "counterparty", width=110), Col("البيان", "description")]
        self.sys = Table(cols, multi=True)
        self.ex = Table(cols, multi=True)
        sp = QSplitter(Qt.Horizontal)
        for t, label in ((self.sys, "كشف النظام"), (self.ex, "كشف الصراف")):
            w = QWidget()
            v = QVBoxLayout(w)
            v.setContentsMargins(0, 0, 0, 0)
            v.addWidget(QLabel(label))
            v.addWidget(t)
            sp.addWidget(w)
            t.selectionModel().selectionChanged.connect(self.update_totals)
        self.lay.addWidget(sp, 1)
        self.totals = QLabel()
        self.totals.setObjectName("totals")
        self.lay.addWidget(self.totals)

    def refresh(self):
        acc, cur = self.account.currentData(), self.currency.currentData()
        for w in (self.account, self.currency):
            w.blockSignals(True)
            w.clear()
        for a in services.accounts(self.conn):
            self.account.addItem(a["name"], a["id"])
        for c in services.currencies(self.conn):
            self.currency.addItem(c["name"], c["code"])
        self.account.setCurrentIndex(max(0, self.account.findData(acc)))
        self.currency.setCurrentIndex(max(0, self.currency.findData(cur)))
        for w in (self.account, self.currency):
            w.blockSignals(False)
        self.load()

    def load(self):
        acc, cur = self.account.currentData(), self.currency.currentData()
        q = self.search.text().strip().lower()
        for side, table in (("system", self.sys), ("exchanger", self.ex)):
            rows = [txn_dict(r) for r in services.txn_rows(self.conn, acc or -1, cur, side, open_only=True)]
            if q:
                rows = [r for r in rows if q in f"{r['reference']} {r['description']} {r['counterparty']} "
                                               f"{r['amount']}".lower()]
            table.set_rows(rows)
        self.update_totals()

    def update_totals(self, *_):
        s, e = self.sys.selected(), self.ex.selected()
        ts, te = sum((r["open"] for r in s), ZERO), sum((r["open"] for r in e), ZERO)
        dirs = {r["eff"] for r in s + e}
        warn = "  ⚠ اتجاهات غير متوافقة" if len(dirs) > 1 else ""
        self.totals.setText(f"المحدد من النظام: {len(s)} = {fmt(ts)}    |    من الصراف: {len(e)} = {fmt(te)}"
                            f"    |    الفرق: {fmt(ts - te)}{warn}")

    def match(self):
        ids = [r["id"] for r in self.sys.selected() + self.ex.selected()]
        if not ids:
            return error(self, "اختر حركات من الجانبين")
        ts = sum((r["open"] for r in self.sys.selected()), ZERO)
        te = sum((r["open"] for r in self.ex.selected()), ZERO)
        reason = None
        if ts != te:
            if not self.allow_partial.isChecked():
                return error(self, f"المجموعان غير متساويين (الفرق {fmt(ts - te)}). فعّل خيار المطابقة الجزئية.")
            d = ReasonDialog(self, f"مطابقة جزئية — سيبقى فرق {fmt(abs(ts - te))} مفتوحًا")
            if not d.exec():
                return
            reason, note = d.reason.currentData(), d.note.text()
        else:
            note = ask_text(self, "مطابقة يدوية", "ملاحظة (اختياري) — تُحفظ في سجل المطابقات:")
            if note is None:
                return
        sel_s, sel_e = self.sys.selected(), self.ex.selected()
        names = services.name_differences(self.conn, ids)
        if names or self.other_payer.isChecked():
            msg = ("الاسم في الفاتورة يختلف عن اسم المحوِّل:\n\n" + "\n".join(names) + "\n\n") if names else ""
            if not confirm(self, msg + f"مجموع النظام {fmt(ts)} — مجموع الصراف {fmt(te)}\n\n"
                                       "هل أنت متأكد أن هذا المبلغ حُوّل عن صاحب الفاتورة وتريد مطابقته؟"):
                return
        if self.other_payer.isChecked():
            payer = sel_e[0]["counterparty"] or sel_e[0]["description"]
            owner = sel_s[0]["counterparty"] or sel_s[0]["description"]
            note = (note + " — " if note else "") + f"حوّلها شخص آخر عن صاحب الفاتورة: {payer}"
        r = self.guard(services.manual_match, self.conn, ids, note, self.allow_partial.isChecked(), reason,
                       ok_msg="تمت المطابقة اليدوية")
        if r and self.other_payer.isChecked() and confirm(
                self, "هل تريد ربط الاسمين حتى يقترح البرنامج هذه الحالة تلقائيًا في المرات القادمة؟"):
            d = LinkDialog(self, self.conn, owner, payer, self.account.currentData())
            if d.exec():
                self.guard(services.add_party_link, self.conn, d.a.text(), d.b.text(), d.scope.currentData(),
                           d.note.text(), ok_msg="حُفظ الربط")
            self.other_payer.setChecked(False)
        if r:
            self.load()


class RetroPage(OpenItemsPage):
    title = "الحركات بأثر رجعي — ظهرت في كشف لاحق بتاريخ سبق تغطيته"

    def __init__(self, win):
        super().__init__(win)
        self.table.model_.cols = [Col("الجانب", "side_t", width=60)] + TXN_COLS + [
            Col("وقت الإدخال", "created_at", width=150)]
        self.filter.status.clear()
        self.filter.status.addItem("كل الحالات", None)
        for s in ("غير مطابقة", "جزئية", "مطابقة"):
            self.filter.status.addItem(s, s)

    def refresh(self):
        self.filter.reload_lists()
        self.all_rows = [txn_dict(r) for r in services.txn_rows(self.conn, retro_only=True)]
        self.apply()


class DuplicatesPage(Page):
    title = "التكرارات المشتبه بها — قرّر: مكررة (تُستبعد) أو ليست مكررة"

    def __init__(self, win):
        super().__init__(win)
        bar = QHBoxLayout()
        bar.addWidget(button("تأكيد أنها مكررة (استبعاد)", self.dup, primary=True))
        bar.addWidget(button("ليست مكررة", self.not_dup))
        bar.addStretch(1)
        self.lay.addLayout(bar)
        self.table = Table([Col("الجانب", "side_t", width=60)] + TXN_COLS + [
            Col("مشتبه تكرارها مع", "dup_suspect_of", width=110)])
        self.lay.addWidget(self.table, 1)
        self.lay.addWidget(QLabel("الحركات المستبعدة كمكررة:"))
        self.done = Table([Col("الجانب", "side_t", width=60)] + TXN_COLS + [Col("مكررة من", "duplicate_of")])
        self.lay.addWidget(self.done, 1)

    def refresh(self):
        self.table.set_rows([txn_dict(r) for r in services.txn_rows(self.conn, suspects_only=True)])
        self.done.set_rows([txn_dict(r) for r in services.txn_rows(self.conn, statuses=("duplicate",))])

    def dup(self):
        r = self.table.current()
        if r and confirm(self, f"استبعاد الحركة {r['id']} كمكررة من {r['dup_suspect_of']}؟ لن تدخل في الأرصدة."):
            self.guard(services.mark_duplicate, self.conn, r["id"], r["dup_suspect_of"], "", ok_msg="استُبعدت")

    def not_dup(self):
        r = self.table.current()
        if r:
            self.guard(services.mark_not_duplicate, self.conn, r["id"], "", ok_msg="حُفظ القرار")


class AuditPage(Page):
    title = "سجل المطابقات والعمليات"

    def __init__(self, win):
        super().__init__(win)
        self.filter = FilterBar(self.conn, self.apply)
        self.lay.addWidget(self.filter)
        self.table = Table([Col("الوقت", "ts", width=150), Col("المستخدم", "user", width=100),
                            Col("العملية", "action", width=150), Col("الحساب", "account_name", width=120),
                            Col("العملة", "currency", width=60), Col("الرقم", "entity_id", width=60),
                            Col("التفاصيل", "details_t")])
        self.lay.addWidget(self.table, 1)
        self.all_rows = []

    def refresh(self):
        self.filter.reload_lists()
        rows = []
        for l in services.audit_rows(self.conn):
            d = json.loads(l["details"]) if l["details"] else {}
            rows.append({**dict(l), "date": l["ts"], "details_t": "، ".join(f"{k}: {v}" for k, v in d.items())})
        self.all_rows = rows
        self.apply()

    def apply(self):
        self.table.set_rows(self.filter.apply(self.all_rows))


# ====================================================================== التقارير والإعدادات

class ReportPage(Page):
    title = "تقرير Excel"

    def __init__(self, win):
        super().__init__(win)
        f = QFormLayout()
        self.account = QComboBox()
        self.d_from, self.d_to = period_box(self)
        f.addRow("الحساب", self.account)
        f.addRow("من", self.d_from)
        f.addRow("إلى", self.d_to)
        w = QWidget()
        w.setLayout(f)
        w.setMaximumWidth(480)
        self.lay.addWidget(w)
        self.lay.addWidget(button("إنشاء التقرير...", self.make, primary=True))
        txt = QLabel("يحتوي التقرير: الملخص لكل عملة (فرق الافتتاحي، فرق الحركات، فرق الختامي، المفسَّر وغير المفسَّر "
                     "مع التحقق من المعادلات)، الموجود بالنظام فقط، الموجود بالصراف فقط، متبقي المطابقات الجزئية، "
                     "المطابقات المعتمدة والجزئية والمركبة، المقترحة، المفكوكة، الحركات بأثر رجعي، التكرارات، "
                     "تحقق أرصدة الكشوفات، وسجل العمليات.")
        txt.setWordWrap(True)
        txt.setObjectName("hint")
        self.lay.addWidget(txt)
        self.lay.addStretch(1)

    def refresh(self):
        cur = self.account.currentData()
        self.account.clear()
        for a in services.accounts(self.conn):
            self.account.addItem(a["name"], a["id"])
        self.account.setCurrentIndex(max(0, self.account.findData(cur)))

    def make(self):
        acc = self.account.currentData()
        if not acc:
            return error(self, "اختر حسابًا")
        df, dt = pydate(self.d_from.date()), pydate(self.d_to.date())
        default = Path(self.win.data_dir) / "reports" / f"مطابقة_{self.account.currentText()}_{df}_{dt}.xlsx"
        default.parent.mkdir(parents=True, exist_ok=True)
        path, _ = QFileDialog.getSaveFileName(self, "حفظ التقرير", str(default), "Excel (*.xlsx)")
        if not path:
            return
        try:
            build_report(self.conn, acc, df, dt, path)
        except PermissionError:
            return error(self, "تعذر الحفظ. أغلق الملف إن كان مفتوحًا في Excel ثم أعد المحاولة.")
        self.win.status("أُنشئ التقرير")
        if confirm(self, f"أُنشئ التقرير:\n{path}\n\nفتحه الآن؟"):
            open_path(Path(path))


class SettingsPage(Page):
    title = "الإعدادات"

    def __init__(self, win):
        super().__init__(win)
        g = QGroupBox("المطابقة")
        f = QFormLayout(g)
        self.user = QLineEdit()
        self.window = QSpinBox()
        self.window.setRange(0, 365)
        self.auto = QCheckBox("اعتماد المطابقات عالية الثقة تلقائيًا")
        self.auto_min = QSpinBox()
        self.auto_min.setRange(70, 100)
        self.min_score = QSpinBox()
        self.min_score.setRange(20, 90)
        self.group = QSpinBox()
        self.group.setRange(2, 6)
        f.addRow("اسم المستخدم (يظهر في السجل)", self.user)
        f.addRow("نافذة فرق التاريخ (أيام)", self.window)
        f.addRow("", self.auto)
        f.addRow("أقل درجة للاعتماد التلقائي", self.auto_min)
        f.addRow("أقل درجة لعرض المقترح", self.min_score)
        f.addRow("أقصى عدد حركات في المطابقة المركبة", self.group)
        self.lay.addWidget(g)
        self.lay.addWidget(button("حفظ الإعدادات", self.save, primary=True))
        g2 = QGroupBox("البيانات")
        v = QVBoxLayout(g2)
        self.path_lbl = QLabel()
        self.path_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(self.path_lbl)
        v.addWidget(button("نسخة احتياطية من قاعدة البيانات...", self.backup))
        v.addWidget(button("فتح مجلد البيانات", lambda: open_path(Path(self.win.data_dir))))
        self.lay.addWidget(g2)
        self.lay.addStretch(1)

    def refresh(self):
        g = lambda k: get_setting(self.conn, k)  # noqa: E731
        self.user.setText(g("user_name"))
        self.window.setValue(int(g("date_window_days")))
        self.auto.setChecked(g("auto_approve") == "1")
        self.auto_min.setValue(int(g("auto_approve_min_score")))
        self.min_score.setValue(int(g("min_proposal_score")))
        self.group.setValue(int(g("max_group_size")))
        self.path_lbl.setText(f"مجلد البيانات: {self.win.data_dir}")

    def save(self):
        vals = {"user_name": self.user.text().strip() or "المحاسب", "date_window_days": self.window.value(),
                "auto_approve": "1" if self.auto.isChecked() else "0", "auto_approve_min_score": self.auto_min.value(),
                "min_proposal_score": self.min_score.value(), "max_group_size": self.group.value()}
        with Tx(self.conn):
            for k, v in vals.items():
                set_setting(self.conn, k, str(v))
            services.audit(self.conn, "تعديل الإعدادات", **vals)
        self.win.status("حُفظت الإعدادات")

    def backup(self):
        default = Path(self.win.data_dir) / "backups" / f"sarraf_recon_{date.today()}.db"
        default.parent.mkdir(parents=True, exist_ok=True)
        path, _ = QFileDialog.getSaveFileName(self, "نسخة احتياطية", str(default), "SQLite (*.db)")
        if not path:
            return
        dest = sqlite3.connect(path)
        with dest:
            self.conn.backup(dest)
        dest.close()
        info(self, f"حُفظت النسخة الاحتياطية:\n{path}")
