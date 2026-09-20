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

    print(
        f"[DEBUG] Liquidation Value controls={count}",
        flush=True,
    )

    # --------------------------------------------------------
    # FIND ACTUALLY VISIBLE CONTROL
    # --------------------------------------------------------

    visible_control = None

    for i in range(count):

        candidate = controls.nth(i)

        try:
            if await candidate.is_visible():
                visible_control = candidate

                print(
                    f"[DEBUG] Visible VALUE control index={i}",
                    flush=True,
                )

                break

        except Exception:
            continue

    if visible_control is None:
        raise RuntimeError(
            "No visible Liquidation Value control found"
        )

    # No scroll_into_view_if_needed().
    # It was causing the timeout.

    await visible_control.click(
        timeout=10000,
    )

    await page.wait_for_timeout(500)

    # --------------------------------------------------------
    # FIND VISIBLE TRADES OPTION
    # --------------------------------------------------------

    options = page.get_by_text(
        "Liquidation Trades",
        exact=True,
    )

    option_count = await options.count()

    print(
        f"[DEBUG] Liquidation Trades options={option_count}",
        flush=True,
    )

    visible_option = None

    for i in range(option_count):

        candidate = options.nth(i)

        try:
            if await candidate.is_visible():
                visible_option = candidate

                print(
                    f"[DEBUG] Visible TRADES option index={i}",
                    flush=True,
                )

                break

        except Exception:
            continue

    if visible_option is None:
        raise RuntimeError(
            "No visible Liquidation Trades option found"
        )

    await visible_option.click(
        timeout=10000,
    )

    # Allow CoinGlass values to redraw.
    await page.wait_for_timeout(4000)

    print(
        "[DROPDOWN] TRADES selected",
        flush=True,
    )
