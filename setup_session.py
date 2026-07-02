"""
Run this ONCE manually to save your Amazon session cookies.
Opens a headed browser so you can log in manually.
Saves cookies to amazon_session.json when you press Enter.

Usage:
    python setup_session.py
"""
import json
import os

from playwright.sync_api import sync_playwright

SESSION_FILE = os.path.join(os.path.dirname(__file__), "amazon_session.json")
AMAZON_URL = os.getenv("AMAZON_BASE_URL", "https://www.amazon.com")


def setup():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)  # headed — you drive it
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.goto(f"{AMAZON_URL}/ap/signin")
        print("Log in manually in the browser window.")
        print("Complete any OTP or CAPTCHA if prompted.")
        input(
            "Press Enter here once you are fully logged in "
            "and on the Amazon homepage..."
        )
        cookies = context.cookies()
        with open(SESSION_FILE, "w") as f:
            json.dump(cookies, f, indent=2)
        print(f"Session saved to {SESSION_FILE} — {len(cookies)} cookies stored.")
        browser.close()


if __name__ == "__main__":
    setup()
