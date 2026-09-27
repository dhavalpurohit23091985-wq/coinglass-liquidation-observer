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

URL = "https://www.coinglass.com/inflow-outflow-history"

# Page can be checked frequently, but a timestamp is consumed only once.
SCAN_SECONDS = 30

THRESHOLD = 100_000_000.0  # $100M

IST = ZoneInfo("Asia/Kolkata")

PUSHOVER_USER_KEY = os.getenv("PUSHOVER_USER_KEY", "").strip()
PUSHOVER_APP_TOKEN = os.getenv("PUSHOVER_APP_TOKEN", "").strip()

# Worker-local state.
# Same-timestamp protection works continuously while worker is running.
STATE_FILE = "/tmp/btc_netflow_state.json"


# ============================================================
# STATE
# ============================================================

def default_state():
    return {
        "running_total": 0.0,
        "direction": "NONE",       # NONE / BUY / SELL
        "last_timestamp": "",
        "last_value": 0.0,
        "last_trigger": "NONE",
        "last_trigger_time": ""
    }


def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)

            state = default_state()
            state.update(saved)
            return state
    except Exception as exc:
        print(f"[STATE LOAD ERROR] {exc}", flush=True)

    return default_state()


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as exc:
        print(f"[STATE SAVE ERROR] {exc}", flush=True)


state = load_state()


# ============================================================
# HELPERS
# ============================================================

def fmt_money(value):
    sign = "+" if value > 0 else "-" if value < 0 else ""
    n = abs(value)

    if n >= 1_000_000_000:
        return f"{sign}${n / 1_000_000_000:.2f}B"

    if n >= 1_000_000:
        return f"{sign}${n / 1_000_000:.2f}M"

    if n >= 1_000:
        return f"{sign}${n / 1_000:.2f}K"

    return f"{sign}${n:.2f}"


def parse_money(text):
    """
    Examples:
      $20.50M
      -$20.50M
      $500.25K
      -$1.20B
    """
    if not text:
        return None

    s = (
        text.strip()
        .replace(",", "")
        .replace("−", "-")
        .replace("–", "-")
        .replace("—", "-")
        .replace(" ", "")
    )

    match = re.search(r"(-?)\$?([0-9]*\.?[0-9]+)([KMB]?)", s, re.I)

    if not match:
        return None

    sign = -1.0 if match.group(1) == "-" else 1.0
    number = float(match.group(2))
    suffix = match.group(3).upper()

    multiplier = {
        "": 1.0,
        "K": 1_000.0,
        "M": 1_000_000.0,
        "B": 1_000_000_000.0
    }[suffix]

    return sign * number * multiplier


def pushover(title, message):
    if not PUSHOVER_USER_KEY or not PUSHOVER_APP_TOKEN:
        print("[PUSHOVER] Keys missing - notification skipped", flush=True)
        return

    try:
        response = requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": PUSHOVER_APP_TOKEN,
                "user": PUSHOVER_USER_KEY,
                "title": title,
                "message": message
            },
            timeout=20
        )

        print(
            f"[PUSHOVER] status={response.status_code}",
            flush=True
        )

    except Exception as exc:
        print(f"[PUSHOVER ERROR] {exc}", flush=True)


# ============================================================
# PAGE SETUP
# ============================================================

async def select_five_minute(page):
    """
    CoinGlass has a history interval dropdown.
    We explicitly try to set it to 5 minute instead of relying
    on the page default.
    """

    # First try text-based controls.
    candidates = [
        page.get_by_text("5 minute", exact=True),
        page.get_by_text("5 minutes", exact=True),
        page.get_by_text("5 min", exact=True),
    ]

    for candidate in candidates:
        try:
            if await candidate.count() > 0:
                await candidate.last.click(timeout=3000)
                await page.wait_for_timeout(1500)
                print("[INTERVAL] 5 minute selected", flush=True)
                return True
        except Exception:
            pass

    # Try buttons that may currently show another interval.
    try:
        buttons = page.locator("button")

        count = await buttons.count()

        for i in range(count):
            button = buttons.nth(i)

            try:
                txt = (await button.inner_text()).strip().lower()
            except Exception:
                continue

            if txt in {
                "1 hour",
                "1 day",
                "4 hour",
                "15 minute",
                "30 minute",
                "5 minute"
            }:
                try:
                    await button.click(timeout=2000)
                    await page.wait_for_timeout(500)

                    option = page.get_by_text("5 minute", exact=True)

                    if await option.count() > 0:
                        await option.last.click(timeout=3000)
                        await page.wait_for_timeout(1500)

                        print(
                            "[INTERVAL] 5 minute selected via dropdown",
                            flush=True
                        )
                        return True

                except Exception:
                    pass

    except Exception:
        pass

    print(
        "[INTERVAL WARNING] Could not explicitly confirm 5 minute dropdown",
        flush=True
    )

    return False


# ============================================================
# TABLE PARSER
# ============================================================

async def get_latest_btc_5m(page):
    """
    Expected table structure:

    Time | 5 minute | 15 minute | 30 minute | 1 hour | ...

    We only consume:
      Time
      5 minute

    Returns:
      (timestamp_text, value_float)
    """

    rows = page.locator("table tbody tr")

    row_count = await rows.count()

    if row_count == 0:
        # CoinGlass may render div-based tables.
        # Fall back to generic row role.
        rows = page.get_by_role("row")
        row_count = await rows.count()

    print(f"[TABLE] rows found={row_count}", flush=True)

    for i in range(row_count):
        row = rows.nth(i)

        try:
            cells = row.locator("td")

            cell_count = await cells.count()

            if cell_count < 2:
                cells = row.get_by_role("cell")
                cell_count = await cells.count()

            if cell_count < 2:
                continue

            timestamp_text = (await cells.nth(0).inner_text()).strip()
            value_text = (await cells.nth(1).inner_text()).strip()

            # Skip header-like rows.
            if "time" in timestamp_text.lower():
                continue

            value = parse_money(value_text)

            if value is None:
                continue

            # Timestamp should contain digits such as:
            # 09-27 05:05
            if not re.search(r"\d", timestamp_text):
                continue

            return timestamp_text, value

        except Exception:
            continue

    return None, None


# ============================================================
# SIGNAL ENGINE
# ============================================================

def process_new_contribution(timestamp_text, value):
    global state

    # --------------------------------------------------------
    # DUPLICATE TIMESTAMP LOCK
    # --------------------------------------------------------

    if timestamp_text == state["last_timestamp"]:
        print(
            f"[SAME ROW] {timestamp_text} already consumed - SKIP",
            flush=True
        )
        return

    old_total = float(state["running_total"])
    new_total = old_total + value

    print(
        f"[NEW 5M] {timestamp_text}"
        f" | contribution={fmt_money(value)}"
        f" | before={fmt_money(old_total)}"
        f" | after={fmt_money(new_total)}"
        f" | state={state['direction']}",
        flush=True
    )

    state["last_timestamp"] = timestamp_text
    state["last_value"] = value
    state["running_total"] = new_total

    trigger = None

    # ========================================================
    # STRICT ALTERNATION
    #
    # NONE:
    #   +100M -> BUY
    #   -100M -> SELL
    #
    # BUY:
    #   only -100M SELL is eligible
    #
    # SELL:
    #   only +100M BUY is eligible
    # ========================================================

    direction = state["direction"]

    if direction == "NONE":

        if new_total >= THRESHOLD:
            trigger = "BUY"

        elif new_total <= -THRESHOLD:
            trigger = "SELL"

    elif direction == "BUY":

        if new_total <= -THRESHOLD:
            trigger = "SELL"

        elif new_total >= THRESHOLD:
            print(
                "[BLOCKED] +$100M reached but BUY already active "
                "- waiting for SELL",
                flush=True
            )

    elif direction == "SELL":

        if new_total >= THRESHOLD:
            trigger = "BUY"

        elif new_total <= -THRESHOLD:
            print(
                "[BLOCKED] -$100M reached but SELL already active "
                "- waiting for BUY",
                flush=True
            )

    # --------------------------------------------------------
    # TRIGGER
    # --------------------------------------------------------

    if trigger:
        trigger_total = new_total

        state["direction"] = trigger
        state["last_trigger"] = trigger
        state["last_trigger_time"] = timestamp_text

        # Exact locked rule:
        # trigger -> running total RESET TO ZERO
        state["running_total"] = 0.0

        print(
            "====================================================",
            flush=True
        )
        print(
            f"[BTC NETFLOW {trigger}]"
            f" time={timestamp_text}"
            f" | contribution={fmt_money(value)}"
            f" | total={fmt_money(trigger_total)}"
            f" | RESET=0",
            flush=True
        )
        print(
            "====================================================",
            flush=True
        )

        pushover(
            f"BTC NETFLOW | {trigger}",
            (
                f"{trigger} CONDITION\n"
                f"Time: {timestamp_text}\n"
                f"5M contribution: {fmt_money(value)}\n"
                f"Running total: {fmt_money(trigger_total)}\n"
                f"Threshold: ±$100.00M\n"
                f"Reset: $0\n"
                f"Next eligible: "
                f"{'SELL' if trigger == 'BUY' else 'BUY'}"
            )
        )

    save_state(state)


# ============================================================
# MAIN
# ============================================================

async def main():
    print("====================================================", flush=True)
    print("BTC FUTURES NETFLOW OBSERVER", flush=True)
    print("CoinGlass Futures Inflow/Outflow History", flush=True)
    print("Interval: 5 minute", flush=True)
    print("Contribution: BTC 5 minute Netflow", flush=True)
    print("Threshold: +$100M BUY / -$100M SELL", flush=True)
    print("Trigger: reset running total to zero", flush=True)
    print("State: strict BUY <-> SELL alternation", flush=True)
    print("====================================================", flush=True)

    print(
        f"[STATE] total={fmt_money(float(state['running_total']))}"
        f" | direction={state['direction']}"
        f" | last_timestamp={state['last_timestamp'] or 'NONE'}",
        flush=True
    )

    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu"
            ]
        )

        context = await browser.new_context(
            viewport={"width": 1600, "height": 1000},
            locale="en-US"
        )

        page = await context.new_page()

        while True:

            try:
                print(
                    f"\n[SCAN] {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}",
                    flush=True
                )

                await page.goto(
                    URL,
                    wait_until="domcontentloaded",
                    timeout=90000
                )

                await page.wait_for_timeout(6000)

                await select_five_minute(page)

                await page.wait_for_timeout(3000)

                timestamp_text, value = await get_latest_btc_5m(page)

                if timestamp_text is None or value is None:
                    print(
                        "[PARSE WARNING] Latest BTC 5M row not found",
                        flush=True
                    )

                else:
                    print(
                        f"[LATEST] {timestamp_text}"
                        f" | BTC 5M={fmt_money(value)}",
                        flush=True
                    )

                    process_new_contribution(
                        timestamp_text,
                        value
                    )

            except Exception as exc:
                print(
                    f"[SCAN ERROR] {type(exc).__name__}: {exc}",
                    flush=True
                )

            await asyncio.sleep(SCAN_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
