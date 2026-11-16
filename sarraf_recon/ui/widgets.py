"""عناصر واجهة مشتركة: نموذج جدول عام، شريط تصفية، ورسائل."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Callable

from PySide6.QtCore import QAbstractTableModel, QDate, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDateEdit, QHBoxLayout, QHeaderView,
                               QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QTableView,
                               QWidget, QCheckBox)

from ..money import fmt

POS = QColor("#1b6e3a")
NEG = QColor("#b3261e")


class Col:
    def __init__(self, title: str, key: str | Callable[[dict], Any], money: bool = False, width: int | None = None,
                 color_sign: bool = False):
        self.title, self.key, self.money, self.width, self.color_sign = title, key, money, width, color_sign

    def value(self, row: dict):
        return self.key(row) if callable(self.key) else row.get(self.key)


class RecordModel(QAbstractTableModel):
    def __init__(self, cols: list[Col], parent=None):
        super().__init__(parent)
        self.cols = cols
        self.rows: list[dict] = []

    def set_rows(self, rows: list[dict]):
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.cols)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.cols[section].title
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        col = self.cols[index.column()]
        v = col.value(self.rows[index.row()])
        if role == Qt.DisplayRole:
            if v is None:
                return ""
            if col.money or isinstance(v, Decimal):
                return fmt(v)
            return str(v)
        if role == Qt.UserRole:            # للفرز
            if isinstance(v, Decimal):
                return float(v)
            return v if v is not None else ""
        if role == Qt.TextAlignmentRole and (col.money or isinstance(v, (Decimal, int))):
            return int(Qt.AlignLeft | Qt.AlignVCenter)
        if role == Qt.ForegroundRole and col.color_sign and isinstance(v, Decimal) and v != 0:
            return POS if v > 0 else NEG
        return None

    def row(self, i: int) -> dict:
        return self.rows[i]


class Table(QTableView):
    """جدول قابل للفرز يعرض RecordModel."""

    def __init__(self, cols: list[Col], multi: bool = False, parent=None):
        super().__init__(parent)
        self.model_ = RecordModel(cols, self)
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model_)
        self.proxy.setSortRole(Qt.UserRole)
        self.setModel(self.proxy)
        self.setSortingEnabled(True)
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection if multi else QAbstractItemView.SingleSelection)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(26)
        h = self.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.Interactive)
        h.setStretchLastSection(True)
        for i, c in enumerate(cols):
            if c.width:
                self.setColumnWidth(i, c.width)

    def set_rows(self, rows: list[dict]):
        self.model_.set_rows(rows)

    def selected(self) -> list[dict]:
        idx = {self.proxy.mapToSource(i).row() for i in self.selectionModel().selectedRows()}
        return [self.model_.row(i) for i in sorted(idx)]

    def current(self) -> dict | None:
        s = self.selected()
        return s[0] if s else None


def qdate(d: date | str | None) -> QDate:
    if isinstance(d, str):
        d = date.fromisoformat(d)
    if d is None:
        return QDate.currentDate()
    return QDate(d.year, d.month, d.day)


def pydate(q: QDate) -> date:
    return date(q.year(), q.month(), q.day())


def date_edit(d=None) -> QDateEdit:
    w = QDateEdit(qdate(d))
    w.setCalendarPopup(True)
    w.setDisplayFormat("yyyy-MM-dd")
    return w


class FilterBar(QWidget):
    """تصفية موحدة: الحساب، العملة، التاريخ، الحالة، المبلغ، والبحث النصي.

    تعمل على صفوف dict تحتوي المفاتيح: account_id, currency, date, status, amount (Decimal).
    """

    def __init__(self, conn, on_change: Callable[[], None], statuses: list[str] | None = None,
                 use_dates: bool = True, parent=None):
        super().__init__(parent)
        self.conn = conn
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.account = QComboBox()
        self.currency = QComboBox()
        self.use_dates = QCheckBox("التاريخ من")
        self.use_dates.setVisible(use_dates)
        self.d_from = date_edit(date(date.today().year, 1, 1))
        self.d_to = date_edit(date.today())
        self.d_from.setVisible(use_dates)
        self.d_to.setVisible(use_dates)
        self.status = QComboBox()
        self.status.addItem("كل الحالات", None)
        for s in statuses or []:
            self.status.addItem(s, s)
        self.status.setVisible(bool(statuses))
        self.amin = QLineEdit()
        self.amin.setPlaceholderText("مبلغ من")
        self.amax = QLineEdit()
        self.amax.setPlaceholderText("إلى")
        self.amin.setMaximumWidth(90)
        self.amax.setMaximumWidth(90)
        self.text = QLineEdit()
        self.text.setPlaceholderText("بحث في المرجع/البيان/الاسم...")
        lay.addWidget(QLabel("الحساب"))
        lay.addWidget(self.account)
        lay.addWidget(QLabel("العملة"))
        lay.addWidget(self.currency)
        lay.addWidget(self.use_dates)
        lay.addWidget(self.d_from)
        to_lbl = QLabel("إلى")
        to_lbl.setVisible(use_dates)
        lay.addWidget(to_lbl)
        lay.addWidget(self.d_to)
        lay.addWidget(self.status)
        lay.addWidget(self.amin)
        lay.addWidget(self.amax)
        lay.addWidget(self.text, 1)
        self.reload_lists()
        for w in (self.account, self.currency, self.status):
            w.currentIndexChanged.connect(lambda *_: on_change())
        for w in (self.d_from, self.d_to):
            w.dateChanged.connect(lambda *_: on_change())
        self.use_dates.toggled.connect(lambda *_: on_change())
        for w in (self.amin, self.amax, self.text):
            w.textChanged.connect(lambda *_: on_change())

    def reload_lists(self):
        from .. import services
        cur_acc, cur_cur = self.account.currentData(), self.currency.currentData()
        for w in (self.account, self.currency):
            w.blockSignals(True)
            w.clear()
        self.account.addItem("كل الحسابات", None)
        for a in services.accounts(self.conn):
            self.account.addItem(a["name"], a["id"])
        self.currency.addItem("كل العملات", None)
        for c in services.currencies(self.conn):
            self.currency.addItem(f"{c['name']} ({c['code']})", c["code"])
        for w, v in ((self.account, cur_acc), (self.currency, cur_cur)):
            i = w.findData(v)
            w.setCurrentIndex(max(0, i))
            w.blockSignals(False)

    def apply(self, rows: list[dict]) -> list[dict]:
        from ..money import to_decimal, AmountError
        acc, cur, st = self.account.currentData(), self.currency.currentData(), self.status.currentData()
        try:
            amin = to_decimal(self.amin.text()) if self.amin.text().strip() else None
            amax = to_decimal(self.amax.text()) if self.amax.text().strip() else None
        except AmountError:
            amin = amax = None
        txt = self.text.text().strip().lower()
        df = pydate(self.d_from.date()).isoformat()
        dt = pydate(self.d_to.date()).isoformat()
        out = []
        for r in rows:
            if acc and r.get("account_id") != acc:
                continue
            if cur and r.get("currency") != cur:
                continue
            if st and r.get("status") != st:
                continue
            if self.use_dates.isChecked() and r.get("date") and not (df <= r["date"][:10] <= dt):
                continue
            a = r.get("amount")
            if amin is not None and (a is None or a < amin):
                continue
            if amax is not None and (a is None or a > amax):
                continue
            if txt and txt not in " ".join(str(v) for v in r.values() if isinstance(v, str)).lower():
                continue
            out.append(r)
        return out


def button(text: str, slot, primary: bool = False) -> QPushButton:
    b = QPushButton(text)
    b.clicked.connect(slot)
    if primary:
        b.setProperty("primary", True)
    return b


def info(parent, text: str, title: str = "تم"):
    QMessageBox.information(parent, title, text)


def error(parent, text: str):
    QMessageBox.warning(parent, "تنبيه", text)


def confirm(parent, text: str) -> bool:
    return QMessageBox.question(parent, "تأكيد", text, QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes


def ask_text(parent, title: str, label: str, required: bool = False) -> str | None:
    while True:
        text, ok = QInputDialog.getMultiLineText(parent, title, label)
        if not ok:
            return None
        if required and not text.strip():
            error(parent, "هذا الحقل مطلوب")
            continue
        return text.strip()
