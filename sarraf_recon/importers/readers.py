"""قراءة الملفات إلى جداول خام: Excel (xlsx/xls)، CSV، PDF.

قارئ PDF عام (يستخرج الجداول بـ pdfplumber). كشوفات PDF تختلف كثيرًا بين
الصرافين، لذلك يجب تجربته على عينات حقيقية قبل اعتماده لكل صراف.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

from .base import RawTable, cell_text

_ARABIC_RE = re.compile(r"[؀-ۿ]")


def list_sheets(path: str | Path) -> list[str]:
    ext = Path(path).suffix.lower()
    if ext in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        try:
            return list(wb.sheetnames)
        finally:
            wb.close()
    if ext == ".xls":
        import xlrd
        return xlrd.open_workbook(path).sheet_names()
    return [""]


def read_file(path: str | Path, sheet: str | None = None, pdf_fix_rtl: bool = False) -> RawTable:
    path = Path(path)
    ext = path.suffix.lower()
    if ext in (".xlsx", ".xlsm"):
        return _read_xlsx(path, sheet)
    if ext == ".xls":
        return _read_xls(path, sheet)
    if ext in (".csv", ".txt"):
        return _read_csv(path)
    if ext == ".pdf":
        return _read_pdf(path, pdf_fix_rtl)
    raise ValueError(f"نوع ملف غير مدعوم: {ext}")


def _trim(rows: list[list]) -> list[list]:
    out = []
    for r in rows:
        r = list(r)
        while r and (r[-1] is None or cell_text(r[-1]) == ""):
            r.pop()
        out.append(r)
    return out


def _read_xlsx(path: Path, sheet: str | None) -> RawTable:
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[sheet] if sheet else wb.worksheets[0]
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        return RawTable(rows=_trim(rows), source=str(path), sheet=ws.title)
    finally:
        wb.close()


def _read_xls(path: Path, sheet: str | None) -> RawTable:
    import xlrd
    from datetime import datetime
    book = xlrd.open_workbook(path)
    sh = book.sheet_by_name(sheet) if sheet else book.sheet_by_index(0)
    rows = []
    for i in range(sh.nrows):
        r = []
        for j in range(sh.ncols):
            c = sh.cell(i, j)
            if c.ctype == xlrd.XL_CELL_DATE:
                r.append(datetime(*xlrd.xldate_as_tuple(c.value, book.datemode)))
            elif c.ctype == xlrd.XL_CELL_EMPTY:
                r.append(None)
            else:
                r.append(c.value)
        rows.append(r)
    return RawTable(rows=_trim(rows), source=str(path), sheet=sh.name)


def _read_csv(path: Path) -> RawTable:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1256", "utf-16"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("تعذر التعرف على ترميز الملف")
    dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|") if text.strip() else csv.excel
    rows = [list(r) for r in csv.reader(text.splitlines(), dialect)]
    return RawTable(rows=_trim(rows), source=str(path))


def fix_visual_arabic(text: str) -> str:
    """بعض ملفات PDF تخزن العربية بترتيب بصري معكوس. نعكس الكلمات العربية فقط."""
    if not text or not _ARABIC_RE.search(text):
        return text
    words = text.split(" ")
    words = [w[::-1] if _ARABIC_RE.search(w) else w for w in words]
    return " ".join(reversed(words))


def _read_pdf(path: Path, fix_rtl: bool) -> RawTable:
    import pdfplumber
    rows: list[list] = []
    title_parts: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page_no, page in enumerate(pdf.pages):
            tables = page.extract_tables() or []
            if page_no == 0:
                txt = page.extract_text() or ""
                title_parts.append(txt[:1000])
            if tables:
                for t in tables:
                    for r in t:
                        rows.append([fix_visual_arabic(c) if (fix_rtl and isinstance(c, str)) else c
                                     for c in r])
            else:
                # بدون خطوط جدول: نقسم كل سطر على المسافات المتعددة
                for line in (page.extract_text(layout=True) or "").splitlines():
                    if not line.strip():
                        continue
                    parts = [p for p in re.split(r"\s{2,}", line.strip()) if p]
                    if fix_rtl:
                        parts = [fix_visual_arabic(p) for p in parts]
                    rows.append(parts)
    title = "\n".join(title_parts)
    if fix_rtl:
        title = fix_visual_arabic(title)
    return RawTable(rows=_trim(rows), source=str(path), title_text=title)
