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

ENTER_GAP = 5_000_000.0
EXIT_GAP = 4_000_000.0

IST = ZoneInfo("Asia/Kolkata")

PUSHOVER_USER_KEY = os.getenv("PUSHOVER_USER_KEY", "").strip()
PUSHOVER_APP_TOKEN = os.getenv("PUSHOVER_APP_TOKEN", "").strip()
STATE_FILE = os.getenv(
    "COINGLASS_BTC_5M_STATE_FILE",
    "/tmp/coinglass_btc_5m_state.json"
).strip()


# ============================================================
# STATE
# ============================================================

state = {"position": "NONE", "timeframe": None}


def save_state():
    tmp = f"{STATE_FILE}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as exc:
        print(f"[STATE SAVE FAILED] {type(exc).__name__}: {exc}", flush=True)


def load_state():
    global state
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        position = str(saved.get("position", "NONE")).upper()
        timeframe = saved.get("timeframe")
        if position not in {"NONE", "LONG", "SHORT"}:
            position = "NONE"
        if timeframe not in {"4H", "12H"}:
            timeframe = None
        state = {"position": position, "timeframe": timeframe}
        print(f"[STATE RESTORED] position={position} | timeframe={timeframe}", flush=True)
    except FileNotFoundError:
        print("[STATE] No saved state; starting IDLE.", flush=True)
    except Exception as exc:
        print(f"[STATE RESTORE FAILED] {type(exc).__name__}: {exc}", flush=True)


# ============================================================
# BASIC HELPERS
# ============================================================

def now_ist():
    return datetime.now(IST).strftime("%d-%m-%Y %H:%M:%S IST")


def clean_text(value):
    if value is None:
        return ""
    return " ".join(str(value).replace("\xa0", " ").replace("\u200b", "").split())


def parse_number(text):
    s = clean_text(text).upper()
    if not s:
        return 0.0
    s = s.replace("$", "").replace(",", "").replace("−", "-")
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


# ============================================================
# PUSHOVER
# ============================================================

def pushover_ready():
    return bool(PUSHOVER_USER_KEY and PUSHOVER_APP_TOKEN)


def send_pushover(title, message):
    if not pushover_ready():
        print("[PUSHOVER] Not configured - notification skipped", flush=True)
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
            print(f"[PUSHOVER SENT] {title}", flush=True)
            return True
        print(f"[PUSHOVER FAILED] HTTP {response.status_code}", flush=True)
    except Exception as exc:
        print(f"[PUSHOVER FAILED] {type(exc).__name__}: {exc}", flush=True)
    return False


def values_for_timeframe(row, timeframe):
    if timeframe == "4H":
        long_value = float(row["long_4h"])
        short_value = float(row["short_4h"])
    else:
        long_value = float(row["long_12h"])
        short_value = float(row["short_12h"])
    signed_gap = long_value - short_value
    return long_value, short_value, signed_gap, abs(signed_gap)


def entry_candidate(row):
    # 4H first. Only if 4H is below $5M, check 12H.
    for timeframe in ("4H", "12H"):
        long_value, short_value, signed_gap, gap = values_for_timeframe(row, timeframe)
        if gap >= ENTER_GAP:
            liquidation_side = "LONG" if signed_gap > 0 else "SHORT"
            trade_side = "SHORT" if liquidation_side == "LONG" else "LONG"
            return {
                "timeframe": timeframe,
                "long": long_value,
                "short": short_value,
                "gap": gap,
                "liquidation_side": liquidation_side,
                "trade_side": trade_side,
            }
    return None


def send_enter(candidate):
    title = f"BTC {candidate['timeframe']} | ENTER {candidate['trade_side']} | GAP {fmt_money(candidate['gap'])}"
    message = "\n".join([
        f"BTC {candidate['timeframe']}",
        "",
        f"LONG : {fmt_money(candidate['long'])}",
        f"SHORT: {fmt_money(candidate['short'])}",
        f"GAP  : {fmt_money(candidate['gap'])} {candidate['liquidation_side']}",
        "",
        f"ENTER {candidate['trade_side']}",
    ])
    send_pushover(title, message)


def send_exit(row, position, timeframe):
    long_value, short_value, signed_gap, gap = values_for_timeframe(row, timeframe)
    liquidation_side = "LONG" if signed_gap > 0 else "SHORT" if signed_gap < 0 else "EVEN"
    title = f"BTC {timeframe} | EXIT {position} | GAP {fmt_money(gap)}"
    message = "\n".join([
        f"BTC {timeframe}",
        "",
        f"LONG : {fmt_money(long_value)}",
        f"SHORT: {fmt_money(short_value)}",
        f"GAP  : {fmt_money(gap)} {liquidation_side}",
        "",
        f"EXIT {position}",
    ])
    send_pushover(title, message)


def process_btc(row):
    position = state["position"]
    active_tf = state["timeframe"]

    if position == "NONE":
        candidate = entry_candidate(row)
        if candidate is None:
            print("[BTC] IDLE | no 4H/12H >= $5M entry gap", flush=True)
            return

        send_enter(candidate)
        state["position"] = candidate["trade_side"]
        state["timeframe"] = candidate["timeframe"]
        save_state()
        return

    # Once entered, exit is checked on the SAME timeframe that created entry.
    long_value, short_value, _, gap = values_for_timeframe(row, active_tf)
    print(
        f"[BTC ACTIVE] {position} | {active_tf} | "
        f"LONG={fmt_money(long_value)} | SHORT={fmt_money(short_value)} | GAP={fmt_money(gap)}",
        flush=True,
    )

    if gap < EXIT_GAP:
        send_exit(row, position, active_tf)
        state["position"] = "NONE"
        state["timeframe"] = None
        save_state()
        return

    print(f"[BTC HOLD] {position} | exit only when {active_tf} gap < $4M", flush=True)


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
# BTC PARSER
# ============================================================

def is_number_like(text):
    s = clean_text(text)
    return bool(re.fullmatch(r"[-+−]?\$?\d[\d,]*(?:\.\d+)?(?:[KMB])?%?", s, flags=re.I))


def find_value_header(lines):
    for i in range(len(lines)):
        block = " ".join(lines[i:i + 20]).lower()
        if (
            "assets" in block
            and "4h long" in block
            and "4h short" in block
            and "12h long" in block
            and "12h short" in block
        ):
            return i
    raise RuntimeError("VALUE liquidation header not found")


def parse_btc_row(lines):
    header_index = find_value_header(lines)
    search_lines = lines[header_index + 1: header_index + 250]

    for i, item in enumerate(search_lines):
        if clean_text(item).upper() != "BTC":
            continue

        numbers = []
        for candidate in search_lines[i + 1:i + 20]:
            if is_number_like(candidate):
                numbers.append(candidate)

        # price, 24h%, 1hL, 1hS, 4hL, 4hS, 12hL, 12hS...
        if len(numbers) >= 8:
            row = {
                "symbol": "BTC",
                "long_4h": parse_number(numbers[4]),
                "short_4h": parse_number(numbers[5]),
                "long_12h": parse_number(numbers[6]),
                "short_12h": parse_number(numbers[7]),
            }
            print(
                f"[PARSED BTC] 4H L={numbers[4]} S={numbers[5]} | "
                f"12H L={numbers[6]} S={numbers[7]}",
                flush=True,
            )
            return row

    raise RuntimeError("BTC liquidation row not parsed")


# ============================================================
# ONE SCAN
# ============================================================

async def scan_once(page):
    print(
        "\n############################################################\n"
        f"[SCAN START] {now_ist()}\n"
        "############################################################",
        flush=True,
    )

    response = await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    status = response.status if response else None
    print(f"[HTTP] status={status}", flush=True)
    print(f"[HTTP] final_url={page.url}", flush=True)

    if response is not None and response.status >= 400:
        raise RuntimeError(f"CoinGlass HTTP {response.status}")

    await page.wait_for_timeout(8000)
    print(f"[PAGE] title={await page.title()}", flush=True)
    await wait_for_liquidation_section(page)

    lines = await get_rendered_lines(page)
    btc = parse_btc_row(lines)
    process_btc(btc)

    print(f"[SCAN OK] {now_ist()}", flush=True)


# ============================================================
# MAIN
# ============================================================

async def main():
    print("COINGLASS BTC 5M ENTER / 4M EXIT OBSERVER STARTING", flush=True)
    load_state()
    print(f"URL: {URL}", flush=True)
    print(f"SCAN: every {SCAN_SECONDS} seconds", flush=True)
    print("ENTER: 4H first -> 12H fallback | gap >= $5M", flush=True)
    print("EXIT: same entry timeframe | gap < $4M", flush=True)
    print("LONG liquidation gap -> ENTER SHORT", flush=True)
    print("SHORT liquidation gap -> ENTER LONG", flush=True)
    print(f"PUSHOVER: {'READY' if pushover_ready() else 'NOT CONFIGURED'}", flush=True)

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
