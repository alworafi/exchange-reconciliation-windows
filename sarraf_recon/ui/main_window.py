"""النافذة الرئيسية: قائمة جانبية + صفحات."""
from __future__ import annotations

from PySide6.QtWidgets import (QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow, QStackedWidget,
                               QVBoxLayout, QWidget)

from .. import APP_NAME, __version__
from . import pages

# القائمة المبسطة: 7 صفحات. الصفحات المتقاربة تبويبات داخل صفحة واحدة.
NAV = [
    ("الرئيسية", pages.DashboardPage),
    ("الصرافين", pages.AccountsPage),
    ("استيراد كشف", pages.ImportPage),
    ("المطابقة", ("المطابقة", [("① المقترحات — تحتاج قرارك", pages.ProposedPage),
                               ("② المطابقة اليدوية", pages.ManualMatchPage)])),
    ("غير المطابق", ("الحركات غير المطابقة — تبقى مفتوحة حتى تُطابق",
                     [("موجود بالنظام فقط (لم يحوّل)", pages.SystemOnlyPage),
                      ("موجود بالصراف فقط (لم يُسجّل)", pages.ExchangerOnlyPage),
                      ("مكرر مشتبه", pages.DuplicatesPage),
                      ("بأثر رجعي", pages.RetroPage)])),
    ("المعتمد والسجل", ("المطابقات المعتمدة والسجل",
                         [("المعتمدة (فك المطابقة)", pages.ApprovedPage),
                          ("المفكوكة والمرفوضة", pages.HistoryMatchesPage),
                          ("سجل كل العمليات", pages.AuditPage)])),
    ("التقرير والإعدادات", ("التقرير والإعدادات",
                            [("تقرير Excel", pages.ReportPage), ("الإعدادات", pages.SettingsPage)])),
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
        for label, spec in NAV:
            p = pages.TabsPage(self, spec[0], spec[1]) if isinstance(spec, tuple) else spec(self)
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
            if type(p) is cls or (isinstance(p, pages.TabsPage) and p.show_page(cls)):
                self.nav.setCurrentRow(i)
                self.pages[i].refresh()
                return

    def data_changed(self):
        self.stack.currentWidget().refresh()

    def status(self, text: str):
        self.statusBar().showMessage(text, 6000)
