"""Read job pages in a real (headless) Chromium: for postings a plain fetch can't read (JavaScript-only career sites).

Runs with the workspace Playwright venv, not the project venv:
  echo '["https://..."]' | ~/workspace/.tools/pw-venv/bin/python scripts/browser_text.py
stdout: {"url": {"text": "...", "why": null} | {"text": null, "why": "blocked" | "not found" | "error: ..."}}
"""
import json
import re
import sys

from playwright.sync_api import sync_playwright

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
BLOCKED = re.compile(r"access denied|you don't have permission|are you a robot|verify you are human|"
                     r"enable javascript and cookies|request blocked|captcha", re.I)
GONE = re.compile(r"(job|position|posting) (is )?(no longer|not) (available|found|open)|page not found|404", re.I)


def read(urls: list[str]) -> dict:
    out = {}
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(user_agent=UA, locale="en-US", viewport={"width": 1280, "height": 1600})
        for u in urls:
            pg = ctx.new_page()
            try:
                pg.goto(u, wait_until="domcontentloaded", timeout=30000)
                pg.wait_for_timeout(4000)  # career sites render the posting after load
                t = pg.inner_text("body")
                head = t[:600]
                if BLOCKED.search(head) and len(t) < 2000:
                    out[u] = {"text": None, "why": "blocked"}
                elif len(t) < 800:
                    out[u] = {"text": None, "why": "not found" if GONE.search(t) else "too short"}
                else:
                    out[u] = {"text": t, "why": None}
            except Exception as e:  # noqa: BLE001 - one bad page must not stop the rest
                out[u] = {"text": None, "why": "error: " + str(e)[:80]}
            finally:
                pg.close()
        b.close()
    return out


if __name__ == "__main__":
    print(json.dumps(read(json.load(sys.stdin))))
