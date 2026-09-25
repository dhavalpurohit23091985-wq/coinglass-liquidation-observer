import asyncio
import os
import re
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from playwright.async_api import async_playwright


# ============================================================
# CONFIG
# ============================================================

URL = "https://www.coinglass.com/liquidations"

SCAN_SECONDS = 60

TOP_N = 10

VALUE_THRESHOLD = 1_000_000.0

IST = ZoneInfo("Asia/Kolkata")

PUSHOVER_USER_KEY = os.getenv("PUSHOVER_USER_KEY", "").strip()
PUSHOVER_APP_TOKEN = os.getenv("PUSHOVER_APP_TOKEN", "").strip()
STATE_FILE = os.getenv(
    "COINGLASS_STATE_FILE",
    "/tmp/coinglass_observer_state.json"
).strip()



# ============================================================
# STATE
# ============================================================

# Per-symbol selected qualifying state.
# A coin gets ONE vote:
#   4H first if abs(Long-Short) >= $1M
#   otherwise 12H if abs(Long-Short) >= $1M
#   otherwise no vote.
states = {"VALUE": {}}
bootstrap_complete = False


def save_states():
    payload = {
        "states": states,
        "bootstrap_complete": bool(bootstrap_complete),
    }
    tmp_path = f"{STATE_FILE}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_path, STATE_FILE)
    except Exception as exc:
        print(f"[STATE SAVE FAILED] {type(exc).__name__}: {exc}", flush=True)


def load_states():
    global states, bootstrap_complete
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            payload = json.load(handle)

        loaded = payload.get("states", {})
        bucket = loaded.get("VALUE", {}) if isinstance(loaded, dict) else {}
        states["VALUE"] = bucket if isinstance(bucket, dict) else {}
        bootstrap_complete = bool(payload.get("bootstrap_complete", True))

        print(
            f"[STATE RESTORED] VALUE={len(states['VALUE'])} | "
            f"bootstrap_complete={bootstrap_complete}",
            flush=True,
        )
        return True

    except FileNotFoundError:
        print("[STATE RESTORE] No saved state found; first scan will bootstrap.", flush=True)
    except Exception as exc:
        print(f"[STATE RESTORE FAILED] {type(exc).__name__}: {exc}", flush=True)

    states["VALUE"] = {}
    bootstrap_complete = False
    return False


# ============================================================
# BASIC HELPERS
# ============================================================

def now_ist_dt():
    return datetime.now(IST)


def now_ist():
    return now_ist_dt().strftime("%d-%m-%Y %H:%M:%S IST")


def clean_text(value):
    if value is None:
        return ""
    return " ".join(
        str(value).replace("\xa0", " ").replace("\u200b", "").split()
    )


def parse_number(text):
    s = clean_text(text).upper()
    if not s:
        return 0.0

    s = s.replace("$", "").replace(",", "").replace("−", "-")
    match = re.search(r"-?\d+(?:\.\d+)?", s)
    if not match:
        return 0.0

    value = float(match.group())
    if "B" in s:
        value *= 1_000_000_000
    elif "M" in s:
        value *= 1_000_000
    elif "K" in s:
        value *= 1_000
    return value


def fmt_money(value):
    value = float(value)
    if abs(value) >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if abs(value) >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if abs(value) >= 1_000:
        return f"${value / 1_000:.2f}K"
    return f"${value:.2f}"


def fmt_price(value):
    value = float(value or 0.0)
    if value <= 0:
        return "N/A"
    if value >= 1000:
        return f"${value:,.2f}"
    if value >= 1:
        return f"${value:,.4f}".rstrip("0").rstrip(".")
    return f"${value:,.8f}".rstrip("0").rstrip(".")


# ============================================================
# PUSHOVER
# ============================================================

def pushover_ready():
    return bool(PUSHOVER_USER_KEY and PUSHOVER_APP_TOKEN)


def send_pushover(title, message):
    if not pushover_ready():
        print("[PUSHOVER] Not configured - notification skipped", flush=True)
        return False

    try:
        response = requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": PUSHOVER_APP_TOKEN,
                "user": PUSHOVER_USER_KEY,
                "title": title,
                "message": message,
                "priority": 0,
            },
            timeout=15,
        )
        if response.ok:
            print(f"[PUSHOVER SENT] {title}", flush=True)
            return True
        print(f"[PUSHOVER FAILED] HTTP {response.status_code}", flush=True)
    except Exception as exc:
        print(f"[PUSHOVER FAILED] {type(exc).__name__}: {exc}", flush=True)
    return False


def selected_vote(row):
    """Return one selected vote per coin: 4H first, otherwise 12H, otherwise NONE."""
    l4 = float(row.get("long_4h", 0.0) or 0.0)
    s4 = float(row.get("short_4h", 0.0) or 0.0)
    g4_signed = l4 - s4

    if abs(g4_signed) >= VALUE_THRESHOLD:
        return {
            "qualified": True,
            "timeframe": "4H",
            "side": "LONG" if g4_signed > 0 else "SHORT",
            "gap": abs(g4_signed),
        }

    l12 = float(row.get("long_12h", 0.0) or 0.0)
    s12 = float(row.get("short_12h", 0.0) or 0.0)
    g12_signed = l12 - s12

    if abs(g12_signed) >= VALUE_THRESHOLD:
        return {
            "qualified": True,
            "timeframe": "12H",
            "side": "LONG" if g12_signed > 0 else "SHORT",
            "gap": abs(g12_signed),
        }

    return {"qualified": False, "timeframe": None, "side": None, "gap": 0.0}


def vote_key(vote):
    if not vote.get("qualified"):
        return "NONE"
    return f"{vote['timeframe']}:{vote['side']}"


def build_top10_message(rows, trigger_symbols):
    long_votes = 0
    short_votes = 0
    lines = ["COINGLASS TOP-10 | $1M GAP VOTE", ""]

    for rank, row in enumerate(rows[:TOP_N], start=1):
        symbol = row["symbol"]
        vote = selected_vote(row)

        if vote["qualified"]:
            if vote["side"] == "LONG":
                long_votes += 1
            else:
                short_votes += 1
            status = f"{vote['side']} {fmt_money(vote['gap'])} ({vote['timeframe']})"
        else:
            status = "NO $1M GAP"

        marker = "  << TRIGGER" if symbol in trigger_symbols else ""
        lines.append(f"{rank}. {symbol}: {status}{marker}")

    lines.extend([
        "",
        f"LONG GAP >=$1M: {long_votes}/{len(rows[:TOP_N])}",
        f"SHORT GAP >=$1M: {short_votes}/{len(rows[:TOP_N])}",
    ])

    if long_votes > short_votes:
        lines.append(f"COUNT WINNER: LONG {long_votes} vs SHORT {short_votes}")
    elif short_votes > long_votes:
        lines.append(f"COUNT WINNER: SHORT {short_votes} vs LONG {long_votes}")
    else:
        lines.append(f"COUNT WINNER: TIE {long_votes}-{short_votes}")

    return "\n".join(lines), long_votes, short_votes


# ============================================================
# PAGE HELPERS
# ============================================================

async def get_rendered_lines(page):
    body_text = await page.locator(
        "body"
    ).inner_text(
        timeout=15000
    )

    lines = []

    for line in body_text.splitlines():
        line = clean_text(line)

        if line:
            lines.append(line)

    print(
        f"[DEBUG] Rendered text lines="
        f"{len(lines)}",
        flush=True,
    )

    return lines


async def wait_for_liquidation_section(page):
    await page.get_by_text(
        "Total Liquidations",
        exact=False,
    ).first.wait_for(
        state="visible",
        timeout=30000,
    )

    print(
        "[PAGE] Total Liquidations "
        "section visible",
        flush=True,
    )



# ============================================================
# VALUE PARSER HELPERS
# ============================================================

def is_number_like(text):
    s = clean_text(text)
    return bool(
        re.fullmatch(
            r"[-+−]?\$?\d[\d,]*(?:\.\d+)?(?:[KMB])?%?",
            s,
            flags=re.I,
        )
    )


def is_symbol_like(text):
    s = clean_text(text).upper()
    if not re.fullmatch(r"[A-Z0-9]{2,15}", s):
        return False

    ignored = {
        "PRICE", "ASSETS", "LONG", "SHORT", "RANKING", "VALUE",
        "TRADES", "TOTAL", "LIQUIDATIONS", "USD", "USDT",
        "24H", "12H", "4H", "1H",
    }
    return s not in ignored


def find_value_header(lines):
    for i in range(len(lines)):
        block = " ".join(lines[i:i + 20]).lower()
        if (
            "assets" in block
            and "1h long" in block
            and "1h short" in block
            and "4h long" in block
            and "4h short" in block
            and "12h long" in block
            and "12h short" in block
        ):
            return i
    raise RuntimeError("VALUE liquidation header not found")


# ============================================================
# DYNAMIC TOP-10 PARSER
# ============================================================

def parse_value_rows(lines):
    header_index = find_value_header(lines)
    search_lines = lines[header_index + 1: header_index + 500]

    ignored_exact = {
        "Ranking", "Assets", "Price", "Price (24h%)",
        "1h Long", "1h Short", "4h Long", "4h Short",
        "12h Long", "12h Short", "24h Long", "24h Short",
        "Liquidation Value", "Liquidation Trades",
    }
    filtered = [item for item in search_lines if item not in ignored_exact]

    results = []
    seen = set()
    i = 0

    while i < len(filtered) and len(results) < TOP_N:
        current = filtered[i]
        symbol = None
        symbol_index = None
        rank = None

        if (
            re.fullmatch(r"#?\d+", current)
            and i + 1 < len(filtered)
            and is_symbol_like(filtered[i + 1])
        ):
            rank = int(current.lstrip("#"))
            symbol = filtered[i + 1].upper()
            symbol_index = i + 1
        elif is_symbol_like(current):
            symbol = current.upper()
            symbol_index = i

        if symbol is None or symbol in seen:
            i += 1
            continue

        numbers = []
        for j in range(symbol_index + 1, min(symbol_index + 20, len(filtered))):
            candidate = filtered[j]

            if (
                j > symbol_index + 2
                and re.fullmatch(r"#?\d+", candidate)
                and j + 1 < len(filtered)
                and is_symbol_like(filtered[j + 1])
            ):
                break

            if is_number_like(candidate):
                numbers.append(candidate)

        # Expected numeric order after symbol:
        # price, 24h%, 1hL, 1hS, 4hL, 4hS, 12hL, 12hS, 24hL, 24hS...
        if len(numbers) >= 8:
            row = {
                "rank": rank if rank is not None else len(results) + 1,
                "symbol": symbol,
                "price": parse_number(numbers[0]),
                "long_4h": parse_number(numbers[4]),
                "short_4h": parse_number(numbers[5]),
                "long_12h": parse_number(numbers[6]),
                "short_12h": parse_number(numbers[7]),
            }
            results.append(row)
            seen.add(symbol)

            print(
                f"[PARSED] #{row['rank']} {symbol} | "
                f"4H L={numbers[4]} S={numbers[5]} | "
                f"12H L={numbers[6]} S={numbers[7]}",
                flush=True,
            )

        i += 1

    if len(results) < TOP_N:
        raise RuntimeError(f"Only {len(results)}/{TOP_N} Top-10 VALUE rows parsed")

    results = results[:TOP_N]
    print(
        f"[DEBUG] TOP-{TOP_N} rows={len(results)} | "
        f"symbols={','.join(row['symbol'] for row in results)}",
        flush=True,
    )
    return results


# ============================================================
# TOP-10 4H -> 12H VOTE / ALERT ENGINE
# ============================================================

def process_value_rows(rows, allow_alert=True):
    current_symbols = {row["symbol"] for row in rows[:TOP_N]}
    trigger_symbols = []

    # Drop state for coins that are no longer in the current Top-10.
    for symbol in list(states["VALUE"].keys()):
        if symbol not in current_symbols:
            states["VALUE"].pop(symbol, None)

    for row in rows[:TOP_N]:
        symbol = row["symbol"]
        vote = selected_vote(row)
        new_key = vote_key(vote)

        old = states["VALUE"].get(symbol, {})
        old_key = old.get("key", "NONE")

        # Alert on a fresh qualifying state or a selected side/timeframe change.
        # No repeated alert while the same selected 4H/12H side remains active.
        if allow_alert and new_key != "NONE" and new_key != old_key:
            trigger_symbols.append(symbol)

        states["VALUE"][symbol] = {
            "key": new_key,
            "qualified": vote["qualified"],
            "timeframe": vote["timeframe"],
            "side": vote["side"],
        }

    save_states()

    message, long_votes, short_votes = build_top10_message(rows, trigger_symbols)

    print("\n" + message, flush=True)

    if trigger_symbols and allow_alert:
        trigger_text = ",".join(trigger_symbols)
        title = f"COINGLASS TOP-10 | $1M GAP | {trigger_text}"
        send_pushover(title, message)


# ============================================================
# ONE SCAN
# ============================================================

async def scan_once(page):
    global bootstrap_complete

    print(
        "\n############################################################\n"
        f"[SCAN START] {now_ist()}\n"
        "############################################################",
        flush=True,
    )

    response = await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    status = response.status if response else None
    print(f"[HTTP] status={status}", flush=True)
    print(f"[HTTP] final_url={page.url}", flush=True)

    if response is not None and response.status >= 400:
        raise RuntimeError(f"CoinGlass HTTP {response.status}")

    await page.wait_for_timeout(8000)
    print(f"[PAGE] title={await page.title()}", flush=True)
    await wait_for_liquidation_section(page)

    value_lines = await get_rendered_lines(page)
    value_rows = parse_value_rows(value_lines)

    process_value_rows(value_rows, allow_alert=bootstrap_complete)

    if not bootstrap_complete:
        bootstrap_complete = True
        save_states()
        print(
            "[BOOTSTRAP COMPLETE] Current Top-10 vote states seeded; "
            "future fresh $1M qualifications/changes can alert.",
            flush=True,
        )

    print(
        "\n############################################################\n"
        f"[SCAN OK] {now_ist()} | TOP10={len(value_rows)}\n"
        "############################################################",
        flush=True,
    )


# ============================================================
# MAIN
# ============================================================

async def main():
    print("COINGLASS TOP-10 ADVANCED OBSERVER STARTING", flush=True)
    load_states()

    print(f"URL: {URL}", flush=True)
    print(f"SCAN: every {SCAN_SECONDS} seconds", flush=True)
    print(f"THRESHOLD: {fmt_money(VALUE_THRESHOLD)}", flush=True)
    print("MODE: TOP-10 | 4H FIRST -> 12H FALLBACK | ONE VOTE PER COIN", flush=True)
    print(f"PUSHOVER: {'READY' if pushover_ready() else 'NOT CONFIGURED'}", flush=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        context = await browser.new_context(
            viewport={
                "width": 1600,
                "height": 1200,
            },
            locale="en-US",
            timezone_id="Asia/Kolkata",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/140.0.0.0 Safari/537.36"
            ),
        )

        page = await context.new_page()

        while True:
            cycle_started = datetime.now(IST)

            try:
                await scan_once(page)

            except Exception as exc:
                print(
                    f"\n[SCAN FAILED] "
                    f"{now_ist()} | "
                    f"{type(exc).__name__}: "
                    f"{exc}",
                    flush=True,
                )

            elapsed = (
                datetime.now(IST)
                - cycle_started
            ).total_seconds()

            sleep_for = max(
                5,
                SCAN_SECONDS - elapsed,
            )

            print(
                f"[NEXT SCAN] approximately "
                f"{int(sleep_for)} seconds",
                flush=True,
            )

            await asyncio.sleep(
                sleep_for
            )


if __name__ == "__main__":
    asyncio.run(main())
