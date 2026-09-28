import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright


# ============================================================
# CONFIG
# ============================================================

URL = "https://www.coinglass.com/liquidations"

SCAN_SECONDS = 30

IST = ZoneInfo("Asia/Kolkata")


# ============================================================
# PAGE TEST
# ============================================================

async def test_page(page):

    print("====================================================", flush=True)
    print("[COINGLASS LIQUIDATIONS ACCESS TEST]", flush=True)
    print(f"[REQUEST URL] {URL}", flush=True)

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
        return

    # Give CoinGlass time to render client-side content.
    await page.wait_for_timeout(8000)

    print(f"[FINAL URL] {page.url}", flush=True)

    # ========================================================
    # RESPONSE
    # ========================================================

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

            for index, url in enumerate(chain):

                print(
                    f"[REDIRECT {index}] {url}",
                    flush=True
                )

        except Exception as exc:

            print(
                f"[REDIRECT ERROR] {exc}",
                flush=True
            )

    # ========================================================
    # TITLE
    # ========================================================

    try:

        title = await page.title()

    except Exception as exc:

        title = f"ERROR: {exc}"

    print(
        f"[PAGE TITLE] {title}",
        flush=True
    )

    # ========================================================
    # BODY TEXT
    # ========================================================

    try:

        body = await page.locator("body").inner_text(
            timeout=15000
        )

    except Exception as exc:

        body = f"BODY ERROR: {exc}"

    clean_body = (
        body
        .replace("\r", " ")
        .replace("\n", " | ")
    )

    print(
        f"[BODY LENGTH] {len(body)}",
        flush=True
    )

    print(
        f"[BODY START] {clean_body[:8000]}",
        flush=True
    )

    # ========================================================
    # CONTENT CHECK
    # ========================================================

    body_lower = body.lower()

    print(
        "[CONTENT CHECK]"
        f" | btc={'btc' in body_lower}"
        f" | eth={'eth' in body_lower}"
        f" | sol={'sol' in body_lower}"
        f" | liquidation={'liquidation' in body_lower}"
        f" | long={'long' in body_lower}"
        f" | short={'short' in body_lower}",
        flush=True
    )

    # ========================================================
    # BTC SECTION
    # ========================================================

    btc_index = body_lower.find("btc")

    if btc_index >= 0:

        start = max(
            0,
            btc_index - 500
        )

        end = min(
            len(body),
            btc_index + 2500
        )

        btc_section = (
            body[start:end]
            .replace("\r", " ")
            .replace("\n", " | ")
        )

        print(
            f"[BTC SECTION] {btc_section}",
            flush=True
        )

    else:

        print(
            "[BTC SECTION] BTC NOT FOUND",
            flush=True
        )

    # ========================================================
    # ETH SECTION
    # ========================================================

    eth_index = body_lower.find("eth")

    if eth_index >= 0:

        start = max(
            0,
            eth_index - 500
        )

        end = min(
            len(body),
            eth_index + 2500
        )

        eth_section = (
            body[start:end]
            .replace("\r", " ")
            .replace("\n", " | ")
        )

        print(
            f"[ETH SECTION] {eth_section}",
            flush=True
        )

    else:

        print(
            "[ETH SECTION] ETH NOT FOUND",
            flush=True
        )

    # ========================================================
    # SOL SECTION
    # ========================================================

    sol_index = body_lower.find("sol")

    if sol_index >= 0:

        start = max(
            0,
            sol_index - 500
        )

        end = min(
            len(body),
            sol_index + 2500
        )

        sol_section = (
            body[start:end]
            .replace("\r", " ")
            .replace("\n", " | ")
        )

        print(
            f"[SOL SECTION] {sol_section}",
            flush=True
        )

    else:

        print(
            "[SOL SECTION] SOL NOT FOUND",
            flush=True
        )

    print("====================================================", flush=True)


# ============================================================
# MAIN
# ============================================================

async def main():

    print("====================================================", flush=True)
    print("COINGLASS LIQUIDATIONS - PAGE ACCESS TEST", flush=True)
    print("BTC + ETH + SOL", flush=True)
    print("24H LIQUIDATION DATA TEST", flush=True)
    print("NO SIGNAL ENGINE", flush=True)
    print("NO PUSHOVER", flush=True)
    print(f"URL: {URL}", flush=True)
    print("====================================================", flush=True)

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

            now = datetime.now(IST).strftime(
                "%Y-%m-%d %H:%M:%S IST"
            )

            print(
                f"\n[SCAN] {now}",
                flush=True
            )

            try:

                await test_page(page)

            except Exception as exc:

                print(
                    f"[SCAN ERROR] "
                    f"{type(exc).__name__}: {exc}",
                    flush=True
                )

            await asyncio.sleep(
                SCAN_SECONDS
            )


if __name__ == "__main__":
    asyncio.run(main())
