import os
import pytest

RUN_E2E = os.environ.get('RUN_E2E') == '1'


@pytest.mark.skipif(not RUN_E2E, reason='E2E tests are skipped by default; set RUN_E2E=1 to run')
def test_ui_upload_and_progress():
    """Scaffold for an end-to-end UI test using Playwright or Selenium.
    This test is skipped by default. To run, set RUN_E2E=1 and ensure Playwright/Selenium is installed
    and a display or headless runner is available.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        pytest.skip('Playwright not installed')

    # Minimal flow (not executed in CI by default):
    # - open the app at http://127.0.0.1:3000
    # - attach a sample PDF and click upload
    # - wait for progress bar to reach 100 and for download link or error to appear
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto('http://127.0.0.1:3000')
        # user would perform upload and assert results here
        browser.close()