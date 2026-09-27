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
        "direction": "NONE",
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

            result = default_state()
            result.update(saved)
            return result

    except Exception as exc:
        print(f"[STATE LOAD ERROR] {exc}", flush=True)

    return default_state()


def save_state(current_state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(current_state, f, indent=2)

    except Exception as exc:
        print(f"[STATE SAVE ERROR] {exc}", flush=True)


state = load_state()


# ============================================================
# MONEY
# ============================================================

def fmt_money(value):
    sign = "+" if value > 0 else "-" if value < 0 else ""
    number = abs(value)

    if number >= 1_000_000_000:
        return f"{sign}${number / 1_000_000_000:.2f}B"

    if number >= 1_000_000:
        return f"{sign}${number / 1_000_000:.2f}M"

    if number >= 1_000:
        return f"{sign}${number / 1_000:.2f}K"

    return f"{sign}${number:.2f}"


def parse_money(text):
    if not text:
        return None

    cleaned = (
        text.strip()
        .replace(",", "")
        .replace("−", "-")
        .replace("–", "-")
        .replace("—", "-")
        .replace(" ", "")
    )

    match = re.search(
        r"([+-]?)\$?([0-9]*\.?[0-9]+)([KMB]?)",
        cleaned,
        re.I
    )

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


# ============================================================
# PUSHOVER
# ============================================================

def pushover(title, message):
    if not PUSHOVER_USER_KEY or not PUSHOVER_APP_TOKEN:
        print("[PUSHOVER] Keys missing - skipped", flush=True)
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
# NAVIGATION DIAGNOSTIC
# ============================================================

async def navigate_and_debug(page):
    """
    Navigate to exact CoinGlass URL and show:
    - navigation status
    - requested URL
    - final URL
    - redirect chain
    - title
    - first part of body

    This lets us see exactly where the trailing slash / 404
    originates.
    """

    print("====================================================", flush=True)
    print("[NAVIGATION TEST]", flush=True)
    print(f"[REQUEST URL] {URL}", flush=True)

    response = None

    try:
        response = await page.goto(
            URL,
            wait_until="domcontentloaded",
            timeout=90000
        )

    except Exception as exc:
        print(
            f"[NAVIGATION ERROR] {type(exc).__name__}: {exc}",
            flush=True
        )
        return False

    await page.wait_for_timeout(5000)

    print(f"[FINAL URL] {page.url}", flush=True)

    if response is None:
        print("[RESPONSE] None", flush=True)
    else:
        try:
            print(
                f"[RESPONSE STATUS] {response.status}",
                flush=True
            )
            print(
                f"[RESPONSE OK] {response.ok}",
                flush=True
            )
            print(
                f"[RESPONSE URL] {response.url}",
                flush=True
            )
        except Exception as exc:
            print(
                f"[RESPONSE READ ERROR] {exc}",
                flush=True
            )

        # ----------------------------------------------------
        # REDIRECT CHAIN
        # ----------------------------------------------------

        try:
            request = response.request
            chain = []

            while request is not None:
                chain.append(request.url)
                request = request.redirected_from

            chain.reverse()

            print(
                f"[REDIRECT COUNT] {max(0, len(chain) - 1)}",
                flush=True
            )

            for index, item in enumerate(chain):
                print(
                    f"[REDIRECT {index}] {item}",
                    flush=True
                )

        except Exception as exc:
            print(
                f"[REDIRECT DEBUG ERROR] {exc}",
                flush=True
            )

    try:
        title = await page.title()
    except Exception as exc:
        title = f"ERROR: {exc}"

    print(
        f"[PAGE TITLE] {title}",
        flush=True
    )

    try:
        body = await page.locator("body").inner_text(
            timeout=10000
        )
    except Exception as exc:
        body = f"BODY ERROR: {exc}"

    body_clean = (
        body
        .replace("\r", " ")
        .replace("\n", " | ")
    )

    print(
        f"[BODY LENGTH] {len(body)}",
        flush=True
    )

    print(
        f"[BODY START] {body_clean[:2000]}",
        flush=True
    )

    has_history = (
        "BTC Futures Inflow/Outflow History" in body
    )

    print(
        "[CONTENT CHECK]"
        f" history={has_history}"
        f" | 5minute={'5 minute' in body}"
        f" | futures={'Futures' in body}"
        f" | btc={'BTC' in body}",
        flush=True
    )

    print("====================================================", flush=True)

    return has_history


# ============================================================
# SELECT 5 MINUTE
# ============================================================

async def select_five_minute(page):
    print("[INTERVAL] Selecting 5 minute...", flush=True)

    # First inspect clickable buttons.
    try:
        buttons = page.locator("button")
        count = await buttons.count()

        interval_buttons = []

        for i in range(count):
            button = buttons.nth(i)

            try:
                if not await button.is_visible():
                    continue

                text = (
                    await button.inner_text()
                ).strip()

                if text.lower() in {
                    "5 minute",
                    "15 minute",
                    "30 minute",
                    "1 hour",
                    "4 hour",
                    "1 day"
                }:
                    interval_buttons.append(button)

            except Exception:
                continue

        print(
            f"[INTERVAL] candidate buttons={len(interval_buttons)}",
            flush=True
        )

        if interval_buttons:
            # History selector should be the lower/later control.
            control = interval_buttons[-1]

            current = (
                await control.inner_text()
            ).strip()

            print(
                f"[INTERVAL] current={current}",
                flush=True
            )

            # If already 5 minute, confirmed.
            if current.lower() == "5 minute":
                print(
                    "[INTERVAL] 5 minute already selected",
                    flush=True
                )
                return True

            await control.click(timeout=5000)
            await page.wait_for_timeout(500)

            options = page.get_by_text(
                "5 minute",
                exact=True
            )

            option_count = await options.count()

            for i in range(option_count - 1, -1, -1):
                option = options.nth(i)

                try:
                    if await option.is_visible():
                        await option.click(timeout=5000)
                        await page.wait_for_timeout(2000)

                        print(
                            "[INTERVAL] 5 minute selected",
                            flush=True
                        )

                        return True

                except Exception:
                    continue

    except Exception as exc:
        print(
            f"[INTERVAL BUTTON ERROR] {exc}",
            flush=True
        )

    # Fallback: exact visible 5-minute text.
    try:
        matches = page.get_by_text(
            "5 minute",
            exact=True
        )

        count = await matches.count()

        print(
            f"[INTERVAL] text matches={count}",
            flush=True
        )

        for i in range(count - 1, -1, -1):
            candidate = matches.nth(i)

            try:
                if await candidate.is_visible():
                    await candidate.click(timeout=3000)
                    await page.wait_for_timeout(1500)

                    print(
                        "[INTERVAL] 5 minute control confirmed",
                        flush=True
                    )

                    return True

            except Exception:
                continue

    except Exception as exc:
        print(
            f"[INTERVAL TEXT ERROR] {exc}",
            flush=True
        )

    print(
        "[INTERVAL ERROR] Could not select 5 minute",
        flush=True
    )

    return False


# ============================================================
# PARSE LATEST 5-MINUTE ROW
# ============================================================

async def get_latest_btc_5m(page):
    try:
        body = await page.locator("body").inner_text(
            timeout=10000
        )

    except Exception as exc:
        print(f"[PARSE BODY ERROR] {exc}", flush=True)
        return None, None

    body = (
        body
        .replace("−", "-")
        .replace("–", "-")
        .replace("—", "-")
    )

    marker = "BTC Futures Inflow/Outflow History"

    marker_index = body.find(marker)

    if marker_index < 0:
        print(
            "[PARSE ERROR] History marker missing",
            flush=True
        )
        return None, None

    history = body[marker_index:]

    timestamp_pattern = re.compile(
        r"(\d{2}-\d{2}\s+\d{2}:\d{2})"
    )

    timestamps = list(
        timestamp_pattern.finditer(history)
    )

    print(
        f"[PARSE] timestamp matches={len(timestamps)}",
        flush=True
    )

    if not timestamps:
        return None, None

    first = timestamps[0]

    timestamp_text = first.group(1)

    start = first.end()

    if len(timestamps) > 1:
        end = timestamps[1].start()
    else:
        end = min(
            len(history),
            start + 1000
        )

    row = history[start:end]

    money_pattern = re.compile(
        r"([+-]?\$[0-9,.]+(?:\.[0-9]+)?[KMB]?)",
        re.I
    )

    cells = money_pattern.findall(row)

    print(
        f"[ROW] time={timestamp_text}"
        f" | money_cells={len(cells)}",
        flush=True
    )

    if not cells:
        return None, None

    five_minute_text = cells[0]

    value = parse_money(five_minute_text)

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

def process_new_contribution(timestamp_text, value):
    global state

    # Same 5-minute timestamp only once.
    if timestamp_text == state["last_timestamp"]:
        print(
            f"[SAME ROW] {timestamp_text}"
            " already consumed - SKIP",
            flush=True
        )
        return

    before = float(state["running_total"])
    after = before + value

    print(
        f"[NEW 5M] {timestamp_text}"
        f" | contribution={fmt_money(value)}"
        f" | before={fmt_money(before)}"
        f" | after={fmt_money(after)}"
        f" | state={state['direction']}",
        flush=True
    )

    state["last_timestamp"] = timestamp_text
    state["last_value"] = value
    state["running_total"] = after

    trigger = None
    direction = state["direction"]

    # --------------------------------------------------------
    # STRICT BUY -> SELL -> BUY -> SELL
    # --------------------------------------------------------

    if direction == "NONE":
        if after >= THRESHOLD:
            trigger = "BUY"
        elif after <= -THRESHOLD:
            trigger = "SELL"

    elif direction == "BUY":
        if after <= -THRESHOLD:
            trigger = "SELL"
        elif after >= THRESHOLD:
            print(
                "[BLOCKED] BUY already active; "
                "waiting for SELL",
                flush=True
            )

    elif direction == "SELL":
        if after >= THRESHOLD:
            trigger = "BUY"
        elif after <= -THRESHOLD:
            print(
                "[BLOCKED] SELL already active; "
                "waiting for BUY",
                flush=True
            )

    if trigger:
        trigger_total = after

        state["direction"] = trigger
        state["last_trigger"] = trigger
        state["last_trigger_time"] = timestamp_text

        # LOCKED:
        # threshold trigger -> accumulator resets to zero.
        state["running_total"] = 0.0

        print(
            "====================================================",
            flush=True
        )

        print(
            f"[BTC NETFLOW {trigger}]"
            f" | time={timestamp_text}"
            f" | contribution={fmt_money(value)}"
            f" | total={fmt_money(trigger_total)}"
            f" | RESET=$0",
            flush=True
        )

        print(
            "====================================================",
            flush=True
        )

        next_side = "SELL" if trigger == "BUY" else "BUY"

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
    print("====================================================", flush=True)
    print("BTC FUTURES NETFLOW OBSERVER", flush=True)
    print("NAVIGATION / REDIRECT DIAGNOSTIC", flush=True)
    print(f"URL: {URL}", flush=True)
    print("Interval: BTC Futures 5 minute", flush=True)
    print("Threshold: +$100M / -$100M", flush=True)
    print("Reset: trigger -> $0", flush=True)
    print("State: strict BUY <-> SELL", flush=True)
    print("====================================================", flush=True)

    print(
        f"[STATE]"
        f" total={fmt_money(float(state['running_total']))}"
        f" | direction={state['direction']}"
        f" | last={state['last_timestamp'] or 'NONE'}",
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
                now = datetime.now(IST).strftime(
                    "%Y-%m-%d %H:%M:%S IST"
                )

                print(
                    f"\n[SCAN] {now}",
                    flush=True
                )

                history_available = await navigate_and_debug(
                    page
                )

                # If CoinGlass itself returned 404/challenge/etc.,
                # do NOT run parser or signal engine.
                if not history_available:
                    print(
                        "[SCAN] CoinGlass History content unavailable"
                        " - no data consumed",
                        flush=True
                    )

                    await asyncio.sleep(SCAN_SECONDS)
                    continue

                interval_ok = await select_five_minute(
                    page
                )

                if not interval_ok:
                    print(
                        "[SCAN] 5-minute selector failed"
                        " - no data consumed",
                        flush=True
                    )

                    await asyncio.sleep(SCAN_SECONDS)
                    continue

                await page.wait_for_timeout(2000)

                timestamp_text, value = await get_latest_btc_5m(
                    page
                )

                if timestamp_text is None or value is None:
                    print(
                        "[SCAN] Latest BTC 5M not parsed"
                        " - no data consumed",
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
                    f"{type(exc).__name__}: {exc}",
                    flush=True
                )

            await asyncio.sleep(SCAN_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
