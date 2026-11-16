import sys
from datetime import date
from pathlib import Path

import pytest
from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sarraf_recon import db, services  # noqa: E402
from sarraf_recon.importers import detect_mapping, parse_table, read_file  # noqa: E402


@pytest.fixture
def env(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    return {"conn": conn, "dir": tmp_path}


def write_statement(path: Path, rows, title="كشف حساب", currency_title="ريال يمني", with_balance=False,
                    opening=None, sign=1):
    """rows: (date, ref, desc, debit, credit) — يكتب كشفًا بصيغة شائعة مع عناوين عربية."""
    wb = Workbook()
    ws = wb.active
    ws.append([title])
    ws.append([f"العملة: {currency_title}"])
    ws.append([])
    headers = ["التاريخ", "رقم المرجع", "البيان", "مدين", "دائن"] + (["الرصيد"] if with_balance else [])
    ws.append(headers)
    bal = opening or 0
    if opening is not None:
        ws.append([None, None, "رصيد سابق", None, None] + ([sign * bal] if with_balance else []))
    for d, ref, desc, dr, cr in rows:
        bal += (dr or 0) - (cr or 0)
        ws.append([d, ref, desc, dr, cr] + ([sign * bal] if with_balance else []))
    wb.save(path)
    return path


def do_import(env, account_id, side, path, allow_same_file=False, default_currency="YER"):
    table = read_file(path)
    mapping = detect_mapping(table, default_currency=default_currency)
    parsed = parse_table(table, mapping)
    return services.import_statement(env["conn"], account_id, side, path, parsed, mapping, env["dir"],
                                     allow_same_file=allow_same_file)


D = date
