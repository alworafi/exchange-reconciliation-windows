"""تشغيل الواجهة."""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QMessageBox

from .. import APP_NAME, db
from .main_window import MainWindow

STYLE = """
* { font-family: "Segoe UI", "Tahoma", "Noto Sans Arabic", sans-serif; font-size: 10pt; }
QMainWindow, QWidget { background: #f4f6f5; color: #1d2a2e; }
#sidebar { background: #16323a; }
#sidebar QLabel#brand { background: #16323a; color: #e9f1ef; font-size: 13pt; font-weight: 600; padding: 18px 14px 10px 14px; }
QListWidget#nav { background: transparent; border: none; color: #c5d6d3; outline: 0; }
QListWidget#nav::item { padding: 9px 16px; border-radius: 0; }
QListWidget#nav::item:selected { background: #24525c; color: #ffffff; border-right: 4px solid #d9a441; }
QListWidget#nav::item:hover { background: #1f4650; }
QLabel#pageTitle { font-size: 14pt; font-weight: 600; color: #16323a; padding: 8px 0 6px 0; }
QLabel#hint { color: #5a6b6f; }
QTextBrowser { background: #ffffff; border: 1px solid #d5dedc; }
QLabel#totals { background: #fff7e6; border: 1px solid #e8c983; padding: 8px; font-weight: 600; }
QTableView { background: #ffffff; alternate-background-color: #f2f6f5; gridline-color: #e1e7e6;
             selection-background-color: #cfe3df; selection-color: #10282e; border: 1px solid #d5dedc; }
QHeaderView::section { background: #e3ecea; color: #16323a; padding: 5px; border: none;
                       border-left: 1px solid #d0dad8; font-weight: 600; }
QPushButton { background: #ffffff; border: 1px solid #b9c8c5; border-radius: 4px; padding: 6px 14px; }
QPushButton:hover { background: #eef4f3; }
QPushButton[primary="true"] { background: #1f5f6b; color: white; border: 1px solid #1f5f6b; }
QPushButton[primary="true"]:hover { background: #24707e; }
QLineEdit, QComboBox, QDateEdit, QSpinBox, QPlainTextEdit { background: #ffffff; border: 1px solid #c3d0cd;
             border-radius: 3px; padding: 4px; }
QGroupBox { border: 1px solid #d5dedc; border-radius: 4px; margin-top: 14px; padding: 10px; background: #fafcfb; }
QGroupBox::title { subcontrol-origin: margin; right: 10px; padding: 0 4px; color: #16323a; font-weight: 600; }
QStatusBar { background: #e3ecea; }
"""


def excepthook(exc_type, exc, tb):
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    try:
        log = db.default_data_dir() / "error.log"
        with open(log, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        pass
    QMessageBox.critical(None, "خطأ غير متوقع", f"حدث خطأ غير متوقع وسُجّل في error.log:\n\n{exc}")


def main(db_path: str | None = None) -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setLayoutDirection(Qt.RightToLeft)
    app.setFont(QFont("Segoe UI", 10))
    app.setStyleSheet(STYLE)
    sys.excepthook = excepthook
    data_dir = Path(db_path).parent if db_path else db.default_data_dir()
    conn = db.connect(Path(db_path) if db_path else data_dir / "sarraf_recon.db")
    win = MainWindow(conn, data_dir)
    win.show()
    return app.exec()
