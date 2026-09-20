import asyncio
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright


# ============================================================
# CONFIG
# ============================================================

URL = "https://www.coinglass.com/liquidations"

SCAN_SECONDS = 300
VALUE_THRESHOLD = 5_000_000.0
TRADES_THRESHOLD = 500
TOP_N = 12

IST = ZoneInfo("Asia/Kolkata")


# ============================================================
# STATE
# ============================================================

states = {
    "VALUE": {},
    "TRADES": {},
}


# ============================================================
# BASIC HELPERS
# ============================================================

def now_ist():
    return datetime.now(IST).strftime(
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
        s,
    )

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


def fmt_count(value):
    return f"{int(round(value)):,}"


def get_gap(long_value, short_value):

    signed_gap = (
        long_value - short_value
    )

    gap = abs(signed_gap)

    if signed_gap > 0:
        side = "LONG"

    elif signed_gap < 0:
        side = "SHORT"

    else:
        side = "EVEN"

    return gap, side


# ============================================================
# STATE ENGINE
# ============================================================

def update_state(
    metric,
    symbol,
    long_value,
    short_value,
):

    if metric == "VALUE":
        threshold = VALUE_THRESHOLD
    else:
        threshold = TRADES_THRESHOLD

    gap, side = get_gap(
        long_value,
        short_value,
    )

    metric_states = states[metric]

    old = metric_states.get(
        symbol,
        {
            "active": False,
            "side": None,
            "first_observed": None,
        },
    )

    qualifies = (
        gap >= threshold
        and side != "EVEN"
    )

    # --------------------------------------------------------
    # BELOW THRESHOLD
    # --------------------------------------------------------

    if not qualifies:

        if old["active"]:

            print(
                f"[CLEAR] {metric} | "
                f"{symbol} | "
                f"previous={old['side']} | "
                f"time={now_ist()}",
                flush=True,
            )

        metric_states[symbol] = {
            "active": False,
            "side": None,
            "first_observed": None,
        }

        return

    # --------------------------------------------------------
    # NEW CONDITION
    # --------------------------------------------------------

    if not old["active"]:

        first_time = now_ist()

        metric_states[symbol] = {
            "active": True,
            "side": side,
            "first_observed": first_time,
        }

        print(
            "\n"
            "============================================================",
            flush=True,
        )

        if metric == "VALUE":

            print(
                "[NEW VALUE THRESHOLD]\n"
                f"COIN: {symbol}\n"
                f"1H LONG: "
                f"{fmt_money(long_value)}\n"
                f"1H SHORT: "
                f"{fmt_money(short_value)}\n"
                f"GAP: {fmt_money(gap)}\n"
                f"STRONGER: {side}\n"
                f"FIRST OBSERVED: "
                f"{first_time}",
                flush=True,
            )

        else:

            print(
                "[NEW TRADES THRESHOLD]\n"
                f"COIN: {symbol}\n"
                f"1H LONG TRADES: "
                f"{fmt_count(long_value)}\n"
                f"1H SHORT TRADES: "
                f"{fmt_count(short_value)}\n"
                f"GAP: "
                f"{fmt_count(gap)} TRADES\n"
                f"STRONGER: {side}\n"
                f"FIRST OBSERVED: "
                f"{first_time}",
                flush=True,
            )

        print(
            "============================================================",
            flush=True,
        )

        return

    # --------------------------------------------------------
    # SIDE FLIP
    # --------------------------------------------------------

    if old["side"] != side:

        first_time = now_ist()

        metric_states[symbol] = {
            "active": True,
            "side": side,
            "first_observed": first_time,
        }

        print(
            "\n"
            "============================================================\n"
            f"[SIDE CHANGE] {metric}\n"
            f"COIN: {symbol}\n"
            f"OLD SIDE: {old['side']}\n"
            f"NEW SIDE: {side}\n"
            f"NEW FIRST OBSERVED: "
            f"{first_time}\n"
            "============================================================",
            flush=True,
        )

        return

    # Same condition = no duplicate
    metric_states[symbol] = old


# ============================================================
# LOCATE TOTAL LIQUIDATIONS SECTION
# ============================================================

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

    await page.get_by_text(
        "1h Long",
        exact=True,
    ).last.wait_for(
        state="visible",
        timeout=30000,
    )

    print(
        "[PAGE] Total Liquidations section visible",
        flush=True,
    )


# ============================================================
# SCROLL TABLE INTO VIEW
# ============================================================

async def scroll_to_liquidation_table(
    page,
):

    header = page.get_by_text(
        "1h Long",
        exact=True,
    ).last

    await header.scroll_into_view_if_needed()

    await page.wait_for_timeout(1500)


# ============================================================
# GET RENDERED TEXT LINES
# ============================================================

async def get_rendered_lines(page):

    body_text = await page.locator(
        "body"
    ).inner_text(
        timeout=10000
    )

    raw_lines = body_text.splitlines()

    lines = []

    for line in raw_lines:

        line = clean_text(line)

        if line:
            lines.append(line)

    print(
        f"[DEBUG] Rendered text lines="
        f"{len(lines)}",
        flush=True,
    )

    return lines


# ============================================================
# NUMBER-LIKE VALUE
# ============================================================

def is_number_like(text):

    s = clean_text(text)

    if not s:
        return False

    # Price / percentage / dollar / count
    return bool(
        re.fullmatch(
            r"[-+−]?\$?"
            r"\d[\d,]*"
            r"(?:\.\d+)?"
            r"(?:[KMB])?"
            r"%?",
            s,
            flags=re.I,
        )
    )


# ============================================================
# SYMBOL-LIKE VALUE
# ============================================================

def is_symbol_like(text):

    s = clean_text(text).upper()

    if not re.fullmatch(
        r"[A-Z0-9]{2,15}",
        s,
    ):
        return False

    ignored = {
        "PRICE",
        "ASSETS",
        "LONG",
        "SHORT",
        "RANKING",
        "VALUE",
        "TRADES",
        "TOTAL",
        "LIQUIDATIONS",
        "USD",
        "USDT",
        "24H",
        "12H",
        "4H",
        "1H",
    }

    return s not in ignored


# ============================================================
# FIND TABLE HEADER POSITION
# ============================================================

def find_liquidation_header(lines):

    for i in range(len(lines)):

        block = " ".join(
            lines[i:i + 20]
        ).lower()

        if (
            "assets" in block
            and "1h long" in block
            and "1h short" in block
            and "4h long" in block
            and "4h short" in block
        ):

            print(
                f"[DEBUG] Header block starts "
                f"around line={i}",
                flush=True,
            )

            return i

    raise RuntimeError(
        "Liquidation header block "
        "not found in rendered text"
    )


# ============================================================
# DEBUG TEXT AROUND HEADER
# ============================================================

def print_header_debug(
    lines,
    start,
):

    print(
        "\n[DEBUG TEXT AROUND TABLE]",
        flush=True,
    )

    end = min(
        len(lines),
        start + 100,
    )

    for i in range(
        start,
        end,
    ):

        print(
            f"[TEXT {i}] "
            f"{lines[i]}",
            flush=True,
        )


# ============================================================
# PARSE RENDERED TEXT
# ============================================================

def parse_rows_from_lines(
    lines,
    metric,
):

    header_index = find_liquidation_header(
        lines
    )

    # Start after headers.
    search_lines = lines[
        header_index + 1:
        header_index + 350
    ]

    # --------------------------------------------------------
    # Remove known headers/control labels
    # --------------------------------------------------------

    ignored_exact = {
        "Ranking",
        "Assets",
        "Price",
        "Price (24h%)",
        "1h Long",
        "1h Short",
        "4h Long",
        "4h Short",
        "12h Long",
        "12h Short",
        "24h Long",
        "24h Short",
        "Liquidation Value",
        "Liquidation Trades",
    }

    filtered = []

    for item in search_lines:

        if item in ignored_exact:
            continue

        filtered.append(item)

    results = []
    seen = set()

    # --------------------------------------------------------
    # We search for a ticker and then numeric values following
    # it.
    #
    # Expected rendered sequence is roughly:
    #
    # rank
    # BTC
    # price
    # 24h %
    # 1h long
    # 1h short
    # 4h long
    # 4h short ...
    #
    # Sometimes rank and symbol are combined differently,
    # therefore this does not depend on fixed DOM cells.
    # --------------------------------------------------------

    i = 0

    while i < len(filtered):

        current = filtered[i]

        symbol = None
        symbol_index = None

        # Pattern:
        # ranking number followed by ticker
        if (
            re.fullmatch(
                r"#?\d+",
                current,
            )
            and i + 1 < len(filtered)
            and is_symbol_like(
                filtered[i + 1]
            )
        ):

            symbol = (
                filtered[i + 1]
                .upper()
            )

            symbol_index = i + 1

        # Or ticker itself
        elif is_symbol_like(current):

            symbol = current.upper()
            symbol_index = i

        if (
            symbol is None
            or symbol in seen
        ):

            i += 1
            continue

        # ----------------------------------------------------
        # Collect numeric-looking values after symbol.
        # ----------------------------------------------------

        numbers = []

        for j in range(
            symbol_index + 1,
            min(
                symbol_index + 20,
                len(filtered),
            ),
        ):

            candidate = filtered[j]

            # Stop if next obvious ranked asset starts.
            if (
                j > symbol_index + 2
                and re.fullmatch(
                    r"#?\d+",
                    candidate,
                )
                and j + 1 < len(filtered)
                and is_symbol_like(
                    filtered[j + 1]
                )
            ):
                break

            if is_number_like(candidate):
                numbers.append(candidate)

        # ----------------------------------------------------
        # Need:
        # 0 = Price
        # 1 = Price 24h %
        # 2 = 1h Long
        # 3 = 1h Short
        # ----------------------------------------------------

        if len(numbers) >= 4:

            long_raw = numbers[2]
            short_raw = numbers[3]

            long_value = parse_number(
                long_raw
            )

            short_value = parse_number(
                short_raw
            )

            # Sanity:
            # VALUE normally has $ / K / M etc.
            # TRADES should be count-like.
            if metric == "VALUE":

                if (
                    "$" not in long_raw
                    and "$" not in short_raw
                    and not re.search(
                        r"[KMB]",
                        long_raw + short_raw,
                        flags=re.I,
                    )
                ):
                    i += 1
                    continue

            seen.add(symbol)

            results.append(
                {
                    "symbol": symbol,
                    "long": long_value,
                    "short": short_value,
                    "long_raw": long_raw,
                    "short_raw": short_raw,
                }
            )

            print(
                f"[PARSED] {metric} | "
                f"{symbol} | "
                f"1H L={long_raw} | "
                f"1H S={short_raw}",
                flush=True,
            )

        if len(results) >= TOP_N:
            break

        i += 1

    return results


# ============================================================
# READ CURRENT MODE
# ============================================================

async def read_current_mode(
    page,
    metric,
):

    await scroll_to_liquidation_table(
        page
    )

    lines = await get_rendered_lines(
        page
    )

    rows = parse_rows_from_lines(
        lines,
        metric,
    )

    if not rows:

        header_index = (
            find_liquidation_header(
                lines
            )
        )

        # Diagnostic:
        # if parser still fails, logs will now show
        # the exact rendered structure.
        print_header_debug(
            lines,
            header_index,
        )

        raise RuntimeError(
            f"No {metric} rows parsed "
            f"from rendered text"
        )

    print(
        f"[DEBUG] {metric} parsed rows="
        f"{len(rows)}",
        flush=True,
    )

    return rows


# ============================================================
# SWITCH VALUE -> TRADES
# ============================================================

async def select_liquidation_trades(
    page,
):

    print(
        "[DROPDOWN] "
        "Switching VALUE -> TRADES",
        flush=True,
    )

    controls = page.get_by_text(
        "Liquidation Value",
        exact=True,
    )

    count = await controls.count()

    print(
        f"[DEBUG] Liquidation Value "
        f"controls={count}",
        flush=True,
    )

    if count == 0:

        raise RuntimeError(
            "Liquidation Value control "
            "not found"
        )

    control = controls.last

    await control.scroll_into_view_if_needed()

    await control.click(
        timeout=15000
    )

    await page.wait_for_timeout(500)

    options = page.get_by_text(
        "Liquidation Trades",
        exact=True,
    )

    option_count = await options.count()

    print(
        f"[DEBUG] Liquidation Trades "
        f"options={option_count}",
        flush=True,
    )

    if option_count == 0:

        raise RuntimeError(
            "Liquidation Trades option "
            "not found"
        )

    option = options.last

    await option.wait_for(
        state="visible",
        timeout=10000,
    )

    await option.click(
        timeout=10000
    )

    # Allow table values to redraw.
    await page.wait_for_timeout(4000)

    print(
        "[DROPDOWN] TRADES selected",
        flush=True,
    )


# ============================================================
# SANITY CHECK
# ============================================================

def sanity_check(
    value_rows,
    trade_rows,
):

    if not value_rows:
        raise RuntimeError(
            "VALUE rows empty"
        )

    if not trade_rows:
        raise RuntimeError(
            "TRADES rows empty"
        )

    value_symbols = {
        x["symbol"]
        for x in value_rows
    }

    trade_symbols = {
        x["symbol"]
        for x in trade_rows
    }

    common = (
        value_symbols
        .intersection(
            trade_symbols
        )
    )

    print(
        f"[SANITY] common symbols="
        f"{len(common)}",
        flush=True,
    )

    if len(common) < 3:

        raise RuntimeError(
            "VALUE/TRADES symbol "
            "match failed"
        )


# ============================================================
# PRINT SCAN
# ============================================================

def print_scan(
    metric,
    rows,
):

    print(
        f"\n[{metric} SCAN] "
        f"{now_ist()} | "
        f"rows={len(rows)}",
        flush=True,
    )

    for row in rows:

        gap, side = get_gap(
            row["long"],
            row["short"],
        )

        if metric == "VALUE":

            print(
                f"{row['symbol']:8} "
                f"L="
                f"{fmt_money(row['long']):>10} "
                f"S="
                f"{fmt_money(row['short']):>10} "
                f"GAP="
                f"{fmt_money(gap):>10} "
                f"{side}",
                flush=True,
            )

        else:

            print(
                f"{row['symbol']:8} "
                f"L="
                f"{fmt_count(row['long']):>7} "
                f"S="
                f"{fmt_count(row['short']):>7} "
                f"GAP="
                f"{fmt_count(gap):>7} "
                f"{side}",
                flush=True,
            )


# ============================================================
# ONE COMPLETE SCAN
# ============================================================

async def scan_once(page):

    print(
        "\n"
        "############################################################\n"
        f"[SCAN START] {now_ist()}\n"
        "############################################################",
        flush=True,
    )

    # --------------------------------------------------------
    # LOAD / REFRESH PAGE
    # --------------------------------------------------------

    response = await page.goto(
        URL,
        wait_until="domcontentloaded",
        timeout=60000,
    )

    print(
        f"[HTTP] status="
        f"{response.status if response else 'NO RESPONSE'}",
        flush=True,
    )

    print(
        f"[HTTP] final_url="
        f"{page.url}",
        flush=True,
    )

    await page.wait_for_timeout(
        8000
    )

    print(
        f"[PAGE] title="
        f"{await page.title()}",
        flush=True,
    )

    if response and response.status >= 400:

        raise RuntimeError(
            f"CoinGlass HTTP "
            f"{response.status}"
        )

    # --------------------------------------------------------
    # WAIT FOR TOTAL LIQUIDATIONS
    # --------------------------------------------------------

    await wait_for_liquidation_section(
        page
    )

    # --------------------------------------------------------
    # VALUE
    # --------------------------------------------------------

    value_rows = await read_current_mode(
        page,
        "VALUE",
    )

    print_scan(
        "VALUE",
        value_rows,
    )

    # --------------------------------------------------------
    # SWITCH TO TRADES
    # --------------------------------------------------------

    await select_liquidation_trades(
        page
    )

    # --------------------------------------------------------
    # TRADES
    # --------------------------------------------------------

    trade_rows = await read_current_mode(
        page,
        "TRADES",
    )

    print_scan(
        "TRADES",
        trade_rows,
    )

    # --------------------------------------------------------
    # VERIFY
    # --------------------------------------------------------

    sanity_check(
        value_rows,
        trade_rows,
    )

    # --------------------------------------------------------
    # UPDATE STATES
    # --------------------------------------------------------

    for row in value_rows:

        update_state(
            "VALUE",
            row["symbol"],
            row["long"],
            row["short"],
        )

    for row in trade_rows:

        update_state(
            "TRADES",
            row["symbol"],
            row["long"],
            row["short"],
        )

    print(
        f"\n[SCAN OK] "
        f"{now_ist()}",
        flush=True,
    )


# ============================================================
# MAIN
# ============================================================

async def main():

    print(
        "COINGLASS LIQUIDATION "
        "OBSERVER STARTING",
        flush=True,
    )

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
        f"VALUE threshold: "
        f"{fmt_money(VALUE_THRESHOLD)}",
        flush=True,
    )

    print(
        f"TRADES threshold: "
        f"{TRADES_THRESHOLD:,}",
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

        context = (
            await browser.new_context(
                viewport={
                    "width": 1600,
                    "height": 1200,
                },
                locale="en-US",
                timezone_id="Asia/Kolkata",
            )
        )

        page = await context.new_page()

        while True:

            cycle_started = (
                datetime.now(IST)
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
                SCAN_SECONDS - elapsed,
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
    asyncio.run(main())
