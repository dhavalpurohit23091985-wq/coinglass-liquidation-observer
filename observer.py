import asyncio
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from playwright.async_api import async_playwright


# ============================================================
# CONFIG
# ============================================================

URL = "https://www.coinglass.com/liquidations"

SCAN_SECONDS = 300
TOP_N = 12

VALUE_THRESHOLD = 5_000_000.0
TRADES_THRESHOLD = 500

IST = ZoneInfo("Asia/Kolkata")

PUSHOVER_USER_KEY = os.getenv("PUSHOVER_USER_KEY", "").strip()
PUSHOVER_APP_TOKEN = os.getenv("PUSHOVER_APP_TOKEN", "").strip()


# ============================================================
# STATE
# ============================================================

states = {
    "VALUE": {},
    "TRADES": {},
}

# IMPORTANT:
# The first successful scan after a worker start/redeploy is used only to
# bootstrap the current CoinGlass state. This prevents duplicate Pushover
# alerts when Render restarts the process and the in-memory state is lost.
bootstrap_complete = False


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
            f"[PUSHOVER FAILED] "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )

    return False


# ============================================================
# STATE / THRESHOLD ENGINE
# ============================================================

def update_state(metric, symbol, long_value, short_value, allow_alert=True):
    threshold = (
        VALUE_THRESHOLD
        if metric == "VALUE"
        else TRADES_THRESHOLD
    )

    gap, side = get_gap(
        long_value,
        short_value,
    )

    old = states[metric].get(
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
    # BELOW THRESHOLD -> CLEAR
    # --------------------------------------------------------

    if not qualifies:
        if old["active"]:
            print(
                f"[CLEAR] {metric} | "
                f"{symbol} | "
                f"previous={old['side']} | "
                f"{now_ist()}",
                flush=True,
            )

        states[metric][symbol] = {
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

        states[metric][symbol] = {
            "active": True,
            "side": side,
            "first_observed": first_time,
        }

        # On the first successful scan after process start/redeploy, seed the
        # already-active condition without sending a duplicate notification.
        if not allow_alert:
            print(
                f"[BOOTSTRAP ACTIVE] {metric} | "
                f"{symbol} | side={side} | "
                f"gap={gap}",
                flush=True,
            )
            return

        if metric == "VALUE":
            title = (
                f"COINGLASS {symbol} VALUE "
                f"{side} ${gap / 1_000_000:.2f}M GAP"
            )

            message = (
                "COINGLASS 1H LIQUIDATION VALUE\n\n"
                f"COIN: {symbol}\n"
                f"LONG: {fmt_money(long_value)}\n"
                f"SHORT: {fmt_money(short_value)}\n"
                f"GAP: {fmt_money(gap)}\n"
                f"STRONGER: {side}\n"
                f"FIRST OBSERVED: {first_time}"
            )

        else:
            title = (
                f"COINGLASS {symbol} TRADES "
                f"{side} {fmt_count(gap)} GAP"
            )

            message = (
                "COINGLASS 1H LIQUIDATION TRADES\n\n"
                f"COIN: {symbol}\n"
                f"LONG TRADES: {fmt_count(long_value)}\n"
                f"SHORT TRADES: {fmt_count(short_value)}\n"
                f"GAP: {fmt_count(gap)} TRADES\n"
                f"STRONGER: {side}\n"
                f"FIRST OBSERVED: {first_time}"
            )

        print(
            "\n============================================================",
            flush=True,
        )
        print(
            f"[NEW {metric} THRESHOLD]\n{message}",
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

        return

    # --------------------------------------------------------
    # SAME CONDITION -> NO DUPLICATE
    # --------------------------------------------------------

    if old["side"] == side:
        return

    # --------------------------------------------------------
    # OPPOSITE QUALIFYING SIDE
    # --------------------------------------------------------

    old_side = old["side"]
    first_time = now_ist()

    states[metric][symbol] = {
        "active": True,
        "side": side,
        "first_observed": first_time,
    }

    if metric == "VALUE":
        title = (
            f"COINGLASS {symbol} VALUE "
            f"{old_side}->{side}"
        )

        message = (
            "COINGLASS 1H LIQUIDATION VALUE\n\n"
            f"COIN: {symbol}\n"
            f"LONG: {fmt_money(long_value)}\n"
            f"SHORT: {fmt_money(short_value)}\n"
            f"GAP: {fmt_money(gap)}\n"
            f"STATE: {old_side} -> {side}\n"
            f"FIRST OBSERVED: {first_time}"
        )

    else:
        title = (
            f"COINGLASS {symbol} TRADES "
            f"{old_side}->{side}"
        )

        message = (
            "COINGLASS 1H LIQUIDATION TRADES\n\n"
            f"COIN: {symbol}\n"
            f"LONG TRADES: {fmt_count(long_value)}\n"
            f"SHORT TRADES: {fmt_count(short_value)}\n"
            f"GAP: {fmt_count(gap)} TRADES\n"
            f"STATE: {old_side} -> {side}\n"
            f"FIRST OBSERVED: {first_time}"
        )

    print(
        f"[SIDE CHANGE] {metric} | "
        f"{symbol} | {old_side}->{side}",
        flush=True,
    )

    send_pushover(
        title,
        message,
    )


# ============================================================
# PAGE HELPERS
# ============================================================

async def get_rendered_lines(page):
    body_text = await page.locator("body").inner_text(
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
# PARSER HELPERS
# ============================================================

def is_number_like(text):
    s = clean_text(text)

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


def is_integer_like(text):
    s = clean_text(text).replace(",", "")

    return bool(
        re.fullmatch(r"\d+", s)
    )


def is_price_like(text):
    return bool(
        re.fullmatch(
            r"\$\d[\d,]*(?:\.\d+)?",
            clean_text(text),
        )
    )


def is_percent_like(text):
    return bool(
        re.fullmatch(
            r"[+\-−]?\d+(?:\.\d+)?%",
            clean_text(text),
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
# VALUE PARSER
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
            return i

    raise RuntimeError(
        "VALUE liquidation header not found"
    )


def parse_value_rows(lines):
    header_index = find_value_header(lines)

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

    filtered = [
        item
        for item in search_lines
        if item not in ignored_exact
    ]

    results = []
    seen = set()

    i = 0

    while i < len(filtered):
        current = filtered[i]

        symbol = None
        symbol_index = None

        if (
            re.fullmatch(r"#?\d+", current)
            and i + 1 < len(filtered)
            and is_symbol_like(filtered[i + 1])
        ):
            symbol = filtered[i + 1].upper()
            symbol_index = i + 1

        elif is_symbol_like(current):
            symbol = current.upper()
            symbol_index = i

        if symbol is None or symbol in seen:
            i += 1
            continue

        numbers = []

        for j in range(
            symbol_index + 1,
            min(symbol_index + 20, len(filtered)),
        ):
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
                    "long": parse_number(long_raw),
                    "short": parse_number(short_raw),
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
# DROPDOWN
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

    visible_control = None

    for i in range(count):
        candidate = controls.nth(i)

        try:
            if await candidate.is_visible():
                visible_control = candidate
                break
        except Exception:
            pass

    if visible_control is None:
        raise RuntimeError(
            "No visible Liquidation Value control found"
        )

    await visible_control.click(
        timeout=10000
    )

    await page.wait_for_timeout(700)

    options = page.get_by_text(
        "Liquidation Trades",
        exact=True,
    )

    option_count = await options.count()

    visible_option = None

    for i in range(option_count):
        candidate = options.nth(i)

        try:
            if await candidate.is_visible():
                visible_option = candidate
                break
        except Exception:
            pass

    if visible_option is None:
        raise RuntimeError(
            "No visible Liquidation Trades option found"
        )

    await visible_option.click(
        timeout=10000
    )

    await page.wait_for_timeout(4000)

    print(
        "[DROPDOWN] TRADES selected",
        flush=True,
    )


# ============================================================
# TRADES PARSER
# ============================================================

def parse_trades_rows(lines):
    results = []
    seen = set()

    for i in range(len(lines)):
        symbol = lines[i].upper()

        if not is_symbol_like(symbol):
            continue

        if i + 10 >= len(lines):
            continue

        price = lines[i + 1]
        percent = lines[i + 2]

        if not is_price_like(price):
            continue

        if not is_percent_like(percent):
            continue

        trade_fields = lines[
            i + 3:
            i + 11
        ]

        if len(trade_fields) != 8:
            continue

        if not all(
            is_integer_like(x)
            for x in trade_fields
        ):
            continue

        if symbol in seen:
            continue

        long_1h = parse_number(
            trade_fields[0]
        )

        short_1h = parse_number(
            trade_fields[1]
        )

        seen.add(symbol)

        results.append(
            {
                "symbol": symbol,
                "long": long_1h,
                "short": short_1h,
            }
        )

        print(
            f"[PARSED] TRADES | "
            f"{symbol} | "
            f"1H L={fmt_count(long_1h)} | "
            f"1H S={fmt_count(short_1h)}",
            flush=True,
        )

        if len(results) >= TOP_N:
            break

    if not results:
        raise RuntimeError(
            "No TRADES rows parsed"
        )

    print(
        f"[DEBUG] TRADES parsed rows={len(results)}",
        flush=True,
    )

    return results


# ============================================================
# SCAN OUTPUT
# ============================================================

def process_value_rows(rows, allow_alert=True):
    print(
        f"\n[VALUE SCAN] "
        f"{now_ist()} | rows={len(rows)}",
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

        update_state(
            "VALUE",
            row["symbol"],
            row["long"],
            row["short"],
            allow_alert=allow_alert,
        )


def process_trades_rows(rows, allow_alert=True):
    print(
        f"\n[TRADES SCAN] "
        f"{now_ist()} | rows={len(rows)}",
        flush=True,
    )

    for row in rows:
        gap, side = get_gap(
            row["long"],
            row["short"],
        )

        print(
            f"{row['symbol']:8} "
            f"L={fmt_count(row['long']):>8} "
            f"S={fmt_count(row['short']):>8} "
            f"GAP={fmt_count(gap):>8} "
            f"{side}",
            flush=True,
        )

        update_state(
            "TRADES",
            row["symbol"],
            row["long"],
            row["short"],
            allow_alert=allow_alert,
        )


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

    # Fresh page navigation every 5-minute cycle.
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

    await wait_for_liquidation_section(
        page
    )

    # VALUE
    value_lines = await get_rendered_lines(
        page
    )

    value_rows = parse_value_rows(
        value_lines
    )

    process_value_rows(
        value_rows,
        allow_alert=bootstrap_complete,
    )

    # TRADES
    await select_liquidation_trades(
        page
    )

    trades_lines = await get_rendered_lines(
        page
    )

    trades_rows = parse_trades_rows(
        trades_lines
    )

    process_trades_rows(
        trades_rows,
        allow_alert=bootstrap_complete,
    )

    if not bootstrap_complete:
        bootstrap_complete = True
        print(
            "[BOOTSTRAP COMPLETE] Current VALUE/TRADES states seeded; "
            "future fresh crosses/side changes can alert.",
            flush=True,
        )

    print(
        "\n############################################################",
        flush=True,
    )

    print(
        f"[SCAN OK] {now_ist()} | "
        f"VALUE={len(value_rows)} | "
        f"TRADES={len(trades_rows)}",
        flush=True,
    )

    print(
        "############################################################",
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
        f"VALUE threshold: "
        f"{fmt_money(VALUE_THRESHOLD)}",
        flush=True,
    )

    print(
        f"TRADES threshold: "
        f"{TRADES_THRESHOLD:,}",
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
