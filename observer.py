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
# HELPERS
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

def update_state(
    metric,
    symbol,
    long_value,
    short_value,
):

    threshold = (
        VALUE_THRESHOLD
        if metric == "VALUE"
        else TRADES_THRESHOLD
    )

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

    if not old["active"]:

        first_time = now_ist()

        metric_states[symbol] = {
            "active": True,
            "side": side,
            "first_observed": first_time,
        }

        print(
            "\n============================================================",
            flush=True,
        )

        if metric == "VALUE":

            print(
                "[NEW VALUE THRESHOLD]\n"
                f"COIN: {symbol}\n"
                f"1H LONG: {fmt_money(long_value)}\n"
                f"1H SHORT: {fmt_money(short_value)}\n"
                f"GAP: {fmt_money(gap)}\n"
                f"STRONGER: {side}\n"
                f"FIRST OBSERVED: {first_time}",
                flush=True,
            )

        else:

            print(
                "[NEW TRADES THRESHOLD]\n"
                f"COIN: {symbol}\n"
                f"1H LONG TRADES: {fmt_count(long_value)}\n"
                f"1H SHORT TRADES: {fmt_count(short_value)}\n"
                f"GAP: {fmt_count(gap)} TRADES\n"
                f"STRONGER: {side}\n"
                f"FIRST OBSERVED: {first_time}",
                flush=True,
            )

        print(
            "============================================================",
            flush=True,
        )

        return

    if old["side"] != side:

        first_time = now_ist()

        metric_states[symbol] = {
            "active": True,
            "side": side,
            "first_observed": first_time,
        }

        print(
            "\n============================================================\n"
            f"[SIDE CHANGE] {metric}\n"
            f"COIN: {symbol}\n"
            f"OLD SIDE: {old['side']}\n"
            f"NEW SIDE: {side}\n"
            f"NEW FIRST OBSERVED: {first_time}\n"
            "============================================================",
            flush=True,
        )

        return

    metric_states[symbol] = old


# ============================================================
# PAGE HELPERS
# ============================================================

async def wait_for_liquidation_section(page):

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
        f"[DEBUG] Rendered text lines={len(lines)}",
        flush=True,
    )

    return lines


# ============================================================
# DETECTION HELPERS
# ============================================================

def is_number_like(text):

    s = clean_text(text)

    if not s:
        return False

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
# VALUE HEADER FINDER
# ============================================================

def find_value_header(lines):

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
                f"[DEBUG] VALUE header starts around line={i}",
                flush=True,
            )

            return i

    raise RuntimeError(
        "VALUE liquidation header not found"
    )


# ============================================================
# VALUE PARSER
# ============================================================

def parse_value_rows(lines):

    header_index = find_value_header(
        lines
    )

    search_lines = lines[
        header_index + 1:
        header_index + 350
    ]

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

    i = 0

    while i < len(filtered):

        current = filtered[i]

        symbol = None
        symbol_index = None

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

            symbol = filtered[i + 1].upper()
            symbol_index = i + 1

        elif is_symbol_like(current):

            symbol = current.upper()
            symbol_index = i

        if (
            symbol is None
            or symbol in seen
        ):

            i += 1
            continue

        numbers = []

        for j in range(
            symbol_index + 1,
            min(
                symbol_index + 20,
                len(filtered),
            ),
        ):

            candidate = filtered[j]

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

        if len(numbers) >= 4:

            long_raw = numbers[2]
            short_raw = numbers[3]

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
                    "long": parse_number(
                        long_raw
                    ),
                    "short": parse_number(
                        short_raw
                    ),
                    "long_raw": long_raw,
                    "short_raw": short_raw,
                }
            )

            print(
                f"[PARSED] VALUE | "
                f"{symbol} | "
                f"1H L={long_raw} | "
                f"1H S={short_raw}",
                flush=True,
            )

        if len(results) >= TOP_N:
            break

        i += 1

    if not results:
        raise RuntimeError(
            "No VALUE rows parsed"
        )

    print(
        f"[DEBUG] VALUE parsed rows={len(results)}",
        flush=True,
    )

    return results


# ============================================================
# READ VALUE
# ============================================================

async def read_value(page):

    lines = await get_rendered_lines(
        page
    )

    return parse_value_rows(
        lines
    )


# ============================================================
# SWITCH VALUE -> TRADES
# ============================================================

async def select_liquidation_trades(page):

    print(
        "[DROPDOWN] Switching VALUE -> TRADES",
        flush=True,
    )

    controls = page.get_by_text(
        "Liquidation Value",
        exact=True,
    )

    count = await controls.count()

    print(
        f"[DEBUG] Liquidation Value controls={count}",
        flush=True,
    )

    visible_control = None

    for i in range(count):

        candidate = controls.nth(i)

        try:

            if await candidate.is_visible():

                visible_control = candidate

                print(
                    f"[DEBUG] Visible VALUE control index={i}",
                    flush=True,
                )

                break

        except Exception:
            continue

    if visible_control is None:

        raise RuntimeError(
            "No visible Liquidation Value control found"
        )

    await visible_control.click(
        timeout=10000,
    )

    await page.wait_for_timeout(
        700
    )

    options = page.get_by_text(
        "Liquidation Trades",
        exact=True,
    )

    option_count = await options.count()

    print(
        f"[DEBUG] Liquidation Trades options={option_count}",
        flush=True,
    )

    visible_option = None

    for i in range(option_count):

        candidate = options.nth(i)

        try:

            if await candidate.is_visible():

                visible_option = candidate

                print(
                    f"[DEBUG] Visible TRADES option index={i}",
                    flush=True,
                )

                break

        except Exception:
            continue

    if visible_option is None:

        raise RuntimeError(
            "No visible Liquidation Trades option found"
        )

    await visible_option.click(
        timeout=10000,
    )

    await page.wait_for_timeout(
        4000
    )

    print(
        "[DROPDOWN] TRADES selected",
        flush=True,
    )


# ============================================================
# TRADES DEBUG CAPTURE
# ============================================================

async def capture_trades_structure(page):

    lines = await get_rendered_lines(
        page
    )

    print(
        "\n"
        "============================================================\n"
        "[TRADES DEBUG START]\n"
        "============================================================",
        flush=True,
    )

    # Find useful anchors in the Trades view.
    anchor_indexes = []

    keywords = (
        "Liquidation Trades",
        "Ranking",
        "Assets",
        "1h",
        "4h",
        "12h",
        "24h",
        "BTC",
        "ETH",
        "SOL",
    )

    for i, line in enumerate(lines):

        low = line.lower()

        if any(
            keyword.lower() in low
            for keyword in keywords
        ):

            anchor_indexes.append(i)

    print(
        f"[TRADES DEBUG] anchors={anchor_indexes[:40]}",
        flush=True,
    )

    # --------------------------------------------------------
    # Capture around Liquidation Trades
    # --------------------------------------------------------

    trade_control_indexes = [
        i
        for i, line in enumerate(lines)
        if "liquidation trades" in line.lower()
    ]

    print(
        f"[TRADES DEBUG] "
        f"Liquidation Trades indexes="
        f"{trade_control_indexes}",
        flush=True,
    )

    printed = set()

    for anchor in trade_control_indexes:

        start = max(
            0,
            anchor - 25,
        )

        end = min(
            len(lines),
            anchor + 180,
        )

        print(
            f"\n[TRADES DEBUG BLOCK "
            f"around line {anchor}]",
            flush=True,
        )

        for i in range(
            start,
            end,
        ):

            if i in printed:
                continue

            printed.add(i)

            print(
                f"[TRADES TEXT {i}] "
                f"{lines[i]}",
                flush=True,
            )

    # --------------------------------------------------------
    # If Liquidation Trades label isn't present in body text,
    # capture around BTC/ETH/SOL instead.
    # --------------------------------------------------------

    if not trade_control_indexes:

        coin_indexes = []

        for i, line in enumerate(lines):

            if line.upper() in {
                "BTC",
                "ETH",
                "SOL",
            }:

                coin_indexes.append(i)

        print(
            f"[TRADES DEBUG] "
            f"BTC/ETH/SOL indexes="
            f"{coin_indexes[:20]}",
            flush=True,
        )

        for anchor in coin_indexes[:5]:

            start = max(
                0,
                anchor - 30,
            )

            end = min(
                len(lines),
                anchor + 100,
            )

            print(
                f"\n[TRADES DEBUG COIN BLOCK "
                f"around line {anchor}]",
                flush=True,
            )

            for i in range(
                start,
                end,
            ):

                if i in printed:
                    continue

                printed.add(i)

                print(
                    f"[TRADES TEXT {i}] "
                    f"{lines[i]}",
                    flush=True,
                )

    # --------------------------------------------------------
    # Last fallback: capture middle portion where Value header
    # previously appeared (~450 onward).
    # --------------------------------------------------------

    if not printed:

        start = min(
            380,
            len(lines),
        )

        end = min(
            700,
            len(lines),
        )

        print(
            "\n[TRADES DEBUG FALLBACK BLOCK]",
            flush=True,
        )

        for i in range(
            start,
            end,
        ):

            print(
                f"[TRADES TEXT {i}] "
                f"{lines[i]}",
                flush=True,
            )

    print(
        "============================================================\n"
        "[TRADES DEBUG END]\n"
        "============================================================",
        flush=True,
    )

    # Intentional diagnostic stop.
    raise RuntimeError(
        "TRADES structure captured - "
        "send TRADES DEBUG logs"
    )


# ============================================================
# PRINT VALUE SCAN
# ============================================================

def print_value_scan(rows):

    print(
        f"\n[VALUE SCAN] "
        f"{now_ist()} | "
        f"rows={len(rows)}",
        flush=True,
    )

    for row in rows:

        gap, side = get_gap(
            row["long"],
            row["short"],
        )

        print(
            f"{row['symbol']:8} "
            f"L={fmt_money(row['long']):>10} "
            f"S={fmt_money(row['short']):>10} "
            f"GAP={fmt_money(gap):>10} "
            f"{side}",
            flush=True,
        )


# ============================================================
# ONE COMPLETE DIAGNOSTIC SCAN
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

    print(
        f"[HTTP] status="
        f"{response.status if response else 'NO RESPONSE'}",
        flush=True,
    )

    print(
        f"[HTTP] final_url={page.url}",
        flush=True,
    )

    await page.wait_for_timeout(
        8000
    )

    print(
        f"[PAGE] title={await page.title()}",
        flush=True,
    )

    if (
        response
        and response.status >= 400
    ):

        raise RuntimeError(
            f"CoinGlass HTTP "
            f"{response.status}"
        )

    await wait_for_liquidation_section(
        page
    )

    # --------------------------------------------------------
    # WORKING VALUE SIDE
    # --------------------------------------------------------

    value_rows = await read_value(
        page
    )

    print_value_scan(
        value_rows
    )

    # --------------------------------------------------------
    # WORKING DROPDOWN
    # --------------------------------------------------------

    await select_liquidation_trades(
        page
    )

    # --------------------------------------------------------
    # CAPTURE EXACT TRADES STRUCTURE
    # --------------------------------------------------------

    await capture_trades_structure(
        page
    )


# ============================================================
# MAIN
# ============================================================

async def main():

    print(
        "COINGLASS LIQUIDATION OBSERVER "
        "TRADES DEBUG VERSION",
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

            cycle_started = datetime.now(
                IST
            )

            try:

                await scan_once(
                    page
                )

            except Exception as exc:

                print(
                    f"\n[SCAN ENDED] "
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
