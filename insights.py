"""What works, from your own results: match rate and reply rate of every like sent, broken down by the
comment strategy, judge score, looks, age, comment length, time and day sent, week and model.

A like only counts toward a rate once its outcome is known (matched, or no match after 10 days); the rest is
"pending". lessons_text() turns the patterns that survive a false discovery rate correction into a short
block the judge and the chat coach read (on by default, switch on the Insights page).
"""

import json
import math
from datetime import datetime, timedelta

import config

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MIN_N = 30


def _records() -> list[dict]:
    path = config.DATA_DIR / "session_log.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def _sends_by_folder() -> dict:
    import outcomes
    return {(s.get("folder") or "").split("/")[-1]: s for s in outcomes._load()["sends"].values() if s.get("folder")}


def outcome_of(s: dict | None) -> dict | None:
    if not s:
        return None
    matched = bool(s.get("matched"))
    return {"matched": matched, "noMatch": s.get("matched") is False, "replied": matched and bool(s.get("replied")),
            "date": matched and bool(s.get("date_booked")), "unmatched": bool(s.get("unmatched_by_her")),
            "firstMessage": s.get("her_first_message"), "note": s.get("user_note"), "sendId": s.get("send_id")}


def status_of(o: dict | None) -> str:
    if not o:
        return "pending"
    return ("date" if o["date"] else "unmatched" if o["unmatched"] else "replied" if o["replied"]
            else "matched" if o["matched"] else "no match" if o["noMatch"] else "pending")


def likes() -> list[dict]:
    sends = _sends_by_folder()
    out = []
    for r in _records():
        if r.get("decision") != "like" or r.get("dry_run") or r.get("not_sent"):
            continue
        sk = r.get("skill") or {}
        base = (r.get("folder") or "").split("/")[-1]
        try:
            at = datetime.fromisoformat(r["timestamp"])
        except (KeyError, ValueError, TypeError):
            continue
        out.append({"at": at, "name": r.get("name"), "score": sk.get("score"), "looks": sk.get("attractiveness"),
                    "age": sk.get("her_age") or None, "machine": sk.get("machine") or "none",
                    "message": r.get("message") or "", "model": r.get("model"), "likedOn": sk.get("liked_on"),
                    "outcome": outcome_of(sends.get(base))})
    return out


def _band(v, edges):
    if v is None:
        return None
    for hi, label in edges:
        if v <= hi:
            return label
    return edges[-1][1]


def _hour_band(h):
    return _band(h, [(5, "night (0-5)"), (11, "morning (6-11)"), (16, "afternoon (12-16)"), (20, "evening (17-20)"), (23, "late (21-23)")])


STRATEGY_LABEL = {"contradiction": "Contradiction: name it playfully", "rhyme": "Rhyme: the hidden pattern",
                  "claim": "Claim: contest it", "blueprint": "Blueprint: accept it literally", "none": "no hook"}

DIMENSIONS = {
    "strategy": ("Comment strategy", lambda l: (l["machine"], STRATEGY_LABEL.get(l["machine"], l["machine"]), l["machine"])),
    "score": ("Judge score", lambda l: (lambda b: b and (b, b, min(l["score"], 10)))(_band(l["score"], [(5, "6 or less"), (7, "6-7"), (9, "8-9"), (99, "10+")]) if l["score"] is not None else None)),
    "looks": ("Looks rating", lambda l: l["looks"] and (str(l["looks"]), f"{l['looks']}/5", l["looks"])),
    "age": ("Their age", lambda l: (lambda b: b and (b, b, l["age"]))(_band(l["age"], [(24, "under 25"), (27, "25-27"), (30, "28-30"), (99, "31+")]))),
    "length": ("Comment length", lambda l: (lambda n, b: (b, b, n))(len(l["message"]), _band(len(l["message"]), [(39, "under 40 chars"), (69, "40-69 chars"), (999, "70+ chars")]))),
    "likedOn": ("Liked on", lambda l: l["likedOn"] and (l["likedOn"], l["likedOn"], 0 if l["likedOn"] == "prompt" else 1)),
    "hour": ("Time sent", lambda l: (_hour_band(l["at"].hour), _hour_band(l["at"].hour), l["at"].hour)),
    "weekday": ("Day sent", lambda l: (WEEKDAYS[l["at"].weekday()], WEEKDAYS[l["at"].weekday()], l["at"].weekday())),
    "week": ("Week", lambda l: (lambda m: (m, "week of " + datetime.fromisoformat(m).strftime("%b %d").replace(" 0", " "), m))((l["at"] - timedelta(days=l["at"].weekday())).date().isoformat())),
    "model": ("AI model", lambda l: l["model"] and (l["model"], l["model"], l["model"])),
}


def tally(rows: list[dict]) -> dict:
    t = {"sent": len(rows), "matched": 0, "noMatch": 0, "pending": 0, "replied": 0, "dates": 0}
    for r in rows:
        o = r["outcome"]
        if o and o["matched"]:
            t["matched"] += 1
        elif o and o["noMatch"]:
            t["noMatch"] += 1
        else:
            t["pending"] += 1
        if o and o["replied"]:
            t["replied"] += 1
        if o and o["date"]:
            t["dates"] += 1
    t["resolved"] = t["matched"] + t["noMatch"]
    t["matchRate"] = t["matched"] / t["resolved"] if t["resolved"] else None
    t["replyRate"] = t["replied"] / t["matched"] if t["matched"] else None
    t["replyPerLike"] = t["replied"] / t["resolved"] if t["resolved"] else None
    return t


def double_messages(chat_list: list[dict]) -> dict:
    out = {"total": 0, "revived": 0, "examples": []}
    for c in chat_list:
        msgs = c.get("messages") or []
        i = 0
        while i < len(msgs):
            if msgs[i].get("from") != "me":
                i += 1
                continue
            j = i
            while j + 1 < len(msgs) and msgs[j + 1].get("from") == "me":
                j += 1
            run = msgs[i:j + 1]
            if len(run) >= 2 and len({m.get("stamp") or "" for m in run}) >= 2:
                revived = any(m.get("from") == "them" for m in msgs[j + 1:])
                out["total"] += 1
                out["revived"] += revived
                out["examples"].append({"name": c.get("name"), "revived": revived, "run": [m.get("text") for m in run]})
            i = j + 1
    return out


def insights() -> dict:
    ls = likes()
    judged = len(_records())
    try:
        chat_list = json.loads((config.DATA_DIR / "chats.json").read_text(encoding="utf-8")).get("chats") or []
    except (OSError, json.JSONDecodeError):
        chat_list = []
    totals = tally(ls)
    breakdowns = {}
    for key, (label, fn) in DIMENSIONS.items():
        groups = {}
        for l in ls:
            try:
                g = fn(l)
            except (TypeError, ValueError):
                g = None
            if not g or g[0] is None:
                continue
            groups.setdefault(g[0], {"key": g[0], "label": g[1], "sort": g[2], "rows": []})["rows"].append(l)
        rows = [{"key": g["key"], "label": g["label"], "sort": g["sort"], **tally(g["rows"])} for g in groups.values()]
        if key == "strategy":
            rows.sort(key=lambda r: (-(r["matchRate"] or -1), -r["sent"]))
        else:
            rows.sort(key=lambda r: (str(type(r["sort"])), r["sort"]))
        breakdowns[key] = {"label": label, "rows": rows}
    first = min((l["at"] for l in ls), default=None)
    return {"since": first.isoformat() if first else None, "judged": judged, "totals": totals,
            "funnel": [["Profiles judged", judged], ["Likes sent", totals["sent"]], ["Matched", totals["matched"]],
                       ["They replied", totals["replied"]], ["Dates", totals["dates"]]],
            "breakdowns": breakdowns, "doubles": double_messages(chat_list),
            "lessons": {"enabled": lessons_enabled(), "judge": lessons_text("judge", force=True, ins=None)}}


# ---------------------------------------------------------------- lessons for the prompts

def _norm_cdf(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def _versus_rest(m1, n1, m2, n2):
    if n1 < 1 or n2 < 1:
        return None
    p1, p2, p = m1 / n1, m2 / n2, (m1 + m2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if not se:
        return None
    return p1, p2, 2 * (1 - _norm_cdf(abs((p1 - p2) / se)))


def lessons_enabled() -> bool:
    return bool(config.load_settings().get("useLessons", True))


def _pc(v):
    return f"{round(v * 100)}%"


LESSON_DIMS = ("strategy", "score", "looks", "age", "length", "likedOn", "hour", "weekday")


def lessons_text(kind: str, force: bool = False, ins: dict | None = None) -> str:
    if not force and not lessons_enabled():
        return ""
    ls = likes()
    if len(ls) < MIN_N:
        return ""
    totals = tally(ls)
    if kind == "chat":
        try:
            chat_list = json.loads((config.DATA_DIR / "chats.json").read_text(encoding="utf-8")).get("chats") or []
        except (OSError, json.JSONDecodeError):
            return ""
        d = double_messages(chat_list)
        if d["total"] < 8:
            return ""
        return (f"WHAT'S WORKING LATELY (the user's own chats): double texts got an answer {d['revived']} of "
                f"{d['total']} times ({_pc(d['revived'] / d['total'])}). A light prior, never a rule.")
    tests = []
    for key in LESSON_DIMS:
        label, fn = DIMENSIONS[key]
        groups = {}
        for l in ls:
            try:
                g = fn(l)
            except (TypeError, ValueError):
                g = None
            if g and g[0] is not None:
                groups.setdefault(g[1], []).append(l)
        for glabel, rows in groups.items():
            t = tally(rows)
            if t["resolved"] < MIN_N:
                continue
            v = _versus_rest(t["matched"], t["resolved"], totals["matched"] - t["matched"], totals["resolved"] - t["resolved"])
            if v:
                tests.append((v[2], label, glabel, t, v))
    tests.sort(key=lambda x: x[0])
    cut = -1
    for i, x in enumerate(tests):
        if x[0] <= (i + 1) / len(tests) * 0.1:
            cut = i
    passed = [x for x in tests[:cut + 1] if abs(x[4][0] - x[4][1]) >= 0.03]
    head = ("WHAT'S WORKING LATELY (the user's own Hinge results, generated " + datetime.now().strftime("%Y-%m-%d")
            + "). A light prior for choosing between options the rules already allow; it never overrides them.")
    overall = (f"Overall: {totals['matched']} of {totals['resolved']} likes with a known outcome matched"
               + (f" ({_pc(totals['matchRate'])})" if totals["matchRate"] is not None else "")
               + f"; they replied in {totals['replied']} of {totals['matched']} matches.")
    lines = [head, overall]
    if passed:
        lines.append("Patterns with enough data:")
        lines += [f'- {lab}: "{g}" matched {t["matched"]} of {t["resolved"]} ({_pc(v[0])}) vs {_pc(v[1])} for the other likes.'
                  for _, lab, g, t, v in passed]
    else:
        lines.append("No pattern is strong enough yet. Follow the rules as written.")
    return "\n".join(lines)
