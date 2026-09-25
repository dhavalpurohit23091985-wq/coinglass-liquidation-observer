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

# Completely new state file.
# Old consensus state is NOT used.
STATE_FILE = os.getenv(
    "COINGLASS_TOP10_2OF3_STATE_FILE",
    "/tmp/coinglass_top10_2of3_state.json"
).strip()

TIMEFRAMES = ("1H", "4H", "12H")


# ============================================================
# STATE
# ============================================================

# State is maintained separately for every coin and side.
#
# Example:
#
# {
#     "BTC": {
#         "BUY": True,
#         "SELL": False
#     },
#     "ETH": {
#         "BUY": False,
#         "SELL": False
#     }
# }
#
# True means:
# that coin/side has already fired its 2-of-3 alert.
#
# It remains True while the 2-of-3 condition remains active.
#
# As soon as that 2-of-3 condition breaks,
# it silently becomes False again.
#
# There is NO reset notification.

state = {}


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
        directory = os.path.dirname(
            STATE_FILE
        )

        if directory:
            os.makedirs(
                directory,
                exist_ok=True,
            )

        with open(
            tmp,
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                state,
                f,
                indent=2,
            )

        os.replace(
            tmp,
            STATE_FILE,
        )

    except Exception as exc:
        print(
            f"[STATE SAVE FAILED] "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )


def load_state():
    global state

    try:
        with open(
            STATE_FILE,
            "r",
            encoding="utf-8",
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
                        data.get(
                            "BUY",
                            False,
                        )
                    ),
                    "SELL": bool(
                        data.get(
                            "SELL",
                            False,
                        )
                    ),
                }

        state = restored

        print(
            f"[STATE RESTORED] {state}",
            flush=True,
        )

    except FileNotFoundError:
        print(
            "[STATE] No saved Top-10 state. "
            "Starting fresh.",
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
        s,
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
            flags=re.I,
        )
    )


def looks_like_symbol(text):
    s = clean_text(
        text
    ).upper()

    if not s:
        return False

    # Coin symbols are short.
    if len(s) > 15:
        return False

    if not re.fullmatch(
        r"[A-Z0-9]+",
        s,
    ):
        return False

    blocked = {
        "ASSETS",
        "ASSET",
        "PRICE",
        "TOTAL",
        "LONG",
        "SHORT",
        "1H",
        "4H",
        "12H",
        "24H",
        "LIQUIDATION",
        "LIQUIDATIONS",
    }

    if s in blocked:
        return False

    return True


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
    message,
):
    if not pushover_ready():
        print(
            "[PUSHOVER] "
            "Not configured - notification skipped",
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
            f"[PUSHOVER FAILED] "
            f"HTTP {response.status_code} | "
            f"{response.text[:300]}",
            flush=True,
        )

    except Exception as exc:
        print(
            f"[PUSHOVER FAILED] "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )

    return False


# ============================================================
# PAGE HELPERS
# ============================================================

async def get_rendered_lines(
    page,
):
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
        f"[DEBUG] Rendered text lines="
        f"{len(lines)}",
        flush=True,
    )

    return lines


async def wait_for_liquidation_section(
    page,
):
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
# VALUE TABLE HEADER
# ============================================================

def find_value_header(
    lines,
):
    for i in range(
        len(lines)
    ):

        block = " ".join(
            lines[
                i:i + 24
            ]
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
# DYNAMIC TOP-10 PARSER
# ============================================================

def try_parse_symbol_row(
    search_lines,
    symbol_index,
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

    # Existing working CoinGlass table structure:
    #
    # PRICE
    # 24H %
    # 1H LONG
    # 1H SHORT
    # 4H LONG
    # 4H SHORT
    # 12H LONG
    # 12H SHORT

    for candidate in search_lines[
        symbol_index + 1:
        symbol_index + 24
    ]:

        if is_number_like(
            candidate
        ):
            numbers.append(
                candidate
            )

        if len(numbers) >= 8:
            break

    if len(numbers) < 8:
        return None

    row = {
        "symbol": symbol,

        "long_1h":
            parse_number(
                numbers[2]
            ),

        "short_1h":
            parse_number(
                numbers[3]
            ),

        "long_4h":
            parse_number(
                numbers[4]
            ),

        "short_4h":
            parse_number(
                numbers[5]
            ),

        "long_12h":
            parse_number(
                numbers[6]
            ),

        "short_12h":
            parse_number(
                numbers[7]
            ),
    }

    print(
        f"[PARSED {symbol}] "
        f"1H L={numbers[2]} "
        f"S={numbers[3]} | "
        f"4H L={numbers[4]} "
        f"S={numbers[5]} | "
        f"12H L={numbers[6]} "
        f"S={numbers[7]}",
        flush=True,
    )

    return row


def parse_top_rows(
    lines,
):
    header_index = find_value_header(
        lines
    )

    # Large enough window for Top-10.
    search_lines = lines[
        header_index + 1:
        header_index + 800
    ]

    rows = []
    seen_symbols = set()

    for i, item in enumerate(
        search_lines
    ):

        symbol = clean_text(
            item
        ).upper()

        if symbol in seen_symbols:
            continue

        if not looks_like_symbol(
            symbol
        ):
            continue

        row = try_parse_symbol_row(
            search_lines,
            i,
        )

        if row is None:
            continue

        # Additional sanity:
        # A real row should contain at least some liquidation value.
        liquidation_total = (
            row["long_1h"]
            + row["short_1h"]
            + row["long_4h"]
            + row["short_4h"]
            + row["long_12h"]
            + row["short_12h"]
        )

        if liquidation_total <= 0:
            continue

        rows.append(
            row
        )

        seen_symbols.add(
            symbol
        )

        if len(rows) >= TOP_N:
            break

    if len(rows) < TOP_N:
        raise RuntimeError(
            f"Only {len(rows)} Top rows parsed; "
            f"expected {TOP_N}"
        )

    print(
        "[TOP-10] "
        + ", ".join(
            row["symbol"]
            for row in rows
        ),
        flush=True,
    )

    return rows


# ============================================================
# TIMEFRAME VALUES
# ============================================================

def values_for_timeframe(
    row,
    timeframe,
):
    tf = timeframe.lower()

    long_value = float(
        row[
            f"long_{tf}"
        ]
    )

    short_value = float(
        row[
            f"short_{tf}"
        ]
    )

    return (
        long_value,
        short_value,
    )


# ============================================================
# COIN ANALYSIS
# ============================================================

def analyse_coin(
    row,
):
    symbol = row[
        "symbol"
    ]

    buy_lines = []
    sell_lines = []

    print(
        f"\n[{symbol}] CHECKING 1H / 4H / 12H",
        flush=True,
    )

    for timeframe in TIMEFRAMES:

        (
            long_value,
            short_value,
        ) = values_for_timeframe(
            row,
            timeframe,
        )

        # BUY-side gap:
        #
        # SHORT liquidation - LONG liquidation
        #
        # Qualifies only when >= $5M.

        buy_gap = (
            short_value
            - long_value
        )

        # SELL-side gap:
        #
        # LONG liquidation - SHORT liquidation
        #
        # Qualifies only when >= $5M.

        sell_gap = (
            long_value
            - short_value
        )

        if (
            buy_gap
            >= GAP_THRESHOLD
        ):

            buy_lines.append(
                f"{timeframe} | "
                f"LONG {fmt_money(long_value)} | "
                f"SHORT {fmt_money(short_value)} | "
                f"GAP {fmt_money(buy_gap)} SHORT "
                f"✓ BUY"
            )

            print(
                f"[{symbol} {timeframe}] "
                f"BUY QUALIFY | "
                f"LONG={fmt_money(long_value)} | "
                f"SHORT={fmt_money(short_value)} | "
                f"SHORT GAP={fmt_money(buy_gap)}",
                flush=True,
            )

        elif (
            sell_gap
            >= GAP_THRESHOLD
        ):

            sell_lines.append(
                f"{timeframe} | "
                f"LONG {fmt_money(long_value)} | "
                f"SHORT {fmt_money(short_value)} | "
                f"GAP {fmt_money(sell_gap)} LONG "
                f"✓ SELL"
            )

            print(
                f"[{symbol} {timeframe}] "
                f"SELL QUALIFY | "
                f"LONG={fmt_money(long_value)} | "
                f"SHORT={fmt_money(short_value)} | "
                f"LONG GAP={fmt_money(sell_gap)}",
                flush=True,
            )

        else:

            print(
                f"[{symbol} {timeframe}] "
                f"NO QUALIFY | "
                f"LONG={fmt_money(long_value)} | "
                f"SHORT={fmt_money(short_value)} | "
                f"BUY_GAP={fmt_money(buy_gap)} | "
                f"SELL_GAP={fmt_money(sell_gap)}",
                flush=True,
            )

    # Minimum 2 out of 3 SAME-SIDE timeframes.

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
        flush=True,
    )

    return {
        "symbol": symbol,

        "buy_active":
            buy_active,

        "sell_active":
            sell_active,

        "buy_lines":
            buy_lines,

        "sell_lines":
            sell_lines,
    }


# ============================================================
# ALERT SNAPSHOT
# ============================================================

def build_alert_message(
    analyses,
):
    message_lines = []

    # BUY qualifying coins
    for result in analyses:

        if not result[
            "buy_active"
        ]:
            continue

        message_lines.append(
            f"🟢 {result['symbol']} BUY"
        )

        # IMPORTANT:
        # Only >= $5M BUY qualifying TFs.
        # Non-qualifying TF is NOT shown.

        message_lines.extend(
            result[
                "buy_lines"
            ]
        )

        message_lines.append(
            ""
        )

    # SELL qualifying coins
    for result in analyses:

        if not result[
            "sell_active"
        ]:
            continue

        message_lines.append(
            f"🔴 {result['symbol']} SELL"
        )

        # IMPORTANT:
        # Only >= $5M SELL qualifying TFs.
        # Non-qualifying TF is NOT shown.

        message_lines.extend(
            result[
                "sell_lines"
            ]
        )

        message_lines.append(
            ""
        )

    while (
        message_lines
        and message_lines[-1] == ""
    ):
        message_lines.pop()

    return "\n".join(
        message_lines
    )


# ============================================================
# PROCESS ALL TOP-10 COINS
# ============================================================

def process_top10(
    rows,
):
    analyses = [
        analyse_coin(
            row
        )
        for row in rows
    ]

    fresh_triggers = []
    state_changed = False

    # ========================================================
    # CHECK FRESH TRIGGERS + SILENT RE-ARM
    # ========================================================

    for result in analyses:

        symbol = result[
            "symbol"
        ]

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

        # ----------------------------------------------------
        # FRESH BUY
        # ----------------------------------------------------

        if (
            buy_now
            and not buy_before
        ):

            fresh_triggers.append(
                f"{symbol} BUY"
            )

            print(
                f"[FRESH TRIGGER] "
                f"{symbol} BUY",
                flush=True,
            )

        # ----------------------------------------------------
        # FRESH SELL
        # ----------------------------------------------------

        if (
            sell_now
            and not sell_before
        ):

            fresh_triggers.append(
                f"{symbol} SELL"
            )

            print(
                f"[FRESH TRIGGER] "
                f"{symbol} SELL",
                flush=True,
            )

        # ----------------------------------------------------
        # UPDATE BUY STATE
        #
        # True -> False = silent re-arm.
        # No Pushover is sent for reset.
        # ----------------------------------------------------

        if (
            buy_before
            != buy_now
        ):

            state[
                symbol
            ][
                "BUY"
            ] = buy_now

            state_changed = True

            if (
                buy_before
                and not buy_now
            ):
                print(
                    f"[RE-ARM] "
                    f"{symbol} BUY "
                    f"2/3 condition broke - "
                    f"silently re-armed",
                    flush=True,
                )

        # ----------------------------------------------------
        # UPDATE SELL STATE
        # ----------------------------------------------------

        if (
            sell_before
            != sell_now
        ):

            state[
                symbol
            ][
                "SELL"
            ] = sell_now

            state_changed = True

            if (
                sell_before
                and not sell_now
            ):
                print(
                    f"[RE-ARM] "
                    f"{symbol} SELL "
                    f"2/3 condition broke - "
                    f"silently re-armed",
                    flush=True,
                )

    if state_changed:
        save_state()

    # ========================================================
    # NO FRESH CROSS = NO ALERT
    # ========================================================

    if not fresh_triggers:

        print(
            "\n[ALERT] "
            "No fresh 2-of-3 trigger. "
            "No Pushover.",
            flush=True,
        )

        return

    # ========================================================
    # FRESH CROSS EXISTS
    #
    # Build ONE snapshot containing ALL Top-10 coins
    # currently satisfying 2-of-3 BUY or SELL.
    # ========================================================

    message = build_alert_message(
        analyses
    )

    if not message:

        print(
            "[ALERT ERROR] "
            "Fresh trigger found but "
            "qualifying snapshot is empty.",
            flush=True,
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
        flush=True,
    )

    print(
        f"FRESH: "
        f"{', '.join(fresh_triggers)}",
        flush=True,
    )

    print(
        "\n"
        f"{message}"
        "\n",
        flush=True,
    )

    print(
        "============================================================",
        flush=True,
    )

    send_pushover(
        title,
        message,
    )


# ============================================================
# ONE SCAN
# ============================================================

async def scan_once(
    page,
):
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
            f"CoinGlass HTTP "
            f"{response.status}"
        )

    await page.wait_for_timeout(
        8000
    )

    print(
        f"[PAGE] "
        f"title={await page.title()}",
        flush=True,
    )

    await wait_for_liquidation_section(
        page
    )

    lines = await get_rendered_lines(
        page
    )

    # Dynamically take first 10 rows
    # from the CoinGlass value table.

    rows = parse_top_rows(
        lines
    )

    # Every coin is evaluated independently
    # across its own 1H / 4H / 12H.

    process_top10(
        rows
    )

    print(
        f"[SCAN OK] {now_ist()}",
        flush=True,
    )


# ============================================================
# MAIN
# ============================================================

async def main():

    print(
        "COINGLASS TOP-10 "
        "2-OF-3 LIQUIDATION OBSERVER STARTING",
        flush=True,
    )

    load_state()

    print(
        f"URL: {URL}",
        flush=True,
    )

    print(
        f"SCAN: every "
        f"{SCAN_SECONDS} seconds",
        flush=True,
    )

    print(
        f"COINS: dynamic CoinGlass "
        f"Top {TOP_N}",
        flush=True,
    )

    print(
        "TIMEFRAMES: 1H / 4H / 12H",
        flush=True,
    )

    print(
        "BUY TF: "
        "SHORT - LONG >= $5M",
        flush=True,
    )

    print(
        "SELL TF: "
        "LONG - SHORT >= $5M",
        flush=True,
    )

    print(
        "COIN ALERT: "
        "minimum 2-of-3 SAME SIDE",
        flush=True,
    )

    print(
        "REPEAT: "
        "NO repeat while 2/3 remains active",
        flush=True,
    )

    print(
        "RE-ARM: "
        "silent when 2/3 condition breaks",
        flush=True,
    )

    print(
        "ALERT SNAPSHOT: "
        "all currently qualifying Top-10 coins",
        flush=True,
    )

    print(
        "DISPLAY: "
        "only >= $5M qualifying timeframe lines",
        flush=True,
    )

    print(
        "OLD BTC/ETH/SOL CONSENSUS: REMOVED",
        flush=True,
    )

    print(
        "OLD $4M RESET: REMOVED",
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
                    flush=True,
                )

            elapsed = (
                datetime.now(IST)
                - cycle_started
            ).total_seconds()

            sleep_for = max(
                5,
                SCAN_SECONDS
                - elapsed,
            )

            print(
                f"[NEXT SCAN] "
                f"approximately "
                f"{int(sleep_for)} seconds",
                flush=True,
            )

            await asyncio.sleep(
                sleep_for
            )


if __name__ == "__main__":
    asyncio.run(
        main()
    )
