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

SCAN_SECONDS = 30

# +$100M BUY condition / -$100M SELL condition
THRESHOLD = 100_000_000.0

IST = ZoneInfo("Asia/Kolkata")

PUSHOVER_USER_KEY = os.getenv("PUSHOVER_USER_KEY", "").strip()
PUSHOVER_APP_TOKEN = os.getenv("PUSHOVER_APP_TOKEN", "").strip()

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
# MONEY HELPERS
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

    match = re.search(
        r"([+-]?)\$?([0-9]*\.?[0-9]+)([KMB]?)",
        s,
        re.I
    )

    if not match:
        return None

    sign_text = match.group(1)
    number = float(match.group(2))
    suffix = match.group(3).upper()

    sign = -1.0 if sign_text == "-" else 1.0

    multiplier = {
        "": 1.0,
        "K": 1_000.0,
        "M": 1_000_000.0,
        "B": 1_000_000_000.0
    }[suffix]

    return sign * number * multiplier


# ============================================================
# PUSHOVER
# ============================================================

def pushover(title, message):
    if not PUSHOVER_USER_KEY or not PUSHOVER_APP_TOKEN:
        print(
            "[PUSHOVER] Keys missing - notification skipped",
            flush=True
        )
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
        print(
            f"[PUSHOVER ERROR] {exc}",
            flush=True
        )


# ============================================================
# PAGE HELPERS
# ============================================================

async def wait_for_history_section(page):
    """
    Wait until BTC Futures Inflow/Outflow History appears.
    """

    try:
        await page.get_by_text(
            "BTC Futures Inflow/Outflow History",
            exact=False
        ).first.wait_for(
            state="visible",
            timeout=30000
        )

        print(
            "[PAGE] BTC Futures Inflow/Outflow History found",
            flush=True
        )

        return True

    except Exception:
        print(
            "[PAGE WARNING] History heading not found",
            flush=True
        )

        return False


async def select_five_minute(page):
    """
    IMPORTANT:
    Every page reload may restore another interval.

    Therefore every scan:
      1. locate History section
      2. inspect buttons near that section
      3. open interval dropdown
      4. choose 5 minute
      5. wait for table to refresh
    """

    print(
        "[INTERVAL] Selecting 5 minute...",
        flush=True
    )

    # --------------------------------------------------------
    # Locate History heading
    # --------------------------------------------------------

    heading = page.get_by_text(
        "BTC Futures Inflow/Outflow History",
        exact=False
    ).first

    try:
        await heading.wait_for(
            state="visible",
            timeout=15000
        )

    except Exception:
        print(
            "[INTERVAL ERROR] History heading unavailable",
            flush=True
        )
        return False

    # --------------------------------------------------------
    # First method:
    # find visible "5 minute" control/text around the page.
    #
    # If 5 minute is already selected after reload, this also
    # confirms it.
    # --------------------------------------------------------

    try:
        five_text = page.get_by_text(
            "5 minute",
            exact=True
        )

        count = await five_text.count()

        if count > 0:
            # Prefer the last visible match because chart controls
            # are above and History controls are lower on the page.
            for i in range(count - 1, -1, -1):
                candidate = five_text.nth(i)

                try:
                    if await candidate.is_visible():
                        await candidate.click(
                            timeout=3000
                        )

                        await page.wait_for_timeout(700)

                        # Dropdown may have opened.
                        # Look again for a visible 5-minute option.
                        options = page.get_by_text(
                            "5 minute",
                            exact=True
                        )

                        option_count = await options.count()

                        if option_count > count:
                            for j in range(
                                option_count - 1,
                                -1,
                                -1
                            ):
                                option = options.nth(j)

                                try:
                                    if await option.is_visible():
                                        await option.click(
                                            timeout=3000
                                        )

                                        await page.wait_for_timeout(
                                            2000
                                        )

                                        print(
                                            "[INTERVAL] 5 minute selected",
                                            flush=True
                                        )

                                        return True

                                except Exception:
                                    continue

                        # If clicking did not create another option,
                        # it may already have been selected.
                        print(
                            "[INTERVAL] 5 minute control confirmed",
                            flush=True
                        )

                        return True

                except Exception:
                    continue

    except Exception:
        pass

    # --------------------------------------------------------
    # Second method:
    # inspect buttons/select-like controls after History heading.
    # --------------------------------------------------------

    try:
        buttons = page.locator("button")
        button_count = await buttons.count()

        possible_intervals = {
            "5 minute",
            "15 minute",
            "30 minute",
            "1 hour",
            "4 hour",
            "1 day"
        }

        interval_buttons = []

        for i in range(button_count):
            button = buttons.nth(i)

            try:
                if not await button.is_visible():
                    continue

                text = (
                    await button.inner_text()
                ).strip().lower()

                if text in possible_intervals:
                    interval_buttons.append(button)

            except Exception:
                continue

        # History interval selector is normally lower on page,
        # so use the last matching interval button.
        if interval_buttons:
            interval_button = interval_buttons[-1]

            current_text = (
                await interval_button.inner_text()
            ).strip()

            print(
                f"[INTERVAL] Current control={current_text}",
                flush=True
            )

            await interval_button.click(
                timeout=5000
            )

            await page.wait_for_timeout(500)

            option = page.get_by_text(
                "5 minute",
                exact=True
            )

            option_count = await option.count()

            for i in range(
                option_count - 1,
                -1,
                -1
            ):
                item = option.nth(i)

                try:
                    if await item.is_visible():
                        await item.click(
                            timeout=5000
                        )

                        await page.wait_for_timeout(
                            2000
                        )

                        print(
                            "[INTERVAL] 5 minute selected via dropdown",
                            flush=True
                        )

                        return True

                except Exception:
                    continue

    except Exception as exc:
        print(
            f"[INTERVAL METHOD2 ERROR] {exc}",
            flush=True
        )

    # --------------------------------------------------------
    # Third method:
    # native select elements, if CoinGlass changes rendering.
    # --------------------------------------------------------

    try:
        selects = page.locator("select")
        select_count = await selects.count()

        for i in range(select_count):
            select = selects.nth(i)

            try:
                options = await select.locator(
                    "option"
                ).all_inner_texts()

                normalized = [
                    x.strip().lower()
                    for x in options
                ]

                if "5 minute" in normalized:
                    idx = normalized.index(
                        "5 minute"
                    )

                    values = await select.locator(
                        "option"
                    ).evaluate_all(
                        """
                        els => els.map(
                            e => e.value
                        )
                        """
                    )

                    await select.select_option(
                        values[idx]
                    )

                    await page.wait_for_timeout(
                        2000
                    )

                    print(
                        "[INTERVAL] 5 minute selected via native select",
                        flush=True
                    )

                    return True

            except Exception:
                continue

    except Exception:
        pass

    print(
        "[INTERVAL ERROR] Could not select/confirm 5 minute",
        flush=True
    )

    return False


# ============================================================
# HISTORY DATA PARSER
# ============================================================

async def get_latest_btc_5m(page):
    """
    CoinGlass screenshot structure:

    Time | 5 minute | 15 minute | 30 minute | 1 hour | ...

    CoinGlass does not necessarily use a normal HTML <table>.
    Therefore we parse rendered page text.

    We want ONLY:
        latest timestamp
        first money value after that timestamp = 5-minute value
    """

    try:
        body_text = await page.locator(
            "body"
        ).inner_text()

    except Exception as exc:
        print(
            f"[BODY ERROR] {exc}",
            flush=True
        )
        return None, None

    if not body_text:
        return None, None

    # --------------------------------------------------------
    # Find History section only.
    # This prevents chart/header numbers being mistaken as rows.
    # --------------------------------------------------------

    marker = "BTC Futures Inflow/Outflow History"

    marker_index = body_text.find(marker)

    if marker_index >= 0:
        history_text = body_text[
            marker_index:
        ]
    else:
        history_text = body_text

    # Normalize minus signs.
    history_text = (
        history_text
        .replace("−", "-")
        .replace("–", "-")
        .replace("—", "-")
    )

    # --------------------------------------------------------
    # Expected timestamp:
    # 09-27 06:15
    #
    # Capture timestamp followed by nearby rendered content.
    # --------------------------------------------------------

    timestamp_pattern = re.compile(
        r"(\d{2}-\d{2}\s+\d{2}:\d{2})"
    )

    matches = list(
        timestamp_pattern.finditer(
            history_text
        )
    )

    print(
        f"[PARSE] timestamp matches={len(matches)}",
        flush=True
    )

    if not matches:
        preview = history_text[:1000].replace(
            "\n",
            " | "
        )

        print(
            f"[PARSE DEBUG] {preview}",
            flush=True
        )

        return None, None

    # Page is newest -> oldest.
    # First timestamp after History heading = latest row.
    first_match = matches[0]

    timestamp_text = first_match.group(1)

    row_start = first_match.end()

    # End at next timestamp if present.
    if len(matches) > 1:
        row_end = matches[1].start()
    else:
        row_end = min(
            len(history_text),
            row_start + 500
        )

    row_text = history_text[
        row_start:row_end
    ]

    # --------------------------------------------------------
    # First money value after timestamp = 5-minute column.
    # --------------------------------------------------------

    money_pattern = re.compile(
        r"([+-]?\$[0-9,.]+(?:\.[0-9]+)?[KMB]?)",
        re.I
    )

    money_matches = money_pattern.findall(
        row_text
    )

    print(
        f"[ROW] time={timestamp_text}"
        f" | money_cells={len(money_matches)}",
        flush=True
    )

    if not money_matches:
        debug_row = row_text[:500].replace(
            "\n",
            " | "
        )

        print(
            f"[ROW DEBUG] {debug_row}",
            flush=True
        )

        return None, None

    five_minute_text = money_matches[0]

    value = parse_money(
        five_minute_text
    )

    if value is None:
        return None, None

    print(
        f"[LATEST] {timestamp_text}"
        f" | BTC 5M={five_minute_text}"
        f" | parsed={fmt_money(value)}",
        flush=True
    )

    return timestamp_text, value


# ============================================================
# SIGNAL ENGINE
# ============================================================

def process_new_contribution(
    timestamp_text,
    value
):
    global state

    # --------------------------------------------------------
    # SAME TIMESTAMP MUST NEVER BE ADDED TWICE
    # --------------------------------------------------------

    if timestamp_text == state["last_timestamp"]:
        print(
            f"[SAME ROW] {timestamp_text}"
            f" already consumed - SKIP",
            flush=True
        )
        return

    old_total = float(
        state["running_total"]
    )

    new_total = (
        old_total + value
    )

    print(
        f"[NEW 5M] {timestamp_text}"
        f" | contribution={fmt_money(value)}"
        f" | before={fmt_money(old_total)}"
        f" | after={fmt_money(new_total)}"
        f" | state={state['direction']}",
        flush=True
    )

    state["last_timestamp"] = (
        timestamp_text
    )

    state["last_value"] = value

    state["running_total"] = (
        new_total
    )

    trigger = None

    direction = state["direction"]

    # ========================================================
    # STRICT ALTERNATION
    # ========================================================

    if direction == "NONE":

        if new_total >= THRESHOLD:
            trigger = "BUY"

        elif new_total <= -THRESHOLD:
            trigger = "SELL"

    elif direction == "BUY":

        # BUY -> only SELL eligible

        if new_total <= -THRESHOLD:
            trigger = "SELL"

        elif new_total >= THRESHOLD:
            print(
                "[BLOCKED] BUY already active."
                " Waiting for -$100M SELL.",
                flush=True
            )

    elif direction == "SELL":

        # SELL -> only BUY eligible

        if new_total >= THRESHOLD:
            trigger = "BUY"

        elif new_total <= -THRESHOLD:
            print(
                "[BLOCKED] SELL already active."
                " Waiting for +$100M BUY.",
                flush=True
            )

    # ========================================================
    # TRIGGER
    # ========================================================

    if trigger:
        trigger_total = new_total

        state["direction"] = trigger

        state["last_trigger"] = (
            trigger
        )

        state["last_trigger_time"] = (
            timestamp_text
        )

        # LOCKED RULE:
        # BUY/SELL trigger -> accumulated total = ZERO

        state["running_total"] = 0.0

        print(
            "====================================================",
            flush=True
        )

        print(
            f"[BTC NETFLOW {trigger}]"
            f" | time={timestamp_text}"
            f" | 5M={fmt_money(value)}"
            f" | accumulated={fmt_money(trigger_total)}"
            f" | RESET=$0",
            flush=True
        )

        print(
            "====================================================",
            flush=True
        )

        next_side = (
            "SELL"
            if trigger == "BUY"
            else "BUY"
        )

        pushover(
            f"BTC NETFLOW | {trigger}",
            (
                f"{trigger} CONDITION\n"
                f"Time: {timestamp_text}\n"
                f"5M contribution: {fmt_money(value)}\n"
                f"Accumulated: {fmt_money(trigger_total)}\n"
                f"Threshold: ±$100.00M\n"
                f"Reset: $0\n"
                f"Next eligible: {next_side}"
            )
        )

    save_state(state)


# ============================================================
# MAIN
# ============================================================

async def main():

    print(
        "====================================================",
        flush=True
    )

    print(
        "BTC FUTURES NETFLOW OBSERVER",
        flush=True
    )

    print(
        "CoinGlass Futures Inflow/Outflow History",
        flush=True
    )

    print(
        "Dropdown: select 5 minute EVERY SCAN",
        flush=True
    )

    print(
        "Contribution: BTC 5-minute Netflow",
        flush=True
    )

    print(
        "Threshold: +$100M BUY / -$100M SELL",
        flush=True
    )

    print(
        "Trigger: reset accumulated total to zero",
        flush=True
    )

    print(
        "State: strict BUY <-> SELL alternation",
        flush=True
    )

    print(
        "====================================================",
        flush=True
    )

    print(
        f"[STATE]"
        f" total={fmt_money(float(state['running_total']))}"
        f" | direction={state['direction']}"
        f" | last_timestamp="
        f"{state['last_timestamp'] or 'NONE'}",
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
            viewport={
                "width": 1600,
                "height": 1000
            },
            locale="en-US"
        )

        page = await context.new_page()

        while True:

            try:
                now = datetime.now(
                    IST
                ).strftime(
                    "%Y-%m-%d %H:%M:%S IST"
                )

                print(
                    f"\n[SCAN] {now}",
                    flush=True
                )

                # Fresh reload each scan.
                await page.goto(
                    URL,
                    wait_until="domcontentloaded",
                    timeout=90000
                )

                # Give CoinGlass JS time to render.
                await page.wait_for_timeout(
                    6000
                )

                history_ok = (
                    await wait_for_history_section(
                        page
                    )
                )

                if not history_ok:
                    print(
                        "[SCAN] History section unavailable",
                        flush=True
                    )

                    await asyncio.sleep(
                        SCAN_SECONDS
                    )

                    continue

                # IMPORTANT:
                # Select/confirm 5-minute dropdown after EVERY reload.
                interval_ok = (
                    await select_five_minute(
                        page
                    )
                )

                if not interval_ok:
                    print(
                        "[SCAN] 5-minute selection failed",
                        flush=True
                    )

                    await asyncio.sleep(
                        SCAN_SECONDS
                    )

                    continue

                # Wait for History data after selector update.
                await page.wait_for_timeout(
                    2500
                )

                timestamp_text, value = (
                    await get_latest_btc_5m(
                        page
                    )
                )

                if (
                    timestamp_text is None
                    or value is None
                ):
                    print(
                        "[PARSE WARNING]"
                        " Latest BTC 5M value not found",
                        flush=True
                    )

                else:
                    process_new_contribution(
                        timestamp_text,
                        value
                    )

            except Exception as exc:
                print(
                    f"[SCAN ERROR]"
                    f" {type(exc).__name__}: {exc}",
                    flush=True
                )

            await asyncio.sleep(
                SCAN_SECONDS
            )


if __name__ == "__main__":
    asyncio.run(main())
