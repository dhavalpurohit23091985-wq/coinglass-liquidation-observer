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

# LOCKED:
# +$100M = BUY condition
# -$100M = SELL condition
THRESHOLD = 100_000_000.0

IST = ZoneInfo("Asia/Kolkata")

PUSHOVER_USER_KEY = os.getenv(
    "PUSHOVER_USER_KEY",
    ""
).strip()

PUSHOVER_APP_TOKEN = os.getenv(
    "PUSHOVER_APP_TOKEN",
    ""
).strip()

STATE_FILE = "/tmp/btc_netflow_state.json"


# ============================================================
# STATE
# ============================================================

def default_state():
    return {
        "running_total": 0.0,
        "direction": "NONE",
        "last_timestamp": "",
        "last_value": 0.0,
        "last_trigger": "NONE",
        "last_trigger_time": ""
    }


def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(
                STATE_FILE,
                "r",
                encoding="utf-8"
            ) as f:
                saved = json.load(f)

            new_state = default_state()
            new_state.update(saved)

            return new_state

    except Exception as exc:
        print(
            f"[STATE LOAD ERROR] {exc}",
            flush=True
        )

    return default_state()


def save_state(current_state):
    try:
        with open(
            STATE_FILE,
            "w",
            encoding="utf-8"
        ) as f:
            json.dump(
                current_state,
                f,
                indent=2
            )

    except Exception as exc:
        print(
            f"[STATE SAVE ERROR] {exc}",
            flush=True
        )


state = load_state()


# ============================================================
# MONEY
# ============================================================

def fmt_money(value):
    sign = (
        "+"
        if value > 0
        else "-"
        if value < 0
        else ""
    )

    number = abs(value)

    if number >= 1_000_000_000:
        return (
            f"{sign}$"
            f"{number / 1_000_000_000:.2f}B"
        )

    if number >= 1_000_000:
        return (
            f"{sign}$"
            f"{number / 1_000_000:.2f}M"
        )

    if number >= 1_000:
        return (
            f"{sign}$"
            f"{number / 1_000:.2f}K"
        )

    return f"{sign}${number:.2f}"


def parse_money(text):
    if not text:
        return None

    cleaned = (
        text
        .strip()
        .replace(",", "")
        .replace("−", "-")
        .replace("–", "-")
        .replace("—", "-")
        .replace(" ", "")
    )

    match = re.search(
        r"([+-]?)\$?"
        r"([0-9]*\.?[0-9]+)"
        r"([KMB]?)",
        cleaned,
        re.I
    )

    if not match:
        return None

    sign_text = match.group(1)
    number = float(match.group(2))
    suffix = match.group(3).upper()

    sign = (
        -1.0
        if sign_text == "-"
        else 1.0
    )

    multiplier = {
        "": 1.0,
        "K": 1_000.0,
        "M": 1_000_000.0,
        "B": 1_000_000_000.0
    }[suffix]

    return (
        sign
        * number
        * multiplier
    )


# ============================================================
# PUSHOVER
# ============================================================

def pushover(title, message):
    if (
        not PUSHOVER_USER_KEY
        or not PUSHOVER_APP_TOKEN
    ):
        print(
            "[PUSHOVER] Keys missing - skipped",
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
            f"[PUSHOVER] "
            f"status={response.status_code}",
            flush=True
        )

    except Exception as exc:
        print(
            f"[PUSHOVER ERROR] {exc}",
            flush=True
        )


# ============================================================
# DEBUG
# ============================================================

async def debug_page(page):
    """
    Temporary diagnostic.

    We need to know exactly what CoinGlass serves
    to Render's headless Chromium.
    """

    try:
        title = await page.title()
    except Exception as exc:
        title = f"ERROR: {exc}"

    try:
        current_url = page.url
    except Exception as exc:
        current_url = f"ERROR: {exc}"

    try:
        body = await page.locator(
            "body"
        ).inner_text(
            timeout=10000
        )
    except Exception as exc:
        body = f"BODY ERROR: {exc}"

    clean_body = (
        body
        .replace("\r", " ")
        .replace("\n", " | ")
    )

    print(
        "====================================================",
        flush=True
    )

    print(
        "[COINGLASS DEBUG]",
        flush=True
    )

    print(
        f"[DEBUG TITLE] {title}",
        flush=True
    )

    print(
        f"[DEBUG URL] {current_url}",
        flush=True
    )

    print(
        f"[DEBUG BODY LENGTH] {len(body)}",
        flush=True
    )

    print(
        f"[DEBUG BODY START] "
        f"{clean_body[:3000]}",
        flush=True
    )

    print(
        "[DEBUG CHECK]"
        f" history="
        f"{'BTC Futures Inflow/Outflow History' in body}"
        f" | inflow="
        f"{'Inflow/Outflow' in body}"
        f" | 5minute="
        f"{'5 minute' in body}"
        f" | futures="
        f"{'Futures' in body}"
        f" | btc="
        f"{'BTC' in body}",
        flush=True
    )

    print(
        "====================================================",
        flush=True
    )

    return body


# ============================================================
# HISTORY SECTION
# ============================================================

async def wait_for_history_section(page):
    try:
        heading = page.get_by_text(
            "BTC Futures Inflow/Outflow History",
            exact=False
        ).first

        await heading.wait_for(
            state="visible",
            timeout=15000
        )

        print(
            "[PAGE] BTC Futures "
            "Inflow/Outflow History found",
            flush=True
        )

        return True

    except Exception:
        print(
            "[PAGE WARNING] "
            "History heading not found",
            flush=True
        )

        return False


# ============================================================
# 5-MINUTE DROPDOWN
# ============================================================

async def select_five_minute(page):
    """
    LOCKED REQUIREMENT:

    Every reload:
        reload page
        -> select/confirm 5 minute
        -> read latest BTC 5-minute contribution
    """

    print(
        "[INTERVAL] Selecting 5 minute...",
        flush=True
    )

    # --------------------------------------------------------
    # METHOD 1:
    # visible exact text
    # --------------------------------------------------------

    try:
        candidates = page.get_by_text(
            "5 minute",
            exact=True
        )

        count = await candidates.count()

        print(
            f"[INTERVAL DEBUG] "
            f"5-minute text matches={count}",
            flush=True
        )

        if count > 0:
            for i in range(
                count - 1,
                -1,
                -1
            ):
                candidate = candidates.nth(i)

                try:
                    if not await candidate.is_visible():
                        continue

                    print(
                        f"[INTERVAL] "
                        f"visible 5-minute control index={i}",
                        flush=True
                    )

                    await candidate.click(
                        timeout=3000
                    )

                    await page.wait_for_timeout(
                        800
                    )

                    # If click opened a dropdown,
                    # click the visible 5-minute option.
                    new_candidates = (
                        page.get_by_text(
                            "5 minute",
                            exact=True
                        )
                    )

                    new_count = (
                        await new_candidates.count()
                    )

                    for j in range(
                        new_count - 1,
                        -1,
                        -1
                    ):
                        option = (
                            new_candidates.nth(j)
                        )

                        try:
                            if (
                                await option.is_visible()
                                and j != i
                            ):
                                await option.click(
                                    timeout=3000
                                )

                                await page.wait_for_timeout(
                                    2000
                                )

                                print(
                                    "[INTERVAL] "
                                    "5 minute selected",
                                    flush=True
                                )

                                return True

                        except Exception:
                            continue

                    # Could already be selected.
                    print(
                        "[INTERVAL] "
                        "5 minute control confirmed",
                        flush=True
                    )

                    return True

                except Exception:
                    continue

    except Exception as exc:
        print(
            f"[INTERVAL METHOD1 ERROR] {exc}",
            flush=True
        )

    # --------------------------------------------------------
    # METHOD 2:
    # buttons with interval names
    # --------------------------------------------------------

    try:
        buttons = page.locator(
            "button"
        )

        button_count = (
            await buttons.count()
        )

        possible_intervals = {
            "5 minute",
            "15 minute",
            "30 minute",
            "1 hour",
            "4 hour",
            "1 day"
        }

        interval_buttons = []

        for i in range(
            button_count
        ):
            button = buttons.nth(i)

            try:
                if not await button.is_visible():
                    continue

                text = (
                    await button.inner_text()
                ).strip().lower()

                if text in possible_intervals:
                    interval_buttons.append(
                        button
                    )

            except Exception:
                continue

        print(
            f"[INTERVAL DEBUG] "
            f"interval buttons="
            f"{len(interval_buttons)}",
            flush=True
        )

        if interval_buttons:
            # History selector is lower on page,
            # so prefer last interval control.
            interval_button = (
                interval_buttons[-1]
            )

            current_text = (
                await interval_button.inner_text()
            ).strip()

            print(
                f"[INTERVAL] "
                f"current={current_text}",
                flush=True
            )

            await interval_button.click(
                timeout=5000
            )

            await page.wait_for_timeout(
                800
            )

            options = page.get_by_text(
                "5 minute",
                exact=True
            )

            option_count = (
                await options.count()
            )

            for i in range(
                option_count - 1,
                -1,
                -1
            ):
                option = options.nth(i)

                try:
                    if await option.is_visible():
                        await option.click(
                            timeout=5000
                        )

                        await page.wait_for_timeout(
                            2000
                        )

                        print(
                            "[INTERVAL] "
                            "5 minute selected via dropdown",
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
    # METHOD 3:
    # native select
    # --------------------------------------------------------

    try:
        selects = page.locator(
            "select"
        )

        select_count = (
            await selects.count()
        )

        print(
            f"[INTERVAL DEBUG] "
            f"native selects={select_count}",
            flush=True
        )

        for i in range(
            select_count
        ):
            select = selects.nth(i)

            try:
                options = (
                    await select.locator(
                        "option"
                    ).all_inner_texts()
                )

                normalized = [
                    x.strip().lower()
                    for x in options
                ]

                if "5 minute" not in normalized:
                    continue

                index = normalized.index(
                    "5 minute"
                )

                values = (
                    await select.locator(
                        "option"
                    ).evaluate_all(
                        """
                        els => els.map(
                            e => e.value
                        )
                        """
                    )
                )

                await select.select_option(
                    values[index]
                )

                await page.wait_for_timeout(
                    2000
                )

                print(
                    "[INTERVAL] "
                    "5 minute selected via native select",
                    flush=True
                )

                return True

            except Exception:
                continue

    except Exception as exc:
        print(
            f"[INTERVAL METHOD3 ERROR] {exc}",
            flush=True
        )

    print(
        "[INTERVAL ERROR] "
        "Could not select/confirm 5 minute",
        flush=True
    )

    return False


# ============================================================
# PARSER
# ============================================================

async def get_latest_btc_5m(page):
    """
    Expected rendered History structure:

    Time
    5 minute
    15 minute
    30 minute
    1 hour
    ...

    09-27 06:15
    +$xxx
    ...

    We only consume:
        latest timestamp
        first money value after timestamp
    """

    try:
        body_text = await page.locator(
            "body"
        ).inner_text(
            timeout=10000
        )

    except Exception as exc:
        print(
            f"[BODY ERROR] {exc}",
            flush=True
        )

        return None, None

    if not body_text:
        print(
            "[BODY ERROR] Empty body",
            flush=True
        )

        return None, None

    body_text = (
        body_text
        .replace("−", "-")
        .replace("–", "-")
        .replace("—", "-")
    )

    marker = (
        "BTC Futures Inflow/Outflow History"
    )

    marker_index = (
        body_text.find(marker)
    )

    if marker_index >= 0:
        history_text = body_text[
            marker_index:
        ]

        print(
            "[PARSE] "
            "History marker located",
            flush=True
        )

    else:
        history_text = body_text

        print(
            "[PARSE WARNING] "
            "History marker not located; "
            "using full body",
            flush=True
        )

    timestamp_pattern = re.compile(
        r"(\d{2}-\d{2}\s+\d{2}:\d{2})"
    )

    matches = list(
        timestamp_pattern.finditer(
            history_text
        )
    )

    print(
        f"[PARSE] "
        f"timestamp matches={len(matches)}",
        flush=True
    )

    if not matches:
        preview = (
            history_text[:1500]
            .replace("\n", " | ")
        )

        print(
            f"[PARSE DEBUG] {preview}",
            flush=True
        )

        return None, None

    # Newest row first.
    first_match = matches[0]

    timestamp_text = (
        first_match.group(1)
    )

    row_start = (
        first_match.end()
    )

    if len(matches) > 1:
        row_end = (
            matches[1].start()
        )
    else:
        row_end = min(
            len(history_text),
            row_start + 1000
        )

    row_text = history_text[
        row_start:row_end
    ]

    money_pattern = re.compile(
        r"([+-]?\$"
        r"[0-9,.]+"
        r"(?:\.[0-9]+)?"
        r"[KMB]?)",
        re.I
    )

    money_matches = (
        money_pattern.findall(
            row_text
        )
    )

    print(
        f"[ROW] "
        f"time={timestamp_text}"
        f" | money_cells="
        f"{len(money_matches)}",
        flush=True
    )

    if not money_matches:
        debug_row = (
            row_text[:800]
            .replace("\n", " | ")
        )

        print(
            f"[ROW DEBUG] {debug_row}",
            flush=True
        )

        return None, None

    five_minute_text = (
        money_matches[0]
    )

    value = parse_money(
        five_minute_text
    )

    if value is None:
        print(
            f"[PARSE ERROR] "
            f"Could not parse "
            f"{five_minute_text}",
            flush=True
        )

        return None, None

    print(
        f"[LATEST] "
        f"{timestamp_text}"
        f" | BTC 5M="
        f"{five_minute_text}"
        f" | parsed="
        f"{fmt_money(value)}",
        flush=True
    )

    return (
        timestamp_text,
        value
    )


# ============================================================
# SIGNAL ENGINE
# ============================================================

def process_new_contribution(
    timestamp_text,
    value
):
    global state

    # Same timestamp must never be added twice.
    if (
        timestamp_text
        == state["last_timestamp"]
    ):
        print(
            f"[SAME ROW] "
            f"{timestamp_text}"
            f" already consumed - SKIP",
            flush=True
        )

        return

    old_total = float(
        state["running_total"]
    )

    new_total = (
        old_total
        + value
    )

    print(
        f"[NEW 5M] "
        f"{timestamp_text}"
        f" | contribution="
        f"{fmt_money(value)}"
        f" | before="
        f"{fmt_money(old_total)}"
        f" | after="
        f"{fmt_money(new_total)}"
        f" | state="
        f"{state['direction']}",
        flush=True
    )

    state["last_timestamp"] = (
        timestamp_text
    )

    state["last_value"] = (
        value
    )

    state["running_total"] = (
        new_total
    )

    trigger = None

    direction = (
        state["direction"]
    )

    # ========================================================
    # STRICT ALTERNATION
    # ========================================================

    if direction == "NONE":

        if new_total >= THRESHOLD:
            trigger = "BUY"

        elif new_total <= -THRESHOLD:
            trigger = "SELL"

    elif direction == "BUY":

        # BUY -> only SELL
        if new_total <= -THRESHOLD:
            trigger = "SELL"

        elif new_total >= THRESHOLD:
            print(
                "[BLOCKED] "
                "BUY already active. "
                "Waiting for SELL.",
                flush=True
            )

    elif direction == "SELL":

        # SELL -> only BUY
        if new_total >= THRESHOLD:
            trigger = "BUY"

        elif new_total <= -THRESHOLD:
            print(
                "[BLOCKED] "
                "SELL already active. "
                "Waiting for BUY.",
                flush=True
            )

    # ========================================================
    # TRIGGER
    # ========================================================

    if trigger:
        trigger_total = (
            new_total
        )

        state["direction"] = (
            trigger
        )

        state["last_trigger"] = (
            trigger
        )

        state["last_trigger_time"] = (
            timestamp_text
        )

        # LOCKED RULE:
        # trigger -> accumulated total ZERO
        state["running_total"] = 0.0

        print(
            "====================================================",
            flush=True
        )

        print(
            f"[BTC NETFLOW {trigger}]"
            f" | time={timestamp_text}"
            f" | 5M={fmt_money(value)}"
            f" | accumulated="
            f"{fmt_money(trigger_total)}"
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
                f"5M contribution: "
                f"{fmt_money(value)}\n"
                f"Accumulated: "
                f"{fmt_money(trigger_total)}\n"
                f"Threshold: ±$100.00M\n"
                f"Reset: $0\n"
                f"Next eligible: "
                f"{next_side}"
            )
        )

    save_state(
        state
    )


# ============================================================
# MAIN
# ============================================================

async def main():

    print(
        "====================================================",
        flush=True
    )

    print(
        "BTC FUTURES NETFLOW OBSERVER - DEBUG",
        flush=True
    )

    print(
        "CoinGlass Futures "
        "Inflow/Outflow History",
        flush=True
    )

    print(
        "Dropdown: select 5 minute "
        "EVERY SCAN",
        flush=True
    )

    print(
        "Contribution: BTC "
        "5-minute Netflow",
        flush=True
    )

    print(
        "Threshold: "
        "+$100M BUY / -$100M SELL",
        flush=True
    )

    print(
        "Trigger: reset accumulated "
        "total to zero",
        flush=True
    )

    print(
        "State: strict "
        "BUY <-> SELL alternation",
        flush=True
    )

    print(
        "====================================================",
        flush=True
    )

    print(
        f"[STATE]"
        f" total="
        f"{fmt_money(float(state['running_total']))}"
        f" | direction="
        f"{state['direction']}"
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

        page = (
            await context.new_page()
        )

        while True:

            try:
                now = (
                    datetime.now(IST)
                    .strftime(
                        "%Y-%m-%d "
                        "%H:%M:%S IST"
                    )
                )

                print(
                    f"\n[SCAN] {now}",
                    flush=True
                )

                # --------------------------------------------
                # FRESH PAGE LOAD
                # --------------------------------------------

                await page.goto(
                    URL,
                    wait_until="domcontentloaded",
                    timeout=90000
                )

                # CoinGlass is JS-heavy.
                await page.wait_for_timeout(
                    6000
                )

                # --------------------------------------------
                # TEMP DEBUG
                # --------------------------------------------

                await debug_page(
                    page
                )

                # --------------------------------------------
                # HISTORY SECTION
                # --------------------------------------------

                history_ok = (
                    await wait_for_history_section(
                        page
                    )
                )

                if not history_ok:
                    print(
                        "[SCAN] "
                        "History section unavailable",
                        flush=True
                    )

                    await asyncio.sleep(
                        SCAN_SECONDS
                    )

                    continue

                # --------------------------------------------
                # SELECT 5 MINUTE EVERY RELOAD
                # --------------------------------------------

                interval_ok = (
                    await select_five_minute(
                        page
                    )
                )

                if not interval_ok:
                    print(
                        "[SCAN] "
                        "5-minute selection failed",
                        flush=True
                    )

                    await asyncio.sleep(
                        SCAN_SECONDS
                    )

                    continue

                # Let table update after selector.
                await page.wait_for_timeout(
                    2500
                )

                # --------------------------------------------
                # READ LATEST BTC 5M
                # --------------------------------------------

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
                        "[PARSE WARNING] "
                        "Latest BTC 5M "
                        "value not found",
                        flush=True
                    )

                else:
                    process_new_contribution(
                        timestamp_text,
                        value
                    )

            except Exception as exc:
                print(
                    f"[SCAN ERROR] "
                    f"{type(exc).__name__}: "
                    f"{exc}",
                    flush=True
                )

            await asyncio.sleep(
                SCAN_SECONDS
            )


if __name__ == "__main__":
    asyncio.run(main())
