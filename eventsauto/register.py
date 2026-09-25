"""Automated RSVP / apply on Partiful using a persistent, logged-in browser profile.

Login is phone + SMS code, so `login` opens a visible browser once for you to sign in yourself;
the session is kept in <workspace>/.browser-profile and reused by `register`.
Each attempt is logged to data/registrations.json with a screenshot in data/shots/.
"""
import json
import random
import re
import time
from datetime import datetime

import yaml
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

ACTION_BTN = re.compile(r"^\W*(rsvp|apply|request to join|join waitlist|get on the list|register|going)\b", re.I)
NEXT_BTN = re.compile(r"^(continue|next|submit|send|apply|request|done|rsvp|confirm|join)", re.I)
DONE_TEXT = re.compile(r"you'?re going|you'?re on the list|application (sent|submitted|received)|pending approval|"
                       r"request(ed| sent)|waitlist(ed)?|approved|you applied|see you there|edit rsvp|change rsvp", re.I)


def _clear_stale_lock(profile_dir):
    """A crashed run can leave Chromium's SingletonLock behind; drop it if its pid is gone."""
    import os
    lock = profile_dir / "SingletonLock"
    try:
        pid = int(os.readlink(lock).rsplit("-", 1)[1])
        os.kill(pid, 0)
    except (OSError, ValueError, IndexError):
        for f in profile_dir.glob("Singleton*"):
            f.unlink(missing_ok=True)


def _browser(p, ws, headless):
    _clear_stale_lock(ws.browser_profile)
    return p.chromium.launch_persistent_context(
        str(ws.browser_profile), headless=headless, viewport={"width": 1280, "height": 900}, service_workers="block",
        args=["--disable-blink-features=AutomationControlled"])


def login(ws, timeout_min=15):
    with sync_playwright() as p:
        ctx = _browser(p, ws, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://partiful.com/login?redirect=%2Fevents")
        print("A browser window opened. Sign in to Partiful there (phone + SMS code).", flush=True)
        deadline = time.time() + timeout_min * 60
        while time.time() < deadline:
            page.wait_for_timeout(3000)
            if "/login" not in page.url and page.get_by_text(re.compile(r"welcome back|upcoming", re.I)).count():
                page.wait_for_timeout(3000)  # let auth persist to storage
                print(f"Logged in. Session saved in {ws.browser_profile}", flush=True)
                break
        else:
            print("Timed out waiting for login.")
        ctx.close()


def is_logged_in(page):
    """The app shell briefly shows a "Login" link before auth resolves, so only trust the
    logged-in home page appearing (up to ~15 s)."""
    page.goto("https://partiful.com/events", wait_until="domcontentloaded")
    for _ in range(30):
        page.wait_for_timeout(500)
        if page.get_by_text(re.compile(r"welcome back|upcoming", re.I)).count():
            return True
        if "/login" in page.url:
            return False
    return False


def ensure_login(ws):
    """Partiful web sessions expire; if so, open the visible login window and wait."""
    with sync_playwright() as p:
        ctx = _browser(p, ws, headless=True)
        ok = is_logged_in(ctx.pages[0] if ctx.pages else ctx.new_page())
        ctx.close()
    if not ok:
        print("Partiful session expired — opening the login window; sign in there (phone + SMS).", flush=True)
        login(ws)


def _pick_option(ans, options):
    a = str(ans).lower()
    exact = [o for o in options if o.lower().strip() == a]
    if exact:
        return exact[0]
    word = re.compile(r"(?<![a-z])" + re.escape(a) + r"(?![a-z])")  # "man" must not match "woman"
    part = [o for o in options if word.search(o.lower())] or [o for o in options if o.lower().strip() and o.lower() in a]
    return part[0] if part else None


def _answer_for(question, profile):
    """Answer from, in order: standard Tech Week fields, then the ordered `answers` rules.
    Multiple-choice answers must match an option; otherwise the next rule is tried."""
    me = {k: ("" if v is None else v) for k, v in profile["me"].items()}
    qt = question["text"].lower()
    opts = question.get("options") or []
    fixed = {"techweek_first_name": me["first_name"], "techweek_last_name": me["last_name"],
             "techweek_email": me["email"], "techweek_linkedin": me["linkedin"], "techweek_company": me["company"],
             "techweek_role": me["job_title"], "techweek_github": me.get("github", "")}
    # hosts sometimes repurpose a standard field (same id, new question) — require the text to agree
    expect = {"techweek_first_name": "first", "techweek_last_name": "last", "techweek_email": "e-?mail",
              "techweek_linkedin": "linkedin", "techweek_company": "company", "techweek_role": "title|role",
              "techweek_github": "github"}
    if question["id"] in fixed and not opts and re.search(expect[question["id"]], qt):
        return fixed[question["id"]]
    if not opts:
        if question["type"] == "email" or re.search(r"\be-?mail\b", qt) and "agree" not in qt:
            return me["email"]
        if re.search(r"first name", qt):
            return me["first_name"]
        if re.search(r"last name|surname", qt):
            return me["last_name"]
        if re.fullmatch(r"\W*(full )?name\W*\*?\W*|what is your (full )?name\W*", qt):
            return f"{me['first_name']} {me['last_name']}"
    rules = profile.get("answers") or []
    if isinstance(rules, dict):
        rules = list(rules.items())
    for pat, ans in rules:
        if not re.search(pat, qt, re.I):
            continue
        ans = str(ans).format(**me)
        if not opts:
            return ans
        choice = _pick_option(ans, opts)
        if choice:
            return choice
    return None


def _live_fields(page):
    """Read the questionnaire as currently rendered: label, required flag, control kind."""
    return page.locator("form#questionnaire").evaluate("""form => {
        const out = [];
        for (const lab of form.querySelectorAll('span')) {
            const box = lab.parentElement;
            if (lab !== box.firstElementChild) continue;
            const ctl = box.querySelector('input:not([type=hidden]), textarea, button');
            if (!ctl || !lab.innerText.trim()) continue;
            out.push({label: lab.innerText.trim(), kind: ctl.tagName === 'BUTTON' ? 'select' : ctl.tagName.toLowerCase(),
                      value: ctl.tagName === 'BUTTON' ? ctl.innerText.trim() : ctl.value});
        }
        return out;
    }""")


def _fill_live(page, profile, cached_questions):
    """Fill every field of the live questionnaire. Returns (filled, missing_required)."""
    by_text = {q["text"].strip().lower(): q for q in cached_questions}
    form = page.locator("form#questionnaire")
    filled, missing = [], []
    for f in _live_fields(page):
        text = f["label"].rstrip(" *").strip()
        required = f["label"].endswith("*")
        q = dict(by_text.get(text.lower(), {"id": "", "type": "short_answer"}), text=text, required=required)
        box = form.locator("xpath=.//span[normalize-space(.)=" + _xpath_str(f["label"]) + "]/..").first
        if f["kind"] == "select":
            box.locator("button").first.click()
            page.wait_for_timeout(600)
            opts = [o.strip() for o in page.locator("button[aria-selected]").all_inner_texts() if o.strip()]
            q["options"] = opts
            ans = _answer_for(q, profile)
            if ans:
                page.locator("button[aria-selected]").filter(has_text=ans).first.click()
                page.wait_for_timeout(400)
                filled.append((text, ans))
            else:
                page.keyboard.press("Escape")
                if required:
                    missing.append(f"{text}  [options: {' | '.join(opts)}]")
        else:
            q["options"] = []
            ans = _answer_for(q, profile)
            if ans:
                box.locator("input:not([type=hidden]), textarea").first.fill(str(ans))
                filled.append((text, ans))
            elif required and not f["value"]:
                missing.append(text)
    return filled, missing


def _xpath_str(s):
    if "'" not in s:
        return f"'{s}'"
    return "concat('" + s.replace("'", "', \"'\", '") + "')"


def _dialog(page):
    d = page.get_by_role("dialog")
    return d.first if d.count() else page


def _has_questionnaire(page):
    """Read the live event data embedded in the page (fresher than our cached details)."""
    try:
        return bool(page.evaluate("""() => { const ev = JSON.parse(document.getElementById('__NEXT_DATA__')
            .textContent).props.pageProps.event; return ev.questionnaireEnabled &&
            (ev.questionnaire?.questions || []).length > 0; }"""))
    except Exception:
        return None


def register_one(page, ev, detail, profile, dry_run, shots):
    """Partiful flow: action button -> "Get on the list" (Continue) -> host questionnaire
    (Continue) -> done. Dry run never makes the final click. Whether it really went through is
    decided afterwards from the account's event list, not from page text."""
    page.goto(ev["url"], wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    has_q = _has_questionnaire(page)
    btn = page.get_by_role("button", name=ACTION_BTN)
    if not btn.count():
        return "no_button", None
    btn.first.click()
    page.wait_for_timeout(2500)
    dialog = _dialog(page)
    if dialog.get_by_placeholder(re.compile("phone", re.I)).count():
        return "not_logged_in", None

    filled, clicked_final = [], False
    for _step in range(4):
        form = page.locator("form#questionnaire")
        if form.count():
            filled, missing = _fill_live(page, profile, detail.get("questions", []))
            if missing:
                page.screenshot(path=str(shots / f"{ev['id']}_missing.png"))
                return "needs_input", missing
        nxt = dialog.get_by_role("button", name=NEXT_BTN)
        if clicked_final and (not nxt.count() or not form.count()):
            break  # past the last step: dialog now shows a confirmation screen
        if not nxt.count():
            return "stuck", None
        final = bool(form.count()) or has_q is False
        if dry_run and final:
            page.screenshot(path=str(shots / f"{ev['id']}_dryrun.png"))
            return "dry_run_ok", {"filled": filled}
        if dry_run and has_q is None:
            return "dry_run_unsure", None  # can't tell if Continue would submit; don't risk it
        if not nxt.last.is_enabled():
            page.screenshot(path=str(shots / f"{ev['id']}_disabled.png"))
            return "continue_disabled", {"filled": filled}
        had_form = form.count() > 0
        clicked_final = final
        nxt.last.click()
        for _ in range(16):  # wait for the next step (questionnaire) or for the dialog to close
            page.wait_for_timeout(500)
            if not page.get_by_role("dialog").count() or (not had_form and page.locator("form#questionnaire").count()):
                break
        if not page.get_by_role("dialog").count():
            break
        dialog = _dialog(page)
    page.wait_for_timeout(2000)
    page.screenshot(path=str(shots / f"{ev['id']}.png"))
    return "submitted", {"filled": filled}


def register_all(ws, dry_run=False, only=None, headless=False):
    profile = ws.profile
    picks = yaml.safe_load((ws.root / "picks.yaml").read_text(encoding="utf-8"))
    details = ws.load("details.json", {})
    log = ws.load("registrations.json", {})
    shots = ws.data / "shots"
    shots.mkdir(exist_ok=True)
    todo = [p for p in picks if p.get("register")]
    if only:
        ids = set(only.split(","))
        todo = [p for p in todo if p["id"] in ids]
    todo = [p for p in todo if not str(log.get(p["id"], {}).get("status", "")).startswith(("submitted", "already"))]
    mine = sync_mine(ws)  # never double-register what you did by hand
    todo = [p for p in todo if p["url"].rstrip("/").split("/")[-1] not in mine]
    print(f"{len(todo)} events to register{' (dry run)' if dry_run else ''}")
    needs = {}
    with sync_playwright() as p:
        ctx = _browser(p, ws, headless=headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        if not is_logged_in(page):
            ctx.close()
            raise SystemExit("Still not logged in to Partiful — rerun and complete the login window.")
        for p_ in todo:
            try:
                status, extra = register_one(page, p_, details[p_["id"]], profile, dry_run, shots)
            except PWTimeout as ex:
                status, extra = "error", str(ex)[:200]
            print(f"  {p_['when']}  {p_['name'][:60]}  ->  {status}", flush=True)
            if status == "needs_input":
                needs[p_["name"]] = extra
            if status == "not_logged_in":
                break
            if dry_run:
                dry = ws.load("dryrun.json", {})
                dry[p_["id"]] = {"name": p_["name"], "status": status, "extra": extra}
                ws.save("dryrun.json", dry)
            else:
                log[p_["id"]] = {"status": status, "at": datetime.now().isoformat(timespec="seconds"),
                                 "name": p_["name"], "url": p_["url"], "extra": extra}
                ws.save("registrations.json", log)
            time.sleep(4 + random.uniform(0, 4))  # pace like a human
        ctx.close()
    if not dry_run and todo:
        mine = sync_mine(ws)  # the account is the source of truth for what went through
        for p_ in todo:
            r = log.get(p_["id"])
            if r and r["status"] in ("submitted", "stuck", "unknown"):
                m = mine.get(p_["url"].rstrip("/").split("/")[-1])
                r["status"] = f"submitted:{m['status'].lower()}" if m else "not_confirmed"
        ws.save("registrations.json", log)
        from collections import Counter
        print("verified:", dict(Counter(log[p_["id"]]["status"] for p_ in todo if p_["id"] in log)))
    if needs:
        (ws.data / "needs_input.yaml").write_text(yaml.safe_dump(needs, allow_unicode=True, width=200), encoding="utf-8")
        print(f"{len(needs)} events need extra answers -> data/needs_input.yaml "
              "(add regex->answer entries under `answers:` in profile.yaml and rerun)")


def sync_mine(ws, headless=True):
    """Pull the account's upcoming Partiful events + guest status -> data/mine.json
    (keyed by Partiful event id). Covers events registered by hand, too."""
    ensure_login(ws)
    got = {}
    with sync_playwright() as p:
        ctx = _browser(p, ws, headless=headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def on(resp):
            if "getMyUpcomingEventsForHomePage" in resp.url:
                try:
                    got["data"] = resp.json()["result"]["data"]
                except Exception:
                    pass

        page.on("response", on)
        page.goto("https://partiful.com/events", wait_until="domcontentloaded")
        for _ in range(30):
            if "data" in got:
                break
            page.wait_for_timeout(500)
        ctx.close()
    if "data" not in got:
        raise SystemExit("Could not read your Partiful events (logged out?). Run `login` first.")
    mine = {}
    for e in got["data"]["upcomingEvents"]:
        mine[e["id"]] = {"title": e["title"].split("\n")[0], "start": e["startDate"],
                         "status": (e.get("guest") or {}).get("status"), "guest_action": e.get("guestAction"),
                         "url": f"https://partiful.com/e/{e['id']}"}
    ws.save("mine.json", mine)
    from collections import Counter
    print(f"{len(mine)} upcoming events on your Partiful account: {dict(Counter(m['status'] for m in mine.values()))}")
    return mine
