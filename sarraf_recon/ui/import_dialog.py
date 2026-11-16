"""نافذة تحديد أعمدة الكشف ومعاينة القراءة قبل الحفظ."""
from __future__ import annotations

import json

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGridLayout,
                               QGroupBox, QHBoxLayout, QInputDialog, QLabel, QPlainTextEdit, QSpinBox,
                               QTabWidget, QVBoxLayout, QWidget)
from openpyxl.utils import get_column_letter

from .. import services
from ..db import Tx, now_iso
from ..importers import FIELDS, Mapping, detect_mapping, list_sheets, parse_table, read_file
from ..importers.base import cell_text
from .widgets import Col, Table, button, error

KIND_AR = {"txn": "حركة", "opening": "رصيد افتتاحي", "closing": "رصيد ختامي", "skip": "تخطي"}


class MappingDialog(QDialog):
    def __init__(self, conn, path: str, side: str, parent=None):
        super().__init__(parent)
        self.conn, self.path, self.side = conn, path, side
        self.setWindowTitle(f"قراءة الكشف: {path}")
        self.resize(1200, 760)
        self.table = None
        self.mapping: Mapping | None = None
        self.parsed = None

        root = QVBoxLayout(self)
        top = QHBoxLayout()
        self.sheet = QComboBox()
        try:
            for s in list_sheets(path):
                self.sheet.addItem(s or "(الملف)", s or None)
        except Exception as exc:  # noqa: BLE001
            error(self, f"تعذر فتح الملف: {exc}")
        self.fix_rtl = QCheckBox("تصحيح العربية المعكوسة (PDF)")
        self.fix_rtl.setVisible(path.lower().endswith(".pdf"))
        self.profile = QComboBox()
        self._load_profiles()
        top.addWidget(QLabel("الورقة"))
        top.addWidget(self.sheet)
        top.addWidget(self.fix_rtl)
        top.addStretch(1)
        top.addWidget(QLabel("قالب محفوظ"))
        top.addWidget(self.profile)
        top.addWidget(button("تطبيق القالب", self.apply_profile))
        top.addWidget(button("حفظ التحديد كقالب", self.save_profile))
        root.addLayout(top)

        box = QGroupBox("تحديد الأعمدة (اقتراح تلقائي — راجعه وعدّله عند الحاجة)")
        grid = QGridLayout(box)
        self.combos: dict[str, QComboBox] = {}
        for i, (key, label) in enumerate(FIELDS):
            cb = QComboBox()
            self.combos[key] = cb
            grid.addWidget(QLabel(label), i // 5 * 2, i % 5)
            grid.addWidget(cb, i // 5 * 2 + 1, i % 5)
        form = QFormLayout()
        self.header_row = QSpinBox()
        self.header_row.setRange(0, 500)
        self.header_row.setSpecialValueText("بدون صف عناوين")
        self.currency = QComboBox()
        for c in services.currencies(conn):
            self.currency.addItem(f"{c['name']} ({c['code']})", c["code"])
        self.date_order = QComboBox()
        self.date_order.addItem("يوم/شهر/سنة", "DMY")
        self.date_order.addItem("شهر/يوم/سنة", "MDY")
        self.balance_sign = QComboBox()
        self.balance_sign.addItem("اكتشاف تلقائي", 0)
        self.balance_sign.addItem("الرصيد = مدين − دائن", 1)
        self.balance_sign.addItem("الرصيد = دائن − مدين", -1)
        self.neg_is = QComboBox()
        self.neg_is.addItem("المبلغ السالب = دائن", "C")
        self.neg_is.addItem("المبلغ السالب = مدين", "D")
        form.addRow("صف العناوين", self.header_row)
        form.addRow("العملة الافتراضية (إذا لم يوجد عمود عملة)", self.currency)
        form.addRow("ترتيب التاريخ", self.date_order)
        form.addRow("عمود الرصيد", self.balance_sign)
        form.addRow("عمود المبلغ الواحد", self.neg_is)
        hl = QHBoxLayout()
        hl.addWidget(box, 3)
        fw = QWidget()
        fw.setLayout(form)
        hl.addWidget(fw, 2)
        root.addLayout(hl)

        tabs = QTabWidget()
        self.raw_view = Table([Col("#", "_n", width=45)])
        self.parsed_view = Table([
            Col("صف", "row", width=45), Col("النوع", "kind", width=90), Col("التاريخ", "date", width=95),
            Col("العملة", "currency", width=55), Col("المرجع", "ref", width=110), Col("الاسم", "party", width=140),
            Col("البيان", "desc", width=260), Col("مدين", "debit", money=True, width=110),
            Col("دائن", "credit", money=True, width=110), Col("الرصيد", "balance", money=True, width=120),
            Col("ملاحظة", "reason")])
        self.warn = QPlainTextEdit()
        self.warn.setReadOnly(True)
        tabs.addTab(self.raw_view, "الملف كما هو")
        tabs.addTab(self.parsed_view, "نتيجة القراءة")
        tabs.addTab(self.warn, "تنبيهات")
        self.tabs = tabs
        root.addWidget(tabs, 1)

        bottom = QHBoxLayout()
        self.summary = QLabel()
        bottom.addWidget(self.summary, 1)
        bottom.addWidget(button("فحص القراءة", self.preview))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("استيراد")
        bb.button(QDialogButtonBox.Cancel).setText("إلغاء")
        bb.accepted.connect(self.accept_import)
        bb.rejected.connect(self.reject)
        bottom.addWidget(bb)
        root.addLayout(bottom)

        self.sheet.currentIndexChanged.connect(self.load)
        self.fix_rtl.toggled.connect(self.load)
        self.load()

    # ---------------------------------------------------------- loading
    def load(self):
        try:
            self.table = read_file(self.path, self.sheet.currentData(), self.fix_rtl.isChecked())
        except Exception as exc:  # noqa: BLE001
            error(self, f"تعذر قراءة الملف: {exc}")
            return
        width = self.table.width
        cols = [Col("#", "_n", width=45)] + [Col(get_column_letter(j + 1), str(j), width=110) for j in range(width)]
        self.raw_view.model_.cols = cols
        self.raw_view.set_rows([{"_n": i + 1, **{str(j): cell_text(v) for j, v in enumerate(r)}}
                                for i, r in enumerate(self.table.rows[:400])])
        header_guess = detect_mapping(self.table, self.currency.currentData() or "YER")
        for key, cb in self.combos.items():
            cb.blockSignals(True)
            cb.clear()
            cb.addItem("— غير موجود —", None)
            hdr = self.table.rows[header_guess.header_row] if header_guess.header_row is not None else []
            for j in range(width):
                name = cell_text(hdr[j]) if j < len(hdr) else ""
                cb.addItem(f"{get_column_letter(j + 1)}: {name}" if name else get_column_letter(j + 1), j)
            cb.blockSignals(False)
        self.set_mapping(header_guess)
        self.preview()

    def set_mapping(self, m: Mapping):
        self.header_row.setValue(0 if m.header_row is None else m.header_row + 1)
        for key, cb in self.combos.items():
            cb.setCurrentIndex(max(0, cb.findData(m.columns.get(key))))
        self.currency.setCurrentIndex(max(0, self.currency.findData(m.default_currency)))
        self.date_order.setCurrentIndex(max(0, self.date_order.findData(m.date_order)))
        self.balance_sign.setCurrentIndex(max(0, self.balance_sign.findData(m.balance_sign)))
        self.neg_is.setCurrentIndex(max(0, self.neg_is.findData(m.signed_amount_negative_is)))

    def current_mapping(self) -> Mapping:
        m = Mapping()
        hr = self.header_row.value()
        m.header_row = None if hr == 0 else hr - 1
        m.columns = {k: cb.currentData() for k, cb in self.combos.items()}
        m.default_currency = self.currency.currentData()
        m.date_order = self.date_order.currentData()
        m.balance_sign = self.balance_sign.currentData()
        m.signed_amount_negative_is = self.neg_is.currentData()
        return m

    def preview(self) -> bool:
        if self.table is None:
            return False
        try:
            self.mapping = self.current_mapping()
            self.parsed = parse_table(self.table, self.mapping)
        except ValueError as exc:
            self.summary.setText(f"⚠ {exc}")
            self.parsed = None
            return False
        rows = [{"row": r.source_row, "kind": KIND_AR[r.kind], "date": r.date.isoformat() if r.date else "",
                 "currency": r.currency, "ref": r.reference, "party": r.counterparty, "desc": r.description,
                 "debit": r.debit if r.kind == "txn" else None, "credit": r.credit if r.kind == "txn" else None,
                 "balance": r.balance, "reason": r.reason} for r in self.parsed.rows]
        self.parsed_view.set_rows(rows)
        tx = self.parsed.txns
        by_cur = {}
        for r in tx:
            d = by_cur.setdefault(r.currency, [0, 0, 0])
            d[0] += 1
            d[1] += r.debit
            d[2] += r.credit
        parts = [f"{c}: {n} حركة، مدين {dr:,.2f}، دائن {cr:,.2f}" for c, (n, dr, cr) in by_cur.items()]
        skipped = len([r for r in self.parsed.rows if r.kind == "skip"])
        self.summary.setText(" | ".join(parts) + f" | صفوف متخطاة: {skipped} | تنبيهات: {len(self.parsed.warnings)}")
        self.warn.setPlainText("\n".join(self.parsed.warnings) or "لا توجد تنبيهات")
        self.tabs.setCurrentIndex(1)
        return True

    def accept_import(self):
        if not self.preview() or not self.parsed.txns:
            error(self, "لم تُقرأ أي حركة. راجع تحديد الأعمدة وصف العناوين.")
            return
        self.accept()

    # ---------------------------------------------------------- profiles
    def _load_profiles(self):
        self.profile.clear()
        for p in self.conn.execute("SELECT * FROM import_profiles ORDER BY name"):
            self.profile.addItem(p["name"], p["mapping_json"])

    def apply_profile(self):
        js = self.profile.currentData()
        if js:
            self.set_mapping(Mapping.from_dict(json.loads(js)))
            self.preview()

    def save_profile(self):
        name, ok = QInputDialog.getText(self, "حفظ قالب", "اسم القالب (مثلًا: كشف صراف الأمانة PDF)")
        if not ok or not name.strip():
            return
        with Tx(self.conn):
            self.conn.execute(
                "INSERT INTO import_profiles(name, side, mapping_json, created_at) VALUES (?,?,?,?)"
                " ON CONFLICT(name) DO UPDATE SET mapping_json=excluded.mapping_json",
                (name.strip(), self.side, json.dumps(self.current_mapping().to_dict(), ensure_ascii=False), now_iso()))
            services.audit(self.conn, "حفظ قالب استيراد", name=name.strip())
        self._load_profiles()
