"""النافذة الرئيسية: قائمة جانبية + صفحات."""
from __future__ import annotations

from PySide6.QtWidgets import (QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow, QStackedWidget,
                               QVBoxLayout, QWidget)

from .. import APP_NAME, __version__
from . import pages

NAV = [
    ("اللوحة الرئيسية", pages.DashboardPage),
    ("حسابات الصرافين", pages.AccountsPage),
    ("استيراد كشف", pages.ImportPage),
    ("المطابقات المقترحة", pages.ProposedPage),
    ("المطابقة اليدوية", pages.ManualMatchPage),
    ("المطابقات المعتمدة", pages.ApprovedPage),
    ("الموجود بالنظام فقط", pages.SystemOnlyPage),
    ("الموجود بالصراف فقط", pages.ExchangerOnlyPage),
    ("الحركات بأثر رجعي", pages.RetroPage),
    ("التكرارات المشتبه بها", pages.DuplicatesPage),
    ("المفكوكة والمرفوضة", pages.HistoryMatchesPage),
    ("سجل المطابقات", pages.AuditPage),
    ("تقرير Excel", pages.ReportPage),
    ("الإعدادات", pages.SettingsPage),
]


class MainWindow(QMainWindow):
    def __init__(self, conn, data_dir):
        super().__init__()
        self.conn, self.data_dir = conn, data_dir
        self.setWindowTitle(f"{APP_NAME} — الإصدار {__version__}")
        self.resize(1400, 860)
        central = QWidget()
        lay = QHBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        side = QWidget()
        side.setObjectName("sidebar")
        sv = QVBoxLayout(side)
        brand = QLabel(APP_NAME)
        brand.setObjectName("brand")
        brand.setWordWrap(True)
        sv.addWidget(brand)
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        sv.addWidget(self.nav, 1)
        side.setFixedWidth(220)

        self.stack = QStackedWidget()
        self.pages = []
        for label, cls in NAV:
            p = cls(self)
            self.pages.append(p)
            self.stack.addWidget(p)
            self.nav.addItem(QListWidgetItem(label))
        lay.addWidget(side)
        lay.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self.nav.currentRowChanged.connect(self._switch)
        self.nav.setCurrentRow(0)

    def _switch(self, i: int):
        self.stack.setCurrentIndex(i)
        self.pages[i].refresh()

    def go(self, cls):
        for i, p in enumerate(self.pages):
            if isinstance(p, cls) and type(p) is cls:
                self.nav.setCurrentRow(i)
                return

    def data_changed(self):
        self.stack.currentWidget().refresh()

    def status(self, text: str):
        self.statusBar().showMessage(text, 6000)
