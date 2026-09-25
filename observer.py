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

ENTER_GAP = 5_000_000.0
EXIT_GAP = 4_000_000.0

IST = ZoneInfo("Asia/Kolkata")

PUSHOVER_USER_KEY = os.getenv("PUSHOVER_USER_KEY", "").strip()
PUSHOVER_APP_TOKEN = os.getenv("PUSHOVER_APP_TOKEN", "").strip()
STATE_FILE = os.getenv(
    "COINGLASS_GAP_STATE_FILE",
    "/tmp/coinglass_gap_state.json"
).strip()


# ============================================================
# STATE
# ============================================================

SYMBOLS = ("BTC", "ETH", "SOL")
TIMEFRAMES = ("1H", "4H", "12H")

state = {
    symbol: {tf: False for tf in TIMEFRAMES}
    for symbol in SYMBOLS
}


def save_state():
    tmp = f"{STATE_FILE}.tmp"
    try:
        directory = os.path.dirname(STATE_FILE)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as exc:
        print(f"[STATE SAVE FAILED] {type(exc).__name__}: {exc}", flush=True)


def load_state():
    global state
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)

        restored = {
            symbol: {tf: False for tf in TIMEFRAMES}
            for symbol in SYMBOLS
        }
        for symbol in SYMBOLS:
            if isinstance(saved.get(symbol), dict):
                for tf in TIMEFRAMES:
                    restored[symbol][tf] = bool(saved[symbol].get(tf, False))
        state = restored
        print(f"[STATE RESTORED] {state}", flush=True)
    except FileNotFoundError:
        print("[STATE] No saved state; starting all combinations IDLE.", flush=True)
    except Exception as exc:
        print(f"[STATE RESTORE FAILED] {type(exc).__name__}: {exc}", flush=True)


# ============================================================
# BASIC HELPERS
# ============================================================

def now_ist():
    return datetime.now(IST).strftime("%d-%m-%Y %H:%M:%S IST")


def clean_text(value):
    if value is None:
        return ""
    return " ".join(str(value).replace("\xa0", " ").replace("\u200b", "").split())


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


def is_number_like(text):
    s = clean_text(text)
    return bool(re.fullmatch(r"[-+−]?\$?\d[\d,]*(?:\.\d+)?(?:[KMB])?%?", s, flags=re.I))


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


# ============================================================
# GAP MONITOR
# ============================================================

def values_for_timeframe(row, timeframe):
    tf = timeframe.lower()
    long_value = float(row[f"long_{tf}"])
    short_value = float(row[f"short_{tf}"])
    signed_gap = long_value - short_value
    gap = abs(signed_gap)
    dominant = "LONG" if signed_gap > 0 else "SHORT" if signed_gap < 0 else "EVEN"
    return long_value, short_value, gap, dominant


def send_gap_alert(symbol, timeframe, long_value, short_value, gap, dominant, high):
    if high:
        title = f"{symbol} {timeframe} | GAP >= $5M | {fmt_money(gap)}"
        status = "GAP CROSSED / REACHED $5M"
    else:
        title = f"{symbol} {timeframe} | GAP < $4M | {fmt_money(gap)}"
        status = "GAP CROSSED BELOW $4M"

    message = "\n".join([
        f"{symbol} {timeframe}",
        "",
        f"LONG : {fmt_money(long_value)}",
        f"SHORT: {fmt_money(short_value)}",
        f"GAP  : {fmt_money(gap)}",
        f"DOMINANT: {dominant} LIQUIDATIONS" if dominant != "EVEN" else "DOMINANT: EVEN",
        "",
        status,
    ])
    send_pushover(title, message)


def process_row(row):
    symbol = row["symbol"]

    for timeframe in TIMEFRAMES:
        long_value, short_value, gap, dominant = values_for_timeframe(row, timeframe)
        active = state[symbol][timeframe]

        print(
            f"[{symbol} {timeframe}] LONG={fmt_money(long_value)} | "
            f"SHORT={fmt_money(short_value)} | GAP={fmt_money(gap)} | "
            f"DOM={dominant} | STATE={'ABOVE_5M' if active else 'IDLE'}",
            flush=True,
        )

        # One alert when gap reaches/crosses $5M.
        # No repeats at $6M/$7M/$8M...
        if not active and gap >= ENTER_GAP:
            send_gap_alert(symbol, timeframe, long_value, short_value, gap, dominant, True)
            state[symbol][timeframe] = True
            save_state()
            continue

        # After that, one reset alert only when gap crosses BELOW $4M.
        # Exact $4.00M does not reset; e.g. $3.99M does.
        # No repeats at $3M/$2M/$1M...
        if active and gap < EXIT_GAP:
            send_gap_alert(symbol, timeframe, long_value, short_value, gap, dominant, False)
            state[symbol][timeframe] = False
            save_state()


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
# BTC / ETH / SOL PARSER
# ============================================================

def find_value_header(lines):
    for i in range(len(lines)):
        block = " ".join(lines[i:i + 24]).lower()
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


def parse_symbol_row(search_lines, symbol):
    for i, item in enumerate(search_lines):
        if clean_text(item).upper() != symbol:
            continue

        numbers = []
        for candidate in search_lines[i + 1:i + 24]:
            if is_number_like(candidate):
                numbers.append(candidate)

        # price, 24h%, 1hL, 1hS, 4hL, 4hS, 12hL, 12hS...
        if len(numbers) >= 8:
            row = {
                "symbol": symbol,
                "long_1h": parse_number(numbers[2]),
                "short_1h": parse_number(numbers[3]),
                "long_4h": parse_number(numbers[4]),
                "short_4h": parse_number(numbers[5]),
                "long_12h": parse_number(numbers[6]),
                "short_12h": parse_number(numbers[7]),
            }
            print(
                f"[PARSED {symbol}] "
                f"1H L={numbers[2]} S={numbers[3]} | "
                f"4H L={numbers[4]} S={numbers[5]} | "
                f"12H L={numbers[6]} S={numbers[7]}",
                flush=True,
            )
            return row

    raise RuntimeError(f"{symbol} liquidation row not parsed")


def parse_rows(lines):
    header_index = find_value_header(lines)
    search_lines = lines[header_index + 1:header_index + 400]
    return {symbol: parse_symbol_row(search_lines, symbol) for symbol in SYMBOLS}


# ============================================================
# ONE SCAN
# ============================================================

async def scan_once(page):
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

    lines = await get_rendered_lines(page)
    rows = parse_rows(lines)

    for symbol in SYMBOLS:
        process_row(rows[symbol])

    print(f"[SCAN OK] {now_ist()}", flush=True)


# ============================================================
# MAIN
# ============================================================

async def main():
    print("COINGLASS BTC + ETH + SOL GAP OBSERVER STARTING", flush=True)
    load_state()
    print(f"URL: {URL}", flush=True)
    print(f"SCAN: every {SCAN_SECONDS} seconds", flush=True)
    print("COINS: BTC, ETH, SOL", flush=True)
    print("TIMEFRAMES: 1H, 4H, 12H - independent", flush=True)
    print("ALERT ONCE: gap >= $5M", flush=True)
    print("RESET ALERT ONCE: gap < $4M", flush=True)
    print("NO BUY/SELL DECISION", flush=True)
    print(f"PUSHOVER: {'READY' if pushover_ready() else 'NOT CONFIGURED'}", flush=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )

        context = await browser.new_context(
            viewport={"width": 1600, "height": 1200},
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
                    f"\n[SCAN FAILED] {now_ist()} | "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )

            elapsed = (datetime.now(IST) - cycle_started).total_seconds()
            sleep_for = max(5, SCAN_SECONDS - elapsed)
            print(f"[NEXT SCAN] approximately {int(sleep_for)} seconds", flush=True)
            await asyncio.sleep(sleep_for)


if __name__ == "__main__":
    asyncio.run(main())
