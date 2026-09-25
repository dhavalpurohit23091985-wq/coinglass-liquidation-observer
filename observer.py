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

# BTC, ETH and SOL.
TARGET_SYMBOLS = {"BTC", "ETH", "SOL"}

VALUE_THRESHOLD = 1_000_000.0

IST = ZoneInfo("Asia/Kolkata")

PUSHOVER_USER_KEY = os.getenv("PUSHOVER_USER_KEY", "").strip()
PUSHOVER_APP_TOKEN = os.getenv("PUSHOVER_APP_TOKEN", "").strip()
STATE_FILE = os.getenv(
    "COINGLASS_STATE_FILE",
    "/tmp/coinglass_observer_state.json"
).strip()


# ============================================================
# STATE
# ============================================================

# BTC/ETH/SOL 4H signal state.
states = {
    "VALUE": {},
}

# Persist state locally so a normal worker restart can restore
# the previous VALUE side instead of treating the current side
# as a fresh bootstrap.
bootstrap_complete = False


def save_states():
    payload = {
        "states": states,
        "bootstrap_complete": bool(bootstrap_complete),
    }

    tmp_path = f"{STATE_FILE}.tmp"

    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                indent=2,
            )

        os.replace(tmp_path, STATE_FILE)

    except Exception as exc:
        print(
            f"[STATE SAVE FAILED] "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )


def load_states():
    global states, bootstrap_complete

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            payload = json.load(handle)

        loaded = payload.get("states")

        if not isinstance(loaded, dict):
            raise ValueError("states missing")

        bucket = loaded.get("VALUE", {})

        if not isinstance(bucket, dict):
            bucket = {}

        # Keep only BTC/ETH/SOL state.
        cleaned = {}

        for symbol, state in bucket.items():
            symbol_upper = str(symbol).upper().strip()

            if symbol_upper in TARGET_SYMBOLS:
                cleaned[symbol_upper] = state

        states["VALUE"] = cleaned

        bootstrap_complete = bool(
            payload.get("bootstrap_complete", True)
        )

        print(
            f"[STATE RESTORED] "
            f"VALUE={len(states['VALUE'])} | "
            f"bootstrap_complete={bootstrap_complete}",
            flush=True,
        )

        return True

    except FileNotFoundError:
        print(
            "[STATE RESTORE] No saved state found; "
            "first scan will bootstrap.",
            flush=True,
        )

    except Exception as exc:
        print(
            f"[STATE RESTORE FAILED] "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )

    bootstrap_complete = False
    return False


# ============================================================
# BASIC HELPERS
# ============================================================

def now_ist_dt():
    return datetime.now(IST)


def now_ist():
    return now_ist_dt().strftime(
        "%d-%m-%Y %H:%M:%S IST"
    )


def format_duration(start_time):
    if not start_time:
        return "0S"

    try:
        start_dt = datetime.strptime(
            start_time,
            "%d-%m-%Y %H:%M:%S IST",
        ).replace(tzinfo=IST)

        total_seconds = max(
            0,
            int(
                (
                    now_ist_dt() - start_dt
                ).total_seconds()
            ),
        )

        hours, remainder = divmod(
            total_seconds,
            3600,
        )

        minutes, seconds = divmod(
            remainder,
            60,
        )

        if hours > 0:
            return f"{hours}H {minutes}M {seconds}S"

        if minutes > 0:
            return f"{minutes}M {seconds}S"

        return f"{seconds}S"

    except Exception:
        return "0S"


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


def fmt_price(value):
    value = float(value or 0.0)

    if value <= 0:
        return "N/A"

    if value >= 1000:
        return f"${value:,.2f}"

    if value >= 1:
        return (
            f"${value:,.4f}"
            .rstrip("0")
            .rstrip(".")
        )

    return (
        f"${value:,.8f}"
        .rstrip("0")
        .rstrip(".")
    )


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
            "[PUSHOVER] Not configured - "
            "notification skipped",
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
            f"HTTP {response.status_code}",
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
# VALUE STATE / THRESHOLD ENGINE
# ============================================================

def update_value_state(symbol, long_4h, short_4h, price=0.0, allow_alert=True):
    symbol = symbol.upper().strip()
    signed_gap = float(long_4h or 0.0) - float(short_4h or 0.0)
    gap = abs(signed_gap)

    # Trading direction is opposite the liquidated side:
    # more LONG liquidations -> SELL; more SHORT liquidations -> BUY.
    if signed_gap >= VALUE_THRESHOLD:
        signal = "SELL"
        stronger = "LONG"
    elif signed_gap <= -VALUE_THRESHOLD:
        signal = "BUY"
        stronger = "SHORT"
    else:
        signal = None
        stronger = "NONE"

    old = states["VALUE"].get(symbol, {"active": False, "side": None, "first_observed": None})

    if signal is None:
        if old.get("active"):
            print(f"[CLEAR] {symbol} 4H | previous={old.get('side')} | gap={fmt_money(gap)} | {now_ist()}", flush=True)
        states["VALUE"][symbol] = {"active": False, "side": None, "first_observed": None}
        save_states()
        return

    if not old.get("active"):
        first_time = now_ist()
        states["VALUE"][symbol] = {"active": True, "side": signal, "first_observed": first_time}
        save_states()
        if not allow_alert:
            print(f"[BOOTSTRAP ACTIVE] {symbol} 4H | signal={signal} | stronger={stronger} | gap={fmt_money(gap)}", flush=True)
            return
        title = f"COINGLASS {symbol} 4H {signal} | GAP {fmt_money(gap)}"
        message = (
            f"COINGLASS {symbol} 4H LIQUIDATION\n\n"
            f"{symbol} PRICE: {fmt_price(price)}\n"
            f"4H LONG: {fmt_money(long_4h)}\n"
            f"4H SHORT: {fmt_money(short_4h)}\n"
            f"DIFFERENCE: {fmt_money(gap)}\n"
            f"MORE LIQUIDATED: {stronger}\n\n"
            f"ALERT: {signal}"
        )
        print(f"[NEW {symbol} 4H SIGNAL] {message}", flush=True)
        send_pushover(title, message)
        return

    if old.get("side") == signal:
        return

    old_side = old.get("side")
    previous_active_for = format_duration(old.get("first_observed"))
    first_time = now_ist()
    states["VALUE"][symbol] = {"active": True, "side": signal, "first_observed": first_time}
    save_states()
    title = f"COINGLASS {symbol} 4H {old_side}->{signal}"
    message = (
        f"COINGLASS {symbol} 4H LIQUIDATION\n\n"
        f"{symbol} PRICE: {fmt_price(price)}\n"
        f"4H LONG: {fmt_money(long_4h)}\n"
        f"4H SHORT: {fmt_money(short_4h)}\n"
        f"DIFFERENCE: {fmt_money(gap)}\n"
        f"MORE LIQUIDATED: {stronger}\n\n"
        f"ALERT: {old_side} -> {signal}\n"
        f"PREVIOUS ACTIVE FOR: {previous_active_for}"
    )
    print(f"[{symbol} 4H REVERSAL] {old_side}->{signal} | gap={fmt_money(gap)}", flush=True)
    send_pushover(title, message)


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
# VALUE PARSER HELPERS
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


# ============================================================
# BTC / ETH / SOL 4H PARSER
# ============================================================

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
            and is_symbol_like(
                filtered[i + 1]
            )
        ):
            symbol = (
                filtered[i + 1]
                .upper()
            )

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

        # We only need BTC, ETH and SOL.
        # Skip all other assets immediately.
        if symbol not in TARGET_SYMBOLS:
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

        if len(numbers) >= 8:
            long_raw = numbers[2]
            short_raw = numbers[3]

            long_4h_raw = numbers[4]
            short_4h_raw = numbers[5]

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
                    "price": parse_number(numbers[0]),
                    "long_4h": parse_number(long_4h_raw),
                    "short_4h": parse_number(short_4h_raw),
                }
            )

            print(
                f"[PARSED] {symbol} | "
                f"4H L={long_4h_raw} | "
                f"4H S={short_4h_raw}",
                flush=True,
            )

        i += 1

    if not results:
        raise RuntimeError(
            "No BTC/ETH/SOL VALUE rows parsed"
        )

    print(
        f"[DEBUG] TARGET VALUE rows="
        f"{len(results)} | "
        f"symbols="
        f"{','.join(row['symbol'] for row in results)}",
        flush=True,
    )

    return results


# ============================================================
# BTC / ETH / SOL 4H PROCESSING
# ============================================================

def process_value_rows(rows, allow_alert=True):
    print(f"\n[BTC/ETH/SOL 4H SCAN] {now_ist()} | rows={len(rows)}", flush=True)

    for row in rows:
        symbol = str(row.get("symbol", "")).upper().strip()

        if symbol not in TARGET_SYMBOLS:
            continue

        long_4h = float(row.get("long_4h", 0.0) or 0.0)
        short_4h = float(row.get("short_4h", 0.0) or 0.0)
        gap = abs(long_4h - short_4h)
        stronger = "LONG" if long_4h > short_4h else "SHORT" if short_4h > long_4h else "EVEN"

        print(
            f"{symbol} 4H L={fmt_money(long_4h)} "
            f"S={fmt_money(short_4h)} "
            f"GAP={fmt_money(gap)} "
            f"STRONGER={stronger}",
            flush=True,
        )

        update_value_state(
            symbol,
            long_4h,
            short_4h,
            price=row.get("price", 0.0),
            allow_alert=allow_alert,
        )


# ============================================================
# ONE SCAN
# ============================================================

async def scan_once(page):
    global bootstrap_complete

    print(
        "\n"
        "############################################################\n"
        f"[SCAN START] {now_ist()}\n"
        "############################################################",
        flush=True,
    )

    # Fresh navigation every 1-minute cycle.
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

    await page.wait_for_timeout(8000)

    print(
        f"[PAGE] title="
        f"{await page.title()}",
        flush=True,
    )

    await wait_for_liquidation_section(
        page
    )

    # VALUE ONLY
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

    if not bootstrap_complete:
        bootstrap_complete = True
        save_states()

        print(
            "[BOOTSTRAP COMPLETE] "
            "Current BTC/ETH/SOL 4H states seeded; "
            "future fresh crosses/side changes "
            "can alert.",
            flush=True,
        )

    print(
        "\n"
        "############################################################",
        flush=True,
    )

    print(
        f"[SCAN OK] "
        f"{now_ist()} | "
        f"VALUE={len(value_rows)}",
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
        "COINGLASS BTC/ETH/SOL 4H "
        "OBSERVER STARTING",
        flush=True,
    )

    load_states()

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
        f"BTC/ETH/SOL 4H difference threshold: "
        f"{fmt_money(VALUE_THRESHOLD)}",
        flush=True,
    )

    print(
        "MODE: BTC/ETH/SOL 4H LONG-vs-SHORT ONLY",
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
