# crash log helpers. paths, tunables and external deps (the live api key, the
# ai.AIError type, the filter and scrub callables) all come from the small wrappers
# in app.py, which keep the module constants and the excepthook wiring. pure stdlib,
# no app/ai/db imports, so this loads with no cycle.
from __future__ import annotations

import asyncio
import re
import traceback
from datetime import datetime
from pathlib import Path


def should_log_crash(exc_type, exc_value, *, ai_error) -> bool:
    # true only for real unexpected errors worth debugging. skips control flow (clean
    # quit, cancellation), client disconnect noise, and the expected Claude conditions
    # (bad key, billing, rate limit, overload) the app already shows friendly messages
    # for. ai_error is ai.AIError, passed in to dodge an import cycle.
    if exc_type is None:
        return False
    if issubclass(exc_type, (KeyboardInterrupt, SystemExit, GeneratorExit,
                             asyncio.CancelledError, BrokenPipeError,
                             ConnectionResetError)):
        return False
    try:
        if issubclass(exc_type, ai_error):  # API conditions we already handle
            return False
    except Exception:
        pass
    return True


def scrub_secrets(text: str, *, api_key: str | None) -> str:
    # strip the Anthropic API key from text before it gets written. the key lives in
    # budget.db and must never travel. scrubs the live key plus anything starting with
    # sk-ant- as a backstop.
    try:
        if api_key:
            text = text.replace(api_key, "<api-key removed>")
    except Exception:
        pass
    return re.sub(r"sk-ant-[A-Za-z0-9_\-]{8,}", "<api-key removed>", text)


def log_crash(exc_type, exc_value, exc_tb, *, context: str = "",
              crash_log: Path, sep: str, header: str,
              max_entries: int, entry_max: int,
              should_log, scrub) -> None:
    # append one real error to crash_log, newest first, keeping the last max_entries.
    # never raises, never touches budget.db, never calls the API, never stores the key.
    # does nothing for filtered errors. should_log and scrub are the app's filter and
    # scrubber, passed in so the live key and ai.AIError stay current.
    try:
        if not should_log(exc_type, exc_value):
            return
        tb = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        head = f"[{stamp}] {exc_type.__name__}: {' '.join(str(exc_value).split())}".rstrip()
        if context:
            head += f"  (while: {context})"
        entry = scrub(f"{head}\n{tb.strip()}")
        if len(entry) > entry_max:
            entry = entry[:entry_max] + "\n...(truncated)"
        existing = ""
        try:
            existing = crash_log.read_text(encoding="utf-8")
        except Exception:
            existing = ""
        prior = [c for c in existing.split(sep)
                 if c.strip() and not c.lstrip().startswith("#")]
        entries = ([entry] + prior)[:max_entries]
        crash_log.parent.mkdir(parents=True, exist_ok=True)
        crash_log.write_text(
            sep.join([header] + entries) + "\n", encoding="utf-8")
    except Exception:
        # logging must never break the app, swallow anything that goes wrong here
        pass
