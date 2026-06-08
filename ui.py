# UI helpers and the CSS theme. money and date formatting, the dark CSS theme, and
# a few small HTML snippet helpers for the search drawer and empty states.
from __future__ import annotations

import html
import unicodedata
from datetime import datetime


MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def amount_str(amount: float, *, grouped: bool = True) -> str:
    # magnitude of amount as a display string. whole dollars drop the cents so 4 stays
    # 4, while 4.33 and 4.50 keep the two decimals. grouped adds thousands separators,
    # pass grouped=False for an editable input value with no commas so it still parses
    # as a float. rounds to 2dp first so float noise like 4.000000001 still reads whole.
    a = abs(amount)
    whole = round(a, 2) == int(round(a, 2))
    if grouped:
        return f"{int(round(a)):,}" if whole else f"{a:,.2f}"
    return f"{int(round(a))}" if whole else f"{a:.2f}"


def money(amount: float, *, signed: bool = False, rounded: bool = False) -> str:
    # rounded rounds to the nearest whole dollar for the home summary cards and
    # Insights. default keeps the cents but drops a trailing .00 on whole dollars so
    # $4 stays $4, while $4.33 keeps the cents (via amount_str).
    body = f"{abs(amount):,.0f}" if rounded else amount_str(amount)
    if signed:
        if amount > 0:
            return f"+${body}"
        if amount < 0:
            return f"−${body}"
        return "$0"
    return f"${body}"


def _emoji_clusters(s: str) -> list[str]:
    # split s into grapheme clusters good enough for emoji, so up to 2 emojis counts
    # what a person sees as one symbol. handles flags (regional indicator pairs 🇨🇦),
    # zwj sequences (👨‍👩‍👧), skin tone modifiers (👍🏽), variation selectors (❤️) and
    # keycaps (1️⃣). no third party deps since stdlib has no grapheme segmentation, so
    # this is a pragmatic approximation aimed at emoji.
    clusters: list[str] = []
    prev_ri = False  # previous code point was the first of a regional indicator pair
    for ch in s:
        cp = ord(ch)
        is_ri = 0x1F1E6 <= cp <= 0x1F1FF
        extend = bool(
            unicodedata.combining(ch)        # combining marks
            or 0xFE00 <= cp <= 0xFE0F        # variation selectors like ❤️
            or 0x1F3FB <= cp <= 0x1F3FF      # skin tone modifiers
            or cp in (0x200D, 0x20E3)        # zwj and combining enclosing keycap
        )
        join_prev = bool(clusters and clusters[-1].endswith("‍"))  # right after a zwj
        if clusters and (extend or join_prev or (is_ri and prev_ri)):
            clusters[-1] += ch
            prev_ri = False  # this one closed a flag pair
        else:
            clusters.append(ch)
            prev_ri = is_ri
    return clusters


def count_emojis(s: str) -> int:
    # how many emoji or symbols a person sees in s (grapheme clusters)
    return len(_emoji_clusters((s or "").strip()))


def clamp_emojis(s: str, max_count: int = 2) -> str:
    # trim s to at most max_count emoji (whole grapheme clusters)
    return "".join(_emoji_clusters((s or "").strip())[:max_count])


def date_header(date_str: str) -> str:
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return d.strftime("%A, %B %-d")
    except ValueError:
        return date_str


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) < 8:
        return "•" * len(key)
    return f"...{key[-4:]}"


# dark theme layered on top of Quasar's defaults. the variables set the brand colors
# and the per element rules clean up Quasar's default chrome to feel more like a
# polished mobile app.
CSS = """
<style>
:root {
  --bb-bg:           #1d1d1d;
  --bb-bg-elevated:  #242424;
  --bb-surface:      #2c2e34;
  --bb-surface-hover:#34373e;
  --bb-border:       #42464e;
  --bb-border-strong:#585c66;
  --bb-text:         #f3f4f6;
  --bb-text-muted:   #b9bfc9;
  --bb-text-very-muted: #878e9c;
  --bb-accent:       #3b82f6;
  --bb-income:       #10b981;
  --bb-expense:      #f87171;
  --bb-warning:      #fbbf24;
  --bb-shadow:       0 1px 3px rgba(0,0,0,0.35);
  --bb-shadow-lg:    0 6px 18px rgba(0,0,0,0.45);
}

html, body, .q-page-container, .q-page {
  background: var(--bb-bg) !important;
  color: var(--bb-text) !important;
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI",
               Roboto, Helvetica, Arial, sans-serif !important;
  -webkit-font-smoothing: antialiased;
}

/* tab content panels. Quasar paints these its own dark, pin them to the app
   background so the page stays one uniform layer instead of a mismatched third
   layer showing through behind the cards */
.q-tab-panels, .q-tab-panel {
  background: var(--bb-bg) !important;
}

/* header, one top bar with tabs on the left and API status on the right */
.bb-header {
  background: var(--bb-bg-elevated) !important;
  border-bottom: 1px solid var(--bb-border);
  padding: 0 18px !important;
  box-shadow: none !important;
  color: var(--bb-text) !important;
  min-height: 56px !important;
}

/* tabs, rendered inside the header */
.q-tabs {
  background: transparent !important;
  min-height: 56px !important;
}
.q-tab {
  color: var(--bb-text-muted) !important;
  text-transform: none !important;
  font-weight: 500 !important;
  font-size: 0.92rem !important;
  letter-spacing: 0 !important;
  min-height: 56px !important;
  padding: 0 14px !important;
}
.q-tab:hover {
  color: var(--bb-text) !important;
  background: rgba(255,255,255,0.03) !important;
}
.q-tab--active {
  color: var(--bb-text) !important;
}
.q-tab__indicator {
  background: var(--bb-accent) !important;
  height: 3px !important;
}
.q-tab__label { font-weight: 500 !important; }

/* Generic cards */
.q-card {
  background: var(--bb-surface) !important;
  color: var(--bb-text) !important;
  border: 1px solid var(--bb-border);
  border-radius: 14px !important;
  box-shadow: var(--bb-shadow) !important;
  transition: border-color 0.15s ease;
}
.q-card:hover {
  border-color: var(--bb-border-strong);
}

/* KPI cards */
.bb-kpi {
  padding: 16px 18px !important;
}
/* equal width KPI columns. the 3 cards on the Home and Insights rows carry flex-grow,
   which on its own leaves flex-basis at auto so each card sizes to its amount text and
   they come out unequal and shift month to month. flex-basis:0 makes them exact equal
   thirds and min-width:0 lets them shrink evenly without overflow at narrow widths.
   scoped to .flex-grow so the standalone Settings spend card with its fixed max-width
   is untouched. */
.bb-kpi.flex-grow {
  flex-basis: 0;
  min-width: 0;
}
.bb-kpi .kpi-label {
  color: var(--bb-text-muted);
  font-size: 0.74rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  font-weight: 500;
}
.bb-kpi .kpi-value {
  font-size: 1.7rem;
  font-weight: 600;
  line-height: 1.15;
  margin-top: 4px;
}
.bb-kpi .kpi-sub {
  color: var(--bb-text-muted);
  font-size: 0.74rem;
  margin-top: 3px;
}
.bb-kpi .kpi-value.income  { color: var(--bb-income); }
.bb-kpi .kpi-value.expense { color: var(--bb-expense); }

/* Transaction row */
.bb-row {
  cursor: pointer !important;
  padding: 12px 16px !important;
  margin: 0 !important;
}
.bb-row:hover {
  background: var(--bb-surface-hover) !important;
}
.bb-row.review {
  border-color: color-mix(in srgb, var(--bb-warning) 55%, var(--bb-border));
  background: color-mix(in srgb, var(--bb-warning) 6%, var(--bb-surface));
}
.bb-row.duplicate {
  border-color: color-mix(in srgb, var(--bb-accent) 55%, var(--bb-border));
  background: color-mix(in srgb, var(--bb-accent) 6%, var(--bb-surface));
}
/* Brief pulse when a search result jumps the user to its Home-list row. */
@keyframes bbFlash {
  0%   { box-shadow: 0 0 0 2px var(--bb-accent), 0 0 20px 3px color-mix(in srgb, var(--bb-accent) 55%, transparent);
         background: color-mix(in srgb, var(--bb-accent) 16%, var(--bb-surface)); }
  100% { box-shadow: none; background: var(--bb-surface); }
}
.bb-row.bb-flash {
  animation: bbFlash 1.8s ease-out;
  border-color: var(--bb-accent) !important;
}
/* Brief pulse on the API-key field when the header chip jumps the user to Settings. */
@keyframes bbFieldFlash {
  0%   { box-shadow: 0 0 0 3px var(--bb-accent), 0 0 18px 3px color-mix(in srgb, var(--bb-accent) 50%, transparent); }
  100% { box-shadow: none; }
}
.bb-flash-field { animation: bbFieldFlash 1.8s ease-out; border-radius: 8px; }
.bb-row .avatar {
  width: 40px; height: 40px;
  border-radius: 11px;
  display: flex; align-items: center; justify-content: center;
  font-size: 1.1rem; flex-shrink: 0;
  white-space: nowrap;          /* keep 2 emojis on one line, no vertical stack */
}
/* Two-emoji categories: same 40px box, just a smaller font + tight tracking so
   both glyphs sit side-by-side instead of wrapping on top of each other. */
.bb-row .avatar.duo { font-size: 0.8rem; letter-spacing: -0.5px; }
.bb-row .merchant {
  font-size: 0.97rem; font-weight: 500; color: var(--bb-text);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.bb-row .category-chip {
  font-size: 0.78rem; font-weight: 500;
}
.bb-row .amount {
  font-size: 1.0rem; font-weight: 600;
  white-space: nowrap;
}
.bb-row .amount.income  { color: var(--bb-income); }
.bb-row .amount.expense { color: var(--bb-expense); }

/* swipe to reveal quick delete on Home rows. the row is a horizontal scroll
   container whose flex children are the row content (full width) and a red trash
   button just past the right edge. swiping the row left scrolls that button into
   view, and scroll snap settles it cleanly open or closed. a swipe alone never
   deletes, only a tap on the revealed button does, and the original tap to edit then
   Delete path is untouched. */
.bb-swipe {
  display: flex;
  flex-wrap: nowrap;
  width: 100%;
  overflow-x: auto;
  overflow-y: hidden;
  scroll-snap-type: x mandatory;
  -webkit-overflow-scrolling: touch;
  scrollbar-width: none;            /* Firefox: hide the scrollbar */
  border-radius: 14px;
}
.bb-swipe::-webkit-scrollbar { display: none; }   /* WebKit: hide the scrollbar */
.bb-swipe-pane {
  flex: 0 0 100%;
  min-width: 0;            /* honour the 100% basis instead of growing to content */
  scroll-snap-align: start;
  scroll-snap-stop: always;
}
.bb-swipe-del {
  flex: 0 0 68px;
  margin-left: 8px;
  scroll-snap-align: end;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  border-radius: 14px;
  background: #ef4444;
  color: #fff;
  transition: background 0.15s ease;
}
.bb-swipe-del:hover { background: #dc2626; }
.bb-swipe-del .q-icon { font-size: 1.45rem; }

/* Date headers */
.bb-date-header {
  color: var(--bb-text-muted);
  font-size: 0.75rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  margin: 16px 4px 6px;
}

/* Buttons: generous padding so text never feels cramped */
.q-btn {
  text-transform: none !important;
  font-weight: 500 !important;
  letter-spacing: 0 !important;
  border-radius: 10px !important;
  padding: 8px 18px !important;
  min-height: 36px !important;
  cursor: pointer !important;
  transition: background 0.12s ease, border-color 0.12s ease, transform 0.08s ease;
}
.q-btn:hover { transform: translateY(-1px); }
.q-btn:active { transform: translateY(0); }
.q-btn--standard {
  background: var(--bb-surface) !important;
  color: var(--bb-text) !important;
  border: 1px solid var(--bb-border);
}
.q-btn--flat {
  padding: 6px 14px !important;
}
.q-btn--flat:hover {
  background: var(--bb-surface-hover) !important;
}
.q-btn--dense {
  padding: 4px 10px !important;
  min-height: 32px !important;
}
.q-btn--round {
  padding: 0 !important;
  min-width: 36px !important;
  min-height: 36px !important;
}
.q-btn--disable, .q-btn[disabled] {
  cursor: not-allowed !important;
  opacity: 0.55 !important;
}
.q-btn__content { padding: 0 4px; }
/* lift the primary blue a touch wherever it shows as a foreground color (flat and
   outline button text and their icons, links, and so on) so every blue label reads
   clearer on the dark bg and they all stay matched (Search, Sort & filter, the month
   arrows, Cancel, Close, Reset, Adjust, the Backups refresh icon, View recently
   deleted, and so on). filled primary buttons use bg-primary with white text, so Add
   transaction and the other solid blue buttons stay untouched. one catch with the
   !important rules below: Quasar's own .text-primary lives in its quasar_importants
   cascade layer, and for !important declarations the layer order is flipped, so an
   unlayered !important is the weakest and loses to Quasar's. we have to sit in a layer
   that comes before quasar_importants, and NiceGUI's overrides layer is exactly that
   and is the intended home for app overrides. */
@layer overrides {
  .text-primary, .text-primary .q-icon {
    color: #6da7df !important;
  }
}
/* Material icon inside button: small gap from label, never overlapping */
.q-btn .q-icon { margin-right: 6px !important; }
.q-btn--round .q-icon { margin-right: 0 !important; }

/* pointer cursor on everything clickable */
.q-tab, .q-chip, .q-toggle, .q-checkbox, .q-radio,
.q-uploader__btn, .q-uploader__list .q-item,
.q-item.cursor-pointer, [role="button"], [role="tab"],
.q-card.cursor-pointer, .bb-row, summary {
  cursor: pointer !important;
}
/* date picker fields. clicking anywhere in the box toggles the calendar, so the
   pointer cursor has to cover the whole box, not just the icon. the .cursor-pointer
   class lands on the q-field wrapper, but Quasar's inner control and native input
   keep their own text cursor, so target the field and all its descendants. */
.bb-date-field, .bb-date-field * {
  cursor: pointer !important;
}

/* Inputs: let Quasar handle internal padding (overriding it breaks label
   floating). Only style colors + outlined border so labels never overlap values. */
.q-field--outlined .q-field__control {
  background: var(--bb-surface) !important;
  border-radius: 10px !important;
}
.q-field__native, .q-field__input {
  color: var(--bb-text) !important;
}
.q-field__label {
  color: var(--bb-text-muted) !important;
}
.q-field--outlined .q-field__control:before {
  border-color: var(--bb-border) !important;
}
.q-field--outlined .q-field__control:hover:before {
  border-color: var(--bb-border-strong) !important;
}
.q-field--outlined.q-field--focused .q-field__control:after {
  border-color: var(--bb-accent) !important;
}
.q-field__native::placeholder,
.q-field__input::placeholder {
  color: var(--bb-text-very-muted) !important;
  opacity: 1;
}

/* unsaveable preview field, e.g. amount edited to $0 or an unrecognized store name,
   gets a clear red outline and red text. driven by a CSS class because NiceGUI syncs
   .classes() live in both directions, whereas a live added Quasar error prop does not
   re-render reliably (it works at first render and on removal, but a fresh add mid
   edit never reaches the client, so a field edited to $0 would stay unhighlighted).
   the class stays red even while focused, overriding the accent blue focus ring, so
   the error always reads. */
.bb-field-bad.q-field--outlined .q-field__control:before {
  border-color: var(--bb-expense) !important;
  border-width: 2px !important;
}
.bb-field-bad.q-field--outlined.q-field--focused .q-field__control:after {
  border-color: var(--bb-expense) !important;
}
.bb-field-bad .q-field__native,
.bb-field-bad .q-field__input {
  color: var(--bb-expense) !important;
}

/* Select (dropdown) */
.q-menu {
  background: var(--bb-bg-elevated) !important;
  border: 1px solid var(--bb-border);
  border-radius: 10px;
  box-shadow: var(--bb-shadow-lg) !important;
}
.q-item {
  color: var(--bb-text) !important;
}
.q-item--active, .q-item.q-router-link--active {
  color: var(--bb-accent) !important;
  background: color-mix(in srgb, var(--bb-accent) 12%, transparent) !important;
}

/* Dialog (modal) */
.q-dialog .q-card {
  border-radius: 16px !important;
  min-width: 420px;
  max-width: 92vw;
}

/* Drawer (search) */
.q-drawer {
  background: var(--bb-bg-elevated) !important;
}

/* Empty state card */
.bb-empty {
  padding: 56px 24px !important;
  text-align: center;
}
.bb-empty .emoji { font-size: 3rem; line-height: 1; }
.bb-empty .title { font-size: 1.05rem; font-weight: 600; color: var(--bb-text); margin-top: 8px; }
.bb-empty .msg   { font-size: 0.88rem; color: var(--bb-text-muted); margin-top: 4px; }

/* Plotly */
.js-plotly-plot {
  background: transparent !important;
}

/* Chip: for category labels and API status */
.q-chip {
  border-radius: 999px !important;
  font-weight: 500 !important;
}

/* File upload drop area */
.q-uploader {
  background: var(--bb-surface) !important;
  border: 2px dashed var(--bb-border) !important;
  border-radius: 14px;
  color: var(--bb-text) !important;
}
.q-uploader__header {
  background: transparent !important;
  color: var(--bb-text) !important;
}
/* Tame Quasar's oversized bold title so the prompt reads as one clean line */
.q-uploader__title {
  font-size: 0.95rem !important;
  font-weight: 500 !important;
  line-height: 1.3 !important;
  white-space: normal !important;
}
/* hide Quasar's idle size counter, the app shows its own progress instead */
.q-uploader__subtitle {
  display: none !important;
}
.q-uploader__list {
  color: var(--bb-text);
}
/* hide Quasar's own file list. the app renders its own queued files list below the
   box (with a per file remove cross and a Clear all button) as the single source of
   truth, so removals reliably update what gets extracted. two lists would desync. */
.bb-upload .q-uploader__list {
  display: none !important;
}
/* The app's queued-files panel (custom, below the upload box) */
.bb-queued {
  margin-top: 8px;
  border: 1px solid var(--bb-border);
  border-radius: 12px;
  overflow: hidden;
}
.bb-queued-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 6px 6px 6px 12px;
  background: var(--bb-surface);
  border-bottom: 1px solid var(--bb-border);
}
.bb-queued-file {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 6px 6px 12px;
}
.bb-queued-file + .bb-queued-file {
  border-top: 1px solid var(--bb-border);
}
.bb-queued-file .name {
  flex: 1; min-width: 0; font-size: 0.86rem; color: var(--bb-text);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.bb-queued-file .size { font-size: 0.74rem; color: var(--bb-text-muted); white-space: nowrap; }

/* Tables (used in Top merchants etc.) */
.q-table {
  background: var(--bb-surface) !important;
  color: var(--bb-text) !important;
}
.q-table thead th {
  color: var(--bb-text-muted) !important;
  border-bottom: 1px solid var(--bb-border);
  background: transparent !important;
  font-weight: 500 !important;
  text-transform: uppercase !important;
  font-size: 0.72rem !important;
  letter-spacing: 0.04em !important;
}
.q-table tbody tr:hover {
  background: var(--bb-surface-hover) !important;
}

/* Toggles */
.q-toggle__inner--falsy .q-toggle__track {
  color: var(--bb-text-muted) !important;
}

/* Search drawer result row */
.bb-search-row {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--bb-border);
  transition: background 0.1s ease;
}
.bb-search-row:hover {
  background: var(--bb-surface-hover);
}
.bb-search-row .sr-avatar {
  width: 30px; height: 30px; border-radius: 8px;
  display: flex; align-items: center; justify-content: center;
  font-size: 0.95rem; flex-shrink: 0;
  white-space: nowrap;          /* keep 2 emojis on one line, no vertical stack */
}
.bb-search-row .sr-avatar.duo { font-size: 0.7rem; letter-spacing: -0.5px; }
.bb-search-row .sr-main { flex: 1; min-width: 0; }
.bb-search-row .sr-merchant { color: var(--bb-text); font-weight: 500; font-size: 0.9rem; }
.bb-search-row .sr-sub      { color: var(--bb-text-muted); font-size: 0.74rem; }
.bb-search-row .sr-amount   { font-weight: 600; }
.bb-search-row .sr-amount.income  { color: var(--bb-income); }
.bb-search-row .sr-amount.expense { color: var(--bb-expense); }

/* search results summary, a full width header with the count and total above the rows */
.bb-search-summary {
  display: flex; align-items: baseline; gap: 8px;
  width: 100%;
  padding: 12px;                      /* same horizontal inset as .bb-search-row */
  border-bottom: 1px solid var(--bb-border-strong);
}
.bb-search-summary .label { color: var(--bb-text); font-size: 0.95rem; font-weight: 500; }
.bb-search-summary .total { font-size: 1.02rem; font-weight: 700; white-space: nowrap; }
.bb-search-summary .total.income  { color: var(--bb-income); }
.bb-search-summary .total.expense { color: var(--bb-expense); }

/* Hide Quasar's default page wrapper padding (we manage our own) */
.q-page-container { padding-top: 0 !important; }

/* Smooth scrolling */
* { scrollbar-width: thin; scrollbar-color: var(--bb-border) transparent; }
*::-webkit-scrollbar { width: 8px; height: 8px; }
*::-webkit-scrollbar-track { background: transparent; }
*::-webkit-scrollbar-thumb { background: var(--bb-border); border-radius: 4px; }
*::-webkit-scrollbar-thumb:hover { background: var(--bb-border-strong); }

/* Better focus rings, accent-tinted */
*:focus-visible {
  outline: 2px solid var(--bb-accent) !important;
  outline-offset: 1px;
}

/* Text utility */
.text-positive { color: var(--bb-income) !important; }
.text-negative { color: var(--bb-expense) !important; }
.text-muted { color: var(--bb-text-muted) !important; }

/* Layout */
.bb-container {
  max-width: 1080px;
  margin: 0 auto;
  padding: 24px;
}
</style>
"""


def render_search_row_html(merchant: str, date_str: str, category: str,
                            emoji: str, color: str, amount: float,
                            needs_review: bool = False,
                            is_duplicate: bool = False) -> str:
    # one row of the search results drawer, returned as HTML for fast rendering
    if needs_review and is_duplicate:
        emoji, category, color = "⚠️", "Needs review · possible duplicate", "#fbbf24"
    elif needs_review:
        emoji, category, color = "⚠️", "Needs review", "#fbbf24"
    elif is_duplicate:
        emoji, category, color = "📑", "Possible duplicate", "#a78bfa"
    is_income = amount > 0
    amt = ("+ $" if is_income else "− $") + amount_str(amount)
    cls = "income" if is_income else "expense"
    duo = " duo" if count_emojis(emoji) >= 2 else ""
    return (
        f"<div class='bb-search-row'>"
        f"<div class='sr-avatar{duo}' style='background:color-mix(in srgb, {color} 18%, transparent); "
        f"border:1px solid color-mix(in srgb, {color} 35%, transparent);'>{html.escape(emoji)}</div>"
        f"<div class='sr-main'>"
        f"<div class='sr-merchant'>{html.escape(merchant)}</div>"
        f"<div class='sr-sub'>{html.escape(date_str)} · {html.escape(category)}</div>"
        f"</div>"
        f"<div class='sr-amount {cls}'>{html.escape(amt)}</div>"
        f"</div>"
    )


def render_empty_state_html(emoji: str, title: str, msg: str) -> str:
    return (
        f"<div class='bb-empty'>"
        f"<div class='emoji'>{html.escape(emoji)}</div>"
        f"<div class='title'>{html.escape(title)}</div>"
        f"<div class='msg'>{html.escape(msg)}</div>"
        f"</div>"
    )
