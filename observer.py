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
# STATE ENGINE
# ============================================================

def update_state(metric, symbol, long_value, short_value):

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

        print(
            "\n"
            "============================================================",
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
            f"NEW FIRST OBSERVED: {first_time}\n"
            "============================================================",
            flush=True,
        )

        return

    # Same active condition = no duplicate
    metric_states[symbol] = old


# ============================================================
# FIND TOTAL LIQUIDATIONS TABLE
# ============================================================

async def find_liquidation_table(page):

    # First wait for the header.
    await page.get_by_text(
        "1h Long",
        exact=True,
    ).last.wait_for(
        state="visible",
        timeout=30000,
    )

    tables = page.locator("table")

    count = await tables.count()

    print(
        f"[DEBUG] HTML tables found: {count}",
        flush=True,
    )

    for i in range(count):

        table = tables.nth(i)

        try:

            text = clean_text(
                await table.inner_text(
                    timeout=3000
                )
            )

        except Exception:
            continue

        low = text.lower()

        if (
            "1h long" in low
            and "1h short" in low
            and "4h long" in low
            and "4h short" in low
        ):

            print(
                f"[DEBUG] Liquidation table index={i}",
                flush=True,
            )

            return table

    raise RuntimeError(
        "Total Liquidations table not found"
    )


# ============================================================
# EXTRACT STANDARD ROW
# ============================================================

async def extract_row_from_cells(cells):

    cell_count = await cells.count()

    if cell_count < 5:
        return None

    texts = []

    for j in range(cell_count):

        try:

            txt = clean_text(
                await cells.nth(j).inner_text(
                    timeout=2000
                )
            )

        except Exception:
            txt = ""

        texts.append(txt)

    # Layout:
    # Ranking | Assets | Price | 24h% | 1h Long | 1h Short
    #
    # OR:
    # Assets | Price | 24h% | 1h Long | 1h Short

    if len(texts) >= 6 and re.fullmatch(
        r"#?\d+",
        texts[0],
    ):

        asset_index = 1
        long_index = 4
        short_index = 5

    elif len(texts) >= 5:

        asset_index = 0
        long_index = 3
        short_index = 4

    else:
        return None

    if max(
        asset_index,
        long_index,
        short_index,
    ) >= len(texts):
        return None

    asset_text = texts[asset_index]
    long_text = texts[long_index]
    short_text = texts[short_index]

    tokens = re.findall(
        r"[A-Z][A-Z0-9]{1,14}",
        asset_text.upper(),
    )

    ignored = {
        "USD",
        "USDT",
        "PRICE",
        "LONG",
        "SHORT",
        "ASSETS",
    }

    tokens = [
        token
        for token in tokens
        if token not in ignored
    ]

    if not tokens:
        return None

    if not re.search(r"\d", long_text):
        return None

    if not re.search(r"\d", short_text):
        return None

    symbol = tokens[-1]

    return {
        "symbol": symbol,
        "long": parse_number(long_text),
        "short": parse_number(short_text),
        "long_raw": long_text,
        "short_raw": short_text,
    }


# ============================================================
# READ 1H ROWS
# ============================================================

async def read_1h_rows(page):

    table = await find_liquidation_table(page)

    results = []

    # --------------------------------------------------------
    # METHOD 1 — STANDARD TR
    # --------------------------------------------------------

    rows = table.locator("tr")

    row_count = await rows.count()

    print(
        f"[DEBUG] TR rows found: {row_count}",
        flush=True,
    )

    for i in range(row_count):

        row = rows.nth(i)
        cells = row.locator("td")

        parsed = await extract_row_from_cells(
            cells
        )

        if parsed:
            results.append(parsed)

        if len(results) >= TOP_N:
            break

    if results:

        print(
            f"[DEBUG] Parsed using TR: "
            f"{len(results)} rows",
            flush=True,
        )

        return results

    # --------------------------------------------------------
    # METHOD 2 — ROLE ROW
    # --------------------------------------------------------

    print(
        "[DEBUG] TR parsing empty. "
        "Trying role=row...",
        flush=True,
    )

    role_rows = table.locator(
        '[role="row"]'
    )

    role_count = await role_rows.count()

    print(
        f"[DEBUG] role=row found: {role_count}",
        flush=True,
    )

    for i in range(role_count):

        row = role_rows.nth(i)

        cells = row.locator(
            '[role="cell"], '
            '[role="gridcell"], '
            'td'
        )

        parsed = await extract_row_from_cells(
            cells
        )

        if parsed:
            results.append(parsed)

        if len(results) >= TOP_N:
            break

    if results:

        print(
            f"[DEBUG] Parsed using role rows: "
            f"{len(results)} rows",
            flush=True,
        )

        return results

    # --------------------------------------------------------
    # METHOD 3 — GENERIC DIV FALLBACK
    # --------------------------------------------------------

    print(
        "[DEBUG] Trying generic rendered rows...",
        flush=True,
    )

    raw_rows = await table.evaluate(
        """
        (root) => {

            const output = [];

            const elements = Array.from(
                root.querySelectorAll(
                    'tr, [role="row"], div'
                )
            );

            for (const el of elements) {

                const children =
                    Array.from(el.children);

                if (
                    children.length < 5 ||
                    children.length > 15
                ) {
                    continue;
                }

                const cells = children.map(
                    x => (
                        x.innerText || ""
                    ).trim()
                );

                const joined =
                    cells.join(" ");

                if (
                    /1h\\s*long/i.test(joined) &&
                    /1h\\s*short/i.test(joined)
                ) {
                    continue;
                }

                const numeric =
                    cells.filter(
                        x => /\\d/.test(x)
                    ).length;

                if (numeric < 3) {
                    continue;
                }

                output.push(cells);
            }

            return output;
        }
        """
    )

    print(
        f"[DEBUG] Generic candidates: "
        f"{len(raw_rows)}",
        flush=True,
    )

    seen = set()

    for texts in raw_rows:

        if len(texts) < 5:
            continue

        candidates = []

        if len(texts) >= 6:
            candidates.append((1, 4, 5))

        candidates.append((0, 3, 4))

        for (
            asset_index,
            long_index,
            short_index,
        ) in candidates:

            if max(
                asset_index,
                long_index,
                short_index,
            ) >= len(texts):
                continue

            asset_text = clean_text(
                texts[asset_index]
            )

            long_text = clean_text(
                texts[long_index]
            )

            short_text = clean_text(
                texts[short_index]
            )

            tokens = re.findall(
                r"[A-Z][A-Z0-9]{1,14}",
                asset_text.upper(),
            )

            ignored = {
                "USD",
                "USDT",
                "PRICE",
                "LONG",
                "SHORT",
                "ASSETS",
            }

            tokens = [
                token
                for token in tokens
                if token not in ignored
            ]

            if not tokens:
                continue

            symbol = tokens[-1]

            if symbol in seen:
                continue

            if not re.search(
                r"\d",
                long_text,
            ):
                continue

            if not re.search(
                r"\d",
                short_text,
            ):
                continue

            seen.add(symbol)

            results.append(
                {
                    "symbol": symbol,
                    "long": parse_number(
                        long_text
                    ),
                    "short": parse_number(
                        short_text
                    ),
                    "long_raw": long_text,
                    "short_raw": short_text,
                }
            )

            break

        if len(results) >= TOP_N:
            break

    if not results:

        body_text = clean_text(
            await page.locator(
                "body"
            ).inner_text()
        )

        print(
            "[DEBUG] BODY contains "
            f"'Total Liquidations'="
            f"{'Total Liquidations' in body_text}",
            flush=True,
        )

        print(
            "[DEBUG] BODY contains "
            f"'1h Long'="
            f"{'1h Long' in body_text}",
            flush=True,
        )

        raise RuntimeError(
            "No liquidation rows parsed"
        )

    print(
        f"[DEBUG] Parsed using generic fallback: "
        f"{len(results)} rows",
        flush=True,
    )

    return results


# ============================================================
# DROPDOWN VALUE -> TRADES
# ============================================================

async def select_liquidation_trades(page):

    print(
        "[DROPDOWN] Switching VALUE -> TRADES",
        flush=True,
    )

    value_controls = page.get_by_text(
        "Liquidation Value",
        exact=True,
    )

    count = await value_controls.count()

    print(
        f"[DEBUG] Liquidation Value controls={count}",
        flush=True,
    )

    if count == 0:
        raise RuntimeError(
            "Liquidation Value dropdown not found"
        )

    value_control = value_controls.last

    await value_control.scroll_into_view_if_needed()

    await value_control.click(
        timeout=15000
    )

    trades_options = page.get_by_text(
        "Liquidation Trades",
        exact=True,
    )

    trade_count = await trades_options.count()

    print(
        f"[DEBUG] Liquidation Trades options="
        f"{trade_count}",
        flush=True,
    )

    if trade_count == 0:
        raise RuntimeError(
            "Liquidation Trades option not found"
        )

    trades_option = trades_options.last

    await trades_option.wait_for(
        state="visible",
        timeout=10000,
    )

    await trades_option.click()

    await page.wait_for_timeout(3500)

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

    common = value_symbols.intersection(
        trade_symbols
    )

    if len(common) < 3:
        raise RuntimeError(
            "VALUE/TRADES tables "
            "do not appear to match"
        )


# ============================================================
# PRINT SCAN
# ============================================================

def print_scan(metric, rows):

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
    # LOAD / REFRESH COINGLASS
    # --------------------------------------------------------

    response = await page.goto(
        URL,
        wait_until="domcontentloaded",
        timeout=60000,
    )

    # --------------------------------------------------------
    # HTTP DIAGNOSTICS
    # --------------------------------------------------------

    print(
        f"[HTTP] status="
        f"{response.status if response else 'NO RESPONSE'}",
        flush=True,
    )

    print(
        f"[HTTP] final_url={page.url}",
        flush=True,
    )

    # Give dynamic page time to render.
    await page.wait_for_timeout(8000)

    print(
        f"[PAGE] title={await page.title()}",
        flush=True,
    )

    # --------------------------------------------------------
    # VALUE
    # --------------------------------------------------------

    value_rows = await read_1h_rows(
        page
    )

    print_scan(
        "VALUE",
        value_rows,
    )

    # --------------------------------------------------------
    # SWITCH VALUE -> TRADES
    # --------------------------------------------------------

    await select_liquidation_trades(
        page
    )

    # --------------------------------------------------------
    # TRADES
    # --------------------------------------------------------

    trade_rows = await read_1h_rows(
        page
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
    # UPDATE VALUE STATES
    # --------------------------------------------------------

    for row in value_rows:

        update_state(
            "VALUE",
            row["symbol"],
            row["long"],
            row["short"],
        )

    # --------------------------------------------------------
    # UPDATE TRADES STATES
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
