# SQLite layer for the budget app. the tables are:
# categories(name PK, emoji, color, sort_order, is_protected, created_at)
# transactions(id PK, date, merchant, normalized_merchant, amount, category FK, note,
#   source, source_file, is_excluded, is_recurring, confidence, needs_review,
#   is_duplicate, extraction_mode, created_at, updated_at)
# there is no UNIQUE constraint on transactions on purpose. possible duplicates are
# flagged with is_duplicate, never silently skipped, so the user can review them and
# identical but legit rows can sit side by side.
# merchant_memory(normalized_merchant PK, merchant_display, category, correction_count, last_updated)
# settings(key PK, value)
from __future__ import annotations

import json
import math
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable


def _now_iso() -> str:
    # timezone aware UTC timestamp in ISO format
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _coerce_amount(value: Any) -> float:
    # validate and convert a transaction amount to a finite float, or raise ValueError
    # with a clear plain message. this is the last guard for the user's totals. a bad
    # amount fails loudly instead of silently storing infinity, which would corrupt every
    # total, or NaN, which SQLite turns into NULL and then shows up as the cryptic NOT NULL
    # constraint failed on transactions.amount. callers at the UI or extraction boundary
    # should catch this and show the message to the user.
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError("Amount is required.")
    try:
        amt = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"Amount is not a valid number: {value!r}")
    if not math.isfinite(amt):
        raise ValueError("Amount must be a real number (not infinity or NaN).")
    return amt


def _safe_float(value: Any, default: float = 0.0) -> float:
    # best effort float for values read back from settings (the usage counters). never
    # raises, a corrupt or non numeric stored value falls back to default so a single bad
    # row can't crash the Settings tab or the post upload spend meter update.
    try:
        f = float(value)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default

DEFAULT_CATEGORIES = [
    # (name, emoji, color, is_protected)
    ("Food",          "🍕", "#f97316", 0),
    ("Groceries",     "🛒", "#16a34a", 0),
    ("Transportation","🚗", "#0ea5e9", 0),
    ("Home",          "🏠", "#8b5cf6", 0),
    ("Bills",         "💡", "#eab308", 0),
    ("Entertainment", "🎬", "#ec4899", 0),
    ("Shopping",      "🛍️", "#f43f5e", 0),
    ("Health",        "💊", "#14b8a6", 0),
    ("Travel",        "✈️", "#3b82f6", 0),
    ("Income",        "💰", "#059669", 1),
    ("Transfers",     "🔄", "#64748b", 0),
    ("Needs Review",  "❓", "#a855f7", 1),
]

# how many of the most recently deleted transactions to keep for recovery. a bounded ring
# buffer, a new deletion evicts the oldest once this many are stored.
RECENTLY_DELETED_KEEP = 10


# connection

@contextmanager
def conn(db_path: str):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


# schema init

def _migrate_drop_unique_add_isdup(db_path: str) -> None:
    # one time rebuild. drop the old UNIQUE(date,normalized_merchant,amount) constraint and
    # add the is_duplicate column. we now flag possible duplicates instead of skipping them
    # on insert, so identical rows have to be allowed to coexist and a bad auto skip can
    # never silently delete a real transaction. SQLite can't drop a constraint in place, so
    # we rebuild the table. idempotent, keyed on whether the is_duplicate column is already
    # there. backs up the db file before the destructive rebuild.
    c = sqlite3.connect(db_path)
    # autocommit mode. we drive the transaction ourselves with the BEGIN and COMMIT embedded
    # in the executescript below. mixing a manual c.execute BEGIN with executescript fails
    # because executescript implicitly commits first, leaving no transaction for a later
    # COMMIT, and SQLite then errors that no transaction is active.
    c.isolation_level = None
    try:
        c.row_factory = sqlite3.Row
        tbl = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='transactions'"
        ).fetchone()
        if not tbl:
            return  # fresh db, the CREATE in init_db already uses the new schema
        cols = {r["name"] for r in c.execute("PRAGMA table_info(transactions)").fetchall()}
        if "is_duplicate" in cols:
            return  # already migrated
        # very old dbs may predate extraction_mode, make sure it exists before we copy
        if "extraction_mode" not in cols:
            c.execute("ALTER TABLE transactions ADD COLUMN extraction_mode TEXT")
        # safety snapshot before a destructive rebuild
        try:
            import shutil
            shutil.copy2(db_path, f"{db_path}.bak-{datetime.now():%Y%m%d_%H%M%S}")
        except Exception:
            pass
        # foreign_keys has to be toggled outside any transaction (it's a no-op inside one),
        # so set it here in autocommit mode before the embedded BEGIN starts the rebuild.
        c.execute("PRAGMA foreign_keys=OFF")
        c.executescript(
            """
            BEGIN;
            CREATE TABLE transactions_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                merchant TEXT NOT NULL,
                normalized_merchant TEXT NOT NULL,
                amount REAL NOT NULL,
                category TEXT NOT NULL REFERENCES categories(name) ON UPDATE CASCADE,
                note TEXT,
                source TEXT NOT NULL DEFAULT 'manual',
                source_file TEXT,
                is_excluded INTEGER NOT NULL DEFAULT 0,
                is_recurring INTEGER NOT NULL DEFAULT 0,
                confidence REAL,
                needs_review INTEGER NOT NULL DEFAULT 0,
                is_duplicate INTEGER NOT NULL DEFAULT 0,
                extraction_mode TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            INSERT INTO transactions_new
                (id, date, merchant, normalized_merchant, amount, category, note, source,
                 source_file, is_excluded, is_recurring, confidence, needs_review,
                 extraction_mode, created_at, updated_at)
            SELECT
                id, date, merchant, normalized_merchant, amount, category, note, source,
                source_file, is_excluded, is_recurring, confidence, needs_review,
                extraction_mode, created_at, updated_at
            FROM transactions;
            DROP TABLE transactions;
            ALTER TABLE transactions_new RENAME TO transactions;
            CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(date);
            CREATE INDEX IF NOT EXISTS idx_tx_category ON transactions(category);
            CREATE INDEX IF NOT EXISTS idx_tx_normalized ON transactions(normalized_merchant);
            COMMIT;
            """
        )
        c.execute("PRAGMA foreign_keys=ON")
    finally:
        c.close()


def init_db(db_path: str) -> None:
    # drop the legacy UNIQUE constraint and add is_duplicate (own connection, runs first)
    _migrate_drop_unique_add_isdup(db_path)
    with conn(db_path) as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS categories (
                name TEXT PRIMARY KEY,
                emoji TEXT NOT NULL DEFAULT '📦',
                color TEXT NOT NULL DEFAULT '#6b7280',
                sort_order INTEGER NOT NULL DEFAULT 100,
                is_protected INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                merchant TEXT NOT NULL,
                normalized_merchant TEXT NOT NULL,
                amount REAL NOT NULL,
                category TEXT NOT NULL REFERENCES categories(name) ON UPDATE CASCADE,
                note TEXT,
                source TEXT NOT NULL DEFAULT 'manual',
                source_file TEXT,
                is_excluded INTEGER NOT NULL DEFAULT 0,
                is_recurring INTEGER NOT NULL DEFAULT 0,
                confidence REAL,
                needs_review INTEGER NOT NULL DEFAULT 0,
                is_duplicate INTEGER NOT NULL DEFAULT 0,
                extraction_mode TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(date);
            CREATE INDEX IF NOT EXISTS idx_tx_category ON transactions(category);
            CREATE INDEX IF NOT EXISTS idx_tx_normalized ON transactions(normalized_merchant);

            CREATE TABLE IF NOT EXISTS merchant_memory (
                normalized_merchant TEXT PRIMARY KEY,
                merchant_display TEXT NOT NULL,
                category TEXT NOT NULL REFERENCES categories(name) ON UPDATE CASCADE,
                correction_count INTEGER NOT NULL DEFAULT 1,
                last_updated TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS recently_deleted (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deleted_at TEXT NOT NULL DEFAULT (datetime('now')),
                label TEXT NOT NULL,
                n_rows INTEGER NOT NULL DEFAULT 1,
                payload TEXT NOT NULL
            );
            """
        )

        # lightweight migration, older dbs may not have the extraction_mode column
        existing_cols = {row["name"] for row in c.execute("PRAGMA table_info(transactions)").fetchall()}
        if "extraction_mode" not in existing_cols:
            c.execute("ALTER TABLE transactions ADD COLUMN extraction_mode TEXT")
        # links the occurrences of one recurring transaction into a series, so the user can
        # delete or edit this one versus this and all future ones.
        if "recurrence_id" not in existing_cols:
            c.execute("ALTER TABLE transactions ADD COLUMN recurrence_id TEXT")
        c.execute("CREATE INDEX IF NOT EXISTS idx_tx_recurrence ON transactions(recurrence_id)")

        # one time migration to clean display names of existing rows that still have store
        # numbers or a leading star. idempotent, running it again on clean rows is a no-op.
        migrated_row = c.execute(
            "SELECT value FROM settings WHERE key='migrated_clean_merchants_v1'"
        ).fetchone()
        if not migrated_row:
            for row in c.execute("SELECT id, merchant FROM transactions").fetchall():
                # one time cleanup of pre existing imported rows. title casing the old
                # shouty bank names is intended here, it matches the original import.
                cleaned = titlecase_if_shouting(clean_merchant_display(row["merchant"]))
                if cleaned and cleaned != row["merchant"]:
                    c.execute("UPDATE transactions SET merchant=? WHERE id=?",
                              (cleaned, row["id"]))
            c.execute(
                "INSERT INTO settings(key, value) VALUES ('migrated_clean_merchants_v1', '1') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )

        # seed default categories only if empty
        existing = c.execute("SELECT COUNT(*) AS n FROM categories").fetchone()["n"]
        if existing == 0:
            for i, (name, emoji, color, protected) in enumerate(DEFAULT_CATEGORIES):
                c.execute(
                    "INSERT INTO categories(name, emoji, color, sort_order, is_protected) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (name, emoji, color, i, protected),
                )

        # one time migration that retires the Other category. it was confusing as a
        # selectable option, so existing Other transactions and learned merchants move to
        # Needs Review (the catch all now) and the category itself is removed. idempotent.
        retired = c.execute(
            "SELECT value FROM settings WHERE key='migrated_retire_other_v1'"
        ).fetchone()
        if not retired:
            has_other = c.execute("SELECT 1 FROM categories WHERE name='Other'").fetchone()
            has_review = c.execute("SELECT 1 FROM categories WHERE name='Needs Review'").fetchone()
            if has_other and has_review:
                c.execute("UPDATE transactions SET category='Needs Review' WHERE category='Other'")
                c.execute("UPDATE merchant_memory SET category='Needs Review' WHERE category='Other'")
                c.execute("DELETE FROM categories WHERE name='Other'")
            c.execute(
                "INSERT INTO settings(key, value) VALUES ('migrated_retire_other_v1', '1') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )


# merchant normalization

_POS_PREFIXES = re.compile(
    r"^(tst\*|sq\s*\*|sp\s*\*|sp-|toast\*|paypal\s*\*|amzn\s+mktp|amzn\.com|amazon\s+mktp|sumup\s*\*|"
    r"square\s*\*|stripe\s*\*|wmt\*|wal-mart\s*#?|wm\s+supercenter|costco\s+whse|costco\s+gas)",
    re.IGNORECASE,
)
_STORE_NUM = re.compile(r"#\s*\d+\b|\bstore\s*\d+\b|\b\d{3,}\b")
_PHONE = re.compile(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b")
_PROVINCE_STATE = re.compile(
    r"\b(on|qc|bc|ab|mb|sk|ns|nb|pe|nl|yt|nt|nu|"
    r"al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md|ma|mi|mn|ms|mo|mt|ne|nv|"
    r"nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|vt|va|wa|wv|wi|wy)\b",
    re.IGNORECASE,
)
_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")


def normalize_merchant(name: str) -> str:
    # lowercase, strip POS prefixes, store numbers, phones, punctuation, and common
    # location codes. use this for the dedupe and merchant memory key only, not for display.
    if not name:
        return ""
    s = name.strip()
    s = _POS_PREFIXES.sub("", s)
    s = _PHONE.sub("", s)
    s = _STORE_NUM.sub("", s)
    s = _PUNCT.sub(" ", s)
    s = _PROVINCE_STATE.sub("", s)
    s = _WS.sub(" ", s).strip().lower()
    return s


# patterns for cleaning the visible merchant name (Tim Hortons #4372 becomes Tim Hortons)
_DISPLAY_STRIP_STORE_NUM = re.compile(r"\s*#\s*\d{2,}\b")
_DISPLAY_STRIP_LEADING_PREFIX = re.compile(
    r"^(\*+\s*|tst\*\s*|sq\s*\*\s*|sp\s*\*\s*|toast\*\s*|paypal\s*\*\s*)",
    re.IGNORECASE,
)
_DISPLAY_STRIP_TRAILING_PROVSTATE = re.compile(
    r",?\s+(ON|QC|BC|AB|MB|SK|NS|NB|PE|NL|YT|NT|NU|"
    r"AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|"
    r"NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\s*$",
    re.IGNORECASE,
)
_DISPLAY_STRIP_TRAILING_NUMS = re.compile(r"\s+\d{2,}\s*$")


# common Canadian and US city names that show up as trailing noise on statement lines.
# one source of truth, reused for both display cleanup (end anchored) and the fuzzy
# merchant matcher (anywhere). longer names come before their prefixes (RICHMOND HILL
# before RICHMOND) so the alternation prefers the longer match.
_CITY_ALT = (
    r"TORONTO|NORTH YORK|SCARBOROUGH|MISSISSAUGA|ETOBICOKE|MARKHAM|BRAMPTON|"
    r"OTTAWA|HAMILTON|LONDON|KITCHENER|WATERLOO|GUELPH|BARRIE|OSHAWA|WINDSOR|VAUGHAN|"
    r"RICHMOND HILL|BURLINGTON|OAKVILLE|MONTREAL|QUEBEC|LAVAL|"
    r"VANCOUVER|BURNABY|SURREY|RICHMOND|VICTORIA|"
    r"CALGARY|EDMONTON|WINNIPEG|HALIFAX|"
    r"NEW YORK|NYC|LOS ANGELES|CHICAGO|HOUSTON|PHILADELPHIA|PHOENIX|SAN ANTONIO|SAN DIEGO|"
    r"DALLAS|AUSTIN|SAN FRANCISCO|SEATTLE|BOSTON|DETROIT|MIAMI|ATLANTA|PORTLAND"
)
# strip the trailing CITYNAME ON style suffix, common bank statement noise (display)
_CITY_SUFFIXES = re.compile(r"[\s\-,]*(" + _CITY_ALT + r")\s*$", re.IGNORECASE)
# strip city names anywhere, used only by the fuzzy merchant matcher, never for storage
_MATCH_CITY = re.compile(r"\b(" + _CITY_ALT + r")\b", re.IGNORECASE)

# very common business words that must never carry a fuzzy match on their own
_GENERIC_TOKENS = frozenset({
    "the", "and", "of", "co", "inc", "ltd", "llc", "corp", "company",
    "store", "shop", "services", "service", "group", "intl", "international",
})


def clean_merchant_display(name: str) -> str:
    # strip store numbers, leading POS prefixes, trailing province or state codes, and
    # common city names from a merchant name for display. case is kept exactly as passed
    # in. this is now an import only helper, called solely from ai._clean_extracted on the
    # extraction path. it is not called on any manual write path. manual Add, Home edit,
    # recurring edit, and the upload preview save all store the merchant verbatim (only
    # trimmed) so whatever the user types survives, like Store #5, Studio 54, or a
    # deliberate STARBUCKS. taming shouty all caps bank text is the separate import only job
    # of titlecase_if_shouting, also in ai._clean_extracted. a few examples:
    #   Tim Hortons #4372 TORONTO ON           becomes  Tim Hortons
    #   *RFBT-YONGE SHEPPARD C NORTH YORK ON    becomes  RFBT-YONGE SHEPPARD C
    #   TST* PIZZA NIGHT                        becomes  PIZZA NIGHT (case kept)
    #   AMAZON MKTP US*ABC123 SEATTLE WA        becomes  AMAZON MKTP US (best effort)
    if not name:
        return name or ""
    s = name.strip()
    s = _DISPLAY_STRIP_LEADING_PREFIX.sub("", s)
    s = _DISPLAY_STRIP_STORE_NUM.sub("", s)
    s = _DISPLAY_STRIP_TRAILING_PROVSTATE.sub("", s)
    s = _CITY_SUFFIXES.sub("", s)
    s = _DISPLAY_STRIP_TRAILING_NUMS.sub("", s)
    return s.strip(" -,")


def titlecase_if_shouting(name: str) -> str:
    # turn an all caps name into title case (TIM HORTONS becomes Tim Hortons) and leave any
    # mixed case name alone. banks often shout merchant names in caps, so this runs on the
    # import and extraction path only (ai._clean_extracted). never call it on a value the
    # user typed by hand, a manual all caps entry is a deliberate choice that
    # clean_merchant_display now preserves.
    if name and name.isupper():
        return name.title()
    return name


# categories

def get_categories(db_path: str) -> list[dict]:
    with conn(db_path) as c:
        rows = c.execute(
            "SELECT name, emoji, color, sort_order, is_protected FROM categories "
            "ORDER BY sort_order, name"
        ).fetchall()
        return [dict(r) for r in rows]


def add_category(db_path: str, name: str, emoji: str, color: str) -> bool:
    name = name.strip()
    if not name:
        return False
    with conn(db_path) as c:
        try:
            max_order = c.execute("SELECT COALESCE(MAX(sort_order),0)+1 AS n FROM categories").fetchone()["n"]
            c.execute(
                "INSERT INTO categories(name, emoji, color, sort_order, is_protected) VALUES (?,?,?,?,0)",
                (name, emoji or "📦", color or "#6b7280", max_order),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def update_category(db_path: str, old_name: str, new_name: str, emoji: str, color: str) -> bool:
    # update emoji, color, or name. protected categories can change emoji and color but not
    # their name, since code refers to protected names like Income and Needs Review directly.
    new_name = new_name.strip()
    if not new_name:
        return False
    with conn(db_path) as c:
        row = c.execute("SELECT is_protected FROM categories WHERE name=?", (old_name,)).fetchone()
        if not row:
            return False
        if row["is_protected"] and new_name != old_name:
            return False  # block rename of protected categories
        try:
            c.execute(
                "UPDATE categories SET name=?, emoji=?, color=? WHERE name=?",
                (new_name, emoji, color, old_name),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def delete_category(db_path: str, name: str, reassign_to: str = "Needs Review") -> tuple[bool, str]:
    # delete a category, reassigning any transactions and merchant_memory rows to
    # reassign_to (defaults to Needs Review so the user re-checks them). protected
    # categories can't be deleted.
    with conn(db_path) as c:
        row = c.execute("SELECT is_protected FROM categories WHERE name=?", (name,)).fetchone()
        if not row:
            return False, "Category not found"
        if row["is_protected"]:
            return False, "This category is protected and cannot be deleted"
        if reassign_to == name:
            return False, "Cannot reassign to the same category"
        if not c.execute("SELECT 1 FROM categories WHERE name=?", (reassign_to,)).fetchone():
            return False, f"Reassign target '{reassign_to}' does not exist"
        c.execute("UPDATE transactions SET category=? WHERE category=?", (reassign_to, name))
        c.execute("UPDATE merchant_memory SET category=? WHERE category=?", (reassign_to, name))
        c.execute("DELETE FROM categories WHERE name=?", (name,))
        return True, "Deleted"


# transactions

def add_transaction(
    db_path: str,
    *,
    date: str,
    merchant: str,
    amount: float,
    category: str,
    note: str | None = None,
    source: str = "manual",
    source_file: str | None = None,
    is_excluded: bool = False,
    is_recurring: bool = False,
    confidence: float | None = None,
    needs_review: bool = False,
    is_duplicate: bool = False,
    extraction_mode: str | None = None,
    recurrence_id: str | None = None,
) -> int:
    # insert a transaction and return its new id. duplicates are never skipped, pass
    # is_duplicate=True to flag a row that matches an existing one (use find_duplicate_id to
    # detect). the merchant name is stored verbatim (only trimmed) because this is a manual
    # write path, so whatever the user types survives, like Store #5, Studio 54, or custom
    # casing. display noise stripping is import only (ai._clean_extracted). the normalized
    # form, used for duplicate detection and merchant memory, is still computed from the
    # typed name. recurrence_id links all occurrences of one recurring transaction into a series.
    amount = _coerce_amount(amount)
    normalized = normalize_merchant(merchant)
    display = (merchant or "").strip()
    with conn(db_path) as c:
        cur = c.execute(
            "INSERT INTO transactions(date, merchant, normalized_merchant, amount, category, note, "
            "source, source_file, is_excluded, is_recurring, confidence, needs_review, is_duplicate, "
            "extraction_mode, recurrence_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (date, display, normalized, amount, category, note, source, source_file,
             int(is_excluded), int(is_recurring), confidence, int(needs_review), int(is_duplicate),
             extraction_mode, recurrence_id),
        )
        return cur.lastrowid


def bulk_insert_transactions(db_path: str, rows: Iterable[dict]) -> tuple[int, int]:
    # insert many transactions. duplicates are always flagged, never skipped. returns
    # (inserted, flagged_duplicates) where flagged_duplicates counts rows with the
    # is_duplicate flag set. each row's merchant is stored verbatim (only trimmed). the
    # upload path already cleaned names in ai._clean_extracted before the review preview,
    # and any edit the user makes in that preview is a manual action that has to be kept,
    # like Store #5. the normalized form is used for duplicate detection and merchant memory.
    inserted = 0
    flagged = 0
    with conn(db_path) as c:
        for r in rows:
            raw_merchant = r["merchant"]
            normalized = normalize_merchant(raw_merchant)
            display = (raw_merchant or "").strip()
            is_dup = int(bool(r.get("is_duplicate", False)))
            c.execute(
                "INSERT INTO transactions(date, merchant, normalized_merchant, amount, category, note, "
                "source, source_file, is_excluded, is_recurring, confidence, needs_review, is_duplicate, "
                "extraction_mode) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    r["date"], display, normalized, _coerce_amount(r["amount"]),
                    r["category"], r.get("note"), r.get("source", "ai"),
                    r.get("source_file"), int(r.get("is_excluded", False)),
                    int(r.get("is_recurring", False)), r.get("confidence"),
                    int(r.get("needs_review", False)), is_dup,
                    r.get("extraction_mode"),
                ),
            )
            inserted += 1
            flagged += is_dup
    return inserted, flagged


def find_duplicate_id(db_path: str, date: str, merchant: str, amount: float,
                      exclude_id: int | None = None,
                      ignore_merchant: bool = False) -> int | None:
    # return the id of an existing transaction that looks like a possible duplicate of the
    # given one, or None. used to flag, never to skip, so the user decides. exclude_id skips
    # a specific row, e.g. the one being edited.
    #
    # a row counts as a possible duplicate when it shares the same date and either the same
    # amount (and, unless ignore_merchant is set, the same normalized merchant), or the same
    # normalized merchant regardless of amount. the merchant branch means a charge re-listed
    # on the same day from the same store, even at a slightly different amount, gets surfaced
    # for the user to eyeball (only when the name is meaningful, that is non empty after
    # normalization).
    #
    # by default the amount branch also needs the normalized merchant to match (POS prefixes
    # and store numbers are folded away first, see normalize_merchant). pass
    # ignore_merchant=True for a looser net that matches on date and amount alone, used by
    # the upload flow because extraction can mangle the name (e.g. a phone number pulled out
    # as the merchant), so two rows with the same date and amount but different names still
    # get a ⚠️.
    norm = normalize_merchant(merchant)
    branches: list[str] = []
    params: list[Any] = [date]  # leading date=? (always required)
    if ignore_merchant:
        branches.append("amount=?")
        params.append(float(amount))
    else:
        branches.append("(amount=? AND normalized_merchant=?)")
        params.extend([float(amount), norm])
    # same date and same name, any amount (only when the name is meaningful)
    if norm:
        branches.append("normalized_merchant=?")
        params.append(norm)
    sql = f"SELECT id FROM transactions WHERE date=? AND ({' OR '.join(branches)})"
    if exclude_id is not None:
        sql += " AND id<>?"
        params.append(exclude_id)
    sql += " ORDER BY id LIMIT 1"
    with conn(db_path) as c:
        row = c.execute(sql, params).fetchone()
        return row["id"] if row else None


def update_transaction(db_path: str, tx_id: int, **fields: Any) -> None:
    if not fields:
        return
    if "merchant" in fields:
        raw = fields["merchant"]
        fields["normalized_merchant"] = normalize_merchant(raw)
        # manual edit, store verbatim (only trimmed). numbers and custom casing the user
        # types are kept, display noise stripping is import only (ai._clean_extracted).
        fields["merchant"] = (raw or "").strip()
    if "amount" in fields:
        fields["amount"] = _coerce_amount(fields["amount"])
    fields["updated_at"] = _now_iso()
    cols = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [tx_id]
    with conn(db_path) as c:
        c.execute(f"UPDATE transactions SET {cols} WHERE id=?", values)


def get_transaction(db_path: str, tx_id: int) -> dict | None:
    # return a single transaction as a dict (all columns), or None if not found
    with conn(db_path) as c:
        row = c.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
        return dict(row) if row else None


def delete_transaction(db_path: str, tx_id: int) -> None:
    with conn(db_path) as c:
        c.execute("DELETE FROM transactions WHERE id=?", (tx_id,))


def delete_transactions(db_path: str, tx_ids: list[int]) -> int:
    if not tx_ids:
        return 0
    with conn(db_path) as c:
        placeholders = ",".join("?" * len(tx_ids))
        cur = c.execute(f"DELETE FROM transactions WHERE id IN ({placeholders})", tx_ids)
        return cur.rowcount


# recurring series operations. these back the this and all future actions.
# a recurring transaction's occurrences share one recurrence_id. this and all future means
# rows in that series dated on or after the chosen occurrence, past occurrences are never
# touched.

def count_series_from(db_path: str, recurrence_id: str | None, from_date: str) -> int:
    # how many rows in this series fall on or after from_date (this occurrence and the
    # future ones). returns 0 when recurrence_id is None (not part of a series).
    if not recurrence_id:
        return 0
    with conn(db_path) as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM transactions WHERE recurrence_id=? AND date>=?",
            (recurrence_id, from_date),
        ).fetchone()
        return row["n"]


def get_recurring_series(db_path: str, recurrence_id: str | None) -> list[dict]:
    # all rows in a recurrence series, oldest first. empty list when recurrence_id is None.
    # used to work out a series cadence (weekly vs monthly) and to re-anchor and shift its
    # future dates when the user edits one occurrence's date.
    if not recurrence_id:
        return []
    with conn(db_path) as c:
        rows = c.execute(
            "SELECT * FROM transactions WHERE recurrence_id=? ORDER BY date, id",
            (recurrence_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_recurring_from(db_path: str, recurrence_id: str, from_date: str) -> int:
    # delete this occurrence and all future ones in the series. returns rows deleted.
    with conn(db_path) as c:
        cur = c.execute(
            "DELETE FROM transactions WHERE recurrence_id=? AND date>=?",
            (recurrence_id, from_date),
        )
        return cur.rowcount


def update_recurring_from(db_path: str, recurrence_id: str, from_date: str,
                          **fields: Any) -> int:
    # apply field updates to this occurrence and all future ones in the series. mirrors
    # update_transaction's field handling (merchant cleaning, updated_at). never accepts
    # date, each occurrence keeps its own date. returns rows updated.
    fields.pop("date", None)  # date is per occurrence, never overwrite the whole series
    if not fields:
        return 0
    if "merchant" in fields:
        raw = fields["merchant"]
        fields["normalized_merchant"] = normalize_merchant(raw)
        # manual edit, store verbatim (only trimmed), see update_transaction
        fields["merchant"] = (raw or "").strip()
    if "amount" in fields:
        fields["amount"] = _coerce_amount(fields["amount"])
    fields["updated_at"] = _now_iso()
    cols = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [recurrence_id, from_date]
    with conn(db_path) as c:
        cur = c.execute(
            f"UPDATE transactions SET {cols} WHERE recurrence_id=? AND date>=?", values
        )
        return cur.rowcount


def get_transactions(
    db_path: str,
    *,
    year: int | None = None,
    month: int | None = None,
    categories: list[str] | None = None,
    merchant_search: str | None = None,
    needs_review_only: bool = False,
    flagged_only: bool = False,
    include_excluded: bool = True,
) -> list[dict]:
    sql = "SELECT * FROM transactions WHERE 1=1"
    params: list[Any] = []
    if year is not None:
        sql += " AND strftime('%Y', date)=?"
        params.append(str(year))
    if month is not None:
        sql += " AND strftime('%m', date)=?"
        params.append(f"{month:02d}")
    if categories:
        sql += f" AND category IN ({','.join('?'*len(categories))})"
        params.extend(categories)
    if merchant_search:
        sql += " AND lower(merchant) LIKE ?"
        params.append(f"%{merchant_search.lower()}%")
    if needs_review_only:
        sql += " AND needs_review=1"
    if flagged_only:
        sql += " AND (needs_review=1 OR is_duplicate=1)"
    if not include_excluded:
        sql += " AND is_excluded=0"
    sql += " ORDER BY date DESC, id DESC"
    with conn(db_path) as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def count_flagged(db_path: str) -> int:
    # how many transactions are flagged for review (needs_review or possible duplicate)
    with conn(db_path) as c:
        return c.execute(
            "SELECT COUNT(*) AS n FROM transactions WHERE needs_review=1 OR is_duplicate=1"
        ).fetchone()["n"]


def get_available_years(db_path: str) -> list[int]:
    with conn(db_path) as c:
        rows = c.execute(
            "SELECT DISTINCT strftime('%Y', date) AS y FROM transactions ORDER BY y DESC"
        ).fetchall()
        years = [int(r["y"]) for r in rows if r["y"]]
        if not years:
            years = [datetime.now().year]
        return years


def get_monthly_summary(db_path: str, year: int, month: int) -> dict:
    with conn(db_path) as c:
        row = c.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END), 0) AS income,
                COALESCE(SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END), 0) AS expenses,
                COUNT(*) AS txn_count
            FROM transactions
            WHERE strftime('%Y', date)=? AND strftime('%m', date)=? AND is_excluded=0
            """,
            (str(year), f"{month:02d}"),
        ).fetchone()
        return {
            "income": row["income"] or 0.0,
            "expenses": row["expenses"] or 0.0,
            "net": (row["income"] or 0.0) - (row["expenses"] or 0.0),
            "count": row["txn_count"] or 0,
        }


# merchant memory

def learn_merchant(db_path: str, merchant: str, category: str) -> None:
    # record that this normalized merchant should map to this category
    normalized = normalize_merchant(merchant)
    if not normalized:
        return
    with conn(db_path) as c:
        existing = c.execute(
            "SELECT correction_count FROM merchant_memory WHERE normalized_merchant=?",
            (normalized,),
        ).fetchone()
        if existing:
            c.execute(
                "UPDATE merchant_memory SET category=?, merchant_display=?, "
                "correction_count=correction_count+1, last_updated=datetime('now') "
                "WHERE normalized_merchant=?",
                (category, merchant, normalized),
            )
        else:
            c.execute(
                "INSERT INTO merchant_memory(normalized_merchant, merchant_display, category) "
                "VALUES (?,?,?)",
                (normalized, merchant, category),
            )


def lookup_merchant(db_path: str, merchant: str) -> str | None:
    normalized = normalize_merchant(merchant)
    if not normalized:
        return None
    with conn(db_path) as c:
        row = c.execute(
            "SELECT category FROM merchant_memory WHERE normalized_merchant=?", (normalized,)
        ).fetchone()
        return row["category"] if row else None


def get_learned_category(db_path: str, normalized_merchant: str | None) -> str | None:
    # return the learned category for an exact normalized merchant key, or None if that
    # store isn't learned. unlike lookup_merchant it does not re-normalize, pass a key that
    # is already normalized (e.g. a transaction's stored normalized_merchant). this matters
    # because a display name can normalize differently from the stored key
    # (clean_merchant_display strips city names that normalize_merchant keeps), so looking
    # up by the display would miss the row. used to check, before an edit, whether the store
    # being renamed was already learned, so the entry can follow the rename instead of vanishing.
    if not normalized_merchant:
        return None
    with conn(db_path) as c:
        row = c.execute(
            "SELECT category FROM merchant_memory WHERE normalized_merchant=?",
            (normalized_merchant,),
        ).fetchone()
        return row["category"] if row else None


def get_merchant_memory(db_path: str) -> list[dict]:
    with conn(db_path) as c:
        rows = c.execute(
            "SELECT normalized_merchant, merchant_display, category, correction_count, last_updated "
            "FROM merchant_memory ORDER BY correction_count DESC, last_updated DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def _strong_norm(name: str) -> str:
    # stronger normalization used only for fuzzy merchant matching, never for storage or
    # dedupe. starts from normalize_merchant (lowercase, strips POS prefixes, store numbers,
    # phones, province and state codes, punctuation) and also drops city names anywhere, so
    # STARBUCKS #5 TORONTO ON and Starbucks both collapse to starbucks.
    s = normalize_merchant(name)
    s = _MATCH_CITY.sub(" ", s)
    return _WS.sub(" ", s).strip()


def _match_tokens(name: str) -> set[str]:
    # distinctive word tokens of a merchant, strong normalized with very common business
    # words removed so they can't carry a match on their own.
    return {t for t in _strong_norm(name).split() if t not in _GENERIC_TOKENS}


def match_learned_category(merchant: str, memory_rows: list[dict]) -> tuple[str | None, float, str]:
    # best learned category for merchant, or (None, 0.0, reason) when there is no safe
    # match. the tiers get progressively looser but stay safe, and the first hit wins:
    #   1. exact   normalize_merchant() equals a learned key (the original behavior).
    #   2. cleanup equal after also dropping city names (same store, a different branch).
    #   3. token   a learned store's two or more distinctive words are all contained in this
    #              merchant's words, that store was confirmed at least twice, and the
    #              incoming name adds at most two extra words (same brand, not a word soup).
    #
    # reliability guarantees, so this never invents a wrong category. if the candidate
    # learned stores disagree on category it returns ambiguous so the caller sends the row to
    # Needs Review (the filed under two categories goes to Needs Review rule). single word
    # learned names never match by containment (tier 3 needs two or more tokens), so a
    # learned Uber can't hijack Uber Eats. tier 3 only extrapolates from stores confirmed at
    # least twice, since confidence comes from repetition, so one accepted AI guess can't
    # seed a wrong fuzzy match.
    #
    # memory_rows are dicts with keys normalized_merchant, merchant_display, category, and
    # correction_count (exactly what get_merchant_memory returns). pure, no db or network.
    if not merchant or not memory_rows:
        return (None, 0.0, "")

    # tier 1, exact normalized key (identical to lookup_merchant, key is a PK so 0 or 1 rows)
    n = normalize_merchant(merchant)
    if n:
        hits = {r["category"] for r in memory_rows if r.get("normalized_merchant") == n}
        if len(hits) == 1:
            return (next(iter(hits)), 0.97, "exact")
        if len(hits) > 1:
            return (None, 0.0, "ambiguous")

    # tier 2, equal after dropping city names (same store seen in another city or branch)
    s = _strong_norm(merchant)
    if s:
        hits = {r["category"] for r in memory_rows
                if _strong_norm(r.get("merchant_display", "") or r.get("normalized_merchant", "")) == s}
        if len(hits) == 1:
            return (next(iter(hits)), 0.92, "cleanup")
        if len(hits) > 1:
            return (None, 0.0, "ambiguous")

    # tier 3, multi word containment, confirmed only (repetition gate plus ambiguity guard)
    inc = _match_tokens(merchant)
    if inc:
        hits = set()
        for r in memory_rows:
            if int(r.get("correction_count", 0) or 0) < 2:
                continue
            learned = _match_tokens(r.get("merchant_display", "") or r.get("normalized_merchant", ""))
            if len(learned) >= 2 and learned <= inc and (len(inc) - len(learned)) <= 2:
                hits.add(r["category"])
        if len(hits) == 1:
            return (next(iter(hits)), 0.88, "token")
        if len(hits) > 1:
            return (None, 0.0, "ambiguous")

    return (None, 0.0, "")


def forget_merchant(db_path: str, normalized_merchant: str) -> None:
    with conn(db_path) as c:
        c.execute("DELETE FROM merchant_memory WHERE normalized_merchant=?", (normalized_merchant,))


def resync_merchant_memory(db_path: str, normalized_merchant: str | None) -> None:
    # re-derive a learned store's Confirmed count from the live transactions, and forget the
    # store entirely if nothing is filed under it anymore. the count is set to the number of
    # transactions currently categorized under this store's learned category. call this right
    # after a transaction touching the store is recategorized, deleted, or restored, so the
    # Learned stores table mirrors reality. the count drops when you move or remove a backing
    # transaction, and the row disappears once the last one is gone (the count would hit 0).
    # idempotent, a true no-op when the count already matches and a safe no-op for a store
    # that isn't learned. only the store's learned category is counted, so deleting a Coffee
    # charge never weakens a store learned as Food. never raises for a missing store.
    if not normalized_merchant:
        return
    with conn(db_path) as c:
        row = c.execute(
            "SELECT category, correction_count FROM merchant_memory WHERE normalized_merchant=?",
            (normalized_merchant,),
        ).fetchone()
        if not row:
            return
        n = c.execute(
            "SELECT COUNT(*) AS n FROM transactions "
            "WHERE normalized_merchant=? AND category=?",
            (normalized_merchant, row["category"]),
        ).fetchone()["n"]
        if n <= 0:
            c.execute("DELETE FROM merchant_memory WHERE normalized_merchant=?",
                      (normalized_merchant,))
        elif n != row["correction_count"]:
            c.execute(
                "UPDATE merchant_memory SET correction_count=?, last_updated=datetime('now') "
                "WHERE normalized_merchant=?",
                (n, normalized_merchant),
            )


def resync_all_merchant_memory(db_path: str) -> None:
    # reconcile every learned store's Confirmed count against the live transactions in one
    # pass, the safety net behind the per mutation resync_merchant_memory() hooks. idempotent
    # (a true no-op when all counts already match) and self correcting, it fixes any drifted
    # count and forgets any store that no longer backs a transaction under its learned
    # category. call it wherever the learned stores list is about to be shown (the Categories
    # tab render) so the list stays correct even if some future mutation path forgets to resync.
    with conn(db_path) as c:
        keys = [r["normalized_merchant"] for r in
                c.execute("SELECT normalized_merchant FROM merchant_memory").fetchall()]
    for k in keys:  # snapshot first (resync may delete rows), then reconcile each store
        resync_merchant_memory(db_path, k)


# settings

def get_setting(db_path: str, key: str, default: str | None = None) -> str | None:
    with conn(db_path) as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(db_path: str, key: str, value: str) -> None:
    with conn(db_path) as c:
        c.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


# API usage metering (estimated spend)

def add_api_usage(db_path: str, *, cost: float, tokens: int, calls: int = 1) -> None:
    # accumulate estimated API spend so it survives restarts. stored in settings.
    with conn(db_path) as c:
        def _bump(key: str, delta: float, as_float: bool) -> None:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            # tolerate a corrupt or non numeric stored counter, treat it as 0 and self heal
            # instead of crashing the post upload usage update.
            cur = _safe_float(row["value"]) if row else 0.0
            newv = cur + delta
            val = f"{newv:.6f}" if as_float else str(int(round(newv)))
            c.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, val),
            )
        _bump("api_cost_usd", float(cost), as_float=True)
        _bump("api_total_tokens", float(tokens), as_float=False)
        _bump("api_calls", float(calls), as_float=False)


def get_api_usage(db_path: str) -> dict:
    # cumulative estimated API spend as a dict with cost (float), tokens (int) and calls (int)
    return {
        "cost": _safe_float(get_setting(db_path, "api_cost_usd", "0")),
        "tokens": int(_safe_float(get_setting(db_path, "api_total_tokens", "0"))),
        "calls": int(_safe_float(get_setting(db_path, "api_calls", "0"))),
    }


def reset_api_usage(db_path: str) -> None:
    for k in ("api_cost_usd", "api_total_tokens", "api_calls"):
        set_setting(db_path, k, "0")


def set_api_usage(db_path: str, *, cost: float) -> None:
    # manually set the estimated spend to an exact value, e.g. after checking the real bill.
    # auto metering keeps adding on top of this from here, just like before.
    set_setting(db_path, "api_cost_usd", f"{max(0.0, float(cost)):.6f}")


# recently deleted (recovery)
# a bounded ring buffer of the last RECENTLY_DELETED_KEEP deletions so accidental deletes
# can be restored. each entry snapshots the full rows as JSON (a single delete is 1 row, a
# recurring this and future delete is many rows in one entry), so a restore re-creates them
# byte for byte (merchant display, normalized form, flags, recurrence_id, timestamps)
# instead of re-deriving anything.

def record_deletion(db_path: str, rows: list[dict], *, label: str | None = None,
                    keep: int = RECENTLY_DELETED_KEEP) -> None:
    # snapshot just deleted rows for later recovery, then prune to the newest keep. rows are
    # the transaction dicts as they existed before deletion (capture them first, but the
    # order relative to the actual DELETE doesn't matter). a no-op for an empty list. label
    # is the user facing summary, a sensible one is derived if omitted.
    rows = [dict(r) for r in rows]
    if not rows:
        return
    if label is None:
        label = str(rows[0].get("merchant") or "Transaction")
        if len(rows) > 1:
            label = f"{label} · {len(rows)} transactions"
    payload = json.dumps(rows)
    with conn(db_path) as c:
        c.execute(
            "INSERT INTO recently_deleted(label, n_rows, payload) VALUES (?,?,?)",
            (label, len(rows), payload),
        )
        # evict everything older than the newest keep entries
        c.execute(
            "DELETE FROM recently_deleted WHERE id NOT IN "
            "(SELECT id FROM recently_deleted ORDER BY id DESC LIMIT ?)",
            (max(0, int(keep)),),
        )


def list_recently_deleted(db_path: str, limit: int = RECENTLY_DELETED_KEEP) -> list[dict]:
    # recently deleted entries, newest first. each dict has id, deleted_at, label, n_rows,
    # and transactions (the parsed snapshot rows).
    with conn(db_path) as c:
        rows = c.execute(
            "SELECT id, deleted_at, label, n_rows, payload FROM recently_deleted "
            "ORDER BY id DESC LIMIT ?",
            (max(0, int(limit)),),
        ).fetchall()
    out: list[dict] = []
    for r in rows:
        try:
            txns = json.loads(r["payload"])
        except (ValueError, TypeError):
            txns = []
        out.append({
            "id": r["id"],
            "deleted_at": r["deleted_at"],
            "label": r["label"],
            "n_rows": r["n_rows"],
            "transactions": txns,
        })
    return out


def restore_recently_deleted(db_path: str, entry_id: int) -> int:
    # re-insert the transactions from a recently deleted entry and remove the entry. rows are
    # inserted with their original column values kept (not re-derived), so they come back
    # exactly as they were. if a row's original category no longer exists (deleted while it
    # sat here), it falls back to Needs Review to satisfy the foreign key. returns the number
    # of transactions restored (0 if the entry no longer exists).
    with conn(db_path) as c:
        row = c.execute(
            "SELECT payload FROM recently_deleted WHERE id=?", (entry_id,)
        ).fetchone()
        if not row:
            return 0
        try:
            txns = json.loads(row["payload"])
        except (ValueError, TypeError):
            txns = []
        valid = {r["name"] for r in c.execute("SELECT name FROM categories").fetchall()}
        restored = 0
        touched_stores: set[str] = set()
        for t in txns:
            cat = t.get("category")
            if cat not in valid:
                cat = "Needs Review" if "Needs Review" in valid else cat
            c.execute(
                "INSERT INTO transactions(date, merchant, normalized_merchant, amount, "
                "category, note, source, source_file, is_excluded, is_recurring, confidence, "
                "needs_review, is_duplicate, extraction_mode, recurrence_id, created_at, "
                "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    t.get("date"), t.get("merchant"), t.get("normalized_merchant"),
                    t.get("amount"), cat, t.get("note"), t.get("source", "manual"),
                    t.get("source_file"), int(t.get("is_excluded", 0) or 0),
                    int(t.get("is_recurring", 0) or 0), t.get("confidence"),
                    int(t.get("needs_review", 0) or 0), int(t.get("is_duplicate", 0) or 0),
                    t.get("extraction_mode"), t.get("recurrence_id"),
                    t.get("created_at") or _now_iso(), t.get("updated_at") or _now_iso(),
                ),
            )
            if t.get("normalized_merchant"):
                touched_stores.add(t["normalized_merchant"])
            restored += 1
        c.execute("DELETE FROM recently_deleted WHERE id=?", (entry_id,))
    # after the re-insert commits, bring each restored store's learned Confirmed count back
    # in line with the transactions that just came back (the matching delete had dropped it).
    # resync opens its own connection, so it runs out here.
    for nm in touched_stores:
        resync_merchant_memory(db_path, nm)
    return restored


def clear_recently_deleted(db_path: str) -> int:
    # empty the recently deleted recovery buffer. returns the number of entries removed. does
    # not touch the transactions table, it only discards the ability to restore the rows that
    # were already deleted.
    with conn(db_path) as c:
        cur = c.execute("DELETE FROM recently_deleted")
        return cur.rowcount


# danger

def wipe_all(db_path: str) -> None:
    # wipe transactions and merchant memory. categories and settings stay.
    with conn(db_path) as c:
        c.execute("DELETE FROM transactions")
        c.execute("DELETE FROM merchant_memory")
