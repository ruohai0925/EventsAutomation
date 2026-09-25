"""Profile-driven scoring. Two passes: a cheap pre-filter on calendar text (decides which
events are worth fetching details for), then a full score using the detail description."""
import re
from datetime import datetime, timedelta


def _text(e, detail=None):
    parts = [e.get("name", ""), e.get("company") or "", e.get("excerpt") or "",
             " ".join(h["label"] for h in e.get("facets", {}).get("hosts", []))]
    if detail:
        parts += [detail.get("title", ""), detail.get("description", "")]
    return " ".join(parts).lower()


def score(e, profile, detail=None):
    t = _text(e, detail)
    s, why, terms = 0, [], []
    for name, spec in profile["interests"].items():
        hits = sorted({m.group(0) for m in re.finditer(spec["pattern"], t)})
        if not hits:
            continue
        s += spec["weight"]
        why.append(name)
        terms += hits[:3]
        if "bridge" in spec:  # e.g. robotics only counts if there's an angle for you
            bridge = sorted({m.group(0) for m in re.finditer(spec["bridge"], t)})
            if bridge:
                s += spec.get("bridge_weight", 0)
                why.append(name + "+bridge")
                terms += ["↳ " + b for b in bridge[:4]]
            else:
                s += spec.get("no_bridge_weight", 0)
                why.append(name + "-no_bridge")
    avoid = profile.get("avoid")
    if avoid:
        s += avoid["weight"] * len(set(re.findall(avoid["pattern"], t)))
    food_hits = sorted({m if isinstance(m, str) else m[0] for m in re.findall(profile["food"]["pattern"], t)})
    if food_hits:
        s += profile["food"]["bonus"]
    loc = e.get("location") or ""
    if loc in profile.get("prefer_neighborhoods", []):
        s += 1
    far = profile.get("far")
    if far and re.search(far["pattern"], f"{loc} {(detail or {}).get('address', '')}", re.I):
        s += far["weight"]
        why.append("far")
    score.last_terms = terms  # matched keywords, for display
    return s, why, food_hits


def prefilter(events, profile):
    keep = []
    for e in events:
        if e.get("location") in profile.get("skip_neighborhoods", []):
            continue
        if e.get("registrationStatus") in ("closed", "sold_out"):
            continue
        s, _, _ = score(e, profile)
        if s >= profile.get("min_score", 5):
            keep.append(e)
    return keep


def start_end(e, detail=None):
    if detail and detail.get("start"):
        st = datetime.fromisoformat(detail["start"])
        en = datetime.fromisoformat(detail["end"]) if detail.get("end") else st + timedelta(hours=2)
        return st, en
    st = datetime.fromisoformat(f"{e['date']}T{e['time'] or '12:00'}")
    if e.get("endTime"):
        en = datetime.fromisoformat(f"{e.get('endDate') or e['date']}T{e['endTime']}")
    else:
        en = st + timedelta(hours=2)
    return st, en
