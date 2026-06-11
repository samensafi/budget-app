# backup and startup export helpers. paths and tunables come from the small wrappers
# in app.py, which keep BACKUP_DIR, DB_PATH and the retention constants. no nicegui
# and no app state, so this imports cleanly with no cycle.
from __future__ import annotations

import io
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

import db


def prune_backups(backup_dir: Path, *, keep_recent: int, keep_days: int,
                  keep_weeks: int) -> None:
    # delete old snapshots but keep a generational spread. the newest keep_recent of
    # them, one per day for the last keep_days days, and one per week for the last
    # keep_weeks weeks. everything else goes so the folder never grows forever.
    snaps = sorted(backup_dir.glob("budget_*.db"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    now = datetime.now()
    keep: set[Path] = set(snaps[:keep_recent])
    seen_days: set[str] = set()
    seen_weeks: set[str] = set()
    for p in snaps:
        dt = datetime.fromtimestamp(p.stat().st_mtime)
        age_days = (now - dt).days
        if age_days < keep_days:
            # daily window, keep the newest snapshot per calendar day
            day_key = dt.strftime("%Y-%m-%d")
            if day_key not in seen_days:
                seen_days.add(day_key)
                keep.add(p)
        elif age_days < keep_weeks * 7:
            # older than that, keep the newest snapshot per ISO week
            week_key = dt.strftime("%G-W%V")
            if week_key not in seen_weeks:
                seen_weeks.add(week_key)
                keep.add(p)
    for p in snaps:
        if p not in keep:
            try:
                p.unlink()
            except OSError:
                pass


def prune_data_exports(export_dir: Path, *, keep: int) -> None:
    # only keep the newest exports of each type (csv and xlsx), delete older ones
    if not export_dir.exists():
        return
    for ext in ("csv", "xlsx"):
        files = sorted(export_dir.glob(f"transactions_*.{ext}"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files[keep:]:
            try:
                p.unlink()
            except OSError:
                pass


def text_only_cells(ws) -> None:
    # openpyxl stores any string starting with = as a live formula. these files hold
    # data, never formulas, so flip such cells back to plain text. without this a name
    # like =QUICKMART reads back empty and Excel would try to run it as a formula.
    for row in ws.iter_rows():
        for cell in row:
            if cell.data_type == "f" and isinstance(cell.value, str):
                cell.data_type = "s"


def startup_data_backup(db_path: str, export_dir: Path, *, keep: int) -> None:
    # on launch, write a readable copy of every transaction to export_dir as both a
    # transactions_<ts>.csv and .xlsx, then trim to the newest few. does nothing when
    # there's nothing to back up, and never raises into startup.
    try:
        rows = db.get_transactions(db_path)
        if not rows:
            return  # nothing to back up yet
        df = pd.DataFrame(rows)
        export_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        df.to_csv(export_dir / f"transactions_{ts}.csv", index=False)
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="Transactions", index=False)
            text_only_cells(writer.book["Transactions"])
        (export_dir / f"transactions_{ts}.xlsx").write_bytes(buf.getvalue())
        prune_data_exports(export_dir, keep=keep)
    except Exception as e:
        # best effort, log it and move on, never block startup
        print(f"[data-export] skipped: {e}", file=sys.stderr)
