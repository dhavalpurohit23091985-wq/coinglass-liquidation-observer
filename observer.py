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
FIXED_SYMBOLS = ("BTC", "SOL", "ETH", "ZEC", "NEAR", "XRP", "HYPE")
GAP_THRESHOLD = 1_000_000.0

IST = ZoneInfo("Asia/Kolkata")

PUSHOVER_USER_KEY = os.getenv(
    "PUSHOVER_USER_KEY",
    ""
).strip()

PUSHOVER_APP_TOKEN = os.getenv(
    "PUSHOVER_APP_TOKEN",
    ""
).strip()

STATE_FILE = os.getenv(
    "COINGLASS_24H_1M_STATE_FILE",
    "/tmp/coinglass_24h_1m_state.json"
).strip()


# ============================================================
# STATE
#
# Per coin:
#
# NONE = no previous alert
# BUY  = last alert BUY
# SELL = last alert SELL
#
# BUY -> BUY repeat blocked
# SELL -> SELL repeat blocked
#
# BUY -> SELL allowed
# SELL -> BUY allowed
#
# IMPORTANT:
# A coin temporarily disappearing from one CoinGlass scan
# DOES NOT delete/reset its remembered state.
# ============================================================

state = {}


def load_state():
    global state

    try:
        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            saved = json.load(f)

        if isinstance(saved, dict):
            state = {
                str(symbol).upper(): str(side).upper()
                for symbol, side in saved.items()
                if str(side).upper() in (
                    "NONE",
                    "BUY",
                    "SELL"
                )
            }

        print(
            f"[STATE RESTORED] {state}",
            flush=True
        )

    except FileNotFoundError:
        state = {}

        print(
            "[STATE] No saved state. Starting fresh.",
            flush=True
        )

    except Exception as exc:
        state = {}

        print(
            f"[STATE RESTORE FAILED] "
            f"{type(exc).__name__}: {exc}",
            flush=True
        )


def save_state():
    try:
        directory = os.path.dirname(
            STATE_FILE
        )

        if directory:
            os.makedirs(
                directory,
                exist_ok=True
            )

        tmp = STATE_FILE + ".tmp"

        with open(
            tmp,
            "w",
            encoding="utf-8"
        ) as f:
            json.dump(
                state,
                f,
                indent=2
            )

        os.replace(
            tmp,
            STATE_FILE
        )

    except Exception as exc:
        print(
            f"[STATE SAVE FAILED] "
            f"{type(exc).__name__}: {exc}",
            flush=True
        )


# ============================================================
# HELPERS
# ============================================================

def now_ist():
    return datetime.now(
        IST
    ).strftime(
        "%d-%m-%Y %H:%M:%S IST"
    )


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
    s = clean_text(
        text
    ).upper()

    if not s:
        return 0.0

    s = (
        s.replace("$", "")
        .replace(",", "")
        .replace("−", "-")
    )

    match = re.search(
        r"-?\d+(?:\.\d+)?",
        s
    )

    if not match:
        return 0.0

    value = float(
        match.group()
    )

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
        return (
            f"${value / 1_000_000_000:.2f}B"
        )

    if abs(value) >= 1_000_000:
        return (
            f"${value / 1_000_000:.2f}M"
        )

    if abs(value) >= 1_000:
        return (
            f"${value / 1_000:.2f}K"
        )

    return f"${value:.2f}"


def is_number_like(text):
    s = clean_text(
        text
    )

    return bool(
        re.fullmatch(
            r"[-+−]?\$?\d[\d,]*(?:\.\d+)?(?:[KMB])?%?",
            s,
            flags=re.I
        )
    )


# ============================================================
# SYMBOL VALIDATION
# ============================================================

def looks_like_symbol(text):
    s = clean_text(
        text
    ).upper()

    if not s:
        return False

    # Rank numbers must never become symbols.
    if s.isdigit():
        return False

    if not re.fullmatch(
        r"[A-Z][A-Z0-9]{1,11}",
        s
    ):
        return False

    blocked = {
        "RANKING",
        "RANK",
        "ASSETS",
        "ASSET",
        "PRICE",
        "TOTAL",
        "LONG",
        "SHORT",
        "LONGS",
        "SHORTS",
        "LIQUIDATION",
        "LIQUIDATIONS",
        "TOTALS",
        "VALUE",
        "VALUES",
        "CHANGE",
        "VOLUME",
        "COIN",
        "COINS",
        "SYMBOL",
        "SYMBOLS",
        "MARKET",
        "MARKETS",
        "EXCHANGE",
        "EXCHANGES",
        "TRADE",
        "RATE",
        "1H",
        "4H",
        "12H",
        "24H",
    }

    return s not in blocked


# ============================================================
# PUSHOVER
# ============================================================

def pushover_ready():
    return bool(
        PUSHOVER_USER_KEY
        and PUSHOVER_APP_TOKEN
    )


def send_pushover(
    title,
    message
):
    if not pushover_ready():
        print(
            "[PUSHOVER] Not configured - skipped",
            flush=True
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
                flush=True
            )
            return True

        print(
            f"[PUSHOVER FAILED] "
            f"HTTP {response.status_code} | "
            f"{response.text[:300]}",
            flush=True
        )

    except Exception as exc:
        print(
            f"[PUSHOVER FAILED] "
            f"{type(exc).__name__}: {exc}",
            flush=True
        )

    return False


# ============================================================
# PAGE
# ============================================================

async def get_rendered_lines(page):
    body_text = await page.locator(
        "body"
    ).inner_text(
        timeout=15000
    )

    lines = []

    for line in body_text.splitlines():
        line = clean_text(
            line
        )

        if line:
            lines.append(
                line
            )

    print(
        f"[DEBUG] Rendered text lines={len(lines)}",
        flush=True
    )

    return lines


async def wait_for_liquidation_section(page):
    await page.get_by_text(
        "Total Liquidations",
        exact=False
    ).first.wait_for(
        state="visible",
        timeout=30000
    )

    print(
        "[PAGE] Total Liquidations visible",
        flush=True
    )


# ============================================================
# FIND COIN TABLE
#
# Required columns:
#
# Assets
# Price
# Price (24h%)
# 1h Long
# 1h Short
# 4h Long
# 4h Short
# 12h Long
# 12h Short
# 24h Long
# 24h Short
# ============================================================

def find_value_header(lines):
    for i in range(
        len(lines)
    ):
        block = " ".join(
            lines[i:i + 35]
        ).lower()

        if (
            "assets" in block
            and "1h long" in block
            and "1h short" in block
            and "4h long" in block
            and "4h short" in block
            and "12h long" in block
            and "12h short" in block
            and "24h long" in block
            and "24h short" in block
        ):
            return i

    raise RuntimeError(
        "24H liquidation table header not found"
    )


# ============================================================
# PARSE ONE COIN
#
# Expected numeric fields after symbol:
#
# 0 price
# 1 price 24h %
# 2 1h long
# 3 1h short
# 4 4h long
# 5 4h short
# 6 12h long
# 7 12h short
# 8 24h long
# 9 24h short
# ============================================================

def parse_coin_at(
    search_lines,
    symbol_index
):
    symbol = clean_text(
        search_lines[
            symbol_index
        ]
    ).upper()

    if not looks_like_symbol(
        symbol
    ):
        return None

    numbers = []

    for candidate in search_lines[
        symbol_index + 1:
        symbol_index + 24
    ]:
        candidate_clean = clean_text(
            candidate
        )

        # Another symbol before complete row
        # means this was not a valid row start.
        if (
            looks_like_symbol(
                candidate_clean
            )
            and not is_number_like(
                candidate_clean
            )
        ):
            return None

        if is_number_like(
            candidate_clean
        ):
            numbers.append(
                candidate_clean
            )

            if len(numbers) == 10:
                break

    if len(numbers) != 10:
        return None

    long_24h = parse_number(
        numbers[8]
    )

    short_24h = parse_number(
        numbers[9]
    )

    if (
        long_24h <= 0
        and short_24h <= 0
    ):
        return None

    row = {
        "symbol": symbol,
        "long_24h": long_24h,
        "short_24h": short_24h,
    }

    print(
        f"[PARSED {symbol}] "
        f"24H LONG={fmt_money(long_24h)} | "
        f"SHORT={fmt_money(short_24h)}",
        flush=True
    )

    return row


# ============================================================
# PARSE TABLE
#
# Current Top-10 is retained for notification snapshot.
#
# Every valid parsed coin is monitored.
# `seen` prevents duplicate rows.
# ============================================================

def parse_monitor_rows(lines):
    header_index = find_value_header(
        lines
    )

    search_lines = lines[
        header_index + 1:
        header_index + 1200
    ]

    all_rows = []
    seen = set()

    for i, item in enumerate(
        search_lines
    ):
        symbol = clean_text(
            item
        ).upper()

        if not looks_like_symbol(
            symbol
        ):
            continue

        if symbol in seen:
            continue

        row = parse_coin_at(
            search_lines,
            i
        )

        if row is None:
            continue

        all_rows.append(
            row
        )

        seen.add(
            symbol
        )

    if len(all_rows) < TOP_N:
        raise RuntimeError(
            f"Only {len(all_rows)} real coin rows parsed; "
            f"need at least {TOP_N}"
        )

    # Keep current Top-10 visible in logs.
    top10 = all_rows[:TOP_N]

    # Monitor EVERY valid parsed coin from CoinGlass.
    monitor_rows = list(all_rows)

    print(
        "[TOP-10] "
        + ", ".join(
            row["symbol"]
            for row in top10
        ),
        flush=True
    )

    print(
        "[FIXED] "
        + ", ".join(FIXED_SYMBOLS),
        flush=True
    )

    print(
        f"[ALL COINS MONITORED] {len(monitor_rows)}",
        flush=True
    )

    print(
        "[MONITORING] "
        + ", ".join(
            row["symbol"]
            for row in monitor_rows
        ),
        flush=True
    )

    return monitor_rows


# ============================================================
# SIGNAL
#
# SHORT - LONG >= $1M = BUY
#
# LONG - SHORT >= $1M = SELL
#
# Otherwise NONE.
# ============================================================

def get_signal(row):
    long_value = float(
        row["long_24h"]
    )

    short_value = float(
        row["short_24h"]
    )

    difference = (
        short_value
        - long_value
    )

    if difference >= GAP_THRESHOLD:
        return (
            "BUY",
            abs(difference)
        )

    if difference <= -GAP_THRESHOLD:
        return (
            "SELL",
            abs(difference)
        )

    return (
        "NONE",
        abs(difference)
    )


# ============================================================
# PROCESS
#
# LOCKED RULE:
#
# BTC/CL/etc:
#
# BUY -> BUY repeat = NO ALERT
# SELL -> SELL repeat = NO ALERT
#
# BUY -> SELL = FRESH ALERT
# SELL -> BUY = FRESH ALERT
#
# Falling below $1M does NOT re-arm same side.
#
# Temporary absence from one parsed scan does NOT
# delete/reset the remembered state.
#
# Only an actual opposite-side trigger changes state.
# ============================================================

def process_rows(rows):
    changed = False
    fresh_alerts = []

    # IMPORTANT FIX:
    #
    # OLD CODE REMOVED remembered states whenever a symbol was
    # absent from the current parsed scan.
    #
    # That could cause:
    #
    # CL BUY
    # -> CL temporarily missing
    # -> remembered BUY deleted
    # -> CL returns BUY
    # -> incorrectly treated as FRESH BUY again
    #
    # We DO NOT remove missing symbols anymore.

    for row in rows:
        symbol = row[
            "symbol"
        ]

        long_value = float(
            row["long_24h"]
        )

        short_value = float(
            row["short_24h"]
        )

        signal, gap = get_signal(
            row
        )

        previous = state.get(
            symbol,
            "NONE"
        )

        print(
            f"[{symbol}] "
            f"24H LONG={fmt_money(long_value)} | "
            f"SHORT={fmt_money(short_value)} | "
            f"GAP={fmt_money(gap)} | "
            f"NOW={signal} | "
            f"LAST={previous}",
            flush=True
        )

        # Below $1M:
        # Do nothing.
        # Previous BUY/SELL remains remembered.
        if signal == "NONE":
            continue

        # Same direction:
        # NEVER send another alert.
        if signal == previous:
            print(
                f"[{symbol}] "
                f"SAME {signal} SIDE - NO REPEAT",
                flush=True
            )
            continue

        # Fresh initial signal OR genuine opposite-side signal.
        state[
            symbol
        ] = signal

        changed = True

        if signal == "BUY":
            side_text = (
                "SHORT - LONG"
            )
            emoji = "🟢"

        else:
            side_text = (
                "LONG - SHORT"
            )
            emoji = "🔴"

        fresh_alerts.append(
            {
                "symbol": symbol,
                "signal": signal,
                "emoji": emoji,
                "long": long_value,
                "short": short_value,
                "gap": gap,
                "side_text": side_text,
                "previous": previous,
            }
        )

        print(
            f"[FRESH {signal}] "
            f"{symbol} | "
            f"{side_text}={fmt_money(gap)}",
            flush=True
        )

    if changed:
        save_state()

    if not fresh_alerts:
        print(
            "[ALERT] No fresh $1M 24H trigger.",
            flush=True
        )
        return

    # ========================================================
    # COMPACT ALERT
    # Fresh trigger(s) first
    # Then current Top-10 snapshot
    # ========================================================

    def compact_money(value):
        value = float(value)

        if abs(value) >= 1_000_000_000:
            return f"{value / 1_000_000_000:.2f}B"

        if abs(value) >= 1_000_000:
            return f"{value / 1_000_000:.2f}M"

        if abs(value) >= 1_000:
            return f"{value / 1_000:.2f}K"

        return f"{value:.0f}"

    message_lines = []

    # Fresh trigger(s) at TOP.
    for item in fresh_alerts:
        previous = item["previous"]

        transition = (
            f"{previous}->{item['signal']}"
            if previous in (
                "BUY",
                "SELL"
            )
            else item["signal"]
        )

        message_lines.append(
            f"🔥 {item['symbol']} FRESH "
            f"{item['signal']} | {transition}"
        )

    # Current CoinGlass Top-10 snapshot.
    top10_rows = list(
        rows[:TOP_N]
    )

    total_long = 0.0
    total_short = 0.0

    buy_count = 0
    sell_count = 0

    for row in top10_rows:
        symbol = row[
            "symbol"
        ]

        long_value = float(
            row["long_24h"]
        )

        short_value = float(
            row["short_24h"]
        )

        signal, _ = get_signal(
            row
        )

        total_long += long_value
        total_short += short_value

        if signal == "BUY":
            buy_count += 1

        elif signal == "SELL":
            sell_count += 1

        signed_gap = (
            short_value
            - long_value
        )

        if abs(signed_gap) >= 1_000_000:
            gap_text = (
                f"{signed_gap / 1_000_000:+.2f}M"
            )

        elif abs(signed_gap) >= 1_000:
            gap_text = (
                f"{signed_gap / 1_000:+.0f}K"
            )

        else:
            gap_text = (
                f"{signed_gap:+.0f}"
            )

        message_lines.append(
            f"{symbol} "
            f"L{compact_money(long_value)} "
            f"S{compact_money(short_value)} "
            f"| {gap_text} | {signal}"
        )

    # ========================================================
    # TOTAL
    # ========================================================

    total_difference = (
        total_short
        - total_long
    )

    if total_difference > 0:
        gap_side = "SHORT"

    elif total_difference < 0:
        gap_side = "LONG"

    else:
        gap_side = "EVEN"

    message_lines.append(
        f"TOTAL L{compact_money(total_long)} | "
        f"S{compact_money(total_short)} | "
        f"GAP {compact_money(abs(total_difference))} "
        f"{gap_side}"
    )

    # ========================================================
    # BUY / SELL COUNT
    # ========================================================

    if buy_count > sell_count:
        winner = "BUY"

    elif sell_count > buy_count:
        winner = "SELL"

    else:
        winner = "TIE"

    message_lines.append(
        f"{buy_count} BUY | "
        f"{sell_count} SELL | "
        f"WINNER {winner}"
    )

    message = "\n".join(
        message_lines
    )

    title = (
        "24H $1M LIQUIDATION ALERT"
    )

    print(
        "\n"
        "============================================================\n"
        f"{title}\n"
        "============================================================\n"
        f"{message}\n"
        "============================================================",
        flush=True
    )

    send_pushover(
        title,
        message
    )


# ============================================================
# ONE SCAN
# ============================================================

async def scan_once(page):
    print(
        "\n"
        "############################################################\n"
        f"[SCAN START] {now_ist()}\n"
        "############################################################",
        flush=True
    )

    response = await page.goto(
        URL,
        wait_until="domcontentloaded",
        timeout=60000
    )

    status = (
        response.status
        if response
        else None
    )

    print(
        f"[HTTP] status={status}",
        flush=True
    )

    print(
        f"[HTTP] final_url={page.url}",
        flush=True
    )

    if (
        response is not None
        and response.status >= 400
    ):
        raise RuntimeError(
            f"CoinGlass HTTP "
            f"{response.status}"
        )

    await page.wait_for_timeout(
        8000
    )

    print(
        f"[PAGE] title={await page.title()}",
        flush=True
    )

    await wait_for_liquidation_section(
        page
    )

    lines = await get_rendered_lines(
        page
    )

    rows = parse_monitor_rows(
        lines
    )

    process_rows(
        rows
    )

    print(
        f"[SCAN OK] {now_ist()}",
        flush=True
    )


# ============================================================
# MAIN
# ============================================================

async def main():
    print(
        "\n"
        "============================================================\n"
        "COINGLASS ALL-COINS 24H $1M OBSERVER\n"
        "============================================================",
        flush=True
    )

    load_state()

    print(
        f"URL: {URL}",
        flush=True
    )

    print(
        f"SCAN: every {SCAN_SECONDS} seconds",
        flush=True
    )

    print(
        "DATA: 24H LONG / 24H SHORT ONLY",
        flush=True
    )

    print(
        "BUY: SHORT - LONG >= $1M",
        flush=True
    )

    print(
        "SELL: LONG - SHORT >= $1M",
        flush=True
    )

    print(
        "SAME-SIDE REPEAT: NO",
        flush=True
    )

    print(
        "OPPOSITE SIDE: ALERT + STATE FLIP",
        flush=True
    )

    print(
        "DROP BELOW $1M: NO ALERT / STATE KEPT",
        flush=True
    )

    print(
        "TEMPORARILY MISSING COIN: STATE KEPT",
        flush=True
    )

    print(
        "1H / 4H / 12H LOGIC: REMOVED",
        flush=True
    )

    print(
        "2-OF-3 LOGIC: REMOVED",
        flush=True
    )

    print(
        f"PUSHOVER: "
        f"{'READY' if pushover_ready() else 'NOT CONFIGURED'}",
        flush=True
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ]
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
            )
        )

        page = await context.new_page()

        while True:
            cycle_started = datetime.now(
                IST
            )

            try:
                await scan_once(
                    page
                )

            except Exception as exc:
                print(
                    f"\n[SCAN FAILED] "
                    f"{now_ist()} | "
                    f"{type(exc).__name__}: "
                    f"{exc}",
                    flush=True
                )

            elapsed = (
                datetime.now(IST)
                - cycle_started
            ).total_seconds()

            sleep_for = max(
                5,
                SCAN_SECONDS - elapsed
            )

            print(
                f"[NEXT SCAN] approximately "
                f"{int(sleep_for)} seconds",
                flush=True
            )

            await asyncio.sleep(
                sleep_for
            )


if __name__ == "__main__":
    asyncio.run(
        main()
    )
