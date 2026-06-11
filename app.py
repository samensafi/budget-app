# Budget app, NiceGUI version.
# launch by double-clicking budget-app/app/run.command, or from a terminal:
#   cd ~/budget-app/app && ../userdata/venv/bin/python app.py
# the browser opens automatically at http://localhost:8080.
from __future__ import annotations

import asyncio
import hashlib
import html
import io
import math
import os
import re
import subprocess
import sys
import threading
import traceback
import uuid
from datetime import date, datetime
from itertools import groupby
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from fastapi import Response
from nicegui import ui, app, events
from nicegui.elements.select import Select as _NGSelect

# Compiled-bytecode caches (__pycache__) for our local modules go to userdata/
# (outside the shareable app/ folder) so a transfer never carries them. Must run
# before importing db/ai/ui below. mirrors USERDATA_DIR, an explicit
# PYTHONPYCACHEPREFIX (e.g. set by run.command) takes precedence.
if not os.environ.get("PYTHONPYCACHEPREFIX"):
    sys.pycache_prefix = str(Path(__file__).resolve().parent.parent / "userdata" / "pycache")

import db
import ai
import ui as bb  # CSS + small helpers (same module, different namespace)
import backup    # budget.db snapshot + startup-export helpers (factored out of app.py)
import crashlog  # crash-log filter/scrub/write bodies (factored out of app.py)
# pure stdlib-only rules (amount and name checks, recurring-date math, vision-retry
# decision, time and category helpers) live in logic.py so they're unit-testable
# without the UI. re-imported into this namespace so existing _parse_amount() callers,
# and the test suite which references them as app.<name>, keep working unchanged.
from logic import (  # noqa: F401  re-exported for callers and tests
    _PREVIEW_PLACEHOLDER_NAMES,
    _parse_amount,
    _amount_is_zero,
    _preview_name_bad,
    _recurring_future_dates,
    _infer_series_frequency,
    _reanchored_date,
    _should_retry_with_vision,
    _relative_time_ago,
    _expense_category_names,
    _update_state,
    _banner_decision,
)


# crash log: a tiny, bounded diagnostic file that lives inside app/ so it travels in
# the zip when someone sends their copy back for debugging, unlike everything in
# userdata/ which never leaves their Mac. it records only real, unexpected errors so a
# crash that happened on another machine can be diagnosed here. design guarantees,
# verified by tests:
#   bounded forever: newest-first, only the last _CRASH_LOG_MAX entries kept, older
#   ones auto-pruned, so it can never grow into a mess after years of use.
#   signal not noise: expected or handled conditions (bad key, rate limit, clean
#   Ctrl+C quit, client disconnects) are filtered out, see _should_log_crash.
#   safe and invisible: never raises, never touches budget.db, never calls the API,
#   and the API key is scrubbed out before anything is written. purely additive, it
#   changes no app behavior.
_CRASH_LOG = Path(__file__).resolve().parent / "crash.log"   # = app/crash.log (created lazily)
_CRASH_LOG_MAX = 20            # keep only the newest N errors
_CRASH_ENTRY_MAX = 6000        # cap one entry's length (chars)
_CRASH_SEP = "\n" + ("=" * 72) + "\n"
_CRASH_HEADER = (
    "# Budget crash log, most recent first, newest 20 kept (older auto-pruned).\n"
    "# Real, unexpected errors only. Expected conditions (bad/again API key, rate\n"
    "# limit, clean quit, client disconnects) are filtered out as noise. No personal\n"
    "# data and no API key are stored here.\n"
    "# Claude: if you're asked to fix these, DELETE each fixed entry (or this whole\n"
    "# file once every error is resolved) after the fix is verified."
)


def _should_log_crash(exc_type, exc_value) -> bool:
    # filter: true only for real, unexpected errors (impl in crashlog.should_log_crash).
    # thin wrapper that injects ai.AIError so crashlog needs no import ai, avoids a cycle.
    return crashlog.should_log_crash(exc_type, exc_value, ai_error=ai.AIError)


def _scrub_secrets(text: str) -> str:
    # strip the API key from text before writing (impl in crashlog.scrub_secrets).
    # thin wrapper that reads the live key from state so it always reflects current settings.
    return crashlog.scrub_secrets(text, api_key=getattr(state, "api_key", None))


def _log_crash(exc_type, exc_value, exc_tb, *, context: str = "") -> None:
    # best-effort append of one real error to app/crash.log (impl in
    # crashlog.log_crash). thin wrapper so the test suite can keep patching _CRASH_LOG
    # and the tunables, it reads those module globals and passes them down.
    crashlog.log_crash(exc_type, exc_value, exc_tb, context=context,
                       crash_log=_CRASH_LOG, sep=_CRASH_SEP, header=_CRASH_HEADER,
                       max_entries=_CRASH_LOG_MAX, entry_max=_CRASH_ENTRY_MAX,
                       should_log=_should_log_crash, scrub=_scrub_secrets)


# Catch fatal/startup crashes (the case where the app dies and the terminal
# window closes, losing the traceback) and crashes in worker threads. Both chain
# to the previous hook so the on-screen/terminal behavior is unchanged.
_prev_excepthook = sys.excepthook
def _crash_excepthook(exc_type, exc_value, exc_tb):
    _log_crash(exc_type, exc_value, exc_tb, context="uncaught (fatal)")
    _prev_excepthook(exc_type, exc_value, exc_tb)
sys.excepthook = _crash_excepthook

_prev_threadhook = threading.excepthook
def _crash_threadhook(args):
    _log_crash(args.exc_type, args.exc_value, args.exc_traceback,
               context=f"thread {getattr(args.thread, 'name', '?')}")
    _prev_threadhook(args)
threading.excepthook = _crash_threadhook


# clean quit: Ctrl+C hard-exits via os._exit (see _install_fast_shutdown) so the app
# never hangs waiting on an in-flight upload. that hard exit skips multiprocessing's own
# cleanup, which makes its spawned resource_tracker helper print a scary-looking but
# completely harmless leaked-semaphore-objects line. that helper is a child process that
# reads PYTHONWARNINGS at its own startup, so appending a filter here silences only that
# one message in that one child, it does not change warnings in this process at all
# (this interpreter already read PYTHONWARNINGS when it launched).
_rt_filter = "ignore:::multiprocessing.resource_tracker"
os.environ["PYTHONWARNINGS"] = (
    f"{os.environ['PYTHONWARNINGS']},{_rt_filter}"
    if os.environ.get("PYTHONWARNINGS") else _rt_filter
)


# all shareable code lives in this app/ folder, per-user data lives in its sibling
# userdata/ folder (budget-app/userdata/). keeping userdata/ outside app/ is what makes
# sharing safe: zipping the app/ folder to send for debugging can never include this
# person's budget.db, backups/, or venv/. sending the code back and forth never touches
# their data.
#
# PROJECT_DIR is the parent of app/ (budget-app/), and userdata/ sits there.
#
# _migrate_legacy_layout() is a one-time self-healing move for installs that predate
# userdata/: it relocates an old project-root budget.db and .backups/ into userdata/.
# every move is guarded by a destination-does-not-exist check, so it can never overwrite
# data already in userdata/. safe to run on every launch and a harmless no-op once
# migrated (and on a brand-new install).
#
# this must run before db.init_db() below, otherwise init_db would create an empty
# userdata/budget.db, the guard would then see the destination exists, and the real data
# would be orphaned at the old location.
PROJECT_DIR = Path(__file__).resolve().parent.parent
APP_CODE_DIR = Path(__file__).resolve().parent   # = app/, the git checkout (for self-update)
USERDATA_DIR = PROJECT_DIR / "userdata"
USERDATA_DIR.mkdir(exist_ok=True)


def _migrate_legacy_layout() -> None:
    for src, dest in (
        (PROJECT_DIR / "budget.db", USERDATA_DIR / "budget.db"),
        (PROJECT_DIR / ".backups", USERDATA_DIR / "backups"),
    ):
        if src.exists() and not dest.exists():
            try:
                src.rename(dest)  # atomic on the same filesystem
            except OSError:
                import shutil  # different volume, fall back to copy then remove
                shutil.move(str(src), str(dest))


_migrate_legacy_layout()

DB_PATH = str(USERDATA_DIR / "budget.db")
BACKUP_DIR = USERDATA_DIR / "backups"

# set to 1 by Budget.app's launcher (budget-launcher.command) that wraps run.command.
# when on, the only change is that the app does not auto-open a browser (the launcher
# opens a fresh Safari window itself, and its watchdog fully stops this server the
# moment that window/tab is closed, so nothing lingers in the background). a normal
# double-click of run.command leaves this off, so plain browser/dev use is unchanged.
MANAGED_APP = bool(os.environ.get("BUDGET_APP_MANAGED"))

# the launcher (budget-launcher.command, baked into Budget.app) stamps its own version into
# BUDGET_LAUNCHER_VERSION. REQUIRED_LAUNCHER_VERSION is the lowest launcher this code works
# with: when the running launcher is older, a launcher change shipped that git can't apply (the
# launcher lives inside the .app), so the user must reinstall Budget.app. bump BOTH (here + the
# launcher's export) together, only for a reinstall-worthy change.
REQUIRED_LAUNCHER_VERSION = 1


def _reinstall_required() -> bool:
    # true ONLY when we can read a launcher version AND it's older than required. a missing or
    # unreadable value (a dev run via run.command, an old copy that doesn't stamp one) returns
    # False, so the reinstall banner can never appear by mistake.
    raw = os.environ.get("BUDGET_LAUNCHER_VERSION")
    if not raw:
        return False
    try:
        return int(raw) < REQUIRED_LAUNCHER_VERSION
    except (TypeError, ValueError):
        return False


# defensive monkey-patch: NiceGUI's Select._event_args_to_value crashes when Quasar
# emits an int or raw value instead of the expected {value, label} dict. this wrapper
# handles all the formats Quasar can emit (int, str, dict, list, None) so we never get
# the TypeError where an int is not subscriptable.

_ng_select_orig_to_value = _NGSelect._event_args_to_value

def _safe_event_args_to_value(self, e):
    args = getattr(e, "args", None)
    try:
        # Multi-select: list of either {value, label} dicts or raw values/strings.
        if self.multiple:
            if args is None:
                return []
            if not isinstance(args, list):
                args = [args]
            out = []
            for item in args:
                if isinstance(item, dict) and "value" in item:
                    idx = item["value"]
                    if isinstance(idx, int) and 0 <= idx < len(self._values):
                        out.append(self._values[idx])
                elif isinstance(item, int) and 0 <= item < len(self._values):
                    out.append(self._values[item])
                elif isinstance(item, str):
                    if item in self._values:
                        out.append(item)
                    elif self._props.get("new-value-mode"):
                        nv = self._handle_new_value(item)
                        if nv is not None:
                            out.append(nv)
            return out

        # Single-select.
        if args is None:
            return None
        if isinstance(args, dict) and "value" in args:
            idx = args["value"]
            if isinstance(idx, int) and 0 <= idx < len(self._values):
                return self._values[idx]
            return idx if idx in self._values else None
        if isinstance(args, int):
            if 0 <= args < len(self._values):
                return self._values[args]
            return None
        if isinstance(args, str):
            if args in self._values:
                return args
            if self._props.get("new-value-mode"):
                nv = self._handle_new_value(args)
                if nv in self._values:
                    return nv
            return None
        return None
    except Exception:
        # last-resort fallback, never let a select crash the app.
        return self.value

_NGSelect._event_args_to_value = _safe_event_args_to_value


# Initialise DB & run any pending migrations (e.g. clean merchant names).
db.init_db(DB_PATH)


# silence Safari/iOS auto-requests for touch icons, without these routes NiceGUI logs
# an apple-touch-icon.png not found line on every page load. 204 means no content, no noise.
@app.get("/apple-touch-icon.png")
@app.get("/apple-touch-icon-precomposed.png")
def _no_touch_icon():
    return Response(status_code=204)


# liveness probe for Budget.app's launcher watchdog, must stay a plain route, never
# the page. the launcher (budget-launcher.command) polls the server every ~2s to know
# it's still alive. if that poll hits the page route, it re-runs the @ui.page builder,
# which reassigns the module-level UI globals (containers/main_tabs/header_*) to a
# throwaway request that never opens a WebSocket, orphaning the real browser tab so it
# looks frozen (clicks reach the server and mutate the DB, but live updates land on the
# dead client, and only a manual refresh re-claims the globals, until the next poll 2s
# later). that exact bug is why the app froze only when launched via Budget.app
# and never via run.command, which has no watchdog. this endpoint gives the watchdog
# something to hit that does not run index(). keep the launcher's server_up() pointed
# here, not at the page route. (see CLAUDE.md item 8.)
@app.get("/healthz")
def _healthz():
    return Response(content="ok", media_type="text/plain")


# Meter estimated API spend: every Anthropic response reports its token usage, which we
# accumulate into the DB so the Settings tab can show how much the API has cost so far.
ai.set_usage_hook(lambda cost, tokens: db.add_api_usage(DB_PATH, cost=cost, tokens=tokens))

# load any saved Claude model override (Settings tab). falls back to ai.DEFAULT_MODEL so
# a fresh install just works, and you can swap models later without touching code.
ai.MODEL = db.get_setting(DB_PATH, "model", ai.DEFAULT_MODEL) or ai.DEFAULT_MODEL


# auto-backup: snapshots budget.db into .backups/ after every change.
# retention keeps a useful spread without growing forever (see _prune_backups).

_LAST_BACKUP_HASH: str | None = None

# Retention tiers. Total kept stays bounded (a few dozen files) no matter how
# long the app is used, while still letting the user recover from days or weeks
# ago, not just the very last save.
_BACKUP_KEEP_RECENT = 10   # the N newest snapshots, always (undo a recent mistake)
_BACKUP_KEEP_DAYS = 14     # plus the newest snapshot of each of the last 14 days
_BACKUP_KEEP_WEEKS = 12    # plus the newest snapshot of each of the last 12 weeks

# human-readable startup export: on every launch we also write a full copy of all
# transactions as both .csv and .xlsx into .backups/exports/, then keep only the newest
# few of each (a rolling window, a new launch past the limit deletes the oldest, so this
# never grows without bound). the binary .db snapshots above remain the complete restore
# source (they also include categories, learned stores and settings), and these exports
# are the open-anywhere convenience copy.
_DATA_EXPORT_DIR = BACKUP_DIR / "exports"
_DATA_EXPORT_KEEP = 5      # newest N csv + newest N xlsx startup exports retained


def _prune_backups() -> None:
    # prune old .db snapshots to a generational spread (impl in backup.prune_backups).
    # thin wrapper so the test suite can keep patching BACKUP_DIR and the retention tunables.
    backup.prune_backups(BACKUP_DIR, keep_recent=_BACKUP_KEEP_RECENT,
                         keep_days=_BACKUP_KEEP_DAYS, keep_weeks=_BACKUP_KEEP_WEEKS)


def _make_backup(force: bool = False) -> Path | None:
    # write a snapshot of budget.db to .backups/, then prune old ones.
    # skips writing when the DB is byte-identical to the last snapshot, unless force=True.
    # returns the snapshot path, or None if nothing was written. never raises into the caller.
    global _LAST_BACKUP_HASH
    try:
        BACKUP_DIR.mkdir(exist_ok=True)
        data = Path(DB_PATH).read_bytes()
        h = hashlib.md5(data).hexdigest()
        if h == _LAST_BACKUP_HASH and not force:
            return None  # no changes since last backup
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = BACKUP_DIR / f"budget_{ts}.db"
        dst.write_bytes(data)
        _LAST_BACKUP_HASH = h
        _prune_backups()
        return dst
    except Exception as e:
        # never let a backup failure interrupt the user, just log to stderr.
        print(f"[backup] skipped: {e}", file=sys.stderr)
        return None


def db_backup_if_changed() -> None:
    # snapshot budget.db after a write, only if its bytes changed
    _make_backup(force=False)


def _prune_data_exports() -> None:
    # rotate startup exports to the newest few (impl in backup.prune_data_exports).
    # thin wrapper so the test suite can keep patching _DATA_EXPORT_DIR and _DATA_EXPORT_KEEP.
    backup.prune_data_exports(_DATA_EXPORT_DIR, keep=_DATA_EXPORT_KEEP)


def _startup_data_backup() -> None:
    # write CSV and XLSX copies of all transactions on launch (impl in
    # backup.startup_data_backup). thin wrapper so the suite can keep patching the globals.
    backup.startup_data_backup(DB_PATH, _DATA_EXPORT_DIR, keep=_DATA_EXPORT_KEEP)


db_backup_if_changed()  # initial .db snapshot on startup
_startup_data_backup()  # human-readable CSV + Excel copy of all data on startup


# state, module-level since this is a single-user local app

class _State:
    def __init__(self):
        today = date.today()
        self.view_year: int = today.year
        self.view_month: int = today.month
        self.api_key: str = db.get_setting(DB_PATH, "api_key", "") or ""
        self.extracted_preview: list[dict] = []
        self.preview_categories: dict[int, str] = {}
        self.preview_merchants: dict[int, str] = {}
        self.preview_dates: dict[int, str] = {}
        self.preview_amounts: dict[int, float | None] = {}  # magnitude (always positive)
        self.preview_kinds: dict[int, str] = {}             # Income or Expense
        self.preview_duplicates: dict[int, bool] = {}
        # Home tab sort & filter (persist across month navigation).
        self.home_sort_key: str = "date"     # date or amount
        self.home_sort_dir: str = "desc"     # asc or desc
        self.home_filter_categories: list[str] = []   # empty = all categories
        self.home_filter_min: float | None = None      # min size (abs $), inclusive
        self.home_filter_max: float | None = None      # max size (abs $), inclusive
        self.insights_scope: str = "Monthly"
        self.insights_year: int | None = None       # Annual scope
        self.insights_month: int | None = None      # Monthly scope (1-12)
        self.insights_m_year: int | None = None      # Monthly scope year
        # offline latch, internal only, never shown in the UI. set when a call fails for a
        # persistent reason (rejected or missing key, out of credits) so api_available()
        # stays False and the app stays in offline mode until the key works again or is
        # re-saved. rate-limit and overloaded are transient and do not latch (see
        # _report_api_error). the header chip is a calm two-state marker and never renders
        # these strings, they're kept only to hold the latch and record the reason for
        # debugging. None means healthy.
        self.api_error: str | None = None            # latch reason (internal)
        self.api_error_detail: str | None = None     # latch detail (internal)

state = _State()

# last-known online/offline status, so we can spot the exact moment Claude becomes
# unavailable (green to yellow) and surface the dismissible Working offline warning once
# more. None until the first render, so app startup is never mistaken for a fresh
# transition, a warning dismissed in a previous run must not pop back up just because we
# restarted.
_prev_api_online: bool | None = None


# cached client

_client = None
_client_key = None

def get_client():
    global _client, _client_key
    if not state.api_key:
        return None
    if state.api_key != _client_key:
        _client = ai.make_client(state.api_key)
        _client_key = state.api_key
    return _client


def api_available() -> bool:
    # true when Claude can be used right now: a key is set and no blocking failure is
    # latched. this is the single switch for online vs offline. when it's False the app
    # stays fully usable for manual work (add, edit, categorize, Insights, exports,
    # backups), only the API-only bits (screenshot/PDF/text auto-extraction, and the
    # learned-category rescue that runs on extracted rows) pause. only genuine the-AI-is-
    # unavailable problems latch, a rejected key or running out of credits. transient
    # rate-limit or overload do not, so a momentary hiccup never strands the app offline.
    # the latch clears on the next successful call or when the key is re-saved in Settings,
    # so the app flips back to online on its own once the API is back.
    return bool(state.api_key) and not state.api_error


# convenience accessors

def cats() -> list[dict]:
    return db.get_categories(DB_PATH)

def cat_map() -> dict[str, dict]:
    return {c["name"]: c for c in cats()}

def cat_names() -> list[str]:
    return [c["name"] for c in cats()]

def merchant_memory_dict() -> dict[str, str]:
    return {m["normalized_merchant"]: m["category"]
            for m in db.get_merchant_memory(DB_PATH)}

def recent_corrections() -> list[dict]:
    mem = db.get_merchant_memory(DB_PATH)
    return [{"merchant": m["merchant_display"], "category": m["category"]}
            for m in mem[:25]]


# page

# refs to the per-tab containers so events can refresh them.
containers: dict[str, ui.element] = {}
# the single header API chip plus its tooltip. created once per page, then mutated in
# place on every status change (never cleared and recreated). recreating it brought a
# fresh QTooltip whose anchor could mount before the new chip's DOM node was committed
# during an async upload refresh, the source of the harmless Anchor target-not-found
# console warnings.
_api_chip: ui.element | None = None
_api_chip_tip: ui.element | None = None
# the single Estimated spend value label on the Settings tab. after an upload (which
# spends tokens) we update just this label in place instead of rebuilding the whole
# Settings panel, see _update_spend_meter for why (rebuilding it while the Settings tab
# is inactive recreated a tooltip in an unmounted panel, which logged harmless anchor
# warnings).
_spend_value_label: ui.element | None = None
header_review: ui.element | None = None
# slim status banner under the header. empty when the API is healthy, shows a calm
# working-offline manual-mode message when there's no key or a call has failed.
header_banner: ui.element | None = None
# Refs to the tab bar + Home tab so non-Home actions (e.g. clicking a search result)
# can switch back to Home and jump to a specific transaction.
main_tabs: ui.element | None = None
tab_home_ref: ui.element | None = None
tab_settings_ref: ui.element | None = None


def _handle_uncaught(exc: Exception) -> None:
    # app-wide safety net. NiceGUI funnels every uncaught exception, from event handlers,
    # timers, the outbox, and background tasks, through here, and because it is registered
    # per-client via ui.on_exception it runs inside the page's content slot, so ui.notify
    # is safe to call. the point: an unexpected error must never fail silently or look like
    # the app broke. we show one calm, sticky, manually-closeable message that reassures
    # the user their data is safe and tells them what to do next. the server itself keeps
    # running, NiceGUI catches the exception and the process lives. this is a backstop
    # only: every known or expected error (API key, billing, rate-limit, 529 overload,
    # per-file upload failures) is already handled with its own friendly message and never
    # reaches here.
    # record it for debugging (no-op for filtered or expected errors, never raises).
    _log_crash(type(exc), exc, exc.__traceback__, context="ui handler")
    label = type(exc).__name__
    detail = " ".join(str(exc).split())  # collapse newlines/extra spaces
    if len(detail) > 160:
        detail = detail[:160] + "..."
    tail = f"  ({label}: {detail})" if detail and detail != label else f"  ({label})"
    msg = ("⚠️ Something went wrong, but your data is safe and Budget is still running. "
           "Please try that again. If it keeps happening, quit Budget and start it again."
           + tail)
    try:
        ui.notify(msg, type="negative", timeout=0, close_button=True, multi_line=True)
    except Exception:
        # the safety net itself must never raise. NiceGUI's default global handler has
        # already logged the original error, so quietly swallow any display failure.
        pass


def notify(message, *, group=None, **kwargs):
    # thin wrapper over ui.notify that adds an optional Quasar group. passing a stable
    # group makes rapid, repeated actions (deleting rows one by one, queuing files,
    # per-file upload warnings) collapse into a single toast with a count badge instead
    # of stacking up the screen and burying the page's buttons. everything else (type,
    # color, timeout, etc.) passes straight through to ui.notify. the default toast
    # lifetime is shortened app-wide via Quasar.Notify defaults (see the page's <script>).
    if group is not None:
        kwargs["group"] = group
    ui.notify(message, **kwargs)


@ui.page("/")
def index():
    ui.add_head_html(bb.CSS)
    # App-wide safety net: any uncaught error on this page (event handlers, timers, the
    # outbox, background tasks) is surfaced as one calm, sticky, manually-closeable
    # message instead of failing silently or looking like a crash. See _handle_uncaught.
    ui.on_exception(_handle_uncaught)
    # make the whole upload box open the file browser, not just the + button. one
    # delegated listener at the document level so it survives the upload tab's re-renders,
    # it just forwards the click to Quasar's own hidden file <input>.
    ui.add_body_html("""
<script>
(function () {
  if (window.__bbUploadClickWired) return;
  window.__bbUploadClickWired = true;
  document.addEventListener('click', function (e) {
    const box = e.target.closest('.bb-upload');
    if (!box) return;
    // Only the native add/upload/clear buttons (and the per-file remove button,
    // which Quasar renders as <a class="q-btn">) keep their own behavior. Every
    // other spot in the box, including the empty list/drop area below the label,
    // opens the file browser.
    if (e.target.closest('button, .q-btn')) return;
    const input = box.querySelector('input[type=file]');
    if (input) input.click();
  }, true);
})();
(function () {
  // Tapping the Home tab while it's ALREADY active resets the view to the current
  // month. Capture phase runs before Quasar flips the active class, so the check
  // reflects the pre-click state: active => re-click (reset); inactive => a normal
  // switch from another tab (do nothing, keep the month the user was viewing).
  if (window.__bbHomeReclickWired) return;
  window.__bbHomeReclickWired = true;
  document.addEventListener('click', function (e) {
    const tab = e.target.closest('.bb-home-tab');
    if (tab && tab.classList.contains('q-tab--active')) {
      emitEvent('bb_home_reclick');
    }
  }, true);
})();
(function () {
  // Notifications clear faster app-wide: a shorter default lifetime so routine toasts
  // (saved / deleted / queued) don't linger and stack. Toasts that pass their own
  // timeout (e.g. sticky error messages with timeout:0) are unaffected. Pile-up from
  // rapid repeated actions is handled separately by grouping them (see notify(group=...)).
  function applyNotifyDefaults() {
    if (window.Quasar && Quasar.Notify) {
      Quasar.Notify.setDefaults({ timeout: 2500 });
    } else {
      setTimeout(applyNotifyDefaults, 50);
    }
  }
  applyNotifyDefaults();
})();
</script>
""")

    # single-window guard. budget keeps its on-screen pieces in shared
    # module-level variables (containers/main_tabs/header_*), so it must run in
    # one browser tab at a time. tabs coordinate directly in the browser via a
    # BroadcastChannel, no polling timer and no server round-trip.
    # a newly opened tab announces hello. if a healthy tab answers here, the new
    # tab knows it's a duplicate and blocks itself with a full-screen overlay
    # (you close it or use the original window).
    # the healthy tab that answered also refreshes itself once to reclaim a clean
    # state. opening the 2nd tab had already overwritten the shared pieces above,
    # and the refresh undoes that.
    # refresh-safe: reloading a single tab destroys its old context, so nothing
    # answers here and you're never falsely blocked. same-browser only. if
    # BroadcastChannel is unavailable the app simply runs without the guard.
    ui.add_body_html("""
<style>
#bb-tab-block {
  position: fixed; inset: 0; z-index: 99999;
  display: flex; align-items: center; justify-content: center;
  background: rgba(18, 18, 18, 0.96); backdrop-filter: blur(2px);
  padding: 24px;
}
#bb-tab-block .bb-tab-block-card {
  max-width: 420px; text-align: center;
  background: #2c2e34; border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 14px; padding: 32px 34px;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.5);
}
#bb-tab-block .bb-tab-block-icon { font-size: 42px; line-height: 1; }
#bb-tab-block .bb-tab-block-title {
  margin-top: 14px; font-size: 1.2rem; font-weight: 600; color: #fff;
}
#bb-tab-block .bb-tab-block-msg {
  margin-top: 10px; font-size: 0.9rem; line-height: 1.5;
  color: rgba(255, 255, 255, 0.7);
}
#bb-tab-block .bb-tab-block-btn {
  margin-top: 20px; padding: 8px 22px;
  font-size: 0.9rem; font-weight: 600; color: #fff;
  background: #5898d4; border: none; border-radius: 8px; cursor: pointer;
}
#bb-tab-block .bb-tab-block-btn:hover { background: #6aa6dc; }
</style>
<script>
(function () {
  if (window.__bbTabGuard || !('BroadcastChannel' in window)) return;
  window.__bbTabGuard = true;
  var ch = new BroadcastChannel('bb_single_tab');
  var myId = Math.random().toString(36).slice(2) + '.' + Date.now();
  var blocked = false;
  var claimed = false;

  function block() {
    if (blocked || document.getElementById('bb-tab-block')) return;
    blocked = true;
    var o = document.createElement('div');
    o.id = 'bb-tab-block';
    o.innerHTML =
      '<div class="bb-tab-block-card">' +
        '<div class="bb-tab-block-icon">\\uD83D\\uDD12</div>' +
        '<div class="bb-tab-block-title">Budget is already open</div>' +
        '<div class="bb-tab-block-msg">Budget is already running in another tab ' +
        'or window. To keep your data safe it runs in one place at a time. ' +
        'Please switch to that window. If you have already closed it, reload to ' +
        'use Budget here.</div>' +
        '<button type="button" class="bb-tab-block-btn">Reload</button>' +
      '</div>';
    (document.body || document.documentElement).appendChild(o);
    var btn = o.querySelector('.bb-tab-block-btn');
    if (btn) btn.addEventListener('click', function () { location.reload(); });
  }

  function reclaimOnce() {
    // Opening the duplicate already overwrote this tab's shared state on the
    // server, so refresh once to rebuild it cleanly. A short throttle (kept in
    // sessionStorage, which survives the reload) prevents any reload loop if two
    // brand-new tabs happen to open at the very same instant.
    var now = Date.now();
    try {
      var last = parseInt(sessionStorage.getItem('bb_last_reclaim') || '0', 10);
      if (now - last < 2000) return;
      sessionStorage.setItem('bb_last_reclaim', String(now));
    } catch (_) {}
    setTimeout(function () { location.reload(); }, 80);
  }

  ch.onmessage = function (e) {
    var m = e.data || {};
    if (!m || m.id === myId) return;            // ignore my own messages
    if (m.t === 'hello' && !blocked) {
      // A new tab just opened. Claim ownership so it blocks, then refresh myself
      // once to recover a clean shared state.
      ch.postMessage({ t: 'here', id: myId });
      claimed = true;
      reclaimOnce();
    } else if (m.t === 'here' && !claimed) {
      block();                                  // a healthy tab already exists
    }
  };

  ch.postMessage({ t: 'hello', id: myId });     // announce myself on load
})();
</script>
""")

    # top header: tabs on the left, API status chip on the right, single clear bar.
    with ui.header(elevated=False).classes("bb-header"):
        with ui.row().classes("items-center w-full no-wrap").style("gap: 0;"):
            with ui.tabs().props("dense indicator-color=primary inline-label") as tabs:
                tab_home = ui.tab("🏠 Home").classes("bb-home-tab")
                tab_insights = ui.tab("📊 Insights")
                tab_upload = ui.tab("📤 Upload")
                tab_categories = ui.tab("🏷️ Categories")
                tab_settings = ui.tab("⚙️ Settings")
            global main_tabs, tab_home_ref, tab_settings_ref
            main_tabs, tab_home_ref, tab_settings_ref = tabs, tab_home, tab_settings
            # Re-clicking the already-active Home tab jumps back to the current month.
            # The body script (below) only emits when Home is already active, so a normal
            # tab switch from elsewhere preserves the month you were viewing.
            ui.on("bb_home_reclick", lambda e: _go_to_current_month())
            ui.space()  # pushes the indicators to the right
            global header_review
            review_holder = ui.row().classes("items-center")
            with review_holder:
                _render_review_button()
            header_review = review_holder
            with ui.row().classes("items-center"):
                _render_api_chip()   # creates the single chip (refs stored in _api_chip)

    # slim status banner directly under the header, only shows when offline/manual.
    # the header is fixed (position:fixed, ~57px tall = .bb-header min-height 56px + 1px
    # border) and .q-page-container's top padding is zeroed (ui.py), so the very top of the
    # page content sits under the header. each tab's body clears it via its own top padding,
    # but this banner is the first element in the content flow, so without explicit top room
    # its title tucks behind the header. padding-top here is >= the header height so the
    # banner always clears it regardless of any ambient padding. _refresh_banner hides the
    # whole holder when online or when the warning was dismissed (set_visibility False means
    # display:none means no leftover gap).
    global header_banner
    banner_holder = ui.column().classes("w-full bb-container") \
        .style("gap: 0; padding-top: 64px; padding-bottom: 0;")
    header_banner = banner_holder
    _refresh_banner()

    with ui.tab_panels(tabs, value=tab_home).classes("w-full bb-container"):
        with ui.tab_panel(tab_home):
            containers["home"] = ui.column().classes("w-full")
            refresh_home()
        with ui.tab_panel(tab_insights):
            containers["insights"] = ui.column().classes("w-full")
            refresh_insights()
        with ui.tab_panel(tab_upload):
            containers["upload"] = ui.column().classes("w-full")
            refresh_upload()
        with ui.tab_panel(tab_categories):
            containers["categories"] = ui.column().classes("w-full")
            refresh_categories()
        with ui.tab_panel(tab_settings):
            containers["settings"] = ui.column().classes("w-full")
            refresh_settings()

    # the startup update check runs in the background (a network fetch). poll briefly so the
    # Update available banner surfaces as soon as it finishes, however long the fetch takes,
    # then stop the moment it's done so it never refreshes forever. the reinstall and offline
    # banners don't need this (they're synchronous), and Settings -> Check for updates is the
    # reliable on-demand path.
    _poll = {"timer": None}
    def _poll_for_update():
        _refresh_banner()
        if _update_check_done and _poll["timer"] is not None:
            _poll["timer"].active = False
    _poll["timer"] = ui.timer(2.0, _poll_for_update)


def _render_api_chip():
    # two calm states, both clickable, they go to Settings. online is a green API Ready
    # chip, offline (no key, key removed, or a call failed) is a yellow Offline mode chip.
    # no red and no raw error text, the one-time dismissible banner explains it and this
    # stays a quiet marker. built once per page here, status changes then call
    # _update_api_chip() to change its look in place. we never clear and recreate it: a
    # fresh chip brings a fresh QTooltip whose anchor could mount before the chip's DOM node
    # is committed during an async upload refresh, which logged the harmless but noisy
    # Anchor target not found console errors.
    global _api_chip, _api_chip_tip
    _api_chip = ui.chip("", on_click=_go_to_api_settings).props("outline") \
        .classes("cursor-pointer")
    with _api_chip:
        _api_chip_tip = ui.tooltip("")   # the real Tooltip element, not chip.tooltip(),
                                         # which returns the chip itself for chaining
    _update_api_chip()


def _update_api_chip():
    # point the single header chip at the current online/offline state, mutating it in
    # place (text, icon, color, tooltip). safe to call from a background task, it only
    # patches an existing element, never creates or deletes one, so there's no tooltip
    # remount to race the DOM. no-op if the chip hasn't been built yet.
    if _api_chip is None:
        return
    if api_available():
        _api_chip.set_text("API Ready")
        _api_chip.props("color=positive icon=check_circle")
        if _api_chip_tip is not None:
            _api_chip_tip.set_text("Connected to the AI, uploads can be read automatically.")
    else:
        _api_chip.set_text("Offline mode")
        _api_chip.props("color=warning icon=cloud_off")
        if _api_chip_tip is not None:
            _api_chip_tip.set_text("Working offline. Uploads are paused. Tap to open Settings.")


def _go_to_api_settings():
    # header API chip click switches to the Settings tab and briefly flashes the API-key
    # field (it's the first section there) so it's clear where the user landed. the flash
    # is cosmetic, so any failure is swallowed.
    if main_tabs is not None and tab_settings_ref is not None:
        main_tabs.set_value(tab_settings_ref)
    js = """
    (function() {
      let tries = 0;
      const go = () => {
        const el = document.querySelector('.bb-api-key');
        if (el) {
          el.scrollIntoView({behavior: 'smooth', block: 'center'});
          el.classList.add('bb-flash-field');
          setTimeout(() => el.classList.remove('bb-flash-field'), 1800);
        } else if (tries++ < 25) {
          setTimeout(go, 60);
        }
      };
      go();
    })();
    """
    try:
        ui.run_javascript(js)
    except Exception:
        pass


def _render_status_banner():
    # the one calm amber strip under the header, shown once each time Claude becomes
    # unavailable (see _refresh_banner) until the user dismisses it. same friendly message
    # for every offline reason (no key, key removed, out of credits), it just says reading
    # uploads is paused and that manual entry still works. it never blocks anything.
    accent = "var(--bb-warning)"
    with ui.row().classes("w-full items-center no-wrap").style(
        "gap: 10px; margin: 2px 0 8px; padding: 9px 14px; border-radius: 10px; "
        f"background: color-mix(in srgb, {accent} 12%, var(--bb-surface)); "
        f"border: 1px solid color-mix(in srgb, {accent} 38%, var(--bb-border));"
    ):
        ui.icon("cloud_off").style(f"font-size: 1.2rem; color: {accent}; flex-shrink: 0;")
        with ui.column().style("gap: 1px; min-width: 0;").classes("flex-grow"):
            ui.label("Working offline").style(
                f"font-weight: 700; font-size: 0.82rem; color: {accent};")
            ui.label("The AI API isn't available right now, so reading uploads is paused. You "
                     "can still add transactions by hand from the Home tab. Upload turns "
                     "back on by itself once the AI API is available again.") \
                .classes("text-caption text-muted") \
                .style("line-height: 1.3; white-space: normal;")
        ui.button("Settings", icon="settings", on_click=_go_to_api_settings) \
            .props("flat dense").style("flex-shrink: 0; color: var(--bb-text-muted);")
        ui.button(icon="close", on_click=_dismiss_offline_banner) \
            .props("flat dense round").style("flex-shrink: 0; color: var(--bb-text-muted);") \
            .tooltip("Dismiss")


def _open_releases_page():
    # open the GitHub releases page in a new tab so the user can download the latest Budget.
    # the running app stays open. any failure is swallowed, it's a convenience link.
    try:
        ui.run_javascript(
            "window.open('https://github.com/samensafi/budget-app/releases/latest', '_blank')")
    except Exception:
        pass


def _render_reinstall_banner():
    # an amber strip under the header for the one case git can't fix: a launcher change that
    # needs the user to reinstall Budget.app (see _reinstall_required). Download opens the
    # GitHub releases page; the user downloads the latest and drags it into Applications, their
    # data (in userdata/, outside the app) is untouched. the x dismisses it for this session
    # only, it returns on the next launch because the reinstall still matters, until the new
    # launcher stamps a current version and _reinstall_required() goes False on its own.
    accent = "var(--bb-warning)"
    with ui.row().classes("w-full items-center no-wrap").style(
        "gap: 10px; margin: 2px 0 8px; padding: 9px 14px; border-radius: 10px; "
        f"background: color-mix(in srgb, {accent} 12%, var(--bb-surface)); "
        f"border: 1px solid color-mix(in srgb, {accent} 38%, var(--bb-border));"
    ):
        ui.icon("system_update_alt").style(
            f"font-size: 1.2rem; color: {accent}; flex-shrink: 0;")
        with ui.column().style("gap: 1px; min-width: 0;").classes("flex-grow"):
            ui.label("Reinstall required").style(
                f"font-weight: 700; font-size: 0.82rem; color: {accent};")
            ui.label("This version of Budget needs to be replaced. Download the latest from "
                     "GitHub and drag it into your Applications folder. Your data is safe and "
                     "stays on your Mac.") \
                .classes("text-caption text-muted") \
                .style("line-height: 1.3; white-space: normal;")
        ui.button("Download", icon="download", on_click=_open_releases_page) \
            .props("unelevated dense color=warning text-color=dark").style("flex-shrink: 0;")
        ui.button(icon="close", on_click=_dismiss_reinstall_banner) \
            .props("flat dense round").style("flex-shrink: 0; color: var(--bb-text-muted);") \
            .tooltip("Dismiss")


def _render_update_banner():
    # a calm blue strip under the header announcing a ready in-app update. shown by
    # _refresh_banner when the startup check or a manual Settings check finds a newer version
    # on github and the user hasn't dismissed it. Update now installs it in place (a code-only
    # git pull) then asks the user to quit and reopen, since the app does not hot-reload. the
    # x dismisses it for this session (the next launch re-checks). the installing/installed
    # states mutate this banner's own elements in place, never clearing the holder mid-click.
    accent = "var(--bb-accent)"
    with ui.row().classes("w-full items-center no-wrap").style(
        "gap: 10px; margin: 2px 0 8px; padding: 9px 14px; border-radius: 10px; "
        f"background: color-mix(in srgb, {accent} 12%, var(--bb-surface)); "
        f"border: 1px solid color-mix(in srgb, {accent} 38%, var(--bb-border));"
    ):
        icon = ui.icon("system_update_alt").style(
            f"font-size: 1.2rem; color: {accent}; flex-shrink: 0;")
        with ui.column().style("gap: 1px; min-width: 0;").classes("flex-grow"):
            title = ui.label("Update available").style(
                f"font-weight: 700; font-size: 0.82rem; color: {accent};")
            msg = ui.label("A new version of Budget is ready to install.") \
                .classes("text-caption text-muted") \
                .style("line-height: 1.3; white-space: normal;")
        install_btn = ui.button("Update now", icon="download") \
            .props("unelevated dense color=primary").style("flex-shrink: 0;")
        close_btn = ui.button(icon="close", on_click=_dismiss_update_banner) \
            .props("flat dense round").style("flex-shrink: 0; color: var(--bb-text-muted);") \
            .tooltip("Dismiss")

    async def do_install():
        global _update_available
        install_btn.disable()
        close_btn.set_visibility(False)
        msg.set_text("Installing the update...")
        ok, m = await asyncio.to_thread(apply_update)
        if ok:
            _update_available = False   # set first so a concurrent refresh can't re-show it
        # the banner may have been re-rendered while the off-thread pull ran; updating now-
        # detached elements is harmless, so guard it and never crash the click.
        try:
            msg.set_text(m)
            if ok:
                title.set_text("Update installed")
                icon.props("name=check_circle")
                install_btn.set_visibility(False)
                close_btn.set_visibility(True)   # let the user dismiss the installed notice
            else:
                install_btn.enable()
                close_btn.set_visibility(True)
        except Exception:
            pass
    install_btn.on_click(do_install)


def _refresh_banner():
    # show or hide the two header strips: the update banner (a newer version is available and
    # not dismissed) and the offline warning (we're offline and the episode wasn't dismissed).
    # both share the one fixed-position holder so whichever shows clears the header. we also
    # watch the online to offline edge: the moment Claude goes from available to unavailable
    # we clear the saved offline-dismissed flag so the warning surfaces once for the new
    # episode. startup (prev is None) is never that edge, so a dismissed warning can't pop
    # back up just from a restart. hiding uses set_visibility (display:none) so the padding
    # leaves no gap. callers from inside a banner button defer this via a tiny timer so it
    # never clears its own slot mid-click.
    global _prev_api_online
    if header_banner is None:
        return
    online = api_available()
    if _prev_api_online is True and not online:
        _set_offline_warning_dismissed(False)   # fresh offline episode, warn once more
    _prev_api_online = online
    show_reinstall, show_update, show_offline = _banner_decision(
        reinstall_required=_reinstall_required(),
        reinstall_dismissed=_reinstall_banner_dismissed,
        update_available=bool(_update_available),
        update_dismissed=_update_banner_dismissed,
        offline=(not online),
        offline_dismissed=_offline_warning_dismissed(),
    )
    header_banner.clear()
    header_banner.set_visibility(show_reinstall or show_update or show_offline)
    if show_reinstall or show_update or show_offline:
        with header_banner:
            if show_reinstall:
                _render_reinstall_banner()
            elif show_update:
                _render_update_banner()
            if show_offline:
                _render_status_banner()


def _dismiss_offline_banner():
    # hide the Working offline warning. the yellow Offline mode chip stays, so the user
    # still sees the app is offline, they've just acknowledged the heads-up. the choice is
    # saved, so it does not reappear after a restart, it surfaces again only the next time
    # Claude goes from available to unavailable. the re-render is deferred via a tiny timer
    # so it never clears its own slot mid-click, and an update banner that's also showing
    # stays put.
    _set_offline_warning_dismissed(True)
    ui.timer(0.05, _refresh_banner, once=True)


def _dismiss_update_banner():
    # hide the update banner for this session (the next launch re-checks). deferred re-render
    # for the same reason: never clear our own slot mid-click, and keep an offline banner that
    # is also showing.
    global _update_banner_dismissed
    _update_banner_dismissed = True
    ui.timer(0.05, _refresh_banner, once=True)


def _dismiss_reinstall_banner():
    # hide the reinstall banner for THIS session only. it deliberately returns on the next
    # launch (the reinstall still needs doing) until a current launcher version clears it. the
    # re-render is deferred so it never clears its own slot mid-click and keeps other banners.
    global _reinstall_banner_dismissed
    _reinstall_banner_dismissed = True
    ui.timer(0.05, _refresh_banner, once=True)


def _offline_warning_dismissed() -> bool:
    # true if the user has dismissed the current offline warning. persisted in settings so
    # it survives a restart. any read hiccup is swallowed and treated as not dismissed.
    try:
        return db.get_setting(DB_PATH, "offline_warning_dismissed", "0") == "1"
    except Exception:
        return False


def _set_offline_warning_dismissed(value: bool):
    # persist the offline-warning dismissed flag. never raises, a settings-write hiccup
    # must not break the UI.
    try:
        db.set_setting(DB_PATH, "offline_warning_dismissed", "1" if value else "0")
    except Exception:
        pass


def _set_api_health(label: str | None, detail: str | None = None, *, refresh: bool = True):
    # record (or clear, with label=None) the current API failure so the header chip and
    # tooltip reflect it. safe to call from the main event loop only.
    changed = (label != state.api_error) or (detail != state.api_error_detail)
    state.api_error = label
    state.api_error_detail = detail
    if refresh and changed:
        _update_api_chip()   # mutate the existing chip in place (no clear/recreate)
        # Banner lives in its own container, so this is safe even mid-extraction.
        _refresh_banner()


def _report_api_error(exc: Exception, *, context: str = "") -> bool:
    # handle an AI failure during an upload. returns True if the batch should stop.
    # only genuine the AI is unavailable problems flip the app to offline mode (latch the
    # chip and surface the dismissible banner): a rejected or missing key, and running out
    # of credits. the banner is the calm, user-facing explanation, so there's no scary red
    # toast for these. rate-limit and overloaded are transient (ai.py already retried them),
    # they stop this batch with a brief heads-up but do not flip offline, so a momentary
    # hiccup never strands the app offline, the user just uploads again. a one-off request
    # error is non-fatal, other files keep going.
    # safe to call from a background task only within a with <ui_client>: block, it may
    # create UI (ui.notify), which needs a live slot context.
    prefix = f"{context}: " if context else ""
    if isinstance(exc, ai.APIKeyError):
        _set_api_health("API key rejected", str(exc))    # offline mode, banner explains it
        return True
    if isinstance(exc, ai.BillingError):
        _set_api_health("Out of API credits", str(exc))  # go offline, banner explains it
        return True
    if isinstance(exc, (ai.RateLimitError, ai.OverloadedError)):
        # transient, stay online so we don't get stuck offline over a brief hiccup.
        ui.notify("The AI is busy right now. Please try your upload again in a moment.",
                  type="warning", timeout=6000, close_button=True)
        return True
    # generic or transient (connection blip, odd request), surface it but keep going on the
    # other files, and don't flip offline (it's not a persistent account problem).
    ui.notify(f"{prefix}Couldn't read that one: {exc}",
              type="warning", timeout=6000, close_button=True)
    return False


def _render_review_button():
    # flag icon in the header that opens the flagged-transactions drawer. a floating
    # count badge appears only when something is flagged, no badge means all clear.
    n = db.count_flagged(DB_PATH)
    btn = ui.button(icon="flag" if n else "outlined_flag", on_click=open_review_drawer) \
        .props("flat round dense")
    if n:
        btn.props("color=warning")
        btn.tooltip(f"{n} flagged transaction" + ("" if n == 1 else "s") + " to review")
        with btn:
            ui.badge(str(n)).props("floating color=red")
    else:
        btn.tooltip("No flagged transactions")


def _refresh_header():
    if header_review is not None:
        header_review.clear()
        with header_review:
            _render_review_button()
    _update_api_chip()   # chip mutates in place, no clear/recreate
    _refresh_banner()


def refresh_all():
    # re-render every data-dependent tab so the whole app stays in sync after any add,
    # edit, delete, wipe, or category change. the Upload preview keeps its own in-progress
    # state and is refreshed separately by its own actions.
    refresh_home()
    refresh_insights()
    refresh_categories()
    refresh_settings()
    _refresh_header()


# home tab

def refresh_home():
    c = containers.get("home")
    if c is None:
        return
    c.clear()
    with c:
        _render_home_inner()


def _render_home_inner():
    summary = db.get_monthly_summary(DB_PATH, state.view_year, state.view_month)

    # month navigation: arrows flanking a perfectly centered title block.
    # use a 3-column grid so the title is mathematically centered regardless of side widths.
    with ui.grid(columns="48px 1fr 48px").classes("w-full items-center") \
            .style("margin: 12px 0 8px; gap: 0;"):
        ui.button(icon="chevron_left", on_click=_prev_month) \
            .props("flat round dense").classes("justify-self-start")
        with ui.column().classes("items-center justify-self-center").style("gap: 2px;"):
            ui.label(f"{bb.MONTH_NAMES[state.view_month - 1]} {state.view_year}") \
                .classes("text-h5").style("font-weight: 600; letter-spacing: -0.01em;")
            ui.label(f"{summary['count']} Transaction"
                     + ("" if summary["count"] == 1 else "s")) \
                .classes("text-caption text-muted")
        ui.button(icon="chevron_right", on_click=_next_month) \
            .props("flat round dense").classes("justify-self-end")

    # KPI cards (3-wide). Balance is green when >= 0, red when negative. Color follows
    # the rounded number on the card, so float drift can never paint a red $0.
    balance_cls = "income" if round(summary["net"]) >= 0 else "expense"
    with ui.row().classes("w-full gap-3 no-wrap").style("margin-top: 12px;"):
        for label, value, cls in [
            ("Income",   bb.money(summary["income"], rounded=True),   "income"),
            ("Expenses", bb.money(summary["expenses"], rounded=True), "expense"),
            ("Balance",  bb.money(summary["net"], signed=True, rounded=True), balance_cls),
        ]:
            with ui.card().classes("bb-kpi flex-grow"):
                ui.label(label).classes("kpi-label")
                ui.label(value).classes(f"kpi-value {cls}")

    # action row: Add, Search, Sort and filter
    with ui.row().classes("w-full items-center gap-2").style("margin: 14px 0 6px;"):
        ui.button("Add transaction", icon="add",
                  on_click=open_add_dialog).props("color=primary unelevated")
        ui.button("Search", icon="search",
                  on_click=open_search_drawer).props("flat")
        filt_btn = ui.button("Sort & filter", icon="tune",
                             on_click=open_filter_drawer).props("flat")
        nfilt = _home_active_filter_count()
        if nfilt:
            with filt_btn:
                ui.badge(str(nfilt)).props("floating color=primary")

    # transactions list: pull the month, then apply the user's filters and sort.
    all_month = db.get_transactions(
        DB_PATH,
        year=state.view_year,
        month=state.view_month,
        include_excluded=True,
    )
    if not all_month:
        with ui.card().classes("w-full bb-empty"):
            ui.label("🪶").classes("emoji")
            ui.label("No transactions yet for this month").classes("title")
            ui.label("Use the Upload tab to upload your transactions, or add one manually above.").classes("msg")
        return

    transactions = _apply_home_filters(all_month)
    if not transactions:
        with ui.card().classes("w-full bb-empty"):
            ui.label("🔍").classes("emoji")
            ui.label("No transactions match your filters").classes("title")
            ui.label(f"{len(all_month)} this month are hidden by the current filters.").classes("msg")
            ui.button("Clear filters", icon="filter_alt_off",
                      on_click=_clear_home_filters).props("flat color=primary") \
                .style("margin-top: 10px;")
        return

    cmap = cat_map()
    reverse = (state.home_sort_dir == "desc")
    if state.home_sort_key == "amount":
        # flat list ordered by transaction size, show each row's date for context.
        transactions.sort(key=lambda t: (abs(t["amount"]), t["date"], t["id"]), reverse=reverse)
        with ui.column().classes("w-full gap-1").style("margin-top: 6px;"):
            for t in transactions:
                _render_transaction_row(t, cmap, show_date=True)
    else:
        # Grouped by day, days ordered by the chosen direction.
        transactions.sort(key=lambda t: (t["date"], t["id"]), reverse=reverse)
        for day, day_txns in groupby(transactions, key=lambda t: t["date"]):
            day_list = list(day_txns)
            ui.label(bb.date_header(day)).classes("bb-date-header")
            with ui.column().classes("w-full gap-1"):
                for t in day_list:
                    _render_transaction_row(t, cmap)


def _home_active_filter_count() -> int:
    # how many Home filters are active (sort isn't counted, it's not a filter).
    n = 0
    if state.home_filter_categories:
        n += 1
    if state.home_filter_min is not None or state.home_filter_max is not None:
        n += 1
    return n


def _apply_home_filters(txns: list[dict]) -> list[dict]:
    # filter a month's transactions by the active category and amount-size filters.
    out = txns
    if state.home_filter_categories:
        sel = set(state.home_filter_categories)
        out = [t for t in out if t["category"] in sel]
    lo, hi = state.home_filter_min, state.home_filter_max
    if lo is not None:
        out = [t for t in out if abs(t["amount"]) >= lo]
    if hi is not None:
        out = [t for t in out if abs(t["amount"]) <= hi]
    return out


def _clear_home_filters():
    state.home_filter_categories = []
    state.home_filter_min = None
    state.home_filter_max = None
    refresh_home()


def open_filter_drawer():
    # Map the two sort axes to one friendly dropdown (key, direction).
    sort_options = {
        "date_desc": "Newest first",
        "date_asc": "Oldest first",
        "amount_desc": "Largest amount first",
        "amount_asc": "Smallest amount first",
    }
    cur_sort = f"{state.home_sort_key}_{state.home_sort_dir}"

    with ui.dialog().props("position=top maximized=false") as drawer:
        with ui.card().style("width: 560px; max-width: 92vw; padding: 18px; gap: 12px;"):
            ui.label("Sort & filter").classes("text-h6").style("margin-bottom: 2px;")

            sort_sel = ui.select(sort_options, value=cur_sort, label="Sort by") \
                .props("outlined dense").classes("w-full")

            ui.separator().style("margin: 4px 0;")
            ui.label("Filters").classes("text-subtitle2")

            cat_sel = ui.select(
                cat_names(), value=list(state.home_filter_categories),
                label="Categories", multiple=True, clearable=True,
            ).props("outlined dense use-chips").classes("w-full")

            ui.label("Amount (by size, ignoring +/−)").classes("text-caption text-muted") \
                .style("margin-top: 4px;")
            with ui.row().classes("w-full gap-3 no-wrap"):
                min_in = ui.number(label="At least", value=state.home_filter_min,
                                   min=0, step=1, prefix="$") \
                    .props("outlined dense clearable").classes("flex-grow")
                max_in = ui.number(label="At most", value=state.home_filter_max,
                                   min=0, step=1, prefix="$") \
                    .props("outlined dense clearable").classes("flex-grow")

            def apply_and_close():
                key, _, direction = sort_sel.value.partition("_")
                state.home_sort_key = key
                state.home_sort_dir = direction
                state.home_filter_categories = list(cat_sel.value or [])
                mn = min_in.value
                mx = max_in.value
                state.home_filter_min = float(mn) if mn not in (None, "") else None
                state.home_filter_max = float(mx) if mx not in (None, "") else None
                drawer.close()
                refresh_home()

            def reset_all():
                sort_sel.value = "date_desc"
                cat_sel.value = []
                min_in.value = None
                max_in.value = None

            with ui.row().classes("w-full items-center justify-between").style("margin-top: 8px;"):
                ui.button("Reset", icon="filter_alt_off", on_click=reset_all).props("flat dense")
                with ui.row().classes("gap-2"):
                    ui.button("Cancel", on_click=drawer.close).props("flat")
                    ui.button("Apply", icon="check", on_click=apply_and_close) \
                        .props("color=primary unelevated")
    drawer.open()


async def _delete_txn_flow(txn: dict) -> bool:
    # delete a transaction, shared by the edit dialog's Delete button and the Home
    # swipe-to-delete. handles the recurring-series scope prompt, the DB delete, the
    # backup, the toast and a full refresh. returns False only when the user backs out
    # of the recurring-scope prompt (in which case nothing is deleted).
    rid = txn.get("recurrence_id")
    # n_future excludes this row, only offer the this/future choice if later ones exist.
    n_future = max(db.count_series_from(DB_PATH, rid, txn["date"]) - 1, 0)
    if rid and n_future > 0:
        scope = await _ask_recurring_scope("delete", n_future)
        if scope is None:
            return False  # cancelled, leave everything as is
    else:
        scope = "one"

    if scope == "future":
        # snapshot the rows about to go (this occurrence and future) before deleting,
        # so they can be restored verbatim from the Recently deleted recovery list.
        captured = [r for r in db.get_recurring_series(DB_PATH, rid) if r["date"] >= txn["date"]]
        n = db.delete_recurring_from(DB_PATH, rid, txn["date"])
        label = f"{txn['merchant']} · {n} transactions"
        db.record_deletion(DB_PATH, captured, label=label)
        msg = f"Deleted {n} {txn['merchant']} transactions (this + future)."
    else:
        captured = db.get_transaction(DB_PATH, txn["id"])
        db.delete_transaction(DB_PATH, txn["id"])
        if captured:
            label = f"{txn['merchant']} · {bb.money(captured['amount'], signed=True)}"
            db.record_deletion(DB_PATH, [captured], label=label)
        msg = f"Deleted {txn['merchant']}."
    # removing transactions can leave a learned store with fewer (or zero) backing
    # rows, so keep its Confirmed count honest, and forget it if nothing's left.
    db.resync_merchant_memory(DB_PATH, txn.get("normalized_merchant"))
    db_backup_if_changed()
    # group txn-delete, deleting rows one by one collapses into one toast and count badge.
    notify(msg, type="warning", group="txn-delete")
    refresh_all()
    return True


def _render_transaction_row(txn: dict, cmap: dict[str, dict], *, show_date: bool = False):
    is_review = bool(txn["needs_review"])
    is_duplicate = bool(txn.get("is_duplicate"))
    # card border/background and avatar follow the dominant flag, needs_review
    # (amber) outranks possible-duplicate (blue). but every active flag is
    # surfaced as its own coloured tag, so a row flagged both ways shows both
    # tags. clearing one leaves the other (its colour and the matching border),
    # clearing both returns the row to its normal categorised look.
    flagged = is_review or is_duplicate
    if is_review:
        flag_cls, dom_color = " review", "var(--bb-warning)"
    elif is_duplicate:
        flag_cls, dom_color = " duplicate", "var(--bb-accent)"
    else:
        flag_cls, dom_color = "", None
    cat = None if flagged else cmap.get(txn["category"], {"emoji": "❓", "color": "#6b7280"})

    is_income = txn["amount"] > 0
    amt_str = ("+ $" if is_income else "− $") + bb.amount_str(txn['amount'])
    amt_cls = "income" if is_income else "expense"

    # swipe-to-delete: the row content is one scroll pane, swiping it left scrolls
    # the red trash button (its sibling) into view. the txn-row-<id> marker lets a
    # search-result click scroll to and flash this row.
    with ui.element("div").classes("bb-swipe w-full"):
        with ui.element("div").classes("bb-swipe-pane"):
            card = ui.card().classes(f"bb-row w-full txn-row-{txn['id']}" + flag_cls)
            card.on("click", lambda e, t=txn: open_edit_dialog(t))
            with card:
                with ui.row().classes("items-center w-full no-wrap gap-3"):
                    if flagged:
                        # Flagged (review and/or duplicate): colorable warning avatar
                        # tinted with the dominant flag's colour.
                        ui.icon("warning").classes("avatar").style(
                            f"color: {dom_color}; font-size: 1.25rem; "
                            f"background: color-mix(in srgb, {dom_color} 20%, transparent); "
                            f"border: 1px solid color-mix(in srgb, {dom_color} 35%, transparent);"
                        )
                    else:
                        avatar_cls = "avatar duo" if bb.count_emojis(cat["emoji"]) >= 2 else "avatar"
                        ui.label(cat["emoji"]).classes(avatar_cls).style(
                            f"background: color-mix(in srgb, {cat['color']} 20%, transparent); "
                            f"border: 1px solid color-mix(in srgb, {cat['color']} 35%, transparent);"
                        )
                    with ui.column().classes("flex-grow gap-0").style("min-width: 0;"):
                        ui.label(txn["merchant"]).classes("merchant")
                        if flagged:
                            # one tag per active flag, both show when both are set.
                            with ui.row().classes("items-center gap-2 w-full").style("min-width: 0;"):
                                if is_review:
                                    ui.label("Tap to categorize").classes("category-chip").style(
                                        "color: var(--bb-warning);")
                                if is_duplicate:
                                    ui.label("Possible duplicate").classes("category-chip").style(
                                        "color: var(--bb-accent);")
                        else:
                            ui.label(txn["category"]).classes("category-chip").style(
                                f"color: {cat['color']};")
                        if show_date:
                            # In amount-sorted (flat) mode there are no day headers, so
                            # the row carries its own date for context.
                            try:
                                d_str = datetime.strptime(txn["date"], "%Y-%m-%d").strftime("%b %-d, %Y")
                            except ValueError:
                                d_str = txn["date"]
                            ui.label(d_str).classes("text-caption text-muted")
                    ui.label(amt_str).classes(f"amount {amt_cls}")
        # quick-delete affordance, sits just off the right edge until a left-swipe
        # reveals it. a tap here (never the swipe alone) deletes the transaction.
        del_btn = ui.element("div").classes("bb-swipe-del")
        del_btn.on("click", lambda e, t=txn: _delete_txn_flow(t))
        del_btn.tooltip("Delete")
        with del_btn:
            ui.icon("delete")


def _prev_month():
    if state.view_month == 1:
        state.view_month = 12
        state.view_year -= 1
    else:
        state.view_month -= 1
    refresh_home()
    refresh_insights()


def _next_month():
    if state.view_month == 12:
        state.view_month = 1
        state.view_year += 1
    else:
        state.view_month += 1
    refresh_home()
    refresh_insights()


def _go_to_current_month():
    # jump the Home view back to today's month (fired by re-tapping the active Home
    # tab). no-op when already on the current month so it's cheap to spam.
    today = date.today()
    if state.view_year == today.year and state.view_month == today.month:
        return
    state.view_year, state.view_month = today.year, today.month
    refresh_home()


def _date_picker_field(value: str, *, label: str | None = None, dense: bool = False,
                       classes: str = "", style: str = "", on_change=None):
    # a date field whose calendar lives in a popup that closes the instant a date is
    # picked, and that toggles shut when the Date box itself is clicked again.
    # replaces the native <input type=date>: the browser's own calendar refused to
    # dismiss when you re-clicked the box after selecting. here the calendar is a Quasar
    # menu anchored to the input, so clicking the box toggles it (Quasar handles the
    # open then click-box then close case for us) and choosing a day closes it immediately.
    # the value stays an ISO YYYY-MM-DD string, identical to the old field. returns the
    # ui.input, read its .value (or pass on_change) to get the chosen date.
    props = "outlined readonly" + (" dense" if dense else "")
    inp = ui.input(label=label, value=value).props(props).classes("cursor-pointer bb-date-field")
    if classes:
        inp.classes(classes)
    if style:
        inp.style(style)
    with inp:
        with inp.add_slot("append"):
            ui.icon("event").classes("cursor-pointer")
        with ui.menu() as menu:
            picker = ui.date(value=value)

            def _on_pick():
                inp.value = picker.value or ""
                menu.close()
                if on_change is not None:
                    on_change(inp.value)
            picker.on_value_change(lambda e: _on_pick())
    return inp


# add transaction dialog

def open_add_dialog():
    # Default the date to the month the user is viewing on Home: today if that's the
    # current month, otherwise the 1st of the viewed month (so adding to a past/future
    # month opens the calendar already on that month instead of jumping to today).
    today = date.today()
    if state.view_year == today.year and state.view_month == today.month:
        default_date = today
    else:
        default_date = date(state.view_year, state.view_month, 1)

    with ui.dialog() as dialog, ui.card().style("min-width: 520px; padding: 24px; gap: 14px;"):
        ui.label("Add transaction").classes("text-h6").style("margin-bottom: 4px;")

        with ui.row().classes("w-full gap-3 no-wrap items-end"):
            # start empty (not 0) so an untouched, required field reads as unfilled.
            amount_in = ui.number(label="Amount", value=None, step=0.01, min=0) \
                .props("outlined").classes("flex-grow")
            kind = ui.toggle(["Expense", "Income"], value="Expense") \
                .props("unelevated no-caps")

        store_in = ui.input(label="Name", placeholder="e.g. Loblaws") \
            .props("outlined stack-label").classes("w-full")

        # Manual entries require an explicit category choice (no auto-categorize):
        # start with no selection so the user must pick one.
        cat_options = _expense_category_names(cat_names())
        category_in = ui.select(cat_options, value=None, label="Category") \
            .props("outlined").classes("w-full")

        # form validation helpers (red border and message, cleared on edit)
        def _show_err(el, msg):
            el.props(f'error error-message="{msg}"')

        def _clear_err(el):
            el.props(remove="error")

        def _on_kind_change():
            # income is always categorized as Income (locked). only expenses
            # get a free category choice, so swap the options when the type flips.
            if kind.value == "Income":
                category_in.set_options(["Income"], value="Income")
                category_in.disable()
            else:
                category_in.enable()
                category_in.set_options(_expense_category_names(cat_names()), value=None)
            _clear_err(category_in)
        kind.on_value_change(lambda e: _on_kind_change())

        # Clear a field's error as soon as the user edits it.
        amount_in.on_value_change(lambda e: _clear_err(amount_in))
        store_in.on_value_change(lambda e: _clear_err(store_in))
        category_in.on_value_change(lambda e: _clear_err(category_in))

        date_in = _date_picker_field(default_date.isoformat(), label="Date",
                                     classes="w-full",
                                     on_change=lambda v: _clear_err(date_in))

        with ui.row().classes("w-full gap-4 no-wrap"):
            recurring = ui.switch("Recurring")
            excluded = ui.switch("Exclude from budget")

        # frequency picker, only visible when Recurring is on. options read
        # naturally after the label: Repeat every then Week or Month.
        freq_in = ui.select(["Week", "Month"], value="Month",
                            label="Repeat every") \
            .props("outlined").classes("w-full")
        freq_in.bind_visibility_from(recurring, "value")

        note_in = ui.input(label="Note (optional)").props("outlined").classes("w-full")

        def on_save():
            # Validate every field up front, highlighting each offender in red so
            # the user sees all problems at once (rather than one notify at a time).
            errors = False

            try:
                amount = float(amount_in.value)
                if not math.isfinite(amount):  # reject inf, 1e999, nan
                    amount = None
            except (TypeError, ValueError):
                amount = None
            if amount is None:
                _show_err(amount_in, "Enter a valid amount")
                errors = True
            elif amount <= 0:
                _show_err(amount_in, "Amount must be greater than zero")
                errors = True
            else:
                _clear_err(amount_in)

            store_s = (store_in.value or "").strip()
            if not store_s:
                _show_err(store_in, "Enter a name")
                errors = True
            else:
                _clear_err(store_in)

            base_date = date_in.value
            base_dt = None
            if not base_date:
                _show_err(date_in, "Pick a date")
                errors = True
            else:
                try:
                    base_dt = date.fromisoformat(base_date)
                    _clear_err(date_in)
                except ValueError:
                    _show_err(date_in, "Date is invalid")
                    errors = True

            # income is locked to the Income category, expenses must pick one.
            if kind.value == "Income":
                cat = "Income"
                _clear_err(category_in)
            else:
                cat = category_in.value
                if not cat:
                    _show_err(category_in, "Choose a category")
                    errors = True
                else:
                    _clear_err(category_in)

            if errors:
                ui.notify("Please fill in the highlighted fields.", type="negative")
                return

            signed_amount = abs(amount) if kind.value == "Income" else -abs(amount)
            confidence = 1.0
            needs_review = (cat == ai.REVIEW_CATEGORY)

            # Build the list of dates to insert (base + recurring future entries)
            dates_to_insert = [base_dt]
            if recurring.value:
                freq = {"Week": "weekly"}.get(freq_in.value, "monthly")
                dates_to_insert += _recurring_future_dates(base_dt, freq)

            # a multi-occurrence recurring series shares one recurrence_id so the
            # user can later delete/edit this one vs this and all future ones.
            series_id = uuid.uuid4().hex if len(dates_to_insert) > 1 else None

            # insert every entry, duplicates are flagged for review, never skipped
            # (a bad auto-skip would silently lose a real transaction the user added).
            inserted, flagged = 0, 0
            for d in dates_to_insert:
                iso = d.isoformat()
                is_dup = db.find_duplicate_id(DB_PATH, iso, store_s, signed_amount) is not None
                db.add_transaction(
                    DB_PATH, date=iso, merchant=store_s,
                    amount=signed_amount, category=cat,
                    note=(note_in.value or "").strip() or None,
                    source="manual", is_excluded=bool(excluded.value),
                    is_recurring=bool(recurring.value), confidence=confidence,
                    needs_review=needs_review, is_duplicate=is_dup,
                    recurrence_id=series_id,
                )
                inserted += 1
                if is_dup:
                    flagged += 1
            db_backup_if_changed()

            if not needs_review:
                db.learn_merchant(DB_PATH, store_s, cat)
            if len(dates_to_insert) > 1:
                ui.notify(f"Saved {inserted} entries ({store_s} · {cat})", type="positive")
            else:
                ui.notify(f"Saved: {store_s} ({cat})", type="positive")
            if flagged:
                ui.notify(
                    f"{flagged} possible duplicate{'s' if flagged != 1 else ''} flagged "
                    "for review. Nothing was skipped.",
                    type="warning",
                )
            dialog.close()
            refresh_all()

        with ui.row().classes("w-full gap-2 justify-end").style("margin-top: 8px;"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Save", on_click=on_save).props("color=primary unelevated")

    dialog.open()


# edit transaction dialog

async def _ask_recurring_scope(action: str, n_future: int) -> str | None:
    # ask whether a delete/edit/stop applies to one occurrence or the whole future run.
    # action is delete, stop, or edit. returns one, future, or None (cancelled).
    plural = "s" if n_future != 1 else ""
    verb = {"delete": "Delete", "stop": "Stop recurring for"}.get(
        action, "Apply changes to")
    color = "negative" if action in ("delete", "stop") else "primary"
    with ui.dialog() as d, ui.card().style("min-width: 440px; padding: 22px; gap: 12px;"):
        ui.label("This is a recurring transaction").classes("text-h6")
        ui.label(f"{verb} just this one, or this and the {n_future} future "
                 f"occurrence{plural} too?").classes("text-body2 text-muted")
        with ui.row().classes("w-full justify-end gap-2").style("margin-top: 8px;"):
            ui.button("Cancel", on_click=lambda: d.submit(None)).props("flat")
            ui.button("This one only", on_click=lambda: d.submit("one")).props("flat")
            ui.button("This & all future", on_click=lambda: d.submit("future")) \
                .props(f"unelevated color={color}")
    return await d


def _propagate_series_date(rid: str, old_dt: date, new_dt: date, kind: str) -> int:
    # re-date this occurrence and all future ones after the user moved one occurrence.
    # monthly: re-anchor every occurrence on/after old_dt to the new day-of-month (clipped
    # to each month's length). weekly: shift them all by the same day delta (preserving the
    # 7-day cadence on a new weekday). past occurrences (before old_dt) are left untouched.
    # returns how many rows were actually re-dated.
    from datetime import timedelta
    delta = (new_dt - old_dt).days
    n = 0
    for r in db.get_recurring_series(DB_PATH, rid):
        rd = date.fromisoformat(r["date"])
        if rd < old_dt:
            continue  # past occurrence, leave it alone
        nd = _reanchored_date(rd, new_dt.day) if kind == "monthly" else rd + timedelta(days=delta)
        if nd.isoformat() != r["date"]:
            db.update_transaction(DB_PATH, r["id"], date=nd.isoformat())
            n += 1
    return n


def open_edit_dialog(txn: dict):
    with ui.dialog() as dialog, ui.card().style("min-width: 520px; padding: 24px; gap: 14px;"):
        ui.label("Edit transaction").classes("text-h6").style("margin-bottom: 4px;")

        if txn.get("needs_review"):
            with ui.row().classes("w-full items-center q-pa-sm").style(
                "background: rgba(251,191,36,0.08); border-radius: 10px; "
                "border: 1px solid rgba(251,191,36,0.3);"
            ):
                ui.label("⚠️").style("font-size: 1.2rem;")
                ui.label("Couldn't confidently auto-categorize. Pick the right one "
                         "and I'll remember it.").classes("text-caption")

        if txn.get("is_duplicate"):
            with ui.row().classes("w-full items-center no-wrap q-pa-sm").style(
                "background: rgba(59,130,246,0.08); border-radius: 10px; "
                "border: 1px solid rgba(59,130,246,0.3); gap: 8px;"
            ):
                ui.icon("warning").style("font-size: 1.2rem; color: var(--bb-accent);")
                ui.label("Flagged as a possible duplicate of another transaction. "
                         "It was saved, not skipped. Clear the flag if it's a real, "
                         "separate charge.").classes("text-caption flex-grow")

                def _not_duplicate():
                    db.update_transaction(DB_PATH, txn["id"], is_duplicate=0)
                    db_backup_if_changed()
                    ui.notify("Cleared the duplicate flag.", type="positive")
                    dialog.close()
                    refresh_all()
                ui.button("Not a duplicate", on_click=_not_duplicate).props("flat dense")

        with ui.row().classes("w-full gap-3 no-wrap items-end"):
            amount_in = ui.number(
                label="Amount", value=abs(float(txn["amount"])), step=0.01, min=0.01,
            ).props("outlined").classes("flex-grow")
            kind_in = ui.toggle(
                ["Expense", "Income"],
                value="Income" if txn["amount"] > 0 else "Expense",
            ).props("unelevated no-caps")

        store_in = ui.input(label="Name", value=txn["merchant"]) \
            .props("outlined stack-label").classes("w-full")

        if txn["amount"] > 0:
            category_in = ui.select(
                ["Income"], value="Income", label="Category",
            ).props("outlined").classes("w-full")
            category_in.disable()
        else:
            _eopts = _expense_category_names(cat_names())
            _eval = txn["category"] if txn["category"] in _eopts else _eopts[0]
            category_in = ui.select(
                _eopts, value=_eval, label="Category",
            ).props("outlined").classes("w-full")

        def _on_edit_kind_change():
            if kind_in.value == "Income":
                category_in.set_options(["Income"], value="Income")
                category_in.disable()
            else:
                opts = _expense_category_names(cat_names())
                category_in.enable()
                cur = category_in.value if category_in.value in opts else opts[0]
                category_in.set_options(opts, value=cur)

        kind_in.on_value_change(lambda e: _on_edit_kind_change())

        date_in = _date_picker_field(txn["date"], label="Date", classes="w-full")
        note_in = ui.input(label="Note (optional)", value=txn.get("note") or "") \
            .props("outlined stack-label").classes("w-full")

        # recurring and exclude controls (parity with the Add dialog)
        # Frequency isn't stored, so for an existing series infer the cadence from its
        # occurrence spacing to seed the picker. was_recurring drives the save-time
        # transition logic (start a new series / stop an existing one).
        _series = db.get_recurring_series(DB_PATH, txn.get("recurrence_id"))
        _series_dates = [date.fromisoformat(r["date"]) for r in _series]
        _inferred_freq = _infer_series_frequency(_series_dates, date.fromisoformat(txn["date"]))
        was_recurring = bool(txn.get("is_recurring"))

        with ui.row().classes("w-full gap-4 no-wrap"):
            recurring_in = ui.switch("Recurring", value=was_recurring)
            excluded_in = ui.switch("Exclude from budget",
                                    value=bool(txn.get("is_excluded")))

        freq_in = ui.select(["Week", "Month"],
                            value="Week" if _inferred_freq == "weekly" else "Month",
                            label="Repeat every") \
            .props("outlined").classes("w-full")
        freq_in.bind_visibility_from(recurring_in, "value")

        async def on_save():
            new_store = (store_in.value or "").strip()
            if not new_store:
                ui.notify("Name cannot be empty", type="negative")
                return
            try:
                new_amount = abs(float(amount_in.value or 0))
                if not math.isfinite(new_amount):  # reject inf, 1e999, nan
                    raise ValueError
            except (TypeError, ValueError):
                ui.notify("Amount must be a number", type="negative")
                return
            if new_amount <= 0:
                ui.notify("Amount must be greater than zero", type="negative")
                return
            signed = new_amount if kind_in.value == "Income" else -new_amount
            new_cat = "Income" if kind_in.value == "Income" else category_in.value
            # a transaction needs review exactly when its category is the review
            # category, same rule as the upload save path. so picking Needs Review
            # here re-flags the row, picking a real category clears the flag.
            new_review = (new_cat == ai.REVIEW_CATEGORY)

            # after this edit commits, keep the Learned-stores Confirmed counts honest:
            # the store this transaction is leaving may now back fewer rows (or none, so
            # forgotten), and the store/category it's moving to may back one more. resync
            # is idempotent and a no-op for stores that aren't learned, so it's safe to call
            # on every commit path below.
            _old_norm = txn.get("normalized_merchant")
            _new_norm = db.normalize_merchant(new_store)
            # was the store being edited already a learned store? captured before any write
            # (keyed on the exact stored normalized merchant, not the display, they can
            # differ). if so and the user renames it, we carry its learned entry onto the
            # new name below so the Learned-stores row follows the rename instead of being
            # orphaned and forgotten by the resync.
            old_learned_cat = db.get_learned_category(DB_PATH, _old_norm)

            def _resync_learned():
                db.resync_merchant_memory(DB_PATH, _old_norm)
                if _new_norm != _old_norm:
                    db.resync_merchant_memory(DB_PATH, _new_norm)

            new_date = date_in.value
            try:
                new_dt = date.fromisoformat(new_date)
            except (TypeError, ValueError):
                ui.notify("Date is invalid.", type="negative")
                return
            old_dt = date.fromisoformat(txn["date"])

            # Plain field changes (everything except the recurrence structure itself).
            updates = {}
            if new_date != txn["date"]:
                updates["date"] = new_date
            store_renamed = new_store != txn["merchant"]
            cat_changed = new_cat != txn["category"]
            if store_renamed:
                updates["merchant"] = new_store
            if cat_changed:
                updates["category"] = new_cat
            # teach the store to category mapping so it's remembered next time. fire it when
            # the category changed (the classic I corrected this signal) or when a store that
            # was already learned gets renamed, so its Learned-stores entry adapts to the new
            # name instead of vanishing (after a rename the old name backs 0 transactions, so
            # _resync_learned would otherwise just forget it). never teach the review category,
            # Needs Review isn't a real category to auto-apply later.
            if not new_review and (cat_changed or (store_renamed and old_learned_cat is not None)):
                db.learn_merchant(DB_PATH, new_store, new_cat)
            if signed != float(txn["amount"]):
                updates["amount"] = signed
            new_note = (note_in.value or "").strip()
            old_note = txn.get("note") or ""
            if new_note != old_note:
                updates["note"] = new_note or None
            if bool(excluded_in.value) != bool(txn.get("is_excluded")):
                updates["is_excluded"] = int(bool(excluded_in.value))

            now_recurring = bool(recurring_in.value)
            recurrence_changed = (now_recurring != was_recurring)
            review_changed = int(new_review) != int(bool(txn.get("needs_review")))

            if not updates and not recurrence_changed and not review_changed:
                ui.notify("No changes to save.")
                dialog.close()
                refresh_all()
                return

            # (A) turning recurrence on for a one-off materialises the future run.
            if now_recurring and not was_recurring:
                freq = "weekly" if freq_in.value == "Week" else "monthly"
                future = _recurring_future_dates(new_dt, freq)
                series_id = uuid.uuid4().hex
                this_updates = dict(updates)
                this_updates.update(is_recurring=1, recurrence_id=series_id,
                                    needs_review=int(new_review))
                db.update_transaction(DB_PATH, txn["id"], **this_updates)
                for d in future:
                    db.add_transaction(
                        DB_PATH, date=d.isoformat(), merchant=new_store, amount=signed,
                        category=new_cat, note=new_note or None,
                        source=txn.get("source", "manual"),
                        is_excluded=bool(excluded_in.value), is_recurring=True,
                        confidence=txn.get("confidence"), recurrence_id=series_id,
                        needs_review=new_review,
                    )
                _resync_learned()
                db_backup_if_changed()
                ui.notify(f"Now recurring. Added {len(future)} future "
                          f"occurrence{'s' if len(future) != 1 else ''}.", type="positive")
                dialog.close()
                refresh_all()
                return

            # (B) turning recurrence off for a series member stops recurring.
            if was_recurring and not now_recurring:
                rid = txn.get("recurrence_id")
                n_future = max(db.count_series_from(DB_PATH, rid, txn["date"]) - 1, 0)
                scope = "one"
                if rid and n_future > 0:
                    scope = await _ask_recurring_scope("stop", n_future)
                    if scope is None:
                        return  # cancelled, leave everything as is
                this_updates = dict(updates)
                this_updates.update(is_recurring=0, recurrence_id=None,
                                    needs_review=int(new_review))
                db.update_transaction(DB_PATH, txn["id"], **this_updates)
                if scope == "future" and rid:
                    from datetime import timedelta
                    after = (old_dt + timedelta(days=1)).isoformat()
                    removed = db.delete_recurring_from(DB_PATH, rid, after)
                    msg = (f"Stopped recurring. Removed {removed} future "
                           f"occurrence{'s' if removed != 1 else ''}.")
                else:
                    msg = "Stopped recurring for this one (other occurrences kept)."
                _resync_learned()
                db_backup_if_changed()
                ui.notify(msg, type="positive")
                dialog.close()
                refresh_all()
                return

            # (C) no recurrence transition, normal edit. offer this and future for a real
            # series. plain fields propagate as before, a date edit propagates only when it
            # stays within the cadence step: same-month for a monthly series (re-anchor the
            # day-of-month) or any shift for a weekly series (shift the whole run by the
            # same delta). a cross-month monthly date edit stays this-occurrence-only.
            rid = txn.get("recurrence_id")
            n_future = max(db.count_series_from(DB_PATH, rid, txn["date"]) - 1, 0)
            propagatable = {k: v for k, v in updates.items() if k != "date"}

            date_changed = "date" in updates
            date_prop_kind = None  # one of: None, monthly, weekly
            if date_changed and rid and n_future > 0:
                freq = _infer_series_frequency(_series_dates, old_dt)
                if freq == "monthly" and (new_dt.year, new_dt.month) == (old_dt.year, old_dt.month):
                    date_prop_kind = "monthly"
                elif freq == "weekly":
                    date_prop_kind = "weekly"

            scope = "one"
            if rid and n_future > 0 and (propagatable or date_prop_kind):
                scope = await _ask_recurring_scope("edit", n_future)
                if scope is None:
                    return  # cancelled, leave everything as is

            updates["needs_review"] = int(new_review)
            if scope == "future":
                series_updates = {k: v for k, v in updates.items() if k != "date"}
                affected = db.update_recurring_from(DB_PATH, rid, txn["date"], **series_updates)
                if date_changed and date_prop_kind:
                    affected = max(affected,
                                   _propagate_series_date(rid, old_dt, new_dt, date_prop_kind))
                elif date_changed:
                    # cross-month or non-cadence date edit, this occurrence only.
                    db.update_transaction(DB_PATH, txn["id"], date=new_date)
                msg = f"Saved to {affected} transactions (this + future)."
            else:
                db.update_transaction(DB_PATH, txn["id"], **updates)
                msg = "Saved."
            _resync_learned()
            db_backup_if_changed()
            ui.notify(msg, type="positive")
            dialog.close()
            refresh_all()

        async def on_delete():
            # shared with the Home swipe-to-delete, only close the dialog if the
            # delete actually went through (the recurring-scope prompt can cancel it).
            if await _delete_txn_flow(txn):
                dialog.close()

        with ui.row().classes("w-full gap-2 justify-between").style("margin-top: 8px;"):
            ui.button("Delete", icon="delete", on_click=on_delete) \
                .props("flat color=negative")
            with ui.row().classes("gap-2"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                ui.button("Save", on_click=on_save).props("color=primary unelevated")

    dialog.open()


# search drawer (with real per-keystroke filtering)

_search_drawer: ui.element | None = None
_search_results_container: ui.element | None = None


def _flash_transaction(tx_id: int | None, client=None):
    # scroll the Home list to a transaction row and briefly flash it. retries for ~1s
    # because the Home tab re-renders asynchronously after the tab switch, so the target
    # row may not be in the DOM the instant this fires.
    # client is the page's Client, captured by the caller before it refreshed the Home
    # tab. we send the JS through it directly because refreshing Home deletes the search
    # drawer (it lives under the Home tab) along with the clicked element's slot, so the
    # ambient ui.run_javascript context is already gone by the time we get here. the
    # flash is purely cosmetic, so any failure (e.g. a disconnected client) is swallowed.
    if tx_id is None:
        return
    js = f"""
    (function() {{
      const sel = '.txn-row-{int(tx_id)}';
      let tries = 0;
      const go = () => {{
        const el = document.querySelector(sel);
        if (el) {{
          el.scrollIntoView({{behavior: 'smooth', block: 'center'}});
          el.classList.add('bb-flash');
          setTimeout(() => el.classList.remove('bb-flash'), 1800);
        }} else if (tries++ < 25) {{
          setTimeout(go, 60);
        }}
      }};
      go();
    }})();
    """
    try:
        if client is not None:
            client.run_javascript(js)
        else:
            ui.run_javascript(js)
    except Exception:
        pass  # cosmetic only, never let a failed flash bubble into the click handler


def _go_to_transaction(txn: dict, drawer=None):
    # jump from a search result to that transaction on the Home tab: move the month
    # view to its date, switch to Home, re-render, then scroll to and flash the row.
    # grab the page client now, while the clicked element's slot is still alive.
    # refresh_home() below deletes the search drawer (it lives under the Home tab),
    # which would otherwise break the ambient run_javascript context resolution.
    try:
        client = ui.context.client
    except Exception:
        client = None
    try:
        d = date.fromisoformat(txn["date"])
        state.view_year, state.view_month = d.year, d.month
    except (ValueError, TypeError, KeyError):
        pass
    if drawer is not None:
        drawer.close()
    if main_tabs is not None and tab_home_ref is not None:
        main_tabs.set_value(tab_home_ref)
    refresh_home()
    refresh_insights()
    _flash_transaction(txn.get("id"), client=client)


def open_review_drawer():
    # list every flagged transaction (needs review or possible duplicate) with its
    # date. tapping a row jumps to it on the Home tab, same flow as search results.
    with ui.dialog().props("position=top maximized=false") as drawer:
        with ui.card().style("width: 560px; max-width: 92vw; padding: 18px;"):
            ui.label("Flagged transactions").classes("text-h6").style("margin-bottom: 4px;")
            ui.label("Transactions that need a second look. Tap one to jump to it.") \
                .classes("text-caption text-muted").style("margin-bottom: 10px;")

            rows = db.get_transactions(DB_PATH, flagged_only=True)
            if not rows:
                ui.html(
                    "<div class='text-caption text-muted' style='padding:18px; text-align:center;'>"
                    "✅ All clear. Nothing flagged right now.</div>")
            else:
                cmap = cat_map()
                ui.html(f"<div class='text-caption text-muted' style='padding:6px 4px;'>"
                        f"{len(rows)} flagged</div>")
                with ui.column().classes("w-full") \
                        .style("max-height: 460px; overflow-y: auto; gap: 0;"):
                    for r in rows:
                        cat = cmap.get(r["category"], {"emoji": "❓", "color": "#6b7280"})
                        row_html = bb.render_search_row_html(
                            merchant=r["merchant"], date_str=r["date"],
                            category=r["category"], emoji=cat["emoji"], color=cat["color"],
                            amount=r["amount"], needs_review=bool(r["needs_review"]),
                            is_duplicate=bool(r.get("is_duplicate")),
                        )
                        item = ui.html(row_html).classes("w-full bb-search-hit") \
                            .style("cursor: pointer;")
                        item.on("click", lambda e, t=dict(r): _go_to_transaction(t, drawer))

            with ui.row().classes("w-full justify-end").style("margin-top: 10px;"):
                ui.button("Close", on_click=drawer.close).props("flat")
    drawer.open()


def open_search_drawer():
    with ui.dialog().props("position=top maximized=false") as drawer:
        with ui.card().style("width: 560px; max-width: 92vw; padding: 18px;"):
            ui.label("Search transactions").classes("text-h6").style("margin-bottom: 4px;")
            ui.label("Filters across your entire history. Tap a result to jump to it.") \
                .classes("text-caption text-muted").style("margin-bottom: 10px;")

            # Input on top, results below (correct reading order).
            search_in = ui.input(placeholder="Type to filter (e.g. tim)") \
                .props("outlined clearable autofocus dense").classes("w-full")
            results = ui.column().classes("w-full").style("gap: 0;")

            def render_results(query: str):
                results.clear()
                query = (query or "").strip()
                with results:
                    if not query:
                        ui.html(
                            "<div class='text-caption text-muted' style='padding:14px; text-align:center;'>"
                            "Start typing a name to filter your transactions.</div>")
                        return
                    rows = db.get_transactions(DB_PATH, merchant_search=query)
                    if not rows:
                        ui.html(
                            f"<div class='text-caption text-muted' style='padding:14px; text-align:center;'>"
                            f"No matches for '{html.escape(query)}'.</div>")
                        return
                    cmap = cat_map()
                    # summary bar: count of results (left) and their total balance (right),
                    # over all matches, not just the 50 shown. re-renders every keystroke.
                    n = len(rows)
                    total = sum(r["amount"] for r in rows)
                    total_cls = "income" if round(total) >= 0 else "expense"  # match the shown rounded total
                    ui.html(
                        "<div class='bb-search-summary'>"
                        f"<span class='label'>{n} transaction{'s' if n != 1 else ''}:</span>"
                        f"<span class='total {total_cls}'>"
                        f"{html.escape(bb.money(total, signed=True, rounded=True))}</span>"
                        "</div>").classes("w-full")
                    with ui.column().classes("w-full") \
                            .style("max-height: 460px; overflow-y: auto; gap: 0;"):
                        for r in rows[:50]:
                            cat = cmap.get(r["category"], {"emoji": "❓", "color": "#6b7280"})
                            row_html = bb.render_search_row_html(
                                merchant=r["merchant"], date_str=r["date"],
                                category=r["category"], emoji=cat["emoji"], color=cat["color"],
                                amount=r["amount"], needs_review=bool(r["needs_review"]),
                            )
                            item = ui.html(row_html).classes("w-full bb-search-hit") \
                                .style("cursor: pointer;")
                            item.on("click", lambda e, t=dict(r): _go_to_transaction(t, drawer))
                        if len(rows) > 50:
                            ui.html(f"<div class='text-caption text-muted' style='padding:8px;'>"
                                    f"and {len(rows)-50} more. Refine your search.</div>")

            search_in.on_value_change(lambda e: render_results(e.value))
            render_results("")

            with ui.row().classes("w-full justify-end").style("margin-top: 10px;"):
                ui.button("Close", on_click=drawer.close).props("flat")
    drawer.open()


# insights tab

def refresh_insights():
    c = containers.get("insights")
    if c is None:
        return
    c.clear()
    with c:
        _render_insights_inner()


def _render_insights_inner():
    years = db.get_available_years(DB_PATH)
    cmap = cat_map()

    today = date.today()
    if state.insights_month is None:
        state.insights_month = today.month
    if state.insights_m_year is None:
        state.insights_m_year = today.year
    # The monthly year picker always offers the current year so the default month
    # is selectable even before any data exists for it.
    m_years = sorted({today.year, *years}, reverse=True)

    with ui.row().classes("w-full items-center gap-3").style("margin: 12px 0;"):
        scope_in = ui.toggle(["Monthly", "Annual"], value=state.insights_scope) \
            .props("dense unelevated")
        picker = ui.row().classes("items-center gap-2")

        def on_scope_change():
            state.insights_scope = scope_in.value
            refresh_insights()
        scope_in.on_value_change(lambda e: on_scope_change())

        if scope_in.value == "Monthly":
            with picker:
                mo = ui.select({i: bb.MONTH_NAMES[i - 1] for i in range(1, 13)},
                               value=state.insights_month, label="Month") \
                    .props("dense").classes("w-36")
                myr = ui.select(m_years, value=state.insights_m_year,
                                label="Year").props("dense").classes("w-28")
                def on_month_change():
                    state.insights_month = mo.value
                    state.insights_m_year = myr.value
                    refresh_insights()
                mo.on_value_change(lambda e: on_month_change())
                myr.on_value_change(lambda e: on_month_change())
        else:
            with picker:
                yr = ui.select(years, value=state.insights_year or years[0],
                               label="Year").props("dense").classes("w-32")
                def on_year_change():
                    state.insights_year = yr.value
                    refresh_insights()
                yr.on_value_change(lambda e: on_year_change())

    if scope_in.value == "Monthly":
        year, month = state.insights_m_year, state.insights_month
        scope_label = f"{bb.MONTH_NAMES[month - 1]} {year}"
    else:
        year = state.insights_year or years[0]
        month = None
        scope_label = str(year)

    txns = db.get_transactions(DB_PATH, year=year, month=month, include_excluded=False)
    if not txns:
        with ui.card().classes("w-full bb-empty"):
            ui.label("📊").classes("emoji")
            ui.label(f"No data for {scope_label}").classes("title")
            ui.label("Try switching scope or upload some statements.").classes("msg")
        return

    df = pd.DataFrame(txns)
    df["date"] = pd.to_datetime(df["date"])
    income = float(df.loc[df["amount"] > 0, "amount"].sum())
    expenses = float(-df.loc[df["amount"] < 0, "amount"].sum())
    net = income - expenses

    balance_cls = "income" if round(net) >= 0 else "expense"  # same drift guard as Home
    with ui.row().classes("w-full gap-3 no-wrap").style("margin-top: 10px;"):
        for label, value, cls in [
            ("Income",   bb.money(income, rounded=True),   "income"),
            ("Expenses", bb.money(expenses, rounded=True), "expense"),
            ("Balance",  bb.money(net, signed=True, rounded=True), balance_cls),
        ]:
            with ui.card().classes("bb-kpi flex-grow"):
                ui.label(label).classes("kpi-label")
                ui.label(value).classes(f"kpi-value {cls}")
                ui.label(scope_label).classes("kpi-sub")

    # Donut: category breakdown
    exp = df[df["amount"] < 0].copy()
    if not exp.empty:
        exp["abs"] = -exp["amount"]
        by_cat = exp.groupby("category", as_index=False)["abs"].sum() \
                    .sort_values("abs", ascending=False)
        # for the donut only, fold the tiniest slices (<4% of total) into one Other
        # wedge so labels never crowd at the bottom. the bar chart below still lists
        # every category with exact amounts, so no detail is lost.
        total_exp = float(by_cat["abs"].sum())
        pie_df = by_cat.copy()
        if total_exp > 0 and len(pie_df) > 6:
            share = pie_df["abs"] / total_exp
            small = pie_df[share < 0.04]
            if len(small) > 1:
                big = pie_df[share >= 0.04]
                other = pd.DataFrame([{"category": "Other",
                                       "abs": float(small["abs"].sum())}])
                pie_df = pd.concat([big, other], ignore_index=True)
        colors = [cmap.get(c, {"color": "#6b7280"})["color"] for c in pie_df["category"]]
        fig = go.Figure(go.Pie(
            labels=pie_df["category"], values=pie_df["abs"], hole=0.55,
            marker=dict(colors=colors), sort=False,
            # percentage sits inside each slice (no outside leader lines to overlap),
            # the legend names the categories and hover shows the exact dollar amount.
            textinfo="percent", textposition="inside",
            insidetextorientation="horizontal",
            textfont=dict(color="#f8fafc", size=13),
            hovertemplate="<b>%{label}</b><br>$%{value:,.0f}<br>%{percent}<extra></extra>",
        ))
        fig.update_layout(
            title=f"Expenses by category ({scope_label})",
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color="#cbd5e1", family="-apple-system, sans-serif"),
            margin=dict(t=50, b=20, l=20, r=20), height=420,
            legend=dict(orientation="v", font=dict(color="#cbd5e1")),
        )
        ui.plotly(fig).classes("w-full")

    # Second chart: bar
    if scope_in.value == "Monthly":
        if not exp.empty:
            by = by_cat.sort_values("abs", ascending=True)
            bar_max = float(by["abs"].max())
            fig2 = go.Figure(go.Bar(
                x=by["abs"], y=by["category"], orientation="h",
                marker_color=[cmap.get(c, {"color": "#6b7280"})["color"] for c in by["category"]],
                text=[f"${v:,.0f}" for v in by["abs"]], textposition="outside",
                cliponaxis=False,
                hovertemplate="<b>%{y}</b><br>$%{x:,.0f}<extra></extra>",
            ))
            fig2.update_layout(
                title=f"Spending by category ({scope_label})",
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color="#cbd5e1", family="-apple-system, sans-serif"),
                margin=dict(t=50, b=30, l=10, r=80),
                height=max(280, 32 * len(by) + 100), showlegend=False,
                # Pad the x-axis past the longest bar so its outside value label
                # has room and isn't clipped at the plot edge.
                xaxis=dict(gridcolor="rgba(255,255,255,0.06)", tickprefix="$",
                           tickformat=",.0f", automargin=True, range=[0, bar_max * 1.2]),
                yaxis=dict(gridcolor="rgba(255,255,255,0.06)",
                           automargin=True, ticksuffix="  "),
            )
            ui.plotly(fig2).classes("w-full")
    else:
        monthly = df.copy()
        monthly["m"] = monthly["date"].dt.month
        inc_per = monthly.loc[monthly["amount"] > 0].groupby("m")["amount"] \
                          .sum().reindex(range(1, 13), fill_value=0)
        exp_per = (-monthly.loc[monthly["amount"] < 0].groupby("m")["amount"] \
                   .sum()).reindex(range(1, 13), fill_value=0)
        fig2 = go.Figure()
        fig2.add_trace(go.Bar(x=bb.MONTH_NAMES, y=inc_per.values, name="Income",
                              marker_color="#10b981",
                              hovertemplate="<b>%{x}</b><br>Income $%{y:,.0f}<extra></extra>"))
        fig2.add_trace(go.Bar(x=bb.MONTH_NAMES, y=exp_per.values, name="Expenses",
                              marker_color="#f87171",
                              hovertemplate="<b>%{x}</b><br>Expenses $%{y:,.0f}<extra></extra>"))
        fig2.update_layout(
            title=f"Monthly income vs expenses ({year})", barmode="group",
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color="#cbd5e1", family="-apple-system, sans-serif"),
            margin=dict(t=50, b=30, l=10, r=20), height=360,
            xaxis=dict(gridcolor="rgba(255,255,255,0.06)", automargin=True),
            yaxis=dict(gridcolor="rgba(255,255,255,0.06)", tickprefix="$",
                       tickformat=",.0f", automargin=True),
        )
        ui.plotly(fig2).classes("w-full")

    if not exp.empty:
        top = exp.groupby("merchant", as_index=False)["abs"].sum() \
                  .sort_values("abs", ascending=False).head(15)
        top["amount"] = top["abs"].map(lambda v: f"${v:,.0f}")
        ui.label(f"Top stores/purchases ({scope_label})").classes("text-subtitle1") \
          .style("margin-top: 16px;")
        cols = [
            {"name": "merchant", "label": "Name", "field": "merchant", "align": "left"},
            {"name": "amount", "label": "Amount", "field": "amount", "align": "right"},
        ]
        ui.table(columns=cols, rows=top[["merchant", "amount"]].to_dict("records"),
                 row_key="merchant").classes("w-full").props("flat dense")


# upload tab

def refresh_upload():
    c = containers.get("upload")
    if c is None:
        return
    c.clear()
    with c:
        _render_upload_inner()      # full upload form online, offline card when not
        _render_upload_preview()    # any extracted rows, shown in both modes so a batch
                                    # interrupted by an API failure stays reviewable/savable


def _extract_one_file(client, name, data, *, category_names,
                      merchant_memory, recent_corrections):
    # blocking per-file extraction: local-first, then a single vision retry when a
    # local pass found nothing. returns the txns list with extraction_mode tagged on
    # each row. exceptions propagate to the orchestrator, which isolates them per file.
    txns, mode = ai.extract_transactions_from_file(
        client, data, name,
        category_names=category_names, merchant_memory=merchant_memory,
        recent_corrections=recent_corrections, force_vision=False,
    )
    if _should_retry_with_vision(txns, mode):
        txns, mode = ai.extract_transactions_from_file(
            client, data, name,
            category_names=category_names, merchant_memory=merchant_memory,
            recent_corrections=recent_corrections, force_vision=True,
        )
    for t in txns:
        t["extraction_mode"] = mode
    return txns


def _apply_learned_rescue(transactions: list[dict], memory_rows: list[dict],
                          category_names: list[str]) -> int:
    # after extraction, categorize rows the AI was unsure about (Needs Review) using the
    # user's learned library, locally, with no API call. only fills in a confident,
    # unambiguous match (exact, then city-stripped, then multi-word), it never changes a row
    # the AI was already confident about, so it cannot introduce a false positive.
    # conflicting learned categories stay Needs Review. mutates rows in place, returns how
    # many were rescued. this is what lets the app lean more on its own learning over time.
    if not transactions or not memory_rows:
        return 0
    rescued = 0
    for t in transactions:
        if not t.get("needs_review"):
            continue
        cat, conf, _tier = db.match_learned_category(t.get("merchant", ""), memory_rows)
        if cat and cat in category_names and cat != ai.REVIEW_CATEGORY:
            t["category"] = cat
            t["needs_review"] = False
            t["confidence"] = max(float(t.get("confidence") or 0.0), conf)
            crumb = "auto-categorized from your learned stores"
            note = t.get("note")
            t["note"] = f"{note} | {crumb}" if note else crumb
            rescued += 1
    return rescued


# Bounded concurrency for multi-file uploads. Deliberately low: a single user firing
# a few requests at once stays comfortably under Anthropic's rate limits, so the new
# parallelism adds a big speedup on large batches without a meaningful new failure
# surface. _RATE_LIMIT_BACKOFF is how long a rate-limited file waits before its one
# retry (tests set it to 0 to stay fast).
_UPLOAD_CONCURRENCY = 3
_RATE_LIMIT_BACKOFF = 2.0


async def _run_extractions(items, *, extract_fn, on_progress=None,
                           on_file_done=None, on_error=None,
                           concurrency: int = _UPLOAD_CONCURRENCY):
    # run extract_fn over items=[(name, data), ...], return (ordered_txns, fatal).
    # reliability contract (covered by tests):
    #   order-preserving, results concatenate in submission order no matter which file
    #     finishes first.
    #   cache-warm, the first file runs alone before the rest fan out, so the prompt
    #     cache is written once and every later call is a cheap cache read.
    #   isolated, one file raising never drops another's results.
    #   fatal short-circuit, if on_error(name, exc) returns True (bad key, billing, or
    #     rate-limit, things that block all calls), no new files are started.
    #   transient-error resilient, a RateLimitError or OverloadedError (Anthropic 529)
    #     is retried once after a short backoff before being surfaced.
    # extract_fn(name, data) is a blocking callable run in a worker thread so the UI
    # event loop keeps breathing. on_progress(done, total), on_file_done(name, txns),
    # on_error(name, exc) returns bool fatal. callbacks may each be None.
    total = len(items)
    if total == 0:
        return [], False
    results: dict[int, list[dict]] = {}
    done = 0
    fatal = False

    async def attempt(name, data):
        try:
            return await asyncio.to_thread(extract_fn, name, data)
        except (ai.RateLimitError, ai.OverloadedError):
            await asyncio.sleep(_RATE_LIMIT_BACKOFF)  # brief backoff, then one retry
            return await asyncio.to_thread(extract_fn, name, data)

    async def process(idx, name, data):
        nonlocal done, fatal
        try:
            txns = await attempt(name, data)
            results[idx] = txns
            if on_file_done is not None:
                on_file_done(name, txns)
        except Exception as exc:  # noqa: BLE001, per-file isolation is the point
            if on_error is not None and on_error(name, exc):
                fatal = True
        finally:
            done += 1
            if on_progress is not None:
                on_progress(done, total)

    # 1) first file alone, warms the cache and trips a fatal key/billing error before
    #    we fan dozens of calls out against a broken key.
    await process(0, items[0][0], items[0][1])

    # 2) Remaining files at bounded concurrency, stopping early on a fatal error.
    if not fatal and total > 1:
        sem = asyncio.Semaphore(max(1, concurrency))

        async def worker(idx, name, data):
            if fatal:
                return
            async with sem:
                if fatal:
                    return
                await process(idx, name, data)

        await asyncio.gather(*(worker(i, n, d)
                               for i, (n, d) in enumerate(items) if i >= 1))

    ordered: list[dict] = []
    for i in range(total):
        ordered.extend(results.get(i, []))
    return ordered, fatal


def _render_upload_unavailable():
    # shown on the Upload tab while the app is offline. reading files is the only thing
    # paused, so we say so plainly and point the user to the Home tab's Add transaction
    # button. no buttons here, the header chip/banner already cover Settings. just a clear
    # heads-up.
    has_key = bool(state.api_key)
    with ui.column().classes("w-full items-center").style(
            "gap: 8px; padding: 32px 16px; text-align: center;"):
        ui.icon("cloud_off").style("font-size: 2rem; color: var(--bb-text-muted);")
        ui.label("Upload is off in offline mode").classes("text-subtitle1")
        if has_key:
            ui.label("Reading files needs the AI API, and it isn't available right now. Add "
                     "transactions by hand from the Home tab. Upload turns back on by itself "
                     "once the AI API is available again.") \
              .classes("text-caption text-muted").style("max-width: 440px; line-height: 1.4;")
        else:
            ui.label("Reading files needs the AI API, so upload is off until you add an API key. "
                     "Add transactions by hand from the Home tab, or add a key in Settings.") \
              .classes("text-caption text-muted").style("max-width: 440px; line-height: 1.4;")


def _render_upload_inner():
    if not api_available():
        _render_upload_unavailable()
        return

    ui.label("Import your transactions").classes("text-h6").style("margin-top: 8px;")
    ui.label("Add transactions from a file or by pasting text.") \
      .classes("text-caption text-muted")

    uploaded_files_state = {"files": []}
    MAX_QUEUED = 10  # how many files can wait to be extracted at once

    def _fmt_size(n: int) -> str:
        if n < 1024:
            return f"{n} B"
        if n < 1024 * 1024:
            return f"{n / 1024:.0f} KB"
        return f"{n / (1024 * 1024):.1f} MB"

    def render_queued():
        # redraw the app's own list of files waiting to be extracted. this list, not
        # Quasar's hidden built-in one, is the single source of truth for what Extract
        # will read, so the per-file remove cross and Clear-all below reliably change it.
        queued_box.clear()
        files = uploaded_files_state["files"]
        if not files:
            return
        with queued_box:
            with ui.element("div").classes("bb-queued"):
                with ui.element("div").classes("bb-queued-head"):
                    ui.label(f"{len(files)} file{'s' if len(files) != 1 else ''} ready "
                             "to extract").classes("text-caption text-muted")
                    ui.button("Clear all", icon="close", on_click=clear_all_files) \
                        .props("flat dense color=negative")
                for idx, (fname, fdata) in enumerate(files):
                    with ui.element("div").classes("bb-queued-file"):
                        ui.icon("description").style("color: var(--bb-text-muted);")
                        ui.label(fname).classes("name")
                        ui.label(_fmt_size(len(fdata))).classes("size")
                        ui.button(icon="close", on_click=lambda e, i=idx: remove_file(i)) \
                            .props("flat round dense") \
                            .style("color: var(--bb-text-muted);")

    def _reset_upload_widget():
        # clear Quasar's own internal upload queue (separate from uploaded_files_state).
        # Quasar remembers every file it has handled and its dedup filter then rejects
        # picking the same file again (Some files were skipped...) even after the user
        # removed it from our list. resetting after any removal lets the same file be
        # chosen again. cosmetic, never let it break the click.
        try:
            upload.reset()
        except Exception:  # noqa: BLE001, cosmetic reset, never let it break the click
            pass

    def remove_file(idx: int):
        # drop one queued file (the close cross beside it), e.g. the user added it by mistake.
        files = uploaded_files_state["files"]
        if 0 <= idx < len(files):
            files.pop(idx)
        _reset_upload_widget()  # so Quasar won't block re-picking this same file later
        render_queued()

    def clear_all_files():
        # empty the whole queue (Clear all) so the user can start the upload over.
        uploaded_files_state["files"].clear()
        _reset_upload_widget()  # also clear Quasar's internal queue/counter
        render_queued()

    async def on_upload(e: events.UploadEventArguments):
        try:
            data = await e.file.read()
            name = e.file.name
            if len(uploaded_files_state["files"]) >= MAX_QUEUED:
                notify(f"You can queue up to {MAX_QUEUED} files at once, extract or remove "
                       "some first.", type="warning", group="upload-limit")
                return
            # skip an exact duplicate already waiting (same name and size). because a per-file
            # remove now resets Quasar's queue, a file still shown in our list could otherwise
            # be appended a second time if the user re-picked it.
            if any(n == name and len(d) == len(data)
                   for n, d in uploaded_files_state["files"]):
                return
            uploaded_files_state["files"].append((name, data))
            render_queued()  # the visible list is the feedback (no per-file toast pile-up)
        except Exception as ex:
            notify(f"Couldn't read file: {ex}", type="negative", group="upload-error")

    def on_rejected():
        # Fires when a drop/selection breaks a size or type limit (the file-count cap is
        # enforced in on_upload against our own queue). Quasar doesn't say which, so the
        # message covers both.
        notify("Some files were skipped. Max 32 MB each, PDF/PNG/JPG/TXT only.",
               type="warning", group="upload-reject")

    ui.label("Upload files").classes("text-subtitle1").style("margin-top: 14px; margin-bottom: 6px;")
    upload = ui.upload(
        label="Drop files or click to browse",
        multiple=True, auto_upload=True,
        max_file_size=32 * 1024 * 1024,
        on_upload=on_upload,
        on_rejected=on_rejected,
    ).props('accept=".pdf,.png,.jpg,.jpeg,.txt"').classes("w-full bb-upload") \
        .style("cursor: pointer;")

    # the app's own queued-files list (per-file remove and Clear-all), directly under the box.
    queued_box = ui.column().classes("w-full").style("gap: 0;")
    render_queued()

    # OR divider, the two boxes are alternatives (upload a file or paste text),
    # not a two-step sequence. the centred label between two rules makes that explicit.
    with ui.row().classes("w-full items-center no-wrap").style("margin: 18px 0 6px; gap: 12px;"):
        ui.element("div").classes("flex-grow").style("height: 1px; background: var(--bb-border);")
        ui.label("OR").classes("text-caption text-muted") \
            .style("font-weight: 700; letter-spacing: 0.12em;")
        ui.element("div").classes("flex-grow").style("height: 1px; background: var(--bb-border);")

    ui.label("Paste text").classes("text-subtitle1").style("margin-top: 0; margin-bottom: 6px;")
    paste_area = ui.textarea(
        placeholder="Paste your transactions here",
    ).props("outlined autogrow maxlength=20000 counter").classes("w-full")

    _extract_busy = {"running": False}

    def _set_extract_busy(busy: bool):
        # visually and functionally lock the Extract button while a batch is in flight.
        _extract_busy["running"] = busy
        if extract_btn.is_deleted:
            return
        if busy:
            extract_btn.props("loading")
            extract_btn.disable()
        else:
            extract_btn.props(remove="loading")
            extract_btn.enable()

    async def extract():
        # ignore redundant clicks: if a batch is already processing, a second (accidental
        # or impatient) click must not start a second run over the same files.
        if _extract_busy["running"]:
            return
        # Safety net: the API can drop between rendering this form and clicking Extract.
        # If so, flip to manual mode instead of attempting (and failing) a call.
        if not api_available():
            ui.notify("Upload is off in offline mode (the AI API isn't available). Add "
                      "transactions by hand from the Home tab.", type="warning")
            refresh_upload()
            return
        files = list(uploaded_files_state["files"])
        pasted = (paste_area.value or "").strip()
        if pasted:
            files.append(("Pasted transactions.txt", pasted.encode("utf-8")))
        if not files:
            ui.notify("Drop a file, or paste some transactions, above first", type="warning")
            return
        client = get_client()
        cs = cat_names()
        mem = merchant_memory_dict()
        corrections = recent_corrections()

        # Skip empty files up front (with a heads-up) so they don't count toward progress.
        items: list[tuple[str, bytes]] = []
        for name, data in files:
            if not data:
                notify(f"❌ {name}: empty file", type="negative", group="upload-empty")
            else:
                items.append((name, data))
        if not items:
            return
        total = len(items)

        # Lock the button now (everything above ran synchronously, before the first
        # await, so a rapid second click can't have slipped past the guard yet).
        _set_extract_busy(True)

        # Visible progress card so the user knows the app isn't frozen.
        with ui.card().classes("w-full").style("padding: 16px; margin-top: 10px;") as progress_card:
            progress_label = ui.label(f"Reading files...  (0 of {total})").classes("text-body2")
            progress = ui.linear_progress(value=0, show_value=False).classes("w-full")
            progress_sub = ui.label("").classes("text-caption text-muted")

        # the per-file callbacks below run inside asyncio.gather worker tasks, which do
        # not inherit this handler's NiceGUI UI slot. capture the page client here (we're in
        # a valid slot context) so those callbacks can re-enter it via with ui_client:,
        # otherwise ui.notify raises slot stack is empty and the unhandled error breaks
        # the whole page (the exact bug this fixes).
        ui_client = ui.context.client

        def _extract_fn(name, data):
            # blocking, the orchestrator runs it in a worker thread so the websocket
            # heartbeat keeps responding (prevents Connection Lost).
            return _extract_one_file(
                client, name, data,
                category_names=cs, merchant_memory=mem, recent_corrections=corrections)

        # did this batch latch a persistent offline reason (bad key or out of credits)?
        # files run concurrently, so a sibling can fail billing while another parses fine,
        # we must not let that lucky success wipe out the real account problem (see
        # _on_file_done). only the two persistent error types latch, the same set
        # _report_api_error flips offline, keep these in sync.
        batch_latched_offline = False

        def _on_progress(done, total_):
            with ui_client:
                progress.value = done / max(total_, 1)
                progress_label.text = f"Processed {done} of {total_}"

        def _on_file_done(name, txns):
            with ui_client:
                # a call just succeeded, clear a stale warning from a previous attempt.
                # but never clear a latch this batch just set: if a sibling file failed for
                # a bad key or no credits, one file happening to parse doesn't fix the account.
                if not batch_latched_offline:
                    _set_api_health(None)
                if not txns:
                    notify(f"⚠️ {name}: no transactions extracted", type="warning",
                           group="upload-none")
                else:
                    progress_sub.text = f"Found {len(txns)} transactions in {name}"

        def _on_error(name, exc):
            # AIError gets friendly handling (returns True to halt on key/billing/rate/overload).
            # anything else is a one-off, report it and keep going on other files. wrapped in
            # the page client so ui.notify works from inside the gather worker task.
            nonlocal batch_latched_offline
            with ui_client:
                if isinstance(exc, ai.AIError):
                    if isinstance(exc, (ai.APIKeyError, ai.BillingError)):
                        batch_latched_offline = True  # persistent: protect from later success
                    return _report_api_error(exc, context=name)
                notify(f"❌ {name}: {type(exc).__name__}: {exc}",
                       type="negative", timeout=0, close_button=True, group="upload-error")
                return False

        all_extracted: list[dict] = []
        try:
            all_extracted, _fatal = await _run_extractions(
                items, extract_fn=_extract_fn, on_progress=_on_progress,
                on_file_done=_on_file_done, on_error=_on_error)
            progress.value = 1.0
            progress_label.text = "Done"
        except Exception as exc:  # noqa: BLE001, last-resort guard. per-file errors are
            # already handled above, this only catches something truly unexpected so the
            # upload flow can never crash the page. show it and recover.
            with ui_client:
                ui.notify(f"❌ Couldn't finish processing the upload: {exc}",
                          type="negative", timeout=0, close_button=True)
        finally:
            # teardown must never raise here: this runs in a finally, so an exception would
            # mask the extraction result and abort the function before the preview renders (the
            # user would just see the generic something went wrong net and lose their rows).
            # the card can already be gone if the upload container was re-rendered mid-extraction
            # (e.g. a second Extract click whose own refresh_upload() cleared it, or any other
            # re-render): clear() marks descendants is_deleted and drops them from their slot, so a
            # plain .delete() then raises ValueError: list.remove(x): x not in list. skip when
            # already deleted, and swallow any residual lifecycle race so the flow always survives.
            if not progress_card.is_deleted:
                try:
                    progress_card.delete()
                except Exception:  # noqa: BLE001, teardown is best-effort, never crash the page
                    pass
            # Always release the click-lock (refresh_upload() below rebuilds the button
            # anyway, but this also covers any post-processing error before that runs).
            try:
                _set_extract_busy(False)
            except Exception:  # noqa: BLE001, never let unlocking crash the flow
                _extract_busy["running"] = False

        # local, no-API learning rescue: upgrade rows the AI was unsure about (Needs
        # Review) using the user's learned library. never touches a confident AI row, so
        # it can't add false positives, it just means more rows arrive pre-categorized as
        # the library grows, which is the whole point of leaning on learning over time.
        if all_extracted:
            _apply_learned_rescue(all_extracted, db.get_merchant_memory(DB_PATH), cs)

        state.extracted_preview = all_extracted
        state.preview_categories = {i: t["category"] for i, t in enumerate(all_extracted)}
        state.preview_merchants = {i: t["merchant"] for i, t in enumerate(all_extracted)}
        state.preview_dates = {i: t["date"] for i, t in enumerate(all_extracted)}
        state.preview_amounts = {i: abs(float(t["amount"])) for i, t in enumerate(all_extracted)}
        state.preview_kinds = {i: ("Income" if t["amount"] > 0 else "Expense")
                               for i, t in enumerate(all_extracted)}
        upload.reset()
        uploaded_files_state["files"] = []
        if all_extracted:
            ui.notify(f"Extracted {len(all_extracted)} transactions", type="positive")
        refresh_upload()
        # upload spent tokens, update just the spend value, in place. we deliberately do not
        # rebuild the whole Settings panel here: the user is on the Upload tab, so the Settings
        # panel is unmounted, and rebuilding its tooltips there logs harmless
        # Anchor: target not found console errors. _update_spend_meter patches one label.
        _update_spend_meter()

    extract_btn = ui.button("Extract transactions", icon="auto_awesome", on_click=extract) \
        .props("color=primary unelevated").classes("w-full").style("margin-top: 10px;")


def _render_upload_preview():
    # the Review before saving table. rendered separately from the upload form so it
    # survives offline: if an API failure interrupts a batch, the rows already extracted
    # stay here to review, edit and save (saving needs no API) even though the form above
    # has switched to the manual/offline card.
    if not state.extracted_preview:
        return
    ui.separator().style("margin: 18px 0 12px;")
    ui.label("Review before saving").classes("text-h6")
    ui.label("Edit the date, name, category, type (Expense/Income), or amount on any "
             "row, or delete rows you don't want, before saving. Rows that need a "
             "look are tagged: "
             "'Review category' is a low-confidence guess to verify, and "
             "'Possible duplicate' matches something you may already have "
             "(it's saved, not skipped).") \
      .classes("text-caption text-muted").style("margin-bottom: 10px;")

    _render_preview_rows()


def _preview_flag_chip(icon: str, text: str, color: str):
    # a small, clearly-labeled attention tag used in the extraction preview.
    # both flag reasons (category-review and possible-duplicate) render as one of these
    # so they read as the same needs attention family, they share the ⚠️ warning icon
    # and are distinguished by their label and colour. returns the chip element so callers
    # can toggle its visibility live.
    chip = ui.row().classes("items-center no-wrap").style(
        "gap: 4px; padding: 1px 9px 1px 6px; border-radius: 999px; "
        f"background: color-mix(in srgb, {color} 16%, transparent); "
        f"border: 1px solid color-mix(in srgb, {color} 38%, transparent);"
    )
    with chip:
        ui.icon(icon).style(f"font-size: 0.95rem; color: {color};")
        ui.label(text).style(
            f"font-size: 0.72rem; font-weight: 600; color: {color}; "
            "text-transform: none; letter-spacing: 0; white-space: nowrap;")
    return chip


def _effective_amount(i: int, orig: dict) -> float:
    # a preview row's current signed amount: the magnitude from the editable Amount
    # field (falling back to the extracted value) with its sign set by the Expense/Income
    # toggle, Income positive, Expense negative.
    orig_amt = float(orig.get("amount", 0) or 0)
    mag = state.preview_amounts.get(i)
    if mag is None:
        mag = abs(orig_amt)
    kind = state.preview_kinds.get(i) or ("Income" if orig_amt > 0 else "Expense")
    return mag if kind == "Income" else -mag


# upload preview: unsaveable-row guards
# a row in the review-and-save table can't be saved while its amount is $0 or its name is one
# the extractor falls back to when it couldn't read the merchant. these are not skipped, the
# user must fix the amount/name or delete the row first (mirrors the Home add-dialog red-border
# block). deliberately narrow so it never nags on normal rows. the $0/placeholder-name
# rules themselves (_amount_is_zero, _preview_name_bad, _PREVIEW_PLACEHOLDER_NAMES) are pure
# and live in logic.py, the two functions below add the per-row state (the editable amount
# and Expense/Income toggle) that those pure checks can't see.
def _preview_amount_bad(i: int, orig: dict) -> bool:
    # true when a preview row's effective (signed) amount is $0, unsaveable.
    return _amount_is_zero(_effective_amount(i, orig))


def _preview_row_problems(i: int, orig: dict) -> list[str]:
    # human-readable reasons a preview row can't be saved yet (empty list means it's fine).
    problems: list[str] = []
    if _preview_amount_bad(i, orig):
        problems.append("amount is $0")
    cur_name = state.preview_merchants.get(i) or orig.get("merchant")
    if _preview_name_bad(cur_name):
        problems.append("name not recognized")
    return problems


def _set_field_error(el, on: bool) -> None:
    # toggle a preview field's red error outline. uses a CSS class (.bb-field-bad) rather
    # than Quasar's error prop: NiceGUI reliably syncs a live .classes() change in both
    # directions, but a live-added error prop never re-renders on the client (so a field the
    # user edits down to $0 would stay un-highlighted). no message line is shown, so grid rows
    # stay aligned. safe to call at first render and on every live re-validation.
    if on:
        el.classes(add="bb-field-bad")
    else:
        el.classes(remove="bb-field-bad")


def _preview_kind_changed(idx: int, value: str):
    # flip a preview row between Expense and Income, then re-render so the category
    # control re-locks/unlocks to match (Income locks to the Income category) and the
    # amount's sign/colour follow.
    state.preview_kinds[idx] = value
    refresh_upload()


def _delete_preview_row(idx: int):
    # drop one row from the extraction preview, preserving edits made to the others.
    rows = []
    kinds: list[str] = []
    amounts: list[float | None] = []
    for i, t in enumerate(state.extracted_preview):
        if i == idx:
            continue
        r = dict(t)
        if i in state.preview_dates:
            r["date"] = state.preview_dates[i]
        if i in state.preview_merchants:
            r["merchant"] = state.preview_merchants[i]
        if i in state.preview_categories:
            r["category"] = state.preview_categories[i]
        rows.append(r)
        kinds.append(state.preview_kinds.get(i)
                     or ("Income" if float(t["amount"]) > 0 else "Expense"))
        amounts.append(state.preview_amounts.get(i, abs(float(t["amount"]))))
    state.extracted_preview = rows
    state.preview_dates = {i: r["date"] for i, r in enumerate(rows)}
    state.preview_merchants = {i: r["merchant"] for i, r in enumerate(rows)}
    state.preview_categories = {i: r["category"] for i, r in enumerate(rows)}
    state.preview_kinds = {i: k for i, k in enumerate(kinds)}
    state.preview_amounts = {i: a for i, a in enumerate(amounts)}
    state.preview_duplicates = {}
    notify("Removed 1 row." if rows else "All rows removed. Nothing to save.",
           type="info", group="preview-row")
    refresh_upload()


def _render_preview_rows():
    cs = cat_names()

    # flag rows that look like duplicates, of something already saved or of an earlier
    # row in this same batch. these are flags only: the row still saves, the user just
    # gets a ⚠️ heads-up. uses each row's current (possibly edited) date/name value.
    # two signals, both requiring the same date: (a) same amount, names ignored, a loose
    # net for extraction mangling the name (e.g. a phone number pulled out as the merchant),
    # (b) same name regardless of amount, a charge re-listed the same day from the same
    # store. it's only a heads-up, nothing is skipped, the user decides.
    state.preview_duplicates = {}
    seen_amt: set[tuple] = set()
    seen_name: set[tuple] = set()
    for i, t in enumerate(state.extracted_preview):
        d = (state.preview_dates.get(i) or t["date"] or "").strip()
        m = (state.preview_merchants.get(i) or t["merchant"] or "").strip()
        eff = _effective_amount(i, t)
        nm = db.normalize_merchant(m)
        amt_key = (d, eff)
        name_key = (d, nm) if nm else None
        is_dup = (amt_key in seen_amt) or (name_key is not None and name_key in seen_name) or (
            db.find_duplicate_id(DB_PATH, d, m, eff, ignore_merchant=True) is not None)
        state.preview_duplicates[i] = is_dup
        seen_amt.add(amt_key)
        if name_key is not None:
            seen_name.add(name_key)

    # shared column widths, the header and every input row use these exact values plus
    # the same 8px gap, so each column title always sits directly above its own cells.
    # the grid lives in a horizontal-scroll wrapper with a min-width, so on narrow/mobile
    # screens the columns keep their alignment (and scroll) instead of collapsing.
    # flex-shrink:0 keeps every fixed column rigid so the header row and each
    # data row compute the same widths (only the flex-grow Name column flexes,
    # identically in both), otherwise the Quasar fields shrink under flex
    # pressure and the columns drift out from under their titles.
    C_DATE, C_CAT, C_TYPE, C_AMT, C_DEL = (
        "width: 140px; flex-shrink: 0;", "width: 168px; flex-shrink: 0;",
        "width: 168px; flex-shrink: 0;", "width: 120px; flex-shrink: 0;",
        "width: 40px; flex-shrink: 0;")

    with ui.element("div").classes("w-full").style("overflow-x: auto;"):
        with ui.column().classes("w-full").style("min-width: 820px; gap: 0;"):
            # header
            with ui.row().classes("w-full no-wrap items-center").style(
                "padding: 6px 8px; gap: 8px; color: var(--bb-text-muted); "
                "font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.05em;"
            ):
                # Field text inside dense-outlined inputs is inset ~12px by
                # Quasar's control padding, so inset these titles by the same
                # amount to sit directly over their column's values. (Amount is
                # a plain right-aligned label, so it needs no inset.)
                ui.label("").style(C_DEL)
                ui.label("Date").style(C_DATE + " padding-left: 12px;")
                ui.label("Name").classes("flex-grow").style("padding-left: 12px;")
                ui.label("Category").style(C_CAT + " padding-left: 12px;")
                ui.label("Type").style(C_TYPE + " padding-left: 4px;")
                ui.label("Amount").style(C_AMT + " text-align: right;")

            # rows
            for i, t in enumerate(state.extracted_preview):
                # type and magnitude come from state (the toggle / amount field), defaulting
                # to the extracted sign and value on first render.
                kind = state.preview_kinds.get(i) or ("Income" if t["amount"] > 0 else "Expense")
                is_income = (kind == "Income")
                mag = state.preview_amounts.get(i)
                if mag is None:
                    mag = abs(float(t["amount"]))
                is_dup = bool(state.preview_duplicates.get(i))
                # the effective (possibly edited) category drives the review tag, using the
                # same rule as save: a row needs review exactly when its category is
                # Needs Review. computed before the tags so the tag reflects it immediately.
                if is_income:
                    eopts, eval_ = None, "Income"
                    state.preview_categories[i] = "Income"
                else:
                    eopts = _expense_category_names(cs)
                    # prefer the user's prior expense pick, else the AI's guess, else Review.
                    prev = state.preview_categories.get(i)
                    if prev and prev in eopts:
                        eval_ = prev
                    elif t["category"] in eopts:
                        eval_ = t["category"]
                    elif ai.REVIEW_CATEGORY in eopts:
                        eval_ = ai.REVIEW_CATEGORY
                    else:
                        eval_ = eopts[0]
                    state.preview_categories[i] = eval_
                review_now = (eval_ == ai.REVIEW_CATEGORY)
                # unsaveable-row guards: a $0 amount or an unrecognized name (e.g. the
                # extractor's Unknown) blocks the save, flagged red here and re-checked at
                # save time. the row carries an index-based class so a blocked save can scroll
                # the user straight to it.
                cur_name = state.preview_merchants.get(i) or t["merchant"]
                bad_name = _preview_name_bad(cur_name)
                bad_amt = _preview_amount_bad(i, t)
                with ui.column().classes(f"w-full bb-prow-{i}").style(
                    "gap: 6px; padding: 8px; border-bottom: 1px solid var(--bb-border);"
                ):
                    # attention tags, one per reason, clearly labeled and visually distinct.
                    # red fix this tags come first (they block save), amber review category
                    # and blue possible duplicate are heads-ups only. each is built up-front
                    # (hidden unless active) so edits toggle it live, the container collapses
                    # (display:none) when no tag applies.
                    tags_row = ui.row().classes("items-center").style("gap: 6px;")
                    review_chip = None
                    with tags_row:
                        amt_problem_chip = _preview_flag_chip(
                            "error", "Amount can't be $0, fix or delete", "var(--bb-expense)")
                        amt_problem_chip.set_visibility(bad_amt)
                        name_problem_chip = _preview_flag_chip(
                            "error", "Name not recognized, fix or delete", "var(--bb-expense)")
                        name_problem_chip.set_visibility(bad_name)
                        if not is_income:
                            review_chip = _preview_flag_chip(
                                "warning", "Review category", "var(--bb-warning)")
                            review_chip.set_visibility(review_now)
                        if is_dup:
                            _preview_flag_chip("warning", "Possible duplicate", "var(--bb-accent)")
                    tags_row.set_visibility(review_now or is_dup or bad_amt or bad_name)
                    # input row, aligns 1:1 with the header above.
                    with ui.row().classes("w-full no-wrap items-center").style("gap: 8px;"):
                        # delete sits on the left, away from the (also red) amount, so it
                        # can't be mistaken for part of the expense figure.
                        ui.button(icon="delete", on_click=lambda e, idx=i: _delete_preview_row(idx)) \
                            .props("flat dense round color=negative") \
                            .style(C_DEL + " min-width: 40px; min-height: 40px;") \
                            .tooltip("Remove this row from the preview")
                        _date_picker_field(
                            t["date"], dense=True, style=C_DATE,
                            on_change=lambda v, idx=i: state.preview_dates.update({idx: v}))
                        store_input = ui.input(value=t["merchant"]) \
                            .props("dense outlined").classes("flex-grow")
                        if bad_name:
                            _set_field_error(store_input, True)
                        if is_income:
                            # Income rows lock to the Income category (expenses-only pool).
                            sel = ui.select(["Income"], value="Income") \
                                .props("dense outlined").style(C_CAT)
                            sel.disable()
                        else:
                            sel = ui.select(eopts, value=eval_) \
                                .props("dense outlined").style(C_CAT)
                        # type toggle, flips the row between Expense and Income. the amount's
                        # sign and the category lock follow it (handled on re-render).
                        type_toggle = ui.toggle(["Expense", "Income"], value=kind) \
                            .props("dense no-caps spread").style(C_TYPE)
                        type_toggle.on_value_change(
                            lambda e, idx=i: _preview_kind_changed(idx, e.value))
                        # amount is the positive magnitude, the toggle decides the sign. tinted
                        # green/red to echo the type at a glance.
                        color_cls = "text-positive" if is_income else "text-negative"
                        amt_input = ui.input(value=bb.amount_str(mag, grouped=False)) \
                            .props('dense outlined inputmode=decimal prefix=$ '
                                   f'input-class="text-right {color_cls}"') \
                            .style(C_AMT)
                        if bad_amt:
                            _set_field_error(amt_input, True)

                    # live re-validation: as the user edits the name, amount or category, refresh
                    # this row's red borders and chips (and the amber review tag) without a full
                    # re-render, so a fix clears instantly and a new problem shows instantly.
                    def _sync_tags(idx=i, orig=t, trow=tags_row, rchip=review_chip,
                                   achip=amt_problem_chip, nchip=name_problem_chip,
                                   ainp=amt_input, sinp=store_input, dup=is_dup, income=is_income):
                        cur = state.preview_merchants.get(idx) or orig["merchant"]
                        bn = _preview_name_bad(cur)
                        ba = _preview_amount_bad(idx, orig)
                        rn = (not income) and (state.preview_categories.get(idx) == ai.REVIEW_CATEGORY)
                        if rchip is not None:
                            rchip.set_visibility(rn)
                        achip.set_visibility(ba)
                        nchip.set_visibility(bn)
                        _set_field_error(ainp, ba)
                        _set_field_error(sinp, bn)
                        trow.set_visibility(rn or dup or ba or bn)

                    store_input.on_value_change(
                        lambda e, idx=i, sync=_sync_tags:
                            (state.preview_merchants.update({idx: e.value}), sync()))
                    amt_input.on_value_change(
                        lambda e, idx=i, sync=_sync_tags:
                            (state.preview_amounts.update({idx: _parse_amount(e.value)}), sync()))
                    if not is_income:
                        sel.on_value_change(
                            lambda e, idx=i, sync=_sync_tags:
                                (state.preview_categories.update({idx: e.value}), sync()))

    # save / discard
    with ui.row().classes("w-full gap-2").style("margin-top: 12px;"):
        ui.button("Save all transactions", icon="save", on_click=_save_preview) \
            .props("color=primary unelevated").classes("flex-grow")
        ui.button("Discard all", on_click=_discard_preview).props("flat")


def _save_preview():
    # block the save outright if any row is unsaveable, a $0 amount or an unrecognized name
    # (e.g. the extractor's Unknown). these are not skipped: the user must fix the amount or
    # name or delete the row first (same spirit as the Home add-dialog red-border block).
    # deliberately narrow so it never nags on normal rows, we point the user at the first one.
    bad = [(i, p) for i, p in
           ((i, _preview_row_problems(i, orig)) for i, orig in enumerate(state.extracted_preview))
           if p]
    if bad:
        n = len(bad)
        ui.notify(
            f"{n} transaction{'s' if n != 1 else ''} can't be saved yet, fix the amount or "
            "name (or delete the row). The rows needing attention are highlighted in red below.",
            type="negative", multi_line=True, timeout=6000)
        first_idx = bad[0][0]
        ui.run_javascript(
            f'document.querySelector(".bb-prow-{first_idx}")'
            '?.scrollIntoView({behavior: "smooth", block: "center"});')
        return

    clean_rows = []
    seen_amt: set[tuple] = set()
    seen_name: set[tuple] = set()
    for i, orig in enumerate(state.extracted_preview):
        # date, user may have edited it, fall back to the extracted value if blank/invalid.
        raw_date = (state.preview_dates.get(i) or orig["date"] or "").strip()
        try:
            date.fromisoformat(raw_date)
            new_date = raw_date
        except (ValueError, TypeError):
            new_date = orig["date"]
        new_store = state.preview_merchants.get(i, orig["merchant"]).strip() or orig["merchant"]
        # type toggle and editable amount decide the final signed amount, Income rows are
        # always the Income category (expenses-only pool).
        new_amount = _effective_amount(i, orig)
        kind = state.preview_kinds.get(i) or ("Income" if orig["amount"] > 0 else "Expense")
        if kind == "Income":
            new_cat = "Income"
        else:
            new_cat = state.preview_categories.get(i, orig["category"])
        still_review = (new_cat == ai.REVIEW_CATEGORY)
        # authoritative duplicate check at save time, flag (never skip) a row matching
        # something already saved or an earlier row in this same batch. mirrors the preview:
        # same date and same amount (names ignored) or same date and same name (any amount), so
        # the persisted flag matches exactly what the user saw in the preview.
        nm = db.normalize_merchant(new_store)
        amt_key = (new_date, float(new_amount))
        name_key = (new_date, nm) if nm else None
        is_dup = (amt_key in seen_amt) or (name_key is not None and name_key in seen_name) or (
            db.find_duplicate_id(DB_PATH, new_date, new_store, new_amount,
                                 ignore_merchant=True) is not None)
        seen_amt.add(amt_key)
        if name_key is not None:
            seen_name.add(name_key)
        clean_rows.append({
            "date": new_date,
            "merchant": new_store,
            "amount": new_amount,
            "category": new_cat,
            "confidence": orig.get("confidence"),
            "needs_review": still_review,
            "is_duplicate": is_dup,
            "note": orig.get("note"),
            "source": orig.get("source", "image"),
            "source_file": orig.get("source_file"),
            "extraction_mode": orig.get("extraction_mode"),
        })
    inserted, flagged = db.bulk_insert_transactions(DB_PATH, clean_rows)
    for r in clean_rows:
        if not r["needs_review"]:
            db.learn_merchant(DB_PATH, r["merchant"], r["category"])
    db_backup_if_changed()
    state.extracted_preview = []
    state.preview_categories.clear()
    state.preview_merchants.clear()
    state.preview_dates.clear()
    state.preview_amounts.clear()
    state.preview_kinds.clear()
    state.preview_duplicates.clear()
    ui.notify(f"Saved {inserted} transaction{'s' if inserted != 1 else ''}.", type="positive")
    if flagged:
        ui.notify(
            f"{flagged} flagged as possible duplicate{'s' if flagged != 1 else ''}. "
            "Saved for review, nothing was skipped.",
            type="warning",
        )
    refresh_upload()
    refresh_all()


def _discard_preview():
    state.extracted_preview = []
    state.preview_categories.clear()
    state.preview_merchants.clear()
    state.preview_dates.clear()
    state.preview_amounts.clear()
    state.preview_kinds.clear()
    state.preview_duplicates.clear()
    refresh_upload()


# categories tab

def refresh_categories():
    c = containers.get("categories")
    if c is None:
        return
    c.clear()
    with c:
        _render_categories_inner()


def _render_categories_inner():
    # safety net: reconcile every learned store's Confirmed count with the live transactions
    # before the list is read/shown below, so it's correct even if some mutation path missed
    # its per-change resync. idempotent, a no-op (no writes) when everything already matches.
    db.resync_all_merchant_memory(DB_PATH)
    ui.label("Categories").classes("text-h6").style("margin: 12px 0 6px;")
    ui.label("Every transaction uses one of these. Edit the name or emoji here "
             "(up to 2 emojis).") \
      .classes("text-caption text-muted")

    # Keep an emoji field to at most 2 emojis as the user types (whole grapheme
    # clusters, so flags/skin-tones/ZWJ families count as one). The guard avoids an
    # echo loop: once clamped, the value already equals the clamp so it stops.
    def _clamp_emoji_field(inp):
        trimmed = bb.clamp_emojis(inp.value or "")
        if (inp.value or "") != trimmed:
            inp.value = trimmed

    # reserved categories (Income / Needs Review) are pinned to the top, they carry a lock
    # and can't be edited or deleted, so they read as a fixed header above the editable ones.
    # sorted() is stable, so within each group the existing (sort_order, name) order is kept.
    ordered_cats = sorted(cats(), key=lambda c: 0 if c["is_protected"] else 1)
    for cat in ordered_cats:
        with ui.row().classes("w-full no-wrap items-center").style(
            "padding: 8px 0; border-bottom: 1px solid var(--bb-border); gap: 10px;"
        ):
            emoji_in = ui.input(value=cat["emoji"]).props("outlined dense").style("width: 76px;")
            emoji_in.on_value_change(lambda e, inp=emoji_in: _clamp_emoji_field(inp))
            name_in = ui.input(value=cat["name"]).props("outlined dense").classes("flex-grow")
            if cat["is_protected"]:
                # Income / Needs Review are reserved, fully locked (name and emoji), so there's
                # nothing to save: show only the lock badge, no Save button (it would imply an
                # editable, unsaved row, which is confusing). matches the disabled Income and
                # Needs Review selects in the Add/Edit/Upload dialogs.
                name_in.disable()
                emoji_in.disable()
                ui.label("🔒").style("padding: 0 8px; color: var(--bb-text-muted);")
            else:
                def save_cat(c=cat, e_in=emoji_in, n_in=name_in):
                    new_name = (n_in.value or "").strip()
                    if not new_name:
                        ui.notify("Name required", type="negative")
                        return
                    ok = db.update_category(DB_PATH, c["name"], new_name,
                                            bb.clamp_emojis(e_in.value or "") or c["emoji"],
                                            c["color"])
                    if ok:
                        ui.notify(f"Updated {new_name}", type="positive")
                        db_backup_if_changed()
                        refresh_all()
                    else:
                        ui.notify("Couldn't update (name may already exist).", type="negative")

                ui.button("Save", on_click=save_cat).props("flat dense")

                def del_cat(c=cat):
                    affected = len(db.get_transactions(DB_PATH, categories=[c["name"]]))
                    with ui.dialog() as d, ui.card().style("min-width: 380px; padding: 20px;"):
                        ui.label(f"Delete {c['name']}?").classes("text-h6")
                        ui.label(f"{affected} transaction(s) will move to 'Needs Review'.") \
                          .classes("text-caption text-muted")
                        with ui.row().classes("justify-end").style("margin-top: 12px;"):
                            ui.button("Cancel", on_click=d.close).props("flat")
                            def confirm():
                                ok, msg = db.delete_category(DB_PATH, c["name"])
                                if ok:
                                    notify(f"Deleted {c['name']}.", type="warning",
                                           group="cat-delete")
                                    db_backup_if_changed()
                                    d.close()
                                    refresh_all()
                                else:
                                    ui.notify(msg, type="negative")
                            ui.button("Delete", on_click=confirm).props("color=negative unelevated")
                    d.open()
                ui.button("Delete", on_click=del_cat).props("flat dense color=negative")

    ui.separator().style("margin: 16px 0 10px;")
    ui.label("Add new category").classes("text-subtitle1")

    with ui.row().classes("w-full no-wrap items-end").style("gap: 10px; margin-top: 6px;"):
        new_emoji = ui.input(value="🛒", label="Emoji").props("outlined dense").style("width: 96px;")
        new_emoji.on_value_change(lambda e: _clamp_emoji_field(new_emoji))
        new_name = ui.input(value="", placeholder="e.g. Grocery", label="Name") \
            .props("outlined dense stack-label").classes("flex-grow")

        def add():
            name = (new_name.value or "").strip()
            if not name:
                ui.notify("Name required", type="negative")
                return
            emoji = bb.clamp_emojis(new_emoji.value or "") or "📦"
            h = int(hashlib.md5(name.lower().encode()).hexdigest(), 16)
            palette = ["#f97316", "#16a34a", "#0ea5e9", "#8b5cf6", "#eab308",
                       "#ec4899", "#f43f5e", "#14b8a6", "#3b82f6", "#a855f7"]
            color = palette[h % len(palette)]
            ok = db.add_category(DB_PATH, name, emoji, color)
            if ok:
                ui.notify(f"Added category: {name}", type="positive")
                db_backup_if_changed()
                new_name.value = ""
                refresh_all()
            else:
                ui.notify("Category already exists", type="negative")

        ui.button("Add", on_click=add).props("color=primary unelevated")

    ui.separator().style("margin: 18px 0 8px;")

    mem = db.get_merchant_memory(DB_PATH)
    if not mem:
        ui.label("Learned stores").classes("text-subtitle1")
        ui.label("No learned stores yet. As you categorize transactions, the app remembers "
                 "each store here so it can categorize it automatically next time.") \
          .classes("text-caption text-muted").style("margin-top: 4px;")
        ui.separator().style("margin: 8px 0 18px;")
        return

    # foldable so this list can't make the page grow forever as more stores are learned.
    # collapsed by default (ui.expansion's value defaults to False), the count in the title
    # tells you how many there are without opening it. inside: one list with tick-boxes
    # (each row plus a header select-all box), a live N selected hint, and a single
    # forget-selected action, no separate dropdown duplicating the list.
    rows = [dict(m) for m in mem]
    cols = [
        {"name": "merchant_display", "label": "Name", "field": "merchant_display", "align": "left"},
        {"name": "category", "label": "Category", "field": "category", "align": "left"},
        {"name": "correction_count", "label": "Confirmed", "field": "correction_count", "align": "right"},
    ]

    with ui.expansion(f"Learned stores ({len(mem)})", icon="storefront") \
            .classes("w-full").props("expand-separator"):
        ui.label("Stores the app remembers so it can categorize them automatically. "
                 "Tick any you want it to forget, then choose Forget selected.") \
          .classes("text-caption text-muted").style("display: block; margin: 4px 0 8px;")

        table = ui.table(columns=cols, rows=rows, row_key="normalized_merchant",
                         selection="multiple").classes("w-full").props("flat dense")

        def _forget_selected():
            sel = list(table.selected or [])
            if not sel:
                ui.notify("Tick at least one store first.", type="warning")
                return
            for r in sel:
                db.forget_merchant(DB_PATH, r["normalized_merchant"])
            n = len(sel)
            ui.notify(f"Forgotten {n} store{'s' if n != 1 else ''}.", type="warning")
            refresh_all()

        def _toggle_all():
            # programmatic selection changes don't fire the frontend selection event,
            # so push the new state with update() and refresh the labels ourselves.
            table.selected = [] if len(table.selected) >= len(rows) else list(rows)
            table.update()
            _sync()

        with ui.row().classes("items-center w-full no-wrap") \
                .style("gap: 10px; margin-top: 8px;"):
            toggle_btn = ui.button("Select all", on_click=_toggle_all).props("flat dense")
            sel_lbl = ui.label("None selected").classes("text-caption text-muted")
            ui.space()
            forget_btn = ui.button("Forget selected", on_click=_forget_selected) \
                .props("unelevated color=negative")

        def _sync():
            n = len(table.selected)
            sel_lbl.set_text(f"{n} selected" if n else "None selected")
            forget_btn.set_enabled(n > 0)
            toggle_btn.set_text("Clear" if n and n >= len(rows) else "Select all")

        table.on_select(_sync)
        _sync()

    # close the section off with a matching rule below (like the one above it) so the
    # learned stores block reads as its own bounded section, not loose page tail.
    ui.separator().style("margin: 8px 0 18px;")


# self-update over git. the app folder is the public github checkout, so checking for a
# newer version is a fetch plus a commit compare, and installing one is a fast-forward
# pull (code only, never userdata/). every git call is best-effort: a missing git, no
# network, or a copy that was unzipped instead of cloned just yields a calm message and
# changes nothing. all of this runs off the event loop (asyncio.to_thread / a daemon
# thread) so the UI never blocks. _update_state in logic.py turns the raw facts into the
# state the Settings panel shows, and is unit-tested without a real repo.
_update_available = False   # set by the startup check + manual check; drives the banner + Settings hint
_update_banner_dismissed = False   # session-only: user dismissed the update banner this run
_reinstall_banner_dismissed = False   # session-only: user dismissed the reinstall banner this run
_update_check_done = False   # the one-shot startup update check has finished (the banner can poll for it)


def _git(*args: str, timeout: int = 30) -> tuple[bool, str]:
    # run a git command inside the app folder. returns (ok, trimmed stdout). never raises:
    # no git, a non-zero exit, or a timeout all come back as (False, "").
    try:
        r = subprocess.run(["git", "-C", str(APP_CODE_DIR), *args],
                           capture_output=True, text=True, timeout=timeout)
        return (r.returncode == 0, (r.stdout or "").strip())
    except Exception:
        return (False, "")


def _is_git_checkout() -> bool:
    ok, out = _git("rev-parse", "--is-inside-work-tree")
    return ok and out == "true"


def _git_usable() -> bool:
    # is a working git actually available? a plain `git --version` fails both when the
    # binary is missing and when only macOS's /usr/bin/git stub is present without the
    # Command Line Tools, so this tells "git not installed" apart from "not a git checkout".
    ok, _ = _git("--version")
    return ok


def check_for_update(do_fetch: bool = True) -> tuple[str, str]:
    # compare the local commit with what's on github. returns (state, message) from
    # logic._update_state. fetch first so the comparison sees the latest remote.
    if not _git_usable():
        return _update_state(False, False, "", "", git_installed=False)
    if not _is_git_checkout():
        return _update_state(False, False, "", "")
    fetch_ok = True
    if do_fetch:
        fetch_ok, _ = _git("fetch", "--quiet", "origin")
    _, local = _git("rev-parse", "HEAD")
    ok_r, remote = _git("rev-parse", "@{u}")     # the tracked upstream (origin/main)
    if not ok_r:
        ok_r, remote = _git("rev-parse", "origin/main")
    return _update_state(True, fetch_ok, local, remote if ok_r else "")


def apply_update() -> tuple[bool, str]:
    # pull the latest code. ff-only so it never creates a merge or touches local edits.
    # the app does not hot-reload, so the new code applies on the next launch.
    ok, _ = _git("pull", "--ff-only")
    if ok:
        return (True, "Update installed. Quit Budget and open it again to finish.")
    return (False, "Couldn't install the update. Check your internet connection and try again.")


def _install_git_tools() -> None:
    # best-effort: ask macOS to install the Command Line Tools (which include git), the
    # same prompt the launcher uses on a git-less Mac. harmless if they're already there
    # or mid-install. fired from the Updates panel when git turns out not to be usable.
    try:
        subprocess.run(["xcode-select", "--install"], capture_output=True, timeout=15)
    except Exception:
        pass


def _startup_update_check() -> None:
    # one quiet background check at launch so the banner + Settings can show an update is
    # waiting. failures are swallowed, it's best-effort. _update_check_done is set when it
    # finishes (however long the fetch takes) so each tab's poll surfaces the banner then stops.
    global _update_available, _update_check_done
    try:
        st, _ = check_for_update(do_fetch=True)
        _update_available = (st == "available")
    except Exception:
        _update_available = False
    finally:
        _update_check_done = True


def _start_update_check() -> None:
    threading.Thread(target=_startup_update_check, daemon=True).start()


app.on_startup(_start_update_check)


# settings tab

def refresh_settings():
    c = containers.get("settings")
    if c is None:
        return
    c.clear()
    with c:
        _render_settings_inner()


def _update_spend_meter():
    # update only the Estimated spend value in place, no full Settings rebuild.
    # called after an upload, which spends tokens. the Settings tab is almost always inactive
    # then (the user is on Upload), and an inactive Quasar tab panel isn't mounted in the DOM.
    # rebuilding the whole panel there (the old refresh_settings() call) recreated a tooltip
    # there, whose anchor target then didn't exist in the DOM, so Quasar logged a harmless but
    # noisy anchor target not found warning for it. patching this one label is a plain text
    # update (no anchored component), so it's silent and still shows the fresh number when the
    # user opens Settings. safe to call from the upload coroutine, never raises.
    if _spend_value_label is None:
        return
    try:
        usage = db.get_api_usage(DB_PATH)
        _spend_value_label.set_text(f"${usage['cost']:,.4f}")
    except Exception:
        pass


def _render_settings_inner():
    ui.label("API key").classes("text-h6").style("margin: 12px 0 6px;")

    api_in = ui.input(label="Anthropic API key", value=state.api_key,
                      password=True, placeholder="sk-ant-...") \
        .props("outlined stack-label").classes("w-full bb-api-key")

    def save_key():
        new = (api_in.value or "").strip()
        if new == state.api_key.strip():
            if state.api_error:
                # same key, but we're stuck offline (e.g. they just topped up credits on this
                # exact key). treat Save as a go back online action and clear the latch, this
                # replaces the old Try again button. the refreshes that delete this button's
                # own slot are not called here, so it can't crash.
                _set_api_health(None)   # clears latch, refreshes chip and banner
                refresh_upload()        # restore the full upload form
                ui.notify("Back online.", type="positive")
            else:
                ui.notify("No changes to save.")
            return
        db.set_setting(DB_PATH, "api_key", new)
        state.api_key = new
        global _client, _client_key
        _client = None
        _client_key = None
        _set_api_health(None, refresh=False)  # give the new key a clean slate
        ui.notify("API key saved.", type="positive")
        _refresh_header()
        refresh_upload()
        refresh_settings()  # re-render so the now-relevant Clear key button appears live

    def clear_key():
        api_in.value = ""
        db.set_setting(DB_PATH, "api_key", "")
        state.api_key = ""
        global _client, _client_key
        _client = None
        _client_key = None
        _set_api_health(None, refresh=False)
        ui.notify("API key cleared.", type="warning")
        _refresh_header()
        refresh_upload()
        refresh_settings()  # re-render so the Clear key button disappears live

    with ui.row().classes("gap-2"):
        ui.button("Save", on_click=save_key).props("color=primary unelevated")
        if state.api_key:
            ui.button("Clear key", on_click=clear_key).props("flat color=negative")

    # claude model
    ui.separator().style("margin: 18px 0;")
    ui.label("Claude model").classes("text-h6")
    ui.label("The model used to read and categorize your uploads. Leave this alone unless "
             "Anthropic retires it, then paste the new model name here. No code editing needed.") \
      .classes("text-caption text-muted")

    model_in = ui.input(label="Model name", value=ai.MODEL, placeholder=ai.DEFAULT_MODEL) \
        .props("outlined stack-label").classes("w-full")

    def save_model():
        new = (model_in.value or "").strip()
        if not new:
            ui.notify("Model name can't be empty.", type="warning")
            return
        if new == ai.MODEL:
            ui.notify("No changes to save.")
            return
        db.set_setting(DB_PATH, "model", new)
        ai.MODEL = new
        ui.notify(f"Model set to {new}.", type="positive")
        refresh_settings()

    def reset_model():
        db.set_setting(DB_PATH, "model", ai.DEFAULT_MODEL)
        ai.MODEL = ai.DEFAULT_MODEL
        model_in.value = ai.DEFAULT_MODEL
        ui.notify(f"Reset to default ({ai.DEFAULT_MODEL}).", type="positive")
        refresh_settings()

    with ui.row().classes("gap-2"):
        ui.button("Save", on_click=save_model).props("color=primary unelevated")
        if ai.MODEL != ai.DEFAULT_MODEL:
            ui.button("Reset to default", on_click=reset_model).props("flat")

    # api usage (estimated spend)
    ui.separator().style("margin: 18px 0;")
    ui.label("API usage").classes("text-h6")
    usage = db.get_api_usage(DB_PATH)
    ui.label("Your estimated Claude spend so far. This is an estimate, not your exact bill.") \
      .classes("text-caption text-muted")

    global _spend_value_label
    with ui.card().classes("bb-kpi").style("margin-top: 10px; max-width: 260px;"):
        ui.label("Estimated spend").classes("kpi-label")
        _spend_value_label = ui.label(f"${usage['cost']:,.4f}").classes("kpi-value")

    ui.label("For exact billing, see console.anthropic.com.") \
      .classes("text-caption text-muted").style("margin-top: 8px;")

    def reset_usage():
        db.reset_api_usage(DB_PATH)
        ui.notify("Estimated spend reset to $0.", type="positive")
        refresh_settings()

    def open_adjust_estimate():
        # Let the user pin the estimate to the real number they read at
        # console.anthropic.com. Auto-metering keeps adding on top from here. Read the spend
        # fresh (not the value captured at render) so it's current even after an upload bumped
        # it via _update_spend_meter without a full Settings rebuild.
        cur_cost = db.get_api_usage(DB_PATH)["cost"]
        with ui.dialog() as dlg, ui.card().style("min-width: 340px;"):
            ui.label("Adjust estimated spend").classes("text-h6")
            ui.label("Check your real spend at console.anthropic.com, enter it here, and "
                     "the app keeps adding to this value as you use Claude.") \
              .classes("text-caption text-muted")
            amt = ui.number(label="Estimated spend (USD)", value=round(cur_cost, 4),
                            min=0, step=0.0001, format="%.4f") \
                    .props("outlined prefix=$").classes("w-full").style("margin-top: 8px;")

            def save_estimate():
                v = amt.value
                if v is None or float(v) < 0:
                    ui.notify("Enter a value of $0 or more.", type="negative")
                    return
                db.set_api_usage(DB_PATH, cost=float(v))
                ui.notify(f"Estimate set to ${float(v):,.4f}.", type="positive")
                dlg.close()
                refresh_settings()

            with ui.row().classes("gap-2").style("margin-top: 12px;"):
                ui.button("Save", on_click=save_estimate).props("color=primary unelevated")
                ui.button("Cancel", on_click=dlg.close).props("flat")
        dlg.open()

    with ui.row().classes("gap-2").style("margin-top: 6px;"):
        ui.button("Adjust estimate", icon="edit", on_click=open_adjust_estimate).props("flat dense")
        ui.button("Reset estimate", icon="restart_alt", on_click=reset_usage).props("flat dense")
    # updates. checks github for a newer version and installs it on click. the check and
    # the pull run off the event loop so the panel never freezes. a copy without git just
    # gets told it can't self-update. the startup check may have already flagged one, in
    # which case the Update now button is shown straight away.
    ui.separator().style("margin: 18px 0;")
    ui.label("Updates").classes("text-h6")
    upd_status = ui.label("Check whether a newer version of Budget is available.") \
        .classes("text-caption text-muted")

    async def do_check_update():
        global _update_available, _update_banner_dismissed
        upd_check.disable()
        upd_install.set_visibility(False)
        upd_status.set_text("Checking for updates...")
        st, msg = await asyncio.to_thread(check_for_update, True)
        if st == "no_git":
            await asyncio.to_thread(_install_git_tools)   # pop macOS's installer for them
        upd_status.set_text(msg)
        available = (st == "available")
        upd_install.set_visibility(available)
        _update_available = available
        if available:
            _update_banner_dismissed = False   # a fresh manual find re-shows the banner
        _refresh_banner()                      # surface (or clear) the header update banner
        upd_check.enable()

    async def do_apply_update():
        global _update_available
        upd_install.disable()
        upd_check.disable()
        upd_status.set_text("Installing the update...")
        ok, msg = await asyncio.to_thread(apply_update)
        upd_status.set_text(msg)
        ui.notify(msg, type="positive" if ok else "warning", timeout=0 if ok else 4000)
        if ok:
            upd_install.set_visibility(False)
            _update_available = False
            _refresh_banner()               # clear the now-installed banner
        else:
            upd_install.enable()
        upd_check.enable()

    with ui.row().classes("gap-2 items-center").style("margin-top: 8px;"):
        upd_check = ui.button("Check for updates", icon="refresh", on_click=do_check_update) \
            .props("outline dense")
        upd_install = ui.button("Update now", icon="download", on_click=do_apply_update) \
            .props("unelevated dense color=primary")
    upd_install.set_visibility(False)
    if _update_available:
        upd_status.set_text("A new version of Budget is available.")
        upd_install.set_visibility(True)

    # Auto-backups run after every change. The refresh button forces a fresh one now.
    ui.separator().style("margin: 18px 0;")

    def backup_now():
        if _make_backup(force=True):
            ui.notify("Backup saved.", type="positive")
            refresh_settings()
        else:
            ui.notify("Couldn't save a backup.", type="negative")

    with ui.row().classes("items-center no-wrap").style("gap: 8px;"):
        ui.label("Backups").classes("text-h6")
        ui.button(icon="refresh", on_click=backup_now) \
          .props("flat round dense color=primary").tooltip("Back up now")

    snaps = sorted(BACKUP_DIR.glob("budget_*.db"),
                   key=lambda p: p.stat().st_mtime, reverse=True) if BACKUP_DIR.exists() else []
    if snaps:
        when = datetime.fromtimestamp(snaps[0].stat().st_mtime).strftime("%b %d, %Y at %H:%M")
        ui.label(f"Last backup: {when}.") \
          .classes("text-caption text-muted")
        ui.label("A full snapshot of all your data (transactions, categories, learned "
                 f"stores and your API key) is saved automatically to {BACKUP_DIR} "
                 "after every change.") \
          .classes("text-caption text-muted")
    else:
        ui.label("No backup yet. A full snapshot of all your data (transactions, "
                 "categories, learned stores and your API key) is saved automatically to "
                 f"{BACKUP_DIR} after every change.") \
          .classes("text-caption text-muted")

    ui.separator().style("margin: 18px 0;")
    ui.label("Export").classes("text-h6")
    all_txns = db.get_transactions(DB_PATH)
    if not all_txns:
        ui.label("Nothing to export yet.").classes("text-caption text-muted")
    else:
        # Let the user pick the range before downloading: All-time, a single
        # month (e.g. May 2026), or a full year. The Month/Year selects show or
        # hide reactively with the chosen scope (no panel rebuild), and the file
        # is built on click from the filtered rows so changing scope is free.
        ui.label("Choose a range, then download.") \
          .classes("text-caption text-muted")

        today = date.today()
        # Years that actually have data, plus the current year so a current-month
        # export is selectable even before this year has any rows.
        data_years = sorted({int(t["date"][:4]) for t in all_txns}, reverse=True)
        year_opts = sorted({today.year, *data_years}, reverse=True)

        with ui.column().classes("w-full").style("gap: 10px; margin-top: 8px;"):
            scope_in = ui.toggle(["All-time", "Monthly", "Annual"], value="All-time") \
                .props("dense unelevated")
            with ui.row().classes("items-center gap-2"):
                month_in = ui.select({i: bb.MONTH_NAMES[i - 1] for i in range(1, 13)},
                                     value=today.month, label="Month") \
                    .props("dense").classes("w-36")
                month_in.bind_visibility_from(scope_in, "value", value="Monthly")
                year_in = ui.select(year_opts, value=today.year, label="Year") \
                    .props("dense").classes("w-28")
                year_in.bind_visibility_from(
                    scope_in, "value",
                    backward=lambda v: v in ("Monthly", "Annual"))

            def export(fmt: str):
                # minimal columns: date, name, amount, category, same for CSV and Excel.
                scope = scope_in.value
                if scope == "Monthly":
                    y, m = int(year_in.value), int(month_in.value)
                    rows = db.get_transactions(DB_PATH, year=y, month=m)
                    tag = f"{y:04d}-{m:02d}"
                elif scope == "Annual":
                    y = int(year_in.value)
                    rows = db.get_transactions(DB_PATH, year=y)
                    tag = f"{y:04d}"
                else:
                    rows = db.get_transactions(DB_PATH)
                    tag = "all-time"
                if not rows:
                    ui.notify("No transactions in that range.", type="warning")
                    return
                df_x = pd.DataFrame(rows)[["date", "merchant", "amount", "category"]] \
                         .rename(columns={"merchant": "name"})
                stamp = datetime.now().strftime("%Y%m%d")
                if fmt == "csv":
                    data = df_x.to_csv(index=False).encode("utf-8")
                    ui.download.content(data, f"transactions_{tag}_{stamp}.csv")
                else:
                    buf = io.BytesIO()
                    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                        df_x.to_excel(writer, sheet_name="Transactions", index=False)
                    ui.download.content(buf.getvalue(), f"transactions_{tag}_{stamp}.xlsx")

            with ui.row().classes("gap-2 w-full"):
                ui.button("Transactions to CSV", icon="file_download",
                          on_click=lambda: export("csv")) \
                  .props("flat").classes("flex-grow")
                ui.button("Transactions to Excel", icon="file_download",
                          on_click=lambda: export("xlsx")) \
                  .props("flat").classes("flex-grow")

    # recently deleted (recovery)
    # tucked behind a button so Settings stays short, the list opens in a dialog.
    ui.separator().style("margin: 18px 0;")
    ui.label("Recently deleted").classes("text-h6")
    n_deleted = len(db.list_recently_deleted(DB_PATH))
    ui.label("Restore a transaction you removed by accident. It comes back exactly as it "
             f"was. The last {db.RECENTLY_DELETED_KEEP} deletions are kept; anything older is "
             "permanently removed.").classes("text-caption text-muted")

    def open_recently_deleted():
        with ui.dialog() as dlg, ui.card().style("min-width: 360px; max-width: 92vw;"):
            ui.label("Recently deleted").classes("text-h6")
            list_box = ui.column().classes("w-full") \
                .style("gap: 6px; margin-top: 6px; max-height: 60vh; overflow-y: auto;")

            def render_list():
                # redraw the list and sync both the trigger button's count and the
                # clear-all button's enabled state. touches the dialog only, never
                # refresh_settings (that would tear this dialog down).
                list_box.clear()
                entries = db.list_recently_deleted(DB_PATH)
                view_btn.set_text(f"View recently deleted ({len(entries)})")
                (view_btn.enable if entries else view_btn.disable)()
                (clear_btn.enable if entries else clear_btn.disable)()
                with list_box:
                    if not entries:
                        ui.label("Nothing here, you're all caught up.") \
                          .classes("text-caption text-muted")
                        return
                    for entry in entries:
                        with ui.row().classes("items-center no-wrap w-full") \
                                .style("gap: 10px; padding: 8px 12px; "
                                       "background: var(--bb-surface); "
                                       "border: 1px solid var(--bb-border); "
                                       "border-radius: 10px;"):
                            with ui.column().classes("col").style("gap: 2px; min-width: 0;"):
                                ui.label(entry["label"]).classes("ellipsis") \
                                  .style("font-weight: 600;")
                                ui.label(_relative_time_ago(entry["deleted_at"])) \
                                  .classes("text-caption text-muted")
                            ui.button("Restore", icon="undo",
                                      on_click=lambda e, eid=entry["id"]: restore_entry(eid)) \
                              .props("flat dense color=primary")

            def restore_entry(entry_id: int):
                n = db.restore_recently_deleted(DB_PATH, entry_id)
                if n:
                    db_backup_if_changed()
                    ui.notify(f"Restored {n} transaction{'s' if n != 1 else ''}.",
                              type="positive")
                else:
                    ui.notify("That item is no longer available.", type="warning")
                # refresh the data tabs but not Settings, rebuilding the Settings
                # container would tear this dialog down. the dialog list and buttons are
                # updated in place so it stays open for restoring several at once.
                refresh_home()
                refresh_insights()
                refresh_categories()
                _refresh_header()
                render_list()

            def clear_all():
                entries = db.list_recently_deleted(DB_PATH)
                if not entries:
                    return
                n = len(entries)
                with ui.dialog() as confirm, ui.card().style("min-width: 300px;"):
                    ui.label("Clear recently deleted?").classes("text-h6")
                    ui.label(f"This permanently removes all {n} recoverable item"
                             f"{'s' if n != 1 else ''}. The transactions stay deleted and "
                             "can no longer be restored from here.") \
                      .classes("text-caption text-muted")

                    def do_clear():
                        db.clear_recently_deleted(DB_PATH)
                        ui.notify("Recently deleted cleared.", type="warning")
                        confirm.close()
                        render_list()

                    with ui.row().classes("w-full justify-end") \
                            .style("margin-top: 12px; gap: 8px;"):
                        ui.button("Cancel", on_click=confirm.close).props("flat")
                        ui.button("Clear all", on_click=do_clear) \
                          .props("unelevated color=negative")
                confirm.open()

            with ui.row().classes("w-full items-center no-wrap") \
                    .style("margin-top: 12px; gap: 8px;"):
                clear_btn = ui.button("Clear all", icon="delete_sweep", on_click=clear_all) \
                    .props("flat dense color=negative")
                ui.element("div").classes("col")  # spacer pushes Close to the right
                ui.button("Close", on_click=dlg.close).props("flat")

            render_list()
        dlg.open()

    view_btn = ui.button(f"View recently deleted ({n_deleted})", icon="history",
                         on_click=open_recently_deleted) \
        .props("outline dense").style("margin-top: 10px;")
    if n_deleted == 0:
        view_btn.disable()

    ui.separator().style("margin: 18px 0;")
    ui.label("Danger zone").classes("text-h6").style("color: var(--bb-expense);")
    ui.label("Permanently delete transactions. A backup is always saved first, and you must "
             "type DELETE to confirm.") \
      .classes("text-caption text-muted").style("margin-bottom: 10px;")

    # one section, two scopes chosen with a toggle (a specific month or everything), then a
    # single DELETE-to-confirm field and one wipe button whose label reflects the chosen scope.
    dz_scope = ui.toggle({"month": "A specific month", "all": "Everything"}, value="month") \
        .props("no-caps spread unelevated").classes("w-full")

    _now = datetime.now()
    m_years = db.get_available_years(DB_PATH)
    if _now.year not in m_years:
        m_years = [_now.year] + m_years

    # month picker, shown only for the specific month scope.
    dz_month_box = ui.column().classes("w-full").style("gap: 4px; margin-top: 10px;")
    with dz_month_box:
        with ui.row().classes("w-full no-wrap").style("gap: 8px;"):
            wm_month = ui.select({i: bb.MONTH_NAMES[i - 1] for i in range(1, 13)},
                                 value=_now.month, label="Month") \
                .props("dense outlined").classes("flex-grow")
            wm_year = ui.select(m_years, value=(m_years[0] if m_years else _now.year),
                                label="Year").props("dense outlined").classes("w-32")
        wm_count = ui.label("").classes("text-caption text-muted")

    # explanation, shown only for the everything scope.
    dz_all_note = ui.label("Deletes ALL transactions and learned stores. Your categories and "
                           "API key are kept.") \
        .classes("text-caption text-muted").style("margin-top: 10px;")

    dz_confirm = ui.input(label="Type DELETE to confirm", placeholder="DELETE") \
        .props("outlined stack-label").classes("w-full").style("margin-top: 10px;")
    dz_btn = ui.button("Wipe", icon="local_fire_department") \
        .props("color=negative unelevated").style("margin-top: 4px;")
    dz_btn.disable()

    def _dz_month_label() -> str:
        return f"{bb.MONTH_NAMES[int(wm_month.value) - 1]} {int(wm_year.value)}"

    def dz_refresh():
        # Swap the scope-specific helper UI, keep the live month count current, and gate the
        # one wipe button: DELETE must be typed, and for a month it must also have rows.
        is_month = dz_scope.value == "month"
        dz_month_box.set_visibility(is_month)
        dz_all_note.set_visibility(not is_month)
        typed_ok = (dz_confirm.value or "").strip().upper() == "DELETE"
        if is_month:
            n = len(db.get_transactions(DB_PATH, year=int(wm_year.value),
                                        month=int(wm_month.value)))
            wm_count.text = f"{n} transaction{'s' if n != 1 else ''} in {_dz_month_label()}"
            dz_btn.set_text(f"Wipe {_dz_month_label()}")
            ready = n > 0 and typed_ok
        else:
            dz_btn.set_text("Wipe all data")
            ready = typed_ok
        (dz_btn.enable if ready else dz_btn.disable)()

    dz_scope.on_value_change(lambda e: dz_refresh())
    wm_month.on_value_change(lambda e: dz_refresh())
    wm_year.on_value_change(lambda e: dz_refresh())
    dz_confirm.on_value_change(lambda e: dz_refresh())

    def dz_wipe():
        # Re-check the DELETE gate at click time (defence in depth alongside the disabled btn).
        if (dz_confirm.value or "").strip().upper() != "DELETE":
            return
        if dz_scope.value == "month":
            y, m = int(wm_year.value), int(wm_month.value)
            rows = db.get_transactions(DB_PATH, year=y, month=m)
            if not rows:                   # guard a race (rows deleted since last refresh)
                notify("No transactions in that month.", type="warning")
                dz_refresh()
                return
            label = _dz_month_label()
            db_backup_if_changed()         # snapshot before the destructive op
            db.delete_transactions(DB_PATH, [r["id"] for r in rows])
            db.record_deletion(DB_PATH, rows, label=f"{label} · {len(rows)} transactions")
            db.resync_all_merchant_memory(DB_PATH)   # keep learned-store counts honest
            db_backup_if_changed()         # snapshot the result too
            n = len(rows)
            dz_confirm.value = ""
            notify(f"Wiped {n} transaction{'s' if n != 1 else ''} from {label}.", type="warning")
        else:
            db_backup_if_changed()         # snapshot before the destructive op
            db.wipe_all(DB_PATH)
            db_backup_if_changed()         # snapshot the wiped state too
            dz_confirm.value = ""
            notify("All transactions and merchant memory wiped.", type="warning")
        refresh_all()
    dz_btn.on_click(dz_wipe)
    dz_refresh()


# launch

def _install_fast_shutdown() -> None:
    # make Ctrl+C (and a kill/SIGTERM) quit instantly. uvicorn installs a graceful
    # signal handler that waits for any in-flight request to finish first (e.g. an
    # upload's Anthropic call running in a worker thread), and that wait is what made the
    # first Ctrl+C hang and forced a second one. this runs in app.on_startup, which fires
    # after uvicorn captured its own handlers, so ours wins. os._exit skips the worker
    # thread-join and atexit hooks that caused the hang, it is safe here because every DB
    # write is committed per-operation (no long-lived connections, nothing left to flush)
    # and backups are written synchronously at their call sites.
    import signal

    def _instant_exit(_signum, _frame):
        print("\nGoodbye 👋", flush=True)
        os._exit(0)

    for _sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(_sig, _instant_exit)
        except (ValueError, OSError):
            # not the main thread, or signal unsupported on this platform, the
            # except KeyboardInterrupt backstop around ui.run() still applies.
            pass


app.on_startup(_install_fast_shutdown)


if __name__ in {"__main__", "__mp_main__"}:
    def _port_in_use(port: int) -> bool:
        # true only if something is already listening on the port (e.g. another Budget
        # window left open, or a stuck process). uses SO_REUSEADDR exactly like uvicorn's
        # own bind, so a port merely in TIME_WAIT after a clean quit is not a false
        # positive, only a live listener counts.
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("0.0.0.0", port))
                return False
            except OSError:
                return True

    if _port_in_use(8080):
        print(
            "\nPort 8080 is already in use, either Budget is already running, or "
            "another app on your Mac is using that port.\n"
            "If Budget is already open, switch to that window. Otherwise fully close it "
            "(or quit whatever else is using port 8080) and run this again.\n",
            flush=True,
        )
        sys.exit(1)

    try:
        ui.run(
            port=8080,
            title="Budget",
            favicon="💲",
            dark=True,
            reload=False,
            # managed (Mac app): the launcher opens a fresh Safari window itself, so don't
            # also auto-open the default browser (that would make a second, blocked tab).
            show=not MANAGED_APP,
            storage_secret="bb-local-app",
        )
    except KeyboardInterrupt:
        # clean Ctrl+C exit, no traceback noise. os._exit terminates immediately instead
        # of sys.exit, which would otherwise hang waiting to join the upload worker threads
        # (asyncio.to_thread is non-daemon) if an API call is still in flight, that hang
        # forced a second Ctrl+C. connections are short-lived (per-op), so this is safe.
        print("\nGoodbye 👋", flush=True)
        os._exit(0)
