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
GAP_THRESHOLD = 5_000_000.0

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
    "COINGLASS_TOP10_2OF3_STATE_FILE",
    "/tmp/coinglass_top10_2of3_state.json"
).strip()

TIMEFRAMES = ("1H", "4H", "12H")

state = {}


# ============================================================
# STATE
# ============================================================

def default_symbol_state():
    return {
        "BUY": False,
        "SELL": False,
    }


def ensure_symbol_state(symbol):
    if symbol not in state:
        state[symbol] = default_symbol_state()


def save_state():
    tmp = f"{STATE_FILE}.tmp"

    try:
        directory = os.path.dirname(STATE_FILE)

        if directory:
            os.makedirs(
                directory,
                exist_ok=True
            )

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


def load_state():
    global state

    try:
        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            saved = json.load(f)

        restored = {}

        if isinstance(saved, dict):

            for symbol, data in saved.items():

                if not isinstance(data, dict):
                    continue

                restored[
                    str(symbol).upper()
                ] = {
                    "BUY": bool(
                        data.get("BUY", False)
                    ),
                    "SELL": bool(
                        data.get("SELL", False)
                    ),
                }

        state = restored

        print(
            f"[STATE RESTORED] {state}",
            flush=True
        )

    except FileNotFoundError:
        print(
            "[STATE] No saved state. "
            "Starting fresh.",
            flush=True
        )

    except Exception as exc:
        print(
            f"[STATE RESTORE FAILED] "
            f"{type(exc).__name__}: {exc}",
            flush=True
        )


# ============================================================
# BASIC HELPERS
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
    s = clean_text(text).upper()

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
    s = clean_text(text)

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
    s = clean_text(text).upper()

    if not s:
        return False

    # Rank numbers such as 1, 2, 3 must NEVER
    # be treated as coin symbols.
    if s.isdigit():
        return False

    # Normal crypto ticker format.
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


def send_pushover(title, message):

    if not pushover_ready():

        print(
            "[PUSHOVER] "
            "Not configured - notification skipped",
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
        "[PAGE] Total Liquidations section visible",
        flush=True
    )


# ============================================================
# FIND LIQUIDATION VALUE TABLE
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


# ============================================================
# PARSE ONE REAL COIN ROW
# ============================================================

def parse_coin_at(
    search_lines,
    symbol_index
):
    symbol = clean_text(
        search_lines[symbol_index]
    ).upper()

    if not looks_like_symbol(symbol):
        return None

    numbers = []

    # Collect the numeric fields immediately following
    # the actual symbol.
    #
    # Expected:
    # price
    # 24h %
    # 1H long
    # 1H short
    # 4H long
    # 4H short
    # 12H long
    # 12H short

    for candidate in search_lines[
        symbol_index + 1:
        symbol_index + 20
    ]:

        candidate_clean = clean_text(
            candidate
        )

        # IMPORTANT:
        # If another valid symbol appears before we
        # collected the complete row, this was not
        # a valid row start.
        if (
            looks_like_symbol(candidate_clean)
            and not is_number_like(candidate_clean)
        ):
            return None

        if is_number_like(candidate_clean):

            numbers.append(
                candidate_clean
            )

            if len(numbers) == 8:
                break

    if len(numbers) != 8:
        return None

    row = {
        "symbol": symbol,

        "long_1h":
            parse_number(numbers[2]),

        "short_1h":
            parse_number(numbers[3]),

        "long_4h":
            parse_number(numbers[4]),

        "short_4h":
            parse_number(numbers[5]),

        "long_12h":
            parse_number(numbers[6]),

        "short_12h":
            parse_number(numbers[7]),
    }

    total_liquidation = (
        row["long_1h"]
        + row["short_1h"]
        + row["long_4h"]
        + row["short_4h"]
        + row["long_12h"]
        + row["short_12h"]
    )

    if total_liquidation <= 0:
        return None

    print(
        f"[PARSED {symbol}] "
        f"1H L={numbers[2]} S={numbers[3]} | "
        f"4H L={numbers[4]} S={numbers[5]} | "
        f"12H L={numbers[6]} S={numbers[7]}",
        flush=True
    )

    return row


# ============================================================
# PARSE TOP 10 ACTUAL COINS
# ============================================================

def parse_top_rows(lines):

    header_index = find_value_header(
        lines
    )

    search_lines = lines[
        header_index + 1:
        header_index + 800
    ]

    rows = []
    seen = set()

    for i, item in enumerate(search_lines):

        symbol = clean_text(
            item
        ).upper()

        if not looks_like_symbol(symbol):
            continue

        if symbol in seen:
            continue

        row = parse_coin_at(
            search_lines,
            i
        )

        if row is None:
            continue

        rows.append(row)
        seen.add(symbol)

        if len(rows) == TOP_N:
            break

    if len(rows) < TOP_N:

        raise RuntimeError(
            f"Only {len(rows)} real coin rows parsed; "
            f"expected {TOP_N}"
        )

    print(
        "[TOP-10 COINS] "
        + ", ".join(
            row["symbol"]
            for row in rows
        ),
        flush=True
    )

    return rows


# ============================================================
# ANALYSE ONE COIN
# ============================================================

def analyse_coin(row):

    symbol = row["symbol"]

    buy_lines = []
    sell_lines = []

    print(
        f"\n[{symbol}] CHECKING 1H / 4H / 12H",
        flush=True
    )

    for timeframe in TIMEFRAMES:

        tf = timeframe.lower()

        long_value = float(
            row[f"long_{tf}"]
        )

        short_value = float(
            row[f"short_{tf}"]
        )

        buy_gap = (
            short_value
            - long_value
        )

        sell_gap = (
            long_value
            - short_value
        )

        # ====================================================
        # BUY
        # SHORT - LONG >= $5M
        # ====================================================

        if buy_gap >= GAP_THRESHOLD:

            buy_lines.append(
                f"{timeframe} | "
                f"LONG {fmt_money(long_value)} | "
                f"SHORT {fmt_money(short_value)} | "
                f"GAP {fmt_money(buy_gap)} SHORT ✓ BUY"
            )

            print(
                f"[{symbol} {timeframe}] "
                f"BUY QUALIFY | "
                f"GAP={fmt_money(buy_gap)}",
                flush=True
            )

        # ====================================================
        # SELL
        # LONG - SHORT >= $5M
        # ====================================================

        elif sell_gap >= GAP_THRESHOLD:

            sell_lines.append(
                f"{timeframe} | "
                f"LONG {fmt_money(long_value)} | "
                f"SHORT {fmt_money(short_value)} | "
                f"GAP {fmt_money(sell_gap)} LONG ✓ SELL"
            )

            print(
                f"[{symbol} {timeframe}] "
                f"SELL QUALIFY | "
                f"GAP={fmt_money(sell_gap)}",
                flush=True
            )

        else:

            print(
                f"[{symbol} {timeframe}] "
                f"NO QUALIFY | "
                f"LONG={fmt_money(long_value)} | "
                f"SHORT={fmt_money(short_value)}",
                flush=True
            )

    buy_active = (
        len(buy_lines) >= 2
    )

    sell_active = (
        len(sell_lines) >= 2
    )

    print(
        f"[{symbol} RESULT] "
        f"BUY={len(buy_lines)}/3 "
        f"{'ACTIVE' if buy_active else 'NO'} | "
        f"SELL={len(sell_lines)}/3 "
        f"{'ACTIVE' if sell_active else 'NO'}",
        flush=True
    )

    return {
        "symbol": symbol,
        "buy_active": buy_active,
        "sell_active": sell_active,
        "buy_lines": buy_lines,
        "sell_lines": sell_lines,
    }


# ============================================================
# FORMAT ONE COIN SECTION
# ============================================================

def coin_section(
    result,
    side
):
    symbol = result["symbol"]

    if side == "BUY":

        lines = [
            f"🟢 {symbol} BUY"
        ]

        lines.extend(
            result["buy_lines"]
        )

    else:

        lines = [
            f"🔴 {symbol} SELL"
        ]

        lines.extend(
            result["sell_lines"]
        )

    return "\n".join(lines)


# ============================================================
# BUILD ALERT
#
# FRESH COINS FIRST
# ALREADY ACTIVE COINS BELOW
# ============================================================

def build_alert_message(
    analyses,
    fresh_keys
):
    fresh_sections = []
    existing_sections = []

    for result in analyses:

        symbol = result["symbol"]

        if result["buy_active"]:

            key = (
                symbol,
                "BUY"
            )

            section = coin_section(
                result,
                "BUY"
            )

            if key in fresh_keys:
                fresh_sections.append(section)
            else:
                existing_sections.append(section)

        if result["sell_active"]:

            key = (
                symbol,
                "SELL"
            )

            section = coin_section(
                result,
                "SELL"
            )

            if key in fresh_keys:
                fresh_sections.append(section)
            else:
                existing_sections.append(section)

    all_sections = (
        fresh_sections
        + existing_sections
    )

    return "\n\n".join(
        all_sections
    )


# ============================================================
# PROCESS TOP 10
# ============================================================

def process_top10(rows):

    analyses = [
        analyse_coin(row)
        for row in rows
    ]

    current_symbols = {
        result["symbol"]
        for result in analyses
    }

    fresh_keys = set()
    fresh_names = []

    state_changed = False

    # ========================================================
    # IMPORTANT:
    #
    # Coin is no longer inside current Top-10:
    # remove old state.
    #
    # If it later returns to Top-10 already qualifying,
    # it can trigger fresh again.
    # ========================================================

    for old_symbol in list(state.keys()):

        if old_symbol not in current_symbols:

            print(
                f"[STATE REMOVE] "
                f"{old_symbol} left current Top-10",
                flush=True
            )

            del state[old_symbol]
            state_changed = True

    # ========================================================
    # CHECK EACH CURRENT TOP-10 COIN
    # ========================================================

    for result in analyses:

        symbol = result["symbol"]

        ensure_symbol_state(
            symbol
        )

        buy_now = result[
            "buy_active"
        ]

        sell_now = result[
            "sell_active"
        ]

        buy_before = state[
            symbol
        ][
            "BUY"
        ]

        sell_before = state[
            symbol
        ][
            "SELL"
        ]

        # ====================================================
        # FRESH BUY
        # ====================================================

        if (
            buy_now
            and not buy_before
        ):

            fresh_keys.add(
                (
                    symbol,
                    "BUY"
                )
            )

            fresh_names.append(
                f"{symbol} BUY"
            )

            print(
                f"[FRESH TRIGGER] "
                f"{symbol} BUY",
                flush=True
            )

        # ====================================================
        # FRESH SELL
        # ====================================================

        if (
            sell_now
            and not sell_before
        ):

            fresh_keys.add(
                (
                    symbol,
                    "SELL"
                )
            )

            fresh_names.append(
                f"{symbol} SELL"
            )

            print(
                f"[FRESH TRIGGER] "
                f"{symbol} SELL",
                flush=True
            )

        # ====================================================
        # SILENT BUY RE-ARM
        # ====================================================

        if (
            buy_before
            and not buy_now
        ):

            print(
                f"[RE-ARM] "
                f"{symbol} BUY "
                f"2/3 condition broke",
                flush=True
            )

        # ====================================================
        # SILENT SELL RE-ARM
        # ====================================================

        if (
            sell_before
            and not sell_now
        ):

            print(
                f"[RE-ARM] "
                f"{symbol} SELL "
                f"2/3 condition broke",
                flush=True
            )

        if buy_before != buy_now:

            state[
                symbol
            ][
                "BUY"
            ] = buy_now

            state_changed = True

        if sell_before != sell_now:

            state[
                symbol
            ][
                "SELL"
            ] = sell_now

            state_changed = True

    # Save new state
    if state_changed:
        save_state()

    # ========================================================
    # NO FRESH $5M 2/3 TRIGGER
    # ========================================================

    if not fresh_keys:

        print(
            "\n[ALERT] "
            "No fresh 2-of-3 $5M trigger. "
            "No Pushover.",
            flush=True
        )

        return

    # ========================================================
    # FRESH COIN(S) AT TOP
    #
    # ALL OTHER CURRENTLY QUALIFYING COINS BELOW
    # ========================================================

    message = build_alert_message(
        analyses,
        fresh_keys
    )

    if not message:

        print(
            "[ALERT ERROR] "
            "Fresh trigger exists but "
            "snapshot is empty.",
            flush=True
        )

        return

    title = (
        "TOP-10 LIQUIDATION ALERT"
    )

    print(
        "\n"
        "============================================================\n"
        "TOP-10 LIQUIDATION ALERT\n"
        "============================================================",
        flush=True
    )

    print(
        f"FRESH: "
        f"{', '.join(fresh_names)}",
        flush=True
    )

    print(
        "\n"
        + message
        + "\n",
        flush=True
    )

    print(
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
        f"[PAGE] "
        f"title={await page.title()}",
        flush=True
    )

    await wait_for_liquidation_section(
        page
    )

    lines = await get_rendered_lines(
        page
    )

    rows = parse_top_rows(
        lines
    )

    process_top10(
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
        "COINGLASS TOP-10 "
        "2-OF-3 $5M GAP OBSERVER STARTING",
        flush=True
    )

    load_state()

    print(
        f"URL: {URL}",
        flush=True
    )

    print(
        f"SCAN: every "
        f"{SCAN_SECONDS} seconds",
        flush=True
    )

    print(
        "COINS: CoinGlass Top 10 actual coin rows",
        flush=True
    )

    print(
        "TIMEFRAMES: 1H / 4H / 12H",
        flush=True
    )

    print(
        "BUY TF: SHORT - LONG >= $5M",
        flush=True
    )

    print(
        "SELL TF: LONG - SHORT >= $5M",
        flush=True
    )

    print(
        "TRIGGER: minimum 2/3 same-side TFs",
        flush=True
    )

    print(
        "FRESH TRIGGER COIN: shown first",
        flush=True
    )

    print(
        "OTHER ACTIVE COINS: shown below",
        flush=True
    )

    print(
        "REPEAT ALERT: NO while condition stays active",
        flush=True
    )

    print(
        "RE-ARM: silent when 2/3 condition breaks",
        flush=True
    )

    print(
        "NO 5-MINUTE LOGIC",
        flush=True
    )

    print(
        "NO RANKING LOGIC",
        flush=True
    )

    print(
        "NO $4M RESET LOGIC",
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
