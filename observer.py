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

# New state file for 2-of-3 consensus logic
STATE_FILE = os.getenv(
    "COINGLASS_CONSENSUS_STATE_FILE",
    "/tmp/coinglass_consensus_state.json"
).strip()


# ============================================================
# STATE
# ============================================================

SYMBOLS = ("BTC", "ETH", "SOL")
TIMEFRAMES = ("1H", "4H", "12H")

# False = waiting for 2-of-3 >= $5M
# True  = high alert already sent; waiting for 2-of-3 < $4M
state = {
    timeframe: False
    for timeframe in TIMEFRAMES
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
        print(
            f"[STATE SAVE FAILED] {type(exc).__name__}: {exc}",
            flush=True,
        )


def load_state():
    global state

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)

        restored = {
            timeframe: bool(saved.get(timeframe, False))
            for timeframe in TIMEFRAMES
        }

        state = restored

        print(
            f"[STATE RESTORED] {state}",
            flush=True,
        )

    except FileNotFoundError:
        print(
            "[STATE] No saved consensus state; "
            "starting all timeframes waiting for >= $5M.",
            flush=True,
        )

    except Exception as exc:
        print(
            f"[STATE RESTORE FAILED] "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )


# ============================================================
# BASIC HELPERS
# ============================================================

def now_ist():
    return datetime.now(IST).strftime("%d-%m-%Y %H:%M:%S IST")


def clean_text(value):
    if value is None:
        return ""

    return " ".join(
        str(value)
        .replace("\xa0", " ")
        .replace("\u200b", "")
        .split()
    )


def parse_number(text):
    s = clean_text(text).upper()

    if not s:
        return 0.0

    s = (
        s.replace("$", "")
        .replace(",", "")
        .replace("−", "-")
    )

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

    return bool(
        re.fullmatch(
            r"[-+−]?\$?\d[\d,]*(?:\.\d+)?(?:[KMB])?%?",
            s,
            flags=re.I,
        )
    )


# ============================================================
# PUSHOVER
# ============================================================

def pushover_ready():
    return bool(
        PUSHOVER_USER_KEY
        and PUSHOVER_APP_TOKEN
    )


def send_pushover(title, message):
    if not pushover_ready():
        print(
            "[PUSHOVER] Not configured - notification skipped",
            flush=True,
        )
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
            print(
                f"[PUSHOVER SENT] {title}",
                flush=True,
            )
            return True

        print(
            f"[PUSHOVER FAILED] HTTP {response.status_code}",
            flush=True,
        )

    except Exception as exc:
        print(
            f"[PUSHOVER FAILED] {type(exc).__name__}: {exc}",
            flush=True,
        )

    return False


# ============================================================
# GAP / CONSENSUS MONITOR
# ============================================================

def values_for_timeframe(row, timeframe):
    tf = timeframe.lower()

    long_value = float(row[f"long_{tf}"])
    short_value = float(row[f"short_{tf}"])

    signed_gap = long_value - short_value
    gap = abs(signed_gap)

    dominant = (
        "LONG"
        if signed_gap > 0
        else "SHORT"
        if signed_gap < 0
        else "EVEN"
    )

    return long_value, short_value, gap, dominant


def get_snapshot(rows, timeframe):
    snapshot = {}

    for symbol in SYMBOLS:
        long_value, short_value, gap, dominant = values_for_timeframe(
            rows[symbol],
            timeframe,
        )

        snapshot[symbol] = {
            "long": long_value,
            "short": short_value,
            "gap": gap,
            "dominant": dominant,
        }

    return snapshot


def send_consensus_alert(
    timeframe,
    snapshot,
    qualified_symbols,
    high,
):
    count = len(qualified_symbols)

    if high:
        title = (
            f"{timeframe} | {count}/3 CONSENSUS | GAP >= $5M"
        )
        status = "2-OF-3 GAP CROSSED / REACHED $5M"

    else:
        title = (
            f"{timeframe} | {count}/3 CONSENSUS | GAP < $4M"
        )
        status = "2-OF-3 GAP CROSSED BELOW $4M"

    message_lines = [
        f"{timeframe} LIQUIDATION CONSENSUS",
        "",
        f"STRENGTH: {count}/3 COINS",
        "",
    ]

    for symbol in SYMBOLS:
        data = snapshot[symbol]

        if symbol in qualified_symbols:
            condition = "TRIGGER"
        else:
            condition = "-"

        message_lines.extend([
            f"{symbol}: {condition}",
            f"LONG : {fmt_money(data['long'])}",
            f"SHORT: {fmt_money(data['short'])}",
            f"GAP  : {fmt_money(data['gap'])}",
            (
                f"DOM  : {data['dominant']} LIQUIDATIONS"
                if data["dominant"] != "EVEN"
                else "DOM  : EVEN"
            ),
            "",
        ])

    message_lines.append(status)

    send_pushover(
        title,
        "\n".join(message_lines),
    )


def process_consensus(rows):

    for timeframe in TIMEFRAMES:

        snapshot = get_snapshot(
            rows,
            timeframe,
        )

        high_symbols = [
            symbol
            for symbol in SYMBOLS
            if snapshot[symbol]["gap"] >= ENTER_GAP
        ]

        low_symbols = [
            symbol
            for symbol in SYMBOLS
            if snapshot[symbol]["gap"] < EXIT_GAP
        ]

        active = state[timeframe]

        print(
            "\n"
            f"[{timeframe} CONSENSUS] "
            f"HIGH={len(high_symbols)}/3 {high_symbols} | "
            f"LOW={len(low_symbols)}/3 {low_symbols} | "
            f"STATE={'WAITING_FOR_<4M' if active else 'WAITING_FOR_>=5M'}",
            flush=True,
        )

        for symbol in SYMBOLS:
            data = snapshot[symbol]

            print(
                f"[{symbol} {timeframe}] "
                f"LONG={fmt_money(data['long'])} | "
                f"SHORT={fmt_money(data['short'])} | "
                f"GAP={fmt_money(data['gap'])} | "
                f"DOM={data['dominant']}",
                flush=True,
            )

        # ====================================================
        # STATE 1:
        # Waiting for minimum 2 coins >= $5M
        #
        # Once fired:
        # $6M / $7M / $8M etc. DO NOT alert again.
        # ====================================================

        if not active:

            if len(high_symbols) >= 2:

                print(
                    f"[{timeframe}] "
                    f"HIGH TRIGGER {len(high_symbols)}/3 >= $5M",
                    flush=True,
                )

                send_consensus_alert(
                    timeframe,
                    snapshot,
                    high_symbols,
                    True,
                )

                state[timeframe] = True
                save_state()

            else:
                print(
                    f"[{timeframe}] NO HIGH ALERT - "
                    f"only {len(high_symbols)}/3 coins >= $5M",
                    flush=True,
                )

            continue

        # ====================================================
        # STATE 2:
        # High alert already fired.
        #
        # Now ONLY waiting for minimum 2 coins < $4M.
        #
        # $6M / $7M / $8M = NO ALERT
        # ====================================================

        if active:

            if len(low_symbols) >= 2:

                print(
                    f"[{timeframe}] "
                    f"LOW TRIGGER {len(low_symbols)}/3 < $4M",
                    flush=True,
                )

                send_consensus_alert(
                    timeframe,
                    snapshot,
                    low_symbols,
                    False,
                )

                state[timeframe] = False
                save_state()

            else:
                print(
                    f"[{timeframe}] NO LOW ALERT - "
                    f"only {len(low_symbols)}/3 coins < $4M | "
                    "HIGH STATE REMAINS ACTIVE",
                    flush=True,
                )

            continue


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
        f"[DEBUG] Rendered text lines={len(lines)}",
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
        "[PAGE] Total Liquidations section visible",
        flush=True,
    )


# ============================================================
# BTC / ETH / SOL PARSER
# ============================================================

def find_value_header(lines):

    for i in range(len(lines)):

        block = " ".join(
            lines[i:i + 24]
        ).lower()

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

    raise RuntimeError(
        "VALUE liquidation header not found"
    )


def parse_symbol_row(
    search_lines,
    symbol,
):

    for i, item in enumerate(search_lines):

        if clean_text(item).upper() != symbol:
            continue

        numbers = []

        for candidate in search_lines[i + 1:i + 24]:

            if is_number_like(candidate):
                numbers.append(candidate)

        # price, 24h%,
        # 1hL, 1hS,
        # 4hL, 4hS,
        # 12hL, 12hS...
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

    raise RuntimeError(
        f"{symbol} liquidation row not parsed"
    )


def parse_rows(lines):

    header_index = find_value_header(lines)

    search_lines = lines[
        header_index + 1:
        header_index + 400
    ]

    return {
        symbol: parse_symbol_row(
            search_lines,
            symbol,
        )
        for symbol in SYMBOLS
    }


# ============================================================
# ONE SCAN
# ============================================================

async def scan_once(page):

    print(
        "\n"
        "############################################################\n"
        f"[SCAN START] {now_ist()}\n"
        "############################################################",
        flush=True,
    )

    response = await page.goto(
        URL,
        wait_until="domcontentloaded",
        timeout=60000,
    )

    status = (
        response.status
        if response
        else None
    )

    print(
        f"[HTTP] status={status}",
        flush=True,
    )

    print(
        f"[HTTP] final_url={page.url}",
        flush=True,
    )

    if (
        response is not None
        and response.status >= 400
    ):
        raise RuntimeError(
            f"CoinGlass HTTP {response.status}"
        )

    await page.wait_for_timeout(8000)

    print(
        f"[PAGE] title={await page.title()}",
        flush=True,
    )

    await wait_for_liquidation_section(page)

    lines = await get_rendered_lines(page)

    rows = parse_rows(lines)

    # All BTC + ETH + SOL are evaluated together
    # for each SAME timeframe.
    process_consensus(rows)

    print(
        f"[SCAN OK] {now_ist()}",
        flush=True,
    )


# ============================================================
# MAIN
# ============================================================

async def main():

    print(
        "COINGLASS BTC + ETH + SOL "
        "2-OF-3 GAP CONSENSUS OBSERVER STARTING",
        flush=True,
    )

    load_state()

    print(f"URL: {URL}", flush=True)
    print(f"SCAN: every {SCAN_SECONDS} seconds", flush=True)

    print(
        "COINS: BTC, ETH, SOL",
        flush=True,
    )

    print(
        "TIMEFRAMES: 1H, 4H, 12H - independent",
        flush=True,
    )

    print(
        "HIGH ALERT: minimum 2-of-3 coins GAP >= $5M",
        flush=True,
    )

    print(
        "AFTER HIGH: no alerts at $6M/$7M/$8M...",
        flush=True,
    )

    print(
        "LOW ALERT: after HIGH, minimum 2-of-3 coins GAP < $4M",
        flush=True,
    )

    print(
        "AFTER LOW: no alerts at $3M/$2M/$1M...",
        flush=True,
    )

    print(
        "CYCLE: >=$5M ALERT -> <$4M ALERT -> >=$5M ALERT...",
        flush=True,
    )

    print(
        "1 COIN ALONE: NEVER ALERT",
        flush=True,
    )

    print(
        "NO BUY/SELL DECISION",
        flush=True,
    )

    print(
        f"PUSHOVER: "
        f"{'READY' if pushover_ready() else 'NOT CONFIGURED'}",
        flush=True,
    )

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
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
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
                    f"{type(exc).__name__}: {exc}",
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

            await asyncio.sleep(sleep_for)


if __name__ == "__main__":
    asyncio.run(main())
