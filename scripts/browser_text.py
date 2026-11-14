"""Read job pages in a real (headless) Chromium: for postings a plain fetch can't read (JavaScript-only career sites).

Runs with the workspace Playwright venv, not the project venv:
  echo '["https://..."]' | ~/workspace/.tools/pw-venv/bin/python scripts/browser_text.py
stdout: one JSON line per page, printed as soon as it is read (so a timeout keeps the pages already read):
  {"url": "...", "text": "...", "why": null} | {"url": "...", "text": null, "why": "blocked" | "not found" | "error: ..."}
"""
import json
import re
import sys

from playwright.sync_api import sync_playwright

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
BLOCKED = re.compile(r"access denied|you don't have permission|are you a robot|verify you are human|"
                     r"enable javascript and cookies|request blocked|captcha", re.I)
GONE = re.compile(r"(job|position|posting) (is )?(no longer|not) (available|found|open)|page not found|404", re.I)


def read(urls: list[str]) -> None:
    def emit(u, text, why):
        print(json.dumps({"url": u, "text": text, "why": why}), flush=True)

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(user_agent=UA, locale="en-US", viewport={"width": 1280, "height": 1600})
        for u in urls:
            pg = ctx.new_page()
            try:
                pg.goto(u, wait_until="domcontentloaded", timeout=20000)
                pg.wait_for_timeout(4000)  # career sites render the posting after load
                t = pg.inner_text("body")
                head = t[:600]
                if BLOCKED.search(head) and len(t) < 2000:
                    emit(u, None, "blocked")
                elif len(t) < 800:
                    emit(u, None, "not found" if GONE.search(t) else "too short")
                else:
                    emit(u, t, None)
            except Exception as e:  # noqa: BLE001 - one bad page must not stop the rest
                emit(u, None, "error: " + str(e)[:80])
            finally:
                pg.close()
        b.close()


if __name__ == "__main__":
    read(json.load(sys.stdin))
