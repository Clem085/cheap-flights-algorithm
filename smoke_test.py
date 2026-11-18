#!/usr/bin/env python3
import argparse
import shutil
from playwright.sync_api import sync_playwright

p = argparse.ArgumentParser()
p.add_argument("--headless", action="store_true")
args = p.parse_args()

browser_path = (
    shutil.which("chromium")
    or shutil.which("google-chrome-stable")
    or shutil.which("google-chrome")
    or shutil.which("brave")
)
if not browser_path:
    raise SystemExit("No Chromium-family browser found. On Arch: sudo pacman -S chromium")

with sync_playwright() as pw:
    browser = pw.chromium.launch(
        executable_path=browser_path,
        headless=args.headless,
    )
    page = browser.new_page()
    page.goto("https://www.google.com/travel/flights", wait_until="domcontentloaded")
    print("Browser:", browser_path)
    print("Title:", page.title())
    print("URL:", page.url)
    browser.close()
