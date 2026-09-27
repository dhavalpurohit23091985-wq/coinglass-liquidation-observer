import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright


# ============================================================
# CONFIG
# ============================================================
URL = "https://www.coinglass.com/inflow-outflow-history"
SCAN_SECONDS = 30

IST = ZoneInfo("Asia/Kolkata")


# ============================================================
# PAGE TEST
# ============================================================

async def test_page(page):

    print("====================================================", flush=True)
    print("[COINGLASS INFLOW-OUTFLOW ACCESS TEST]", flush=True)
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

    # --------------------------------------------------------
    # TITLE
    # --------------------------------------------------------

    try:
        title = await page.title()

    except Exception as exc:
        title = f"ERROR: {exc}"

    print(
        f"[PAGE TITLE] {title}",
        flush=True
    )

    # --------------------------------------------------------
    # BODY
    # --------------------------------------------------------

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
        f"[BODY START] {clean_body[:4000]}",
        flush=True
    )

    # --------------------------------------------------------
    # CONTENT CHECK
    # --------------------------------------------------------

    body_lower = body.lower()

    print(
        "[CONTENT CHECK]"
        f" | btc={'btc' in body_lower}"
        f" | futures={'futures' in body_lower}"
        f" | 5minute={'5 minute' in body_lower}"
        f" | inflow={'inflow' in body_lower}"
        f" | outflow={'outflow' in body_lower}",
        flush=True
    )

    # Useful context around BTC if available.
    btc_index = body_lower.find("btc")

    if btc_index >= 0:

        start = max(0, btc_index - 500)
        end = min(len(body), btc_index + 2000)

        btc_section = (
            body[start:end]
            .replace("\r", " ")
            .replace("\n", " | ")
        )

        print(
            f"[BTC SECTION] {btc_section}",
            flush=True
        )

    print("====================================================", flush=True)


# ============================================================
# MAIN
# ============================================================

async def main():

    print("====================================================", flush=True)
    print("COINGLASS BTC NETFLOW - PAGE ACCESS TEST", flush=True)
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

            await asyncio.sleep(SCAN_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
