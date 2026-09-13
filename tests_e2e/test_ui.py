import re

from playwright.sync_api import expect

from conftest import enroll_device, save_configurations


def test_enroll_device_via_form_lands_on_configure_page(page, live_server):
    page.goto(live_server)

    page.get_by_label("Device identifier").fill("board-e2e")
    page.get_by_role("button", name="Enroll new device").click()

    page.wait_for_url("**/devices/board-e2e/configure")
    expect(page.locator("h1")).to_contain_text("board-e2e")
    # Enrollment pre-populates one blank default configuration row.
    expect(page.locator(".config-row")).to_have_count(1)

    # The device now shows up in the list with configure/dashboard links.
    page.goto(live_server)
    row = page.locator("table.devices tbody tr")
    expect(row).to_have_count(1)
    expect(row.locator("td").first).to_have_text("board-e2e")
    expect(row.get_by_role("link", name="Configure")).to_be_visible()
    expect(row.get_by_role("link", name="View dashboard")).to_be_visible()


def test_enroll_duplicate_device_shows_error(page, live_server):
    enroll_device(live_server, "board-e2e")

    page.goto(live_server)
    page.get_by_label("Device identifier").fill("board-e2e")
    page.get_by_role("button", name="Enroll new device").click()

    expect(page.locator("#enroll-status")).to_contain_text("already enrolled")


def test_configure_device_via_station_picker(page, live_server):
    enroll_device(live_server, "board-e2e")
    page.goto(f"{live_server}/devices/board-e2e/configure")

    origin = page.locator(".config-row .origin-input")
    origin.fill("Far")
    options = page.locator(".station-picker-list li")
    expect(options).to_have_count(1)
    expect(options.first).to_have_text("Far Away Ave (A)")
    options.first.click()
    expect(origin).to_have_value("Far Away Ave")

    # The service dropdown is limited to routes serving the origin station.
    service = page.locator(".config-row .service-select")
    expect(service.locator("option")).to_have_text(["A"])

    page.locator(".config-row .direction-select").select_option("downtown")

    page.get_by_role("button", name="Save configurations").click()
    expect(page.locator("#save-status")).to_have_text("Saved.")


def test_duplicate_service_is_rejected_in_page(page, live_server):
    enroll_device(live_server, "board-e2e")
    page.goto(f"{live_server}/devices/board-e2e/configure")

    page.get_by_role("button", name="+ Add configuration").click()
    for row in page.locator(".config-row").all():
        row.locator(".origin-input").fill("Test")
        page.get_by_text("Test Station (A, C)", exact=True).click()
        row.locator(".service-select").select_option("A")
        row.locator(".direction-select").select_option("uptown")

    page.get_by_role("button", name="Save configurations").click()
    expect(page.locator("#save-status")).to_contain_text("one configuration per service")


def test_dashboard_shows_upcoming_departure(page, live_server):
    enroll_device(live_server, "board-e2e")
    save_configurations(
        live_server,
        "board-e2e",
        [{"origin_station_id": "101", "service": "A", "direction": "uptown"}],
    )

    page.goto(f"{live_server}/devices/board-e2e/dashboard")
    board = page.locator(".departures-board")
    expect(board.locator(".row-index").first).to_have_text("1")
    expect(board.locator(".service-bullet").first).to_have_text("A")
    expect(board.locator(".terminus").first).to_have_text("Far Away Ave")
    # The stub train arrives ~5 minutes after server start.
    expect(board.locator(".minutes").first).to_contain_text(re.compile(r"^[45]"))


def test_dashboard_debug_pretty_prints_api_json(page, live_server):
    enroll_device(live_server, "board-e2e")
    save_configurations(
        live_server,
        "board-e2e",
        [{"origin_station_id": "101", "service": "A", "direction": "uptown"}],
    )

    page.goto(f"{live_server}/devices/board-e2e/dashboard?debug=1")
    # The board still renders...
    expect(page.locator(".departures-board .terminus").first).to_have_text("Far Away Ave")
    # ...with the exact API payload pretty-printed below it.
    debug = page.locator(".debug-json")
    expect(debug).to_contain_text('"service": "A"')
    expect(debug).to_contain_text('"terminus": "Far Away Ave"')
    expect(debug).to_contain_text('"arrives_at":')


def test_dashboard_updates_last_request_timestamp(page, live_server):
    enroll_device(live_server, "board-e2e")
    save_configurations(
        live_server,
        "board-e2e",
        [{"origin_station_id": "101", "service": "A", "direction": "uptown"}],
    )

    page.goto(f"{live_server}/devices/board-e2e/dashboard")
    expect(page.locator(".departures-board .terminus").first).to_have_text("Far Away Ave")

    page.goto(live_server)
    # Polling the API as the device stamped the row, so it no longer says "never".
    expect(page.locator("table.devices tbody td").nth(1)).not_to_have_text("never")
