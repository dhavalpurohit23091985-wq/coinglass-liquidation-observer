import asyncio
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright


# ============================================================
# CONFIG
# ============================================================

URL = "https://www.coinglass.com/liquidations"

SCAN_SECONDS = 300  # 5 minutes

VALUE_THRESHOLD = 5_000_000.0
TRADES_THRESHOLD = 500

# Current table ke first 12 assets
TOP_N = 12

IST = ZoneInfo("Asia/Kolkata")


# ============================================================
# STATE
#
# Separate state:
#   VALUE
#   TRADES
#
# Example:
# states["VALUE"]["BTC"] = {
#     "active": True,
#     "side": "SHORT",
#     "first_observed": "21-09-2026 03:15:00 IST"
# }
# ============================================================

states = {
    "VALUE": {},
    "TRADES": {},
}


# ============================================================
# HELPERS
# ============================================================

def now_ist():
    return datetime.now(IST).strftime("%d-%m-%Y %H:%M:%S IST")


def clean_text(value):
    if value is None:
        return ""
    return " ".join(str(value).replace("\xa0", " ").split())


def parse_number(text):
    """
    Examples:
        $5.83M  -> 5,830,000
        $333.65K -> 333,650
        $567.22 -> 567.22
        991 -> 991
        1,287 -> 1287
    """

    s = clean_text(text).upper()

    if not s:
        return 0.0

    s = s.replace("$", "")
    s = s.replace(",", "")
    s = s.replace("−", "-")

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


def fmt_count(value):
    return f"{int(round(value)):,}"


def get_gap(long_value, short_value):
    signed_gap = long_value - short_value
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

def update_state(metric, symbol, long_value, short_value):
    if metric == "VALUE":
        threshold = VALUE_THRESHOLD
    else:
        threshold = TRADES_THRESHOLD

    gap, side = get_gap(long_value, short_value)

    metric_states = states[metric]

    old = metric_states.get(
        symbol,
        {
            "active": False,
            "side": None,
            "first_observed": None,
        },
    )

    qualifies = gap >= threshold and side != "EVEN"

    # --------------------------------------------------------
    # BELOW THRESHOLD
    # Active condition clears.
    # --------------------------------------------------------

    if not qualifies:

        if old["active"]:
            print(
                f"[CLEAR] {metric} | {symbol} | "
                f"gap below threshold | "
                f"previous side={old['side']} | "
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
    # FIRST QUALIFYING OBSERVATION
    # --------------------------------------------------------

    if not old["active"]:

        first_time = now_ist()

        metric_states[symbol] = {
            "active": True,
            "side": side,
            "first_observed": first_time,
        }

        if metric == "VALUE":
            print(
                "\n"
                "============================================================\n"
                f"[NEW VALUE THRESHOLD]\n"
                f"COIN: {symbol}\n"
                f"1H LONG: {fmt_money(long_value)}\n"
                f"1H SHORT: {fmt_money(short_value)}\n"
                f"GAP: {fmt_money(gap)}\n"
                f"STRONGER: {side}\n"
                f"FIRST OBSERVED: {first_time}\n"
                "============================================================",
                flush=True,
            )

        else:
            print(
                "\n"
                "============================================================\n"
                f"[NEW TRADES THRESHOLD]\n"
                f"COIN: {symbol}\n"
                f"1H LONG TRADES: {fmt_count(long_value)}\n"
                f"1H SHORT TRADES: {fmt_count(short_value)}\n"
                f"GAP: {fmt_count(gap)} TRADES\n"
                f"STRONGER: {side}\n"
                f"FIRST OBSERVED: {first_time}\n"
                "============================================================",
                flush=True,
            )

        return

    # --------------------------------------------------------
    # DIRECT SIDE FLIP WHILE STILL ABOVE THRESHOLD
    #
    # Example:
    # SHORT gap >= threshold
    # directly becomes LONG gap >= threshold
    #
    # Treat as a new qualifying state.
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
            f"NEW FIRST OBSERVED: {first_time}\n"
            "============================================================",
            flush=True,
        )

        return

    # --------------------------------------------------------
    # SAME ACTIVE CONDITION
    # No duplicate alert.
    # --------------------------------------------------------

    metric_states[symbol] = old


# ============================================================
# FIND TOTAL LIQUIDATIONS TABLE
# ============================================================

async def find_liquidation_table(page):
    tables = page.locator("table")
    count = await tables.count()

    for i in range(count):
        table = tables.nth(i)

        try:
            text = clean_text(await table.inner_text(timeout=3000))
        except Exception:
            continue

        low = text.lower()

        if (
            "1h long" in low
            and "1h short" in low
            and "4h long" in low
            and "4h short" in low
        ):
            return table

    raise RuntimeError("Total Liquidations table not found")


# ============================================================
# READ TABLE
# ============================================================

async def read_1h_rows(page):
    table = await find_liquidation_table(page)

    rows = table.locator("tbody tr")
    row_count = await rows.count()

    results = []

    for i in range(min(row_count, TOP_N)):
        row = rows.nth(i)

        cells = row.locator("td")
        cell_count = await cells.count()

        # Expected:
        # 0 Ranking
        # 1 Assets
        # 2 Price
        # 3 Price (24h%)
        # 4 1h Long
        # 5 1h Short
        if cell_count < 6:
            continue

        asset_text = clean_text(await cells.nth(1).inner_text())
        long_text = clean_text(await cells.nth(4).inner_text())
        short_text = clean_text(await cells.nth(5).inner_text())

        # Asset cell may contain icon/extra text.
        # Extract ticker-like symbol.
        tokens = re.findall(r"[A-Z0-9]{2,15}", asset_text.upper())

        if not tokens:
            continue

        symbol = tokens[-1]

        long_value = parse_number(long_text)
        short_value = parse_number(short_text)

        results.append(
            {
                "symbol": symbol,
                "long": long_value,
                "short": short_value,
                "long_raw": long_text,
                "short_raw": short_text,
            }
        )

    if not results:
        raise RuntimeError("No liquidation rows parsed")

    return results


# ============================================================
# DROPDOWN HELPERS
# ============================================================

async def select_liquidation_trades(page):
    """
    Refresh normally returns page to Liquidation Value.

    We locate visible Liquidation Value control,
    click it, then choose Liquidation Trades.
    """

    value_control = page.get_by_text(
        "Liquidation Value",
        exact=True,
    ).last

    await value_control.wait_for(
        state="visible",
        timeout=15000,
    )

    await value_control.click()

    trades_option = page.get_by_text(
        "Liquidation Trades",
        exact=True,
    ).last

    await trades_option.wait_for(
        state="visible",
        timeout=10000,
    )

    await trades_option.click()

    # Allow React/table to update.
    await page.wait_for_timeout(2500)


# ============================================================
# SANITY CHECK
# ============================================================

def sanity_check(value_rows, trade_rows):
    if not value_rows:
        raise RuntimeError("VALUE rows empty")

    if not trade_rows:
        raise RuntimeError("TRADES rows empty")

    value_symbols = {x["symbol"] for x in value_rows}
    trade_symbols = {x["symbol"] for x in trade_rows}

    common = value_symbols.intersection(trade_symbols)

    if len(common) < 3:
        raise RuntimeError(
            "VALUE/TRADES tables do not appear to match"
        )

    # Trade counts should normally be integer-like.
    # This also helps detect a failed dropdown switch where
    # dollar values are accidentally read again.
    for row in trade_rows:
        if row["long"] < 0 or row["short"] < 0:
            raise RuntimeError("Invalid negative trade count")


# ============================================================
# DISPLAY CURRENT SCAN
# ============================================================

def print_scan(metric, rows):
    print(
        f"\n[{metric} SCAN] {now_ist()} | rows={len(rows)}",
        flush=True,
    )

    for row in rows:
        gap, side = get_gap(row["long"], row["short"])

        if metric == "VALUE":
            print(
                f"{row['symbol']:8} "
                f"L={fmt_money(row['long']):>10} "
                f"S={fmt_money(row['short']):>10} "
                f"GAP={fmt_money(gap):>10} "
                f"{side}",
                flush=True,
            )

        else:
            print(
                f"{row['symbol']:8} "
                f"L={fmt_count(row['long']):>7} "
                f"S={fmt_count(row['short']):>7} "
                f"GAP={fmt_count(gap):>7} "
                f"{side}",
                flush=True,
            )


# ============================================================
# ONE COMPLETE 5-MINUTE SCAN
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
    # 1. REFRESH / LOAD PAGE
    # --------------------------------------------------------

    await page.goto(
        URL,
        wait_until="domcontentloaded",
        timeout=60000,
    )

    # Give dynamic CoinGlass data time to populate.
    await page.wait_for_timeout(5000)

    # --------------------------------------------------------
    # 2. LIQUIDATION VALUE
    # --------------------------------------------------------

    value_rows = await read_1h_rows(page)

    print_scan("VALUE", value_rows)

    # --------------------------------------------------------
    # 3. SWITCH DROPDOWN TO LIQUIDATION TRADES
    # --------------------------------------------------------

    await select_liquidation_trades(page)

    # --------------------------------------------------------
    # 4. LIQUIDATION TRADES
    # --------------------------------------------------------

    trade_rows = await read_1h_rows(page)

    print_scan("TRADES", trade_rows)

    # --------------------------------------------------------
    # 5. SANITY CHECK BEFORE UPDATING STATES
    #
    # If dropdown/load failed, do NOT treat stale/incorrect
    # data as a valid fresh scan.
    # --------------------------------------------------------

    sanity_check(value_rows, trade_rows)

    # --------------------------------------------------------
    # 6. UPDATE VALUE STATES
    # --------------------------------------------------------

    for row in value_rows:
        update_state(
            "VALUE",
            row["symbol"],
            row["long"],
            row["short"],
        )

    # --------------------------------------------------------
    # 7. UPDATE TRADES STATES
    # --------------------------------------------------------

    for row in trade_rows:
        update_state(
            "TRADES",
            row["symbol"],
            row["long"],
            row["short"],
        )

    print(
        f"\n[SCAN OK] {now_ist()}",
        flush=True,
    )


# ============================================================
# MAIN
# ============================================================

async def main():
    print(
        "COINGLASS LIQUIDATION OBSERVER STARTING",
        flush=True,
    )

    print(
        f"URL: {URL}",
        flush=True,
    )

    print(
        f"SCAN: every {SCAN_SECONDS} seconds",
        flush=True,
    )

    print(
        f"VALUE threshold: {fmt_money(VALUE_THRESHOLD)}",
        flush=True,
    )

    print(
        f"TRADES threshold: {TRADES_THRESHOLD:,}",
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

                # Failed cycle is ignored.
                # No stale state update.

            elapsed = (
                datetime.now(IST) - cycle_started
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
