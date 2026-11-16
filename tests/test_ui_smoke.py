"""اختبار دخان للواجهة (يعمل بدون شاشة عبر offscreen). لا يغني عن التجربة على Windows."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from conftest import do_import  # noqa: E402
from pathlib import Path  # noqa: E402

from sarraf_recon import services  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def test_all_pages_open_with_demo_data(env, tmp_path):
    import subprocess, sys
    subprocess.run([sys.executable, str(ROOT / "tools" / "make_demo.py")], check=True, capture_output=True)
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from sarraf_recon.ui.app import STYLE
    from sarraf_recon.ui.main_window import MainWindow

    conn = env["conn"]
    acc = services.create_account(conn, "صراف تجريبي")
    do_import(env, acc, "system", ROOT / "demo" / "كشف_النظام_تجريبي.xlsx")
    do_import(env, acc, "exchanger", ROOT / "demo" / "كشف_الصراف_تجريبي.xlsx")
    services.run_matching(conn, acc)

    app = QApplication.instance() or QApplication([])
    app.setLayoutDirection(Qt.RightToLeft)
    app.setStyleSheet(STYLE)
    win = MainWindow(conn, env["dir"])
    win.resize(1400, 860)
    win.show()
    shots = os.environ.get("UI_SHOTS")
    for i in range(win.nav.count()):
        win.nav.setCurrentRow(i)
        app.processEvents()
        page = win.pages[i]
        if hasattr(page, "table") and page.table.model().rowCount():
            t = page.table
            rect = t.visualRect(t.model().index(t.model().rowCount() - 1, 1))
            QTest.mouseClick(t.viewport(), Qt.LeftButton, Qt.NoModifier, rect.center())
            app.processEvents()
            assert t.selected(), f"page {i}: click did not select a row"
        if shots:
            win.grab().save(str(Path(shots) / f"{i:02d}.png"))
    dash = win.pages[0]
    assert dash.table.model().rowCount() == 2          # YER + SAR
    win.nav.setCurrentRow(0)
    app.processEvents()
    rect = dash.table.visualRect(dash.table.model().index(0, 1))
    QTest.mouseClick(dash.table.viewport(), Qt.LeftButton, Qt.NoModifier, rect.center())
    assert "لماذا يختلف الرصيد" in dash.detail.toPlainText()
    assert win.pages[3].table.model().rowCount() >= 1   # مقترحات
