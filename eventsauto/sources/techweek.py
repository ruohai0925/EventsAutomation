"""a16z Tech Week (tech-week.com) calendar source.

The site sits behind a Vercel bot checkpoint, so everything goes through a real
(headless) Chromium page. Calendar pages come from its tRPC endpoint; each event's
registration link is a /go/event/... redirect, resolved one at a time, slowly.
"""
import asyncio
import json
import random

from playwright.async_api import async_playwright

from ..common import UA

BASE = "https://www.tech-week.com"
REG_HOSTS = ("partiful.com", "luma.com", "lu.ma", "eventbrite.com")


async def _open_calendar(p, city):
    browser = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
    ctx = await browser.new_context(user_agent=UA)
    page = await ctx.new_page()
    await page.goto(f"{BASE}/calendar/{city}", wait_until="networkidle", timeout=90000)
    return browser, page


async def _fetch_calendar(city, delay):
    async with async_playwright() as p:
        browser, page = await _open_calendar(p, city)
        events, cursor = [], 1
        while True:
            body = {"0": {"city": city, "q": "", "featured": False, "day": "all", "track": [], "sponsor": [],
                          "theme": [], "format": [], "location": [], "time": [], "host": [],
                          "sortBy": "time", "sortOrder": "asc", "cursor": cursor, "direction": "forward"}}
            text = await page.evaluate(
                """async (b) => (await fetch('/api/trpc/calendar.events?batch=1',
                    {method: 'POST', headers: {'content-type': 'application/json'}, body: b})).text()""",
                json.dumps(body))
            data = json.loads(text)[0]["result"]["data"]
            data = data.get("json", data)
            if not data["results"]:
                break
            events += data["results"]
            print(f"  page {cursor}: {len(events)}/{data['total']}", flush=True)
            cursor += 1
            await asyncio.sleep(delay + random.uniform(0, 1))
        await browser.close()
        return events


def fetch_calendar(city="sf", delay=2.0):
    return asyncio.run(_fetch_calendar(city, delay))


async def _resolve_links(events, delay, on_progress, city):
    out = {}
    async with async_playwright() as p:
        browser, page = await _open_calendar(p, city)
        target = {}

        def on_request(req):
            # server-side 302 from /go/event/... shows up as a new main-frame navigation
            if req.is_navigation_request() and req.frame == page.main_frame and any(h in req.url for h in REG_HOSTS):
                target["url"] = req.url

        def on_response(resp):
            if "/go/event/" in resp.url and resp.status == 429:
                target["url"] = "RATE_LIMITED"

        page.on("request", on_request)
        page.on("response", on_response)
        for i, e in enumerate(events):
            target.clear()
            try:
                await page.goto(BASE + e["externalHref"], wait_until="commit", timeout=30000)
                await page.goto("about:blank")
            except Exception:
                pass
            for _ in range(40):
                if "url" in target:
                    break
                await asyncio.sleep(0.25)
            url = target.get("url")
            if url == "RATE_LIMITED":
                print("  rate limited; backing off 60s", flush=True)
                await asyncio.sleep(60)
            elif url:
                out[e["id"]] = url.split("?")[0]
            on_progress(i, e, out.get(e["id"]))
            await asyncio.sleep(delay + random.uniform(0, 1.5))
        await browser.close()
    return out


def resolve_links(events, delay=3.0, on_progress=lambda *a: None, city="sf"):
    """Map tech-week event id -> registration URL (Partiful/Luma/...)."""
    return asyncio.run(_resolve_links(events, delay, on_progress, city))
