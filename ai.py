# Claude integration for extracting and categorizing transactions from statements
from __future__ import annotations

import base64
import json
import math
from datetime import datetime
from typing import Any

import anthropic

# default model for every API call. the app loads a saved override from Settings at
# startup and can change ai.MODEL at runtime, so if Anthropic retires this model you
# just update it in the app, no code editing needed.
DEFAULT_MODEL = "claude-sonnet-4-6"
MODEL = DEFAULT_MODEL
CONFIDENCE_THRESHOLD = 0.7

REVIEW_CATEGORY = "Needs Review"


# field sanitizers, the model can hallucinate junk

def _finite_amount(value: Any) -> float:
    # convert a model-provided amount to a finite float, or raise ValueError. mirrors
    # db._coerce_amount so a hallucinated inf, NaN, or non-numeric amount gets dropped at
    # parse time (the bad row is skipped by _parse_response) instead of storing infinity
    # or poisoning the batch insert. json accepts Infinity/-Infinity/NaN and a tool number
    # can arrive as a string, so plain float() is not enough, the finite check is the guard.
    amt = float(value)  # may raise TypeError or ValueError, caller skips this row
    if not math.isfinite(amt):
        raise ValueError(f"non-finite amount: {value!r}")
    return amt


def _clamp_confidence(value: Any, default: float = 0.5) -> float:
    # best-effort confidence in [0.0, 1.0], never raises. a weird confidence (out of
    # range, NaN, inf, or non-numeric) must not cost us a real transaction or make a
    # nonsense inf% note, so we clamp instead of dropping the row.
    try:
        c = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(c):
        return default
    return min(1.0, max(0.0, c))


# api cost metering
# published Claude Sonnet rates (USD per million tokens), for display estimates only.
# Anthropic's API doesn't expose your real account balance, so the app meters token
# usage and multiplies by these rates. adjust here if Anthropic changes pricing.
PRICE_INPUT_PER_MTOK = 3.00
PRICE_OUTPUT_PER_MTOK = 15.00
PRICE_CACHE_WRITE_PER_MTOK = 3.75   # writing the 5-min ephemeral prompt cache
PRICE_CACHE_READ_PER_MTOK = 0.30    # cache hit, cheap, which is why we cache system and tools

_USAGE_HOOK = None  # optional callable(cost_usd: float, tokens: int) -> None


def set_usage_hook(fn) -> None:
    # register a callback invoked after every API response with (estimated_cost, tokens)
    global _USAGE_HOOK
    _USAGE_HOOK = fn


def usage_cost(usage) -> float:
    # estimated USD cost of one API response's token usage
    def g(attr: str) -> float:
        return float(getattr(usage, attr, 0) or 0)
    return (
        g("input_tokens") * PRICE_INPUT_PER_MTOK
        + g("output_tokens") * PRICE_OUTPUT_PER_MTOK
        + g("cache_creation_input_tokens") * PRICE_CACHE_WRITE_PER_MTOK
        + g("cache_read_input_tokens") * PRICE_CACHE_READ_PER_MTOK
    ) / 1_000_000.0


def usage_tokens(usage) -> int:
    # total tokens (input + output + cache write + cache read) for one response
    def g(attr: str) -> int:
        return int(getattr(usage, attr, 0) or 0)
    return (g("input_tokens") + g("output_tokens")
            + g("cache_creation_input_tokens") + g("cache_read_input_tokens"))


def _meter(response) -> None:
    # report a response's token usage to the hook, best-effort, never raises
    if _USAGE_HOOK is None:
        return
    try:
        usage = getattr(response, "usage", None)
        if usage is not None:
            _USAGE_HOOK(usage_cost(usage), usage_tokens(usage))
    except Exception:
        pass


# error classes

# base class for AI extraction errors surfaced to the user
class AIError(Exception):
    pass


class APIKeyError(AIError):
    pass  # invalid or missing API key


class RateLimitError(AIError):
    pass  # rate limited by Anthropic


class BillingError(AIError):
    pass  # out of credits or billing limit reached, the key works but can't be used


# Anthropic's servers are temporarily overloaded (HTTP 529). transient, the key is
# fine and a retry a moment later usually succeeds.
class OverloadedError(AIError):
    pass


def _is_billing_message(msg: str) -> bool:
    # true if an error message looks like an out-of-credits or billing problem
    low = (msg or "").lower()
    return any(s in low for s in (
        "credit balance",
        "purchase credits",
        "plans & billing",
        "billing",
        "payment required",
        "insufficient",
    ))


def _translate_anthropic_error(exc: Exception) -> AIError:
    # map an anthropic SDK exception into a friendly AIError subclass
    name = type(exc).__name__
    msg = str(exc) or name
    if isinstance(exc, anthropic.AuthenticationError):
        return APIKeyError("Your API key was rejected. Double-check it in Settings.")
    if isinstance(exc, anthropic.RateLimitError):
        return RateLimitError(msg)
    # 529 overloaded isn't its own top-level SDK class in this version, it arrives as an
    # APIStatusError with status_code 529 and a message containing overloaded, so detect both
    if getattr(exc, "status_code", None) == 529 or "overloaded" in msg.lower():
        return OverloadedError(
            "Anthropic's servers are busy right now (overloaded). Your API key is fine, "
            "please wait a moment and try the upload again."
        )
    if _is_billing_message(msg):
        return BillingError(
            "Claude couldn't be used, your credit balance is too low. "
            "Add credits at console.anthropic.com (Plans & Billing)."
        )
    if isinstance(exc, anthropic.PermissionDeniedError):
        return APIKeyError("API key lacks permission for this model.")
    if isinstance(exc, anthropic.BadRequestError):
        return AIError(f"Claude rejected the request: {msg}")
    if isinstance(exc, anthropic.APIConnectionError):
        return AIError("Couldn't reach Anthropic. Check your internet connection.")
    if isinstance(exc, anthropic.APIStatusError):
        return AIError(f"Anthropic returned an error: {msg}")
    return AIError(f"{name}: {msg}")


def _build_system_prompt(
    category_names: list[str],
    category_descriptions: dict[str, str],
    recent_corrections: list[dict],
) -> str:
    cats_block = "\n".join(
        f"- {name}: {category_descriptions.get(name, 'general ' + name.lower())}"
        for name in category_names
    )

    # give the model today's date so it can default a missing year to the current year.
    # date only, no time, so the prompt text stays stable within a day and the ephemeral
    # prompt cache still hits across a multi-file batch.
    today = datetime.now()
    today_str = today.strftime("%Y-%m-%d")
    cur_year = today.year

    # the learned-store library is deliberately not injected into the prompt. learned
    # stores are applied locally and authoritatively in _clean_extracted (the AI's
    # category is overridden there for any store in the user's whole library), so listing
    # them here only spent tokens without changing the result. recent corrections (below)
    # are kept, a small high-signal hint for just-fixed stores.
    corr_lines = [
        f"- {c['merchant']!r} -> {c['category']}"
        for c in recent_corrections[:25]
    ]
    corr_block = "\n".join(corr_lines) if corr_lines else "(no corrections yet)"

    return f"""You are a financial assistant that extracts and categorizes transactions from bank or credit-card statements. The input is either a PDF or a screenshot.

CATEGORIES (you MUST pick exactly one of these per transaction, no inventing new ones):
{cats_block}

RECENT USER CORRECTIONS, the user fixed these recently, so learn from them:
{corr_block}

RULES:
1. Find every transaction (purchases, payments, refunds, transfers, fees). Skip running balances, statement totals, interest summaries that are not individual transactions. Each transaction is ONE line item with a real merchant/payee name. A standalone phone number, reference/confirmation number, or store number sitting on or beside a merchant line (e.g. "STARBUCKS 8007927282") is part of THAT merchant's descriptor, never split it into a second transaction, and never output a transaction whose merchant is only digits / a phone number.
2. For each transaction, output:
   - date: YYYY-MM-DD. Today's date is {today_str}. If the transaction shows a full date, use its year. If only a month/day are shown with NO year, ALWAYS use the CURRENT year ({cur_year}), even if that places the date a little in the future. Only when a screenshot's statement context clearly shows a different year should you prefer that shown year.
   - merchant: the cleaned-up display name. RULES for cleaning:
       a. STRIP store numbers like "#4372", "#1234", or trailing digits: "TIM HORTONS #4372" -> "Tim Hortons"
       b. STRIP leading "*" or POS prefixes (TST*, SQ*, SP*, TOAST*, PAYPAL*): "*RFBT-YONGE" -> "RFBT" (or expanded brand name if known)
       c. STRIP trailing city + province/state codes: "STARBUCKS TORONTO ON" -> "Starbucks", "CHIPOTLE 4644 TORONTO, ON" -> "Chipotle"
       d. EXPAND abbreviations to full brand name when you confidently know them: "NYF" -> "New York Fries", "AMZN MKTP" -> "Amazon", "WMT" -> "Walmart"
       e. Use Title Case, not ALL CAPS. e.g. "STARBUCKS" -> "Starbucks"
       f. If you don't recognize the abbreviation (e.g. some local chain), keep the cleaned-up letters but lower your confidence
       g. STRIP trailing phone numbers and long digit runs: "STARBUCKS 8007927282" -> "Starbucks". The digits are not part of the name and are never a separate transaction.
   - raw_description: the original line text from the statement (helpful for the user to verify).
   - amount: SIGNED. Expenses NEGATIVE (e.g. -24.50). Income / refunds / credits POSITIVE.
   - category: one of the categories above, exactly as written.
   - confidence: 0.0 to 1.0. Use < 0.7 when you genuinely can't tell what something is or which category fits.
   - reasoning: one short sentence explaining the category choice. Skip for obvious ones.
3. For totally unclear merchants (random codes, ATM cash-outs with no clue, etc.), still record the transaction but set confidence < 0.7, the app will route it to "{REVIEW_CATEGORY}" for the user to fix.
4. Include EVERY line item exactly as it appears, one record per line. Do NOT merge, deduplicate, or drop transactions, even if two lines look identical (same date, name, and amount) or if a charge appears as both a pending and a posted entry. Return all of them; the app flags possible duplicates for the user to review and decide, never silently drop one.
5. For income, only assign the "Income" category for actual income (salary, refunds clearly marked as such, transfers IN). Don't lump random positive amounts as income unless they obviously are.
6. IMPORTANT: don't be fooled by the literal word "Bill" in a descriptor. Look at the actual service:
   - `apple.com/Bill`, `APPLE.COM/BILL`, `APPLE COM BILL` -> Apple subscription -> **Entertainment**, not Bills
   - `GOOGLE *YouTube Premium`, `play.google.com/bill` -> digital subscription -> **Entertainment**
   - `PAYPAL *NETFLIX`, `PAYPAL *SPOTIFY` -> streaming subscription -> **Entertainment**
   - `BELL CANADA`, `ROGERS WIRELESS`, `TELUS MOBILITY` -> mobile phone bill -> **Bills**
   - `HYDRO ONE`, `ENBRIDGE GAS`, `TORONTO HYDRO` -> utility bill -> **Bills**
   - `STATE FARM`, `INTACT INSURANCE` -> insurance bill -> **Bills**
   Use the *service*, not the word "bill", to decide.

Call the record_transactions tool exactly ONCE with all the transactions you found."""


def _extraction_tool(category_names: list[str]) -> dict:
    return {
        "name": "record_transactions",
        "description": "Record all transactions extracted from the statement.",
        "input_schema": {
            "type": "object",
            "properties": {
                "transactions": {
                    "type": "array",
                    "description": "All transactions found in the statement.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "date": {"type": "string", "description": "YYYY-MM-DD"},
                            "merchant": {"type": "string", "description": "Cleaned-up merchant name"},
                            "raw_description": {"type": "string"},
                            "amount": {
                                "type": "number",
                                "description": "Signed. Negative for expenses, positive for income/refunds.",
                            },
                            "category": {"type": "string", "enum": category_names},
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            "reasoning": {"type": "string"},
                        },
                        "required": ["date", "merchant", "amount", "category", "confidence"],
                    },
                }
            },
            "required": ["transactions"],
        },
    }


CATEGORY_DESCRIPTIONS = {
    "Food":          "restaurants, cafes, fast food, takeout, food delivery, bars, bubble tea, coffee shops",
    "Groceries":     "supermarkets, grocery stores, farmer's markets, butchers",
    "Transportation":"gas, parking, public transit, taxis, ride-share, car maintenance, tolls",
    "Home":          "rent/mortgage, home utilities (water/electricity/gas), furniture, home improvement, hardware",
    "Bills":         (
        "ONLY for traditional utility/service bills you owe a real-world provider: "
        "internet, mobile phone plan, electricity, water, gas, home insurance, car insurance. "
        "DO NOT use this for digital/app/streaming subscriptions even if the descriptor "
        "contains the word 'bill' (e.g. 'apple.com/Bill' is NOT Bills, it's Entertainment)."
    ),
    "Entertainment": (
        "Streaming and digital subscriptions (Netflix, Spotify, Apple Music, Apple TV+, "
        "Apple One, iCloud, Disney+, HBO, YouTube Premium, Twitch), in-app purchases, "
        "games, movies, concerts, events. ALL `apple.com/...` and `play.google.com/...` "
        "charges go here unless you can confidently identify them as a non-entertainment "
        "service (e.g. a productivity app the user has corrected before)."
    ),
    "Shopping":      "clothing, electronics, general retail not in another category, Amazon (unless clearly a subscription)",
    "Health":        "pharmacy, doctor, dental, gym, fitness, wellness, prescriptions",
    "Travel":        "flights, hotels, Airbnb, car rentals, vacation expenses",
    "Income":        "salary, refunds clearly marked as such, transfers IN",
    "Transfers":     "moving money between own accounts, e-transfers between own accounts",
    "Needs Review":  (
        "Use this whenever you cannot CONFIDENTLY identify what the merchant is "
        "or which category fits, the user will pick the right category manually. "
        "When in doubt, choose this."
    ),
}


# public API

def make_client(api_key: str) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=api_key)


def extract_transactions_from_file(
    client: anthropic.Anthropic,
    file_bytes: bytes,
    filename: str,
    category_names: list[str],
    merchant_memory: dict[str, str],
    recent_corrections: list[dict],
    *,
    force_vision: bool = False,
    max_tokens: int = 16000,
) -> tuple[list[dict], str]:
    # extract transactions from a PDF, screenshot, or plain text.
    #
    # pipeline when force_vision is False:
    # 1. .txt or pasted text goes straight to Claude as text, no vision ever.
    # 2. PDFs: pdfplumber text extraction, if it returns text send the text to Claude.
    # 3. screenshots: Apple Vision OCR, if confidence is ok send the text to Claude.
    # 4. PDFs or screenshots that fail local extraction fall back to Claude vision.
    #
    # returns (transactions, mode) where mode is one of:
    #   text-file, plain text, a .txt upload or a pasted list
    #   text-pdf, PDF text extracted locally
    #   text-ocr, screenshot OCR'd locally
    #   vision-pdf, PDF sent as document (scanned or image PDF, or forced)
    #   vision-image, screenshot sent as image (OCR failed or forced)
    lower = filename.lower()
    is_pdf = lower.endswith(".pdf")
    is_image = any(lower.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"))
    is_text = lower.endswith(".txt")
    if not (is_pdf or is_image or is_text):
        raise ValueError(f"Unsupported file type: {filename}")

    # Plain text (a .txt upload or a pasted list) goes straight to the text parser.
    # There's no image to OCR and nothing to fall back to, so force_vision is moot.
    if is_text:
        text = file_bytes.decode("utf-8", errors="replace")
        txns = _extract_from_text(
            client, text, filename,
            category_names, merchant_memory, recent_corrections,
            source_label="text", max_tokens=max_tokens,
        )
        return txns, "text-file"

    # Try local extraction first unless forced.
    if not force_vision:
        try:
            import ocr  # local module
            if is_pdf:
                text = ocr.extract_pdf_text(file_bytes)
                if text:
                    txns = _extract_from_text(
                        client, text, filename,
                        category_names, merchant_memory, recent_corrections,
                        source_label="pdf", max_tokens=max_tokens,
                    )
                    return txns, "text-pdf"
            else:
                text, conf = ocr.extract_image_text(file_bytes)
                if text and conf >= 0.5:
                    txns = _extract_from_text(
                        client, text, filename,
                        category_names, merchant_memory, recent_corrections,
                        source_label="image", max_tokens=max_tokens,
                    )
                    return txns, "text-ocr"
        except Exception:
            # If local extraction itself errors, silently fall through to vision.
            pass

    # Fallback / forced: send the file directly to Claude vision.
    encoded = base64.standard_b64encode(file_bytes).decode("utf-8")
    if is_pdf:
        file_block: dict[str, Any] = {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": encoded},
        }
        mode = "vision-pdf"
    else:
        if lower.endswith(".png"):
            media = "image/png"
        elif lower.endswith(".webp"):
            media = "image/webp"
        elif lower.endswith(".gif"):
            media = "image/gif"
        else:
            media = "image/jpeg"
        file_block = {
            "type": "image",
            "source": {"type": "base64", "media_type": media, "data": encoded},
        }
        mode = "vision-image"

    system = _build_system_prompt(
        category_names, CATEGORY_DESCRIPTIONS, recent_corrections
    )
    tool = _extraction_tool(category_names)

    try:
        # prompt caching: system and tools cached for 5 min so multi-file uploads pay
        # only about 25% of the per-call input cost after the first file.
        response = client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=[
                {"type": "text", "text": system,
                 "cache_control": {"type": "ephemeral"}},
            ],
            tools=[{**tool, "cache_control": {"type": "ephemeral"}}],
            tool_choice={"type": "tool", "name": "record_transactions"},
            messages=[
                {
                    "role": "user",
                    "content": [
                        file_block,
                        {
                            "type": "text",
                            "text": (
                                f"Extract every transaction from this statement ({filename}). "
                                f"Call the record_transactions tool with all of them."
                            ),
                        },
                    ],
                }
            ],
        )
    except anthropic.APIError as e:
        raise _translate_anthropic_error(e) from e

    _meter(response)
    return _parse_response(response, filename, "pdf" if is_pdf else "image",
                           category_names, merchant_memory), mode


def _extract_from_text(
    client: anthropic.Anthropic,
    text: str,
    filename: str,
    category_names: list[str],
    merchant_memory: dict[str, str],
    recent_corrections: list[dict],
    source_label: str,
    max_tokens: int = 16000,
) -> list[dict]:
    # send pre-extracted text (from OCR or pdfplumber) to Claude for parsing and categorization
    system = _build_system_prompt(
        category_names, CATEGORY_DESCRIPTIONS, recent_corrections
    )
    tool = _extraction_tool(category_names)

    if source_label == "text":
        user_text = (
            f"The text below is a list of transactions the user pasted or uploaded "
            f"as a plain-text file ({filename}). It can be in ANY format, there is no "
            f"fixed layout. Columns may be separated by commas, tabs, pipes (|), or just "
            f"runs of spaces, and the order of date / description / amount may vary from "
            f"row to row. Dates may appear in any style (2026-05-01, 05/01/2026, "
            f"1 May 2026, May 1 2026, etc.), normalize every date to YYYY-MM-DD. Amounts "
            f"may use currency symbols, thousands separators, parentheses or a trailing "
            f"CR/DR/-/+ to mark sign (e.g. $1,234.56, (12.30), 12.30 DR), output a SIGNED "
            f"number: negative for money out/expenses, positive for money in/income/"
            f"refunds. Skip header rows and any line that isn't a real transaction "
            f"(column titles, totals, running balances, blank lines).\n\n"
            f"---\n{text}\n---\n\n"
            f"Call the record_transactions tool with every real transaction you find."
        )
    else:
        user_text = (
            f"The text below was extracted locally from the user's statement "
            f"({filename}). Pipes (|) separate columns when present. Some columns may be "
            f"misaligned where OCR was uncertain, use your judgment to map dates, "
            f"merchants, and amounts even if column boundaries are messy. Ignore lines "
            f"that aren't transactions (running balances, statement totals, page numbers).\n\n"
            f"---\n{text}\n---\n\n"
            f"Call the record_transactions tool with every real transaction you find."
        )

    try:
        # Prompt caching on system + tools for multi-file efficiency.
        response = client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=[
                {"type": "text", "text": system,
                 "cache_control": {"type": "ephemeral"}},
            ],
            tools=[{**tool, "cache_control": {"type": "ephemeral"}}],
            tool_choice={"type": "tool", "name": "record_transactions"},
            messages=[{"role": "user", "content": user_text}],
        )
    except anthropic.APIError as e:
        raise _translate_anthropic_error(e) from e
    _meter(response)
    return _parse_response(response, filename, source_label, category_names, merchant_memory)


def _parse_response(
    response,
    filename: str,
    source_label: str,
    category_names: list[str],
    merchant_memory: dict[str, str],
) -> list[dict]:
    raw_transactions: list = []
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "record_transactions":
            payload = block.input if isinstance(getattr(block, "input", None), dict) else {}
            rows = payload.get("transactions", [])
            # Defensive: the schema says a list, but a malformed tool response could send
            # null or an object. list(None) would crash, so only accept a real list.
            raw_transactions = rows if isinstance(rows, list) else []
            break

    cleaned: list[dict] = []
    for t in raw_transactions:
        if not isinstance(t, dict):
            continue  # a stray scalar or list among the rows, skip, don't crash
        try:
            cleaned.append(_clean_extracted(t, filename, source_label, category_names, merchant_memory))
        except Exception:
            continue
    return cleaned


def _clean_extracted(
    t: dict,
    filename: str,
    source_label: str,
    category_names: list[str],
    merchant_memory: dict[str, str],
) -> dict:
    date_str = str(t.get("date", "")).strip()
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"Bad date: {date_str}")

    merchant = str(t.get("merchant", "")).strip() or "Unknown"
    amount = _finite_amount(t.get("amount", 0))
    confidence = _clamp_confidence(t.get("confidence", 0.5))
    ai_category = str(t.get("category", "")).strip()
    raw_desc = str(t.get("raw_description", "")).strip() or None
    reasoning = str(t.get("reasoning", "")).strip()

    from db import normalize_merchant, clean_merchant_display, titlecase_if_shouting  # local import to avoid cycle
    # extra cleanup in case Claude left cruft, then tame shouty bank caps (TIM HORTONS
    # becomes Tim Hortons). case-taming lives on this import path only, so a name the
    # user later types or edits by hand keeps exactly the case they chose, all-caps included.
    merchant = clean_merchant_display(merchant) or merchant
    merchant = titlecase_if_shouting(merchant)

    learned = merchant_memory.get(normalize_merchant(merchant))
    if learned and learned in category_names:
        final_category = learned
        needs_review = False
        confidence = max(confidence, 0.95)
    elif ai_category not in category_names:
        final_category = REVIEW_CATEGORY
        needs_review = True
    elif confidence < CONFIDENCE_THRESHOLD:
        final_category = REVIEW_CATEGORY
        needs_review = True
    else:
        final_category = ai_category
        needs_review = False

    note_parts = []
    if raw_desc and raw_desc.lower() != merchant.lower():
        note_parts.append(f"raw: {raw_desc}")
    if needs_review and ai_category and ai_category != REVIEW_CATEGORY:
        note_parts.append(f"ai-guess: {ai_category} ({confidence:.0%})")
        if reasoning:
            note_parts.append(reasoning)
    note = " | ".join(note_parts) if note_parts else None

    return {
        "date": date_str,
        "merchant": merchant,
        "amount": amount,
        "category": final_category,
        "confidence": confidence,
        "needs_review": needs_review,
        "source": source_label,
        "source_file": filename,
        "note": note,
    }


def categorize_one(
    client: anthropic.Anthropic,
    merchant: str,
    amount: float,
    category_names: list[str],
    merchant_memory: dict[str, str],
) -> tuple[str, float]:
    # categorize a single transaction, used for manual entries with no category
    from db import normalize_merchant

    learned = merchant_memory.get(normalize_merchant(merchant))
    if learned and learned in category_names:
        return learned, 0.99

    system = (
        "You categorize a single transaction into exactly one of these categories: "
        + ", ".join(category_names)
        + ". Reply with JSON only: {\"category\": \"<name>\", \"confidence\": <0..1>}"
    )
    user = f"Merchant: {merchant}\nAmount: {amount}\nPick the best category."
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=200,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.APIError as e:
        raise _translate_anthropic_error(e) from e
    _meter(resp)
    text = ""
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            text += block.text
    try:
        start, end = text.find("{"), text.rfind("}") + 1
        parsed = json.loads(text[start:end])
        cat = parsed.get("category", REVIEW_CATEGORY)
        conf = _clamp_confidence(parsed.get("confidence", 0.5))
        if cat not in category_names:
            cat = REVIEW_CATEGORY
        return cat, conf
    except Exception:
        return REVIEW_CATEGORY, 0.0
