"""Usage: python -m eventsauto <command> --ws SF_tech

  fetch      pull the whole calendar                       -> data/calendar.json
  links      pre-filter by profile, resolve RSVP links     -> data/links.json      (slow, resumable)
  details    fetch full event pages for candidates          -> data/details.json    (throttled, resumable)
  rank       score + build a conflict-free plan             -> shortlist.md, picks.yaml
  login      open the automation browser to sign in to Partiful once
  sync       pull your existing Partiful RSVPs/applications   -> data/mine.json
  register   RSVP/apply to events marked `register: true` in picks.yaml
  serve      local web page to review picks and launch registration (http://localhost:8765)
  calendar   export registered events to .ics / Google Calendar
"""
import argparse
import sys

import requests
import yaml

from . import rank as R
from .common import Throttle, Workspace
from .platforms import partiful
from .sources import techweek


def cmd_fetch(ws, a):
    ev = techweek.fetch_calendar(a.city)
    ws.save("calendar.json", ev)
    print(f"{len(ev)} events")


def cmd_links(ws, a):
    cal, links = ws.load("calendar.json"), ws.load("links.json", {})
    cands = R.prefilter(cal, ws.profile)
    prof = ws.profile
    cands.sort(key=lambda e: -R.score(e, prof)[0])  # most relevant first, so partial runs are useful
    todo = [e for e in cands if e["id"] not in links][: a.limit or None]
    print(f"{len(cands)} candidates match your profile; {len(todo)} links left to resolve")

    def progress(i, e, url):
        if url:
            links[e["id"]] = url
        print(f"  [{i + 1}/{len(todo)}] {e['name'][:60]} -> {url}", flush=True)
        if i % 10 == 0:
            ws.save("links.json", links)

    techweek.resolve_links(todo, delay=a.delay, on_progress=progress, city=a.city)
    ws.save("links.json", links)


def cmd_details(ws, a):
    links, details = ws.load("links.json", {}), ws.load("details.json", {})
    th, s = Throttle(a.delay), requests.Session()
    todo = [(k, u) for k, u in links.items() if k not in details and "partiful.com" in u]
    for i, (k, u) in enumerate(todo):
        th.wait()
        try:
            details[k] = partiful.fetch_event(u, s)
        except Exception as ex:
            print(f"  ! {u}: {ex}")
            continue
        print(f"  [{i + 1}/{len(todo)}] {details[k]['title'][:60]}", flush=True)
        if i % 10 == 0:
            ws.save("details.json", details)
    ws.save("details.json", details)


def _pid(url):
    return url.rstrip("/").split("/")[-1]


def _mine_rows(ws, prof=None, scored=None):
    """Events already on the Partiful account, as plan rows, scored like everything else."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    rows, cache = [], ws.load("mine_details.json", {})
    for pid, m in (ws.load("mine.json", {}) or {}).items():
        if m["status"] in ("DECLINED", "WITHDRAWN", "REJECTED"):
            continue
        try:
            st = datetime.fromisoformat(m["start"].replace("Z", "+00:00")).astimezone(
                ZoneInfo((prof or {}).get("timezone", "America/Los_Angeles"))).replace(tzinfo=None)
        except (ValueError, AttributeError):  # "TBD" on Partiful: fall back to the calendar's time
            if not (scored and pid in scored):
                continue
            st = scored[pid]["_st"]
        row = {"pid": pid, "_st": st, "_en": st + timedelta(hours=2), "name": m["title"], "status": m["status"],
               "score": 0, "why": [], "food": []}
        if scored and pid in scored:  # also in the candidate pool
            src = scored[pid]
            row.update(score=src["score"], why=src["why"], food=src["food"], _st=src["_st"], _en=src["_en"])
        elif prof:
            try:
                if pid not in cache:
                    Throttle(2).wait()
                    cache[pid] = partiful.fetch_event(m["url"])
                d = cache[pid]
                pseudo = {"name": d["title"], "company": "", "excerpt": "", "facets": {},
                          "location": d.get("neighborhood") or "", "date": st.date().isoformat(), "time": f"{st:%H:%M}"}
                row["score"], row["why"], row["food"] = R.score(pseudo, prof, d)
                row["_st"], row["_en"] = R.start_end(pseudo, d)
            except Exception:
                pass
        rows.append(row)
    ws.save("mine_details.json", cache)
    return rows


def _overlaps(a, b, buf_min=30, drop_in_h=3):
    """Time clash with a travel buffer. Long events (summits, all-day hacks, multi-day typos)
    are treated as drop-in: you'd spend ~3 h there, not block the whole day."""
    from datetime import timedelta
    buf, cap = timedelta(minutes=buf_min), timedelta(hours=drop_in_h)
    a_en, b_en = min(a["_en"], a["_st"] + cap), min(b["_en"], b["_st"] + cap)
    return a["_st"] < b_en + buf and b["_st"] < a_en + buf


def _tiers(rows, mine, per_day, backups, a_min, b_min):
    """A = what you'd actually attend: best-scoring, no overlaps, <= per_day per day, chosen
    from new candidates AND your existing registrations together (existing ones get +1 for being
    already in motion). B = up to `backups` approval-based alternatives per A slot."""
    pool = [dict(m, _mine=True, _key="m:" + m["pid"]) for m in mine] + [dict(r, _key=r["id"]) for r in rows]
    a_tier = []
    for r in sorted(pool, key=lambda r: -(r["score"] + (1 if r.get("_mine") else 0))):
        if r["score"] < a_min and not r.get("_mine"):
            continue
        day = [c for c in a_tier if c["_st"].date() == r["_st"].date()]
        if len(day) < per_day and not any(_overlaps(r, c) for c in day):
            a_tier.append(r)
    a_keys = {r["_key"] for r in a_tier}
    cover = {}
    for m in pool:  # existing registrations outside A already act as backups
        if m.get("_mine") and m["_key"] not in a_keys:
            for c in a_tier:
                if _overlaps(m, c):
                    cover[c["_key"]] = cover.get(c["_key"], 0) + 1
    b_keys = set()
    for r in sorted(rows, key=lambda r: -r["score"]):
        if r["id"] in a_keys or r["action"] != "APPLY" or r["score"] < b_min:
            continue
        slot = [c for c in a_tier if _overlaps(r, c)]
        if slot and all(cover.get(c["_key"], 0) < backups for c in slot):
            b_keys.add(r["id"])
            for c in slot:
                cover[c["_key"]] = cover.get(c["_key"], 0) + 1
    return a_keys, b_keys


def _old_picks(ws):
    path = ws.root / "picks.yaml"
    if not path.exists():
        return {}
    return {p["id"]: p for p in yaml.safe_load(path.read_text(encoding="utf-8")) or []}


def cmd_rank(ws, a):
    prof, cal = ws.profile, {e["id"]: e for e in ws.load("calendar.json")}
    links, details = ws.load("links.json", {}), ws.load("details.json", {})
    rows = []
    for k, d in details.items():
        e = cal[k]
        s, why, food = R.score(e, prof, d)
        terms = R.score.last_terms
        if d.get("at_capacity"):
            s -= 2
        st, en = R.start_end(e, d)
        rows.append({"id": k, "score": s, "why": why, "food": food, "_st": st, "_en": en, "name": e["name"],
                     "host": e["company"], "hood": d.get("neighborhood") or e["location"], "venue": d["venue"],
                     "address": d["address"], "url": links[k], "action": d["guest_action"],
                     "full": d.get("at_capacity"), "going": d.get("going"), "terms": terms,
                     "extra_q": [q["text"] for q in d["questions"] if not q["id"].startswith("techweek_")]})
    rows = [r for r in rows if r["score"] >= prof.get("min_score", 5)]
    rows.sort(key=lambda r: (r["_st"], -r["score"]))
    mine = _mine_rows(ws, prof, {_pid(r["url"]): r for r in rows})
    mine_pids = {m["pid"]: m for m in mine}
    for r in rows:
        r["mine"] = mine_pids.get(_pid(r["url"]), {}).get("status")
    declined = {k for k, v in _old_picks(ws).items() if v.get("user_set") and not v.get("register")}
    keys_a, b_ids = _tiers([r for r in rows if not r["full"] and not r["mine"] and r["id"] not in declined],
                           mine, a.per_day, a.backups, a.a_min, a.b_min)
    a_ids = {k for k in keys_a if not k.startswith("m:")}
    mine_a = {k[2:] for k in keys_a if k.startswith("m:")}
    plan = a_ids | b_ids
    ws.save("mine_tiers.json", {m["pid"]: {"tier": "A" if m["pid"] in mine_a else "B", "score": m["score"],
                                           "why": m["why"], "food": m["food"][:4]} for m in mine})

    old = _old_picks(ws)
    # once you've registered for real, never pre-tick new suggestions — you opt in explicitly
    registered_before = any(str(v.get("status", "")).startswith("submitted")
                            for v in (ws.load("registrations.json", {}) or {}).values())
    picks = []
    md = [f"# Shortlist — ✅ already registered · A attend · B backup if an A is rejected · 🍽 food\n"]
    day = None
    n_scored = len(rows)
    c_count = sum(1 for r in rows if not r["mine"] and r["id"] not in plan)
    shown = {_pid(r["url"]) for r in rows}
    for m in mine:
        if m["pid"] not in shown:
            rows.append({"id": None, "mine": m["status"], "score": "", "why": [], "food": [], "_st": m["_st"],
                         "_en": m["_en"], "name": m["name"], "host": "", "hood": "",
                         "url": f"https://partiful.com/e/{m['pid']}", "full": False})
    rows.sort(key=lambda r: r["_st"])
    for r in rows:
        if r["_st"].date() != day:
            day = r["_st"].date()
            md.append(f"\n## {day:%a %b %d}\n\n| | time | score | event | host | where | food | tags |\n|-|-|-|-|-|-|-|-|")
        star = "✅" if r["mine"] else ("A" if r["id"] in a_ids else ("B" if r["id"] in b_ids else ""))
        md.append(f"| {star} | {r['_st']:%H:%M}-{r['_en']:%H:%M} | {r['score']} | [{r['name'][:70]}]({r['url']})"
                  f"{' (FULL)' if r['full'] else ''} | {r['host']} | {r['hood']} | {'🍽 ' + ', '.join(r['food'][:3]) if r['food'] else ''}"
                  f" | {', '.join(r['why'])} |")
        if r["mine"]:
            continue
        tier = "A" if r["id"] in a_ids else ("B" if r["id"] in b_ids else "C")
        picks.append({"id": r["id"], "tier": tier,
                      # keep only choices the user made by hand; otherwise follow the new tier
                      "register": old[r["id"]]["register"] if old.get(r["id"], {}).get("user_set")
                      else (tier in "AB" and not registered_before),
                      "user_set": bool(old.get(r["id"], {}).get("user_set")),
                      "when": f"{r['_st']:%a %m-%d %H:%M}", "start": r["_st"].isoformat(), "end": r["_en"].isoformat(),
                      "score": r["score"], "food": r["food"][:4], "why": r["why"], "terms": r["terms"][:10],
                      "name": r["name"][:100],
                      "host": r["host"], "hood": r["hood"], "action": r["action"], "url": r["url"]})
    md.append(f"\n_{c_count} more matching events not selected (tier C)._")
    (ws.root / "shortlist.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    (ws.root / "picks.yaml").write_text(
        "# Set register: true/false (or use `python -m eventsauto serve`), then run `register`\n"
        + yaml.safe_dump(picks, allow_unicode=True, sort_keys=False, width=200), encoding="utf-8")
    print(f"{n_scored} scored; {len(mine)} already registered ({len(mine_a)} of them in A); "
          f"new tier A {len(a_ids)}, tier B {len(b_ids)} "
          "-> shortlist.md, picks.yaml")


def cmd_login(ws, a):
    from .register import login
    login(ws)


def cmd_sync(ws, a):
    from .register import sync_mine
    sync_mine(ws)


def cmd_serve(ws, a):
    from .serve import serve
    serve(ws, a.port)


def cmd_register(ws, a):
    from .register import register_all
    register_all(ws, dry_run=a.dry_run, only=a.only)


def cmd_calendar(ws, a):
    from .gcal import export
    export(ws, google=a.google)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="eventsauto", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["fetch", "links", "details", "rank", "login", "sync", "register", "calendar", "serve"])
    ap.add_argument("--ws", default=".", help="workspace dir containing profile.yaml")
    ap.add_argument("--city", default="sf")
    ap.add_argument("--delay", type=float, default=3.0, help="seconds between requests (be polite)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--per-day", type=int, default=None, help="events you can realistically attend per day "
                    "(default: per_day in profile.yaml, else 4)")
    ap.add_argument("--backups", type=int, default=3, help="approval-based backups per tier-A slot")
    ap.add_argument("--a-min", type=int, default=8, help="minimum score for a new event to be tier A")
    ap.add_argument("--b-min", type=int, default=7, help="minimum score for a backup (tier B)")
    ap.add_argument("--dry-run", action="store_true", help="fill forms but do not submit")
    ap.add_argument("--only", help="comma-separated event ids")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--google", action="store_true", help="also push to Google Calendar via API")
    a = ap.parse_args(argv)
    ws = Workspace(a.ws)
    if a.per_day is None:
        a.per_day = (ws.profile.get("per_day", 4) if ws.profile_path.exists() else 4)
    globals()[f"cmd_{a.command}"](ws, a)


if __name__ == "__main__":
    sys.exit(main())
