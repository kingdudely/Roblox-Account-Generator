#!/usr/bin/env python3
"""End-to-end harness: install the unpacked extension, seed one key, run one
identity, and print what the runner's console says.

    python3 tools/e2e.py --key key.json            # headless
    python3 tools/e2e.py --key key.json --headful  # a real window

key.json is {"client_id": "...", "client_secret": "...", "refresh_token": "..."}
for a tenant you own. Nothing is stored in this repo.

What it verifies without a browser session of yours:

  * the manifest loads and the add-on installs
  * the runner boots and storage can be seeded
  * an identity is created through the Directory API
  * backup codes are minted (or adopted) before the lane opens
  * the lane opens a container, reaches Google, and the awake shim supplies
    frames to a hidden tab
  * the finalise path: whoever gets past Google is driven through
    /not-approved by the page, not by the upgrade API

It cannot get past Google's own challenge from a clean profile. If the log
stops at "identifier for ...", the page is showing a captcha — that is Google,
not the extension. Run --headful once, solve it in the window, and the same
profile keeps the trust for later runs (--profile to pin it).
"""

import argparse
import json
import sys
import time

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options

DEFAULT_ADDON = "extension"
KEY_ID = "key_e2e"


def runner_handle(driver, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        for handle in driver.window_handles:
            driver.switch_to.window(handle)
            if "runner.html" in driver.current_url:
                return handle
        time.sleep(0.5)
    return None


def seed(driver, key, timeout):
    script = """
    const done = arguments[arguments.length - 1];
    const key = arguments[0];
    const timeout = arguments[1];
    browser.storage.local.get("rac.state").then((bag) => {
      const state = bag["rac.state"] || {};
      state.keys = [key];
      state.identities = [];
      state.accounts = [];
      state.settings = Object.assign({
        count: 1, lanes: 1, openOnInstall: false, usernameMode: "generated",
        birthday: "1996-05-15", gender: 1, acceptTerms: true, laneSurface: "tab"
      }, state.settings || {}, { count: 1, lanes: 1, timeout });
      return browser.storage.local.set({ "rac.state": state });
    }).then(() => done("seeded"), (error) => done("seed failed: " + error));
    """
    return driver.execute_async_script(script, key, timeout)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", required=True, help="JSON with client_id/client_secret/refresh_token")
    parser.add_argument("--addon", default=DEFAULT_ADDON, help="unpacked extension directory")
    parser.add_argument("--timeout", type=int, default=300, help="lane timeout in seconds")
    parser.add_argument("--headful", action="store_true", help="show the browser window")
    parser.add_argument("--profile", help="Firefox profile directory to reuse")
    parser.add_argument("--domain", default="", help="tenant domain, for the key label")
    args = parser.parse_args()

    values = json.load(open(args.key))
    key = {
        "id": KEY_ID,
        "kind": "oauth",
        "label": args.domain or values.get("domain", "e2e"),
        "domain": args.domain or values.get("domain", ""),
        "admin": values.get("admin", ""),
        "status": "ok",
        **{k: values[k] for k in ("client_id", "client_secret", "refresh_token") if k in values},
    }

    options = Options()
    if not args.headful:
        options.add_argument("-headless")
    if args.profile:
        options.add_argument("-profile")
        options.add_argument(args.profile)
    options.set_preference("xpinstall.signatures.required", False)
    options.set_preference("extensions.autoDisableScopes", 0)

    driver = webdriver.Firefox(options=options)
    failed = False
    try:
        driver.install_web_extension(args.addon, permanent=False)
        if not runner_handle(driver):
            raise SystemExit("the runner did not open — is openOnInstall disabled?")
        print("runner:", driver.current_url)
        print(seed(driver, key, args.timeout))

        driver.refresh()
        time.sleep(2)
        driver.find_element(By.ID, "start").click()
        print("batch started")

        seen = 0
        deadline = time.time() + args.timeout + 120
        markers = ("ui finalise · session 200", "ui finalise gave up", "circuit breaker")
        while time.time() < deadline:
            text = driver.find_element(By.ID, "console-body").text
            lines = text.splitlines()
            for line in lines[seen:]:
                print(line)
            seen = len(lines)
            if any(marker in text for marker in markers):
                break
            time.sleep(3)

        state = driver.execute_async_script("""
        const done = arguments[arguments.length - 1];
        browser.storage.local.get("rac.state").then((bag) => {
          const state = bag["rac.state"] || {};
          done(JSON.stringify({
            identities: (state.identities || []).map((row) => ({
              email: row.email, status: row.status, codes: (row.codes || []).length,
            })),
            accounts: (state.accounts || []).map((row) => ({
              email: row.email, status: row.status, user_id: row.user_id,
            })),
          }));
        });
        """)
        print("STATE", state)
        failed = '"status":"live"' not in state.replace(" ", "")
    finally:
        driver.quit()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
