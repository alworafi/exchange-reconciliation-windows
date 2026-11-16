# PyInstaller spec — يُبنى عبر build_exe.bat
# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=["openpyxl.cell._writer", "xlrd", "pdfplumber"],
    excludes=["PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore",
              "PySide6.QtMultimedia", "PySide6.QtQuick", "PySide6.QtQml", "tkinter", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="SarrafRecon", console=False,
          upx=False, version=None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="SarrafRecon")
