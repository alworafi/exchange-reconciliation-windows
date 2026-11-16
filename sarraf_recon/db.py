"""قاعدة بيانات SQLite: المخطط، الترحيل، والاتصال.

اصطلاحات التخزين:
- المبالغ أعداد صحيحة بالوحدة الصغرى (انظر money.py).
- side: 'system' = كشف النظام المحاسبي (دفاترنا)، 'exchanger' = كشف الصراف.
- direction: 'D' مدين، 'C' دائن، كما ورد في الكشف نفسه (قبل أي عكس).
- الأرصدة في statement_balances محفوظة باصطلاح الكشف نفسه: مدين − دائن.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS currencies (
    code  TEXT PRIMARY KEY,
    name  TEXT NOT NULL,
    sort  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    code        TEXT,
    mirror      INTEGER NOT NULL DEFAULT 1,   -- 1: مدين الصراف يقابل دائن النظام
    notes       TEXT,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_batches (
    id              INTEGER PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES accounts(id),
    side            TEXT NOT NULL CHECK (side IN ('system','exchanger')),
    file_name       TEXT NOT NULL,
    stored_path     TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    imported_at     TEXT NOT NULL,
    imported_by     TEXT,
    period_from     TEXT,
    period_to       TEXT,
    total_rows      INTEGER NOT NULL DEFAULT 0,
    new_rows        INTEGER NOT NULL DEFAULT 0,
    duplicate_rows  INTEGER NOT NULL DEFAULT 0,
    skipped_rows    INTEGER NOT NULL DEFAULT 0,
    retro_rows      INTEGER NOT NULL DEFAULT 0,
    mapping_json    TEXT,
    warnings_json   TEXT,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
    id              INTEGER PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES accounts(id),
    side            TEXT NOT NULL CHECK (side IN ('system','exchanger')),
    currency        TEXT NOT NULL,
    txn_date        TEXT NOT NULL,            -- YYYY-MM-DD
    description     TEXT,
    reference       TEXT,
    ref_norm        TEXT,
    counterparty    TEXT,
    debit           INTEGER NOT NULL DEFAULT 0,
    credit          INTEGER NOT NULL DEFAULT 0,
    direction       TEXT NOT NULL CHECK (direction IN ('D','C')),
    amount          INTEGER NOT NULL CHECK (amount > 0),
    stmt_balance    INTEGER,
    fingerprint     TEXT NOT NULL,
    occurrence      INTEGER NOT NULL DEFAULT 1,
    batch_id        INTEGER NOT NULL REFERENCES import_batches(id),
    source_row      INTEGER,
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','duplicate','excluded')),
    duplicate_of    INTEGER REFERENCES transactions(id),
    dup_suspect_of  INTEGER REFERENCES transactions(id),
    dup_reviewed    INTEGER NOT NULL DEFAULT 0,
    is_retroactive  INTEGER NOT NULL DEFAULT 0,
    note            TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE (account_id, side, fingerprint, occurrence)
);
CREATE INDEX IF NOT EXISTS ix_txn_acc_cur ON transactions(account_id, currency, side, status);
CREATE INDEX IF NOT EXISTS ix_txn_date ON transactions(txn_date);

-- كل ظهور لحركة في ملف مستورد (لإثبات أن التداخل لم يُحتسب مرتين)
CREATE TABLE IF NOT EXISTS batch_sightings (
    batch_id    INTEGER NOT NULL REFERENCES import_batches(id),
    txn_id      INTEGER NOT NULL REFERENCES transactions(id),
    source_row  INTEGER,
    PRIMARY KEY (batch_id, txn_id)
);

-- أرصدة واردة في الكشوفات (افتتاحي/ختامي) أو مُدخلة يدويًا
CREATE TABLE IF NOT EXISTS statement_balances (
    id          INTEGER PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts(id),
    side        TEXT NOT NULL CHECK (side IN ('system','exchanger')),
    currency    TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('opening','closing')),
    as_of       TEXT NOT NULL,     -- الافتتاحي: الرصيد قبل حركات هذا اليوم. الختامي: بعد حركات هذا اليوم
    balance     INTEGER NOT NULL,  -- باصطلاح الكشف: مدين − دائن
    source      TEXT NOT NULL DEFAULT 'statement',
    batch_id    INTEGER REFERENCES import_batches(id),
    created_at  TEXT NOT NULL,
    created_by  TEXT,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS matches (
    id              INTEGER PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES accounts(id),
    currency        TEXT NOT NULL,
    kind            TEXT NOT NULL,   -- one_to_one, one_to_many, many_to_one, many_to_many
    is_partial      INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL CHECK (status IN ('proposed','approved','rejected','unmatched')),
    score           INTEGER NOT NULL DEFAULT 0,
    reasons         TEXT,
    method          TEXT NOT NULL CHECK (method IN ('auto','manual')),
    signature       TEXT NOT NULL,
    matched_amount  INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    created_by      TEXT,
    decided_at      TEXT,
    decided_by      TEXT,
    decision_note   TEXT
);
CREATE INDEX IF NOT EXISTS ix_match_status ON matches(account_id, currency, status);

CREATE TABLE IF NOT EXISTS match_items (
    id          INTEGER PRIMARY KEY,
    match_id    INTEGER NOT NULL REFERENCES matches(id),
    txn_id      INTEGER NOT NULL REFERENCES transactions(id),
    side        TEXT NOT NULL,
    amount      INTEGER NOT NULL CHECK (amount > 0)
);
CREATE INDEX IF NOT EXISTS ix_mi_txn ON match_items(txn_id);
CREATE INDEX IF NOT EXISTS ix_mi_match ON match_items(match_id);

CREATE TABLE IF NOT EXISTS rejected_signatures (
    signature   TEXT PRIMARY KEY,
    account_id  INTEGER,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_profiles (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    side          TEXT,
    mapping_json  TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    user        TEXT,
    action      TEXT NOT NULL,
    entity      TEXT,
    entity_id   INTEGER,
    account_id  INTEGER,
    currency    TEXT,
    details     TEXT
);

-- حماية ثانية داخل قاعدة البيانات: لا يُعتمد تخصيص يتجاوز المبلغ المتاح للحركة
CREATE TRIGGER IF NOT EXISTS trg_match_approve_guard
BEFORE UPDATE OF status ON matches
WHEN NEW.status = 'approved' AND OLD.status <> 'approved'
BEGIN
    SELECT RAISE(ABORT, 'OVER_ALLOCATION')
    WHERE EXISTS (
        SELECT 1
        FROM match_items mi
        JOIN transactions t ON t.id = mi.txn_id
        WHERE mi.match_id = NEW.id
          AND (t.status <> 'active'
               OR mi.amount + (SELECT COALESCE(SUM(mi2.amount), 0)
                               FROM match_items mi2 JOIN matches m2 ON m2.id = mi2.match_id
                               WHERE mi2.txn_id = mi.txn_id AND m2.status = 'approved') > t.amount)
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_match_insert_guard
BEFORE INSERT ON matches
WHEN NEW.status = 'approved'
BEGIN
    SELECT RAISE(ABORT, 'INSERT_AS_PROPOSED_THEN_APPROVE');
END;
"""

DEFAULT_CURRENCIES = [
    ("YER", "ريال يمني", 1),
    ("SAR", "ريال سعودي", 2),
    ("USD", "دولار أمريكي", 3),
]

DEFAULT_SETTINGS = {
    "user_name": "المحاسب",
    "date_window_days": "15",
    "auto_approve": "1",
    "auto_approve_min_score": "85",
    "min_proposal_score": "45",
    "max_group_size": "4",
    "date_order": "DMY",
}


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def app_dir() -> Path:
    """مجلد البرنامج (بجانب EXE عند التجميع، أو جذر المشروع)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def default_data_dir() -> Path:
    env = os.environ.get("SARRAF_RECON_DATA")
    if env:
        return Path(env)
    candidate = app_dir() / "data"
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return candidate
    except OSError:
        base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "SarrafRecon"
        base.mkdir(parents=True, exist_ok=True)
        return base


def connect(path: str | Path) -> sqlite3.Connection:
    path = str(path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # نتحكم بالمعاملات يدويًا
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT OR IGNORE INTO currencies(code, name, sort) VALUES (?,?,?)", DEFAULT_CURRENCIES
        )
        conn.executemany(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?,?)", DEFAULT_SETTINGS.items()
        )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


class Tx:
    """معاملة كتابة حصرية: with Tx(conn): ..."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False


def get_setting(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return default if default is not None else DEFAULT_SETTINGS.get(key)
    return row["value"]


def set_setting(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
