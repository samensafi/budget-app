# pure helpers for the budget app. amount parsing, row checks, recurring date math,
# the vision retry call, time formatting, category filtering. stdlib only, no
# nicegui/db/ai/state. app.py imports every name here so app.<name> still works
# in callers and tests.
from __future__ import annotations

import math
from datetime import date, datetime


def _parse_amount(value) -> float | None:
    # strips $ and commas. blank, bad, or not a real number gives None so the caller
    # keeps the extracted value. a typed 0 stays 0, kept and flagged, never reverted.
    try:
        v = abs(float(str(value).replace(",", "").replace("$", "").strip()))
    except (ValueError, TypeError):
        return None
    return v if math.isfinite(v) else None


def _amount_is_zero(amount) -> bool:
    # true when it rounds to $0 or isn't a real number
    try:
        a = float(amount)
    except (TypeError, ValueError):
        return True
    return (not math.isfinite(a)) or round(abs(a), 2) == 0


# extractor only writes Unknown, the rest catch a hand edit to a placeholder
_PREVIEW_PLACEHOLDER_NAMES = {
    "unknown", "unrecognized", "unknown merchant", "unidentified",
    "unnamed", "no name", "not recognized", "n/a", "na", "none",
}


def _preview_name_bad(name) -> bool:
    s = (name or "").strip()  # blank or placeholder, fix or delete before saving
    return (not s) or (s.casefold() in _PREVIEW_PLACEHOLDER_NAMES)


def _recurring_future_dates(base_date: date, frequency: str) -> list[date]:
    # future dates from base_date, one interval on, up to the end of its year.
    # monthly anchors on the base day. a month missing that day uses its last day,
    # but the next month tries the base day again (Jan 31, Feb 28, Mar 31). weekly
    # just adds 7 days so month lengths and leap years sort themselves out.
    import calendar
    year_end = date(base_date.year, 12, 31)
    out: list[date] = []

    if frequency == "weekly":
        from datetime import timedelta
        step = timedelta(days=7)
        nxt = base_date + step
        while nxt <= year_end:
            out.append(nxt)
            nxt += step
        return out

    if frequency == "monthly":
        base_day = base_date.day
        for i in range(1, 13):
            target_month = ((base_date.month - 1 + i) % 12) + 1
            target_year = base_date.year + (base_date.month - 1 + i) // 12
            if target_year > base_date.year:
                break
            last_day = calendar.monthrange(target_year, target_month)[1]
            day = min(base_day, last_day)  # snap to last day if base_day is missing
            nxt = date(target_year, target_month, day)
            if nxt <= year_end:
                out.append(nxt)
        return out

    return out


def _infer_series_frequency(dates: list[date], anchor: date) -> str | None:
    # frequency isn't stored so read it back from the gap to the nearest sibling.
    # about a week is weekly, otherwise monthly. None when there's no sibling to compare.
    others = [d for d in dates if d != anchor]
    if not others:
        return None
    nearest_gap = min(abs((d - anchor).days) for d in others)
    return "weekly" if nearest_gap <= 10 else "monthly"


def _reanchored_date(d: date, new_day: int) -> date:
    # d moved to new_day of its own month, clipped to that month's last day. matches the
    # monthly rule above so a reanchored series lines up with a fresh one.
    import calendar
    last_day = calendar.monthrange(d.year, d.month)[1]
    return date(d.year, d.month, min(new_day, last_day))


def _should_retry_with_vision(txns: list, mode: str) -> bool:
    # only retry a zero result extraction with vision when the first pass used local text
    # (pdf text layer or ocr) and found nothing. skip it when we already have rows, when
    # it was plain text or paste with no image to read, or when vision already ran since
    # rerunning just burns a second identical paid call.
    return (not txns) and mode in ("text-pdf", "text-ocr")


def _relative_time_ago(ts: str | None) -> str:
    # friendly time ago string for a SQLite UTC timestamp
    if not ts:
        return ""
    raw = str(ts).strip().replace("T", " ").split(".")[0]
    dt = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(raw, fmt)
            break
        except ValueError:
            continue
    if dt is None:
        return str(ts)
    from datetime import timezone
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    secs = max(0.0, (now_utc - dt).total_seconds())
    if secs < 60:
        return "just now"
    mins = int(secs // 60)
    if mins < 60:
        return f"{mins} min ago"
    hrs = int(secs // 3600)
    if hrs < 24:
        return f"{hrs} hr{'s' if hrs != 1 else ''} ago"
    days = int(secs // 86400)
    if days < 30:
        return f"{days} day{'s' if days != 1 else ''} ago"
    return dt.strftime("%b %-d, %Y")


def _expense_category_names(all_names: list[str]) -> list[str]:
    # Income is reserved for income and Other was retired so neither is selectable here
    return [c for c in all_names if c not in ("Income", "Other")]


def _update_state(is_repo: bool, fetch_ok: bool, local: str, remote: str,
                  git_installed: bool = True, remote_ahead: bool | None = None) -> tuple[str, str]:
    # turn raw git facts into what the Updates panel should show. pure so it tests without
    # a real repo. states: no_git (git itself isn't installed), not_git (this copy isn't a
    # git checkout), unknown (couldn't reach github or read the commits), current (already
    # newest), available (github ahead).
    if not git_installed:
        return ("no_git", "Budget needs Git to check for updates and it isn't installed. "
                          "A macOS install window should open, click Install, wait for it "
                          "to finish, then open Budget again.")
    if not is_repo:
        return ("not_git", "This copy was not installed with Git, so it can't update "
                           "itself. Re-download Budget to get the latest version.")
    if not fetch_ok or not local or not remote:
        return ("unknown", "Couldn't reach GitHub to check for updates. Check your "
                           "internet connection and try again.")
    if local == remote:
        return ("current", "Budget is up to date.")
    # the commits differ, but that only means an update when github is genuinely ahead
    # (it has commits we don't). remote_ahead is False only when this copy sits ahead of
    # github, an unpushed local commit on a dev machine, where there's nothing to pull, so
    # call it current. when the count couldn't be read (None) keep the old any-difference
    # behavior so a real update is never missed.
    if remote_ahead is False:
        return ("current", "Budget is up to date.")
    return ("available", "A new version of Budget is available.")


def _banner_decision(*, reinstall_required: bool, reinstall_dismissed: bool,
                     update_available: bool, update_dismissed: bool,
                     offline: bool, offline_dismissed: bool) -> tuple[bool, bool, bool]:
    # which of the three header banners to show. kept pure so every combination is unit-tested
    # and the banners can never stand in for each other or get stuck. rules:
    #  - reinstall (a launcher change git can't apply) is the most important; WHILE it's
    #    required the in-app update banner is suppressed entirely, because the in-app update
    #    can't change the launcher anyway, so reinstall is the real action. this holds even if
    #    the reinstall banner itself was dismissed for the session.
    #  - offline is an independent concern and may stack under either.
    #  - each banner is hidden the moment its own dismissed flag is set.
    # returns (show_reinstall, show_update, show_offline).
    show_reinstall = reinstall_required and not reinstall_dismissed
    show_update = update_available and not update_dismissed and not reinstall_required
    show_offline = offline and not offline_dismissed
    return (show_reinstall, show_update, show_offline)
