"""One record per comment actually sent, and what came of it.

main.py writes a record at send time. Outcomes come from reading the chats
(chats.apply_outcomes: in the match list = matched, a message from them =
replied) and from what the user marks on the Profiles page.

Storage: data/sends.json  {"meta": {...}, "sends": {send_id: record}}.
"""

import json
import random
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

import config

SENDS_PATH = Path(config.DEBUG_DIR) / "sends.json"
MATCH_DEADLINE_DAYS = 10     # no report -> matched=false
REPLY_DEADLINE_DAYS = 10     # matched but no reply reported -> replied=false
NAME_LOOKUP_DAYS = 14        # window for name-only replies
ELITE_SCORE = 7

_lock = threading.Lock()
_CODE_ALPHABET = "0123456789ABCDEF"


# ---------- storage ----------

def _load() -> dict:
    try:
        data = json.loads(SENDS_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {"meta": {}, "sends": {}}
    data.setdefault("meta", {})
    data.setdefault("sends", {})
    return data


def _save(data: dict) -> None:
    SENDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SENDS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(SENDS_PATH)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_ts(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


# ---------- writing sends ----------

def _new_code(existing: dict) -> str:
    while True:
        code = "".join(random.choice(_CODE_ALPHABET) for _ in range(4))
        if code not in existing:
            return code


def record_send(*, name: str, comment: str, decision_extra: dict,
                profile_idx: int | None = None, age: int | None = None,
                folder: str | None = None) -> str:
    """Write one send record. Returns its short send_id (e.g. "A7F3")."""
    sk = decision_extra or {}
    with _lock:
        data = _load()
        send_id = _new_code(data["sends"])
        score = sk.get("score")
        data["sends"][send_id] = {
            "send_id": send_id,
            "sent_at": _now(),
            "profile_idx": profile_idx,
            "folder": folder,
            "her_name": (name or "").strip(),
            "her_age": age or sk.get("her_age") or None,
            "attractiveness": sk.get("attractiveness"),
            "score": score,
            "verdict": sk.get("verdict"),
            "elite_flag": bool(isinstance(score, int) and score >= ELITE_SCORE),
            "hook_test": sk.get("hook"),
            "machine": sk.get("machine"),
            "machine_variant": sk.get("variation"),
            "target_text": sk.get("target"),
            "comment_text": comment,
            "word_count": len(comment.split()),
            "landing_word": sk.get("landing_word"),
            "template_signature": sk.get("skeleton"),
            "operator_fact": bool(sk.get("operator_fact_used")),
            "candidates": sk.get("candidates"),
            "flags": sk.get("flags"),
            # outcome fields
            "matched": None, "matched_at": None,
            "replied": None, "replied_at": None, "her_first_message": None,
            "unmatched_by_her": None,
            "date_booked": None, "booked_at": None,
            "user_nudged": None, "nudge_text": None,
            "voice_rating": None, "user_note": None,
            "resolved": False, "resolved_at": None,
        }
        _save(data)
    return send_id


def get(send_id: str) -> dict | None:
    return _load()["sends"].get(send_id)


# ---------- routing a reply to a send ----------

CODE_RE = re.compile(r"#([0-9A-F]{4})\b", re.IGNORECASE)


def find_by_code(text: str) -> dict | None:
    m = CODE_RE.search(text or "")
    return _load()["sends"].get(m.group(1).upper()) if m else None


def find_by_name(text: str, days: int = NAME_LOOKUP_DAYS) -> list[dict]:
    """Unresolved sends from the last `days` whose name appears in `text`."""
    cutoff = datetime.now() - timedelta(days=days)
    words = set(re.findall(r"[a-z]+", (text or "").lower()))
    out = []
    for s in _load()["sends"].values():
        name = (s.get("her_name") or "").strip().lower()
        ts = _parse_ts(s.get("sent_at"))
        if not name or not ts or ts < cutoff or s.get("resolved"):
            continue
        if name in words:
            out.append(s)
    return sorted(out, key=lambda s: s["sent_at"], reverse=True)


# ---------- reply grammar ----------

_TEXT_FORMS = {
    "m": "match_text", "match": "match_text", "matched": "match_text",
    "r": "reply_text", "reply": "reply_text",
    "n": "nudge_text",
}
_FLAG_FORMS = {
    "m": "matched", "match": "matched", "matched": "matched",
    "x": "unmatched", "no": "unmatched", "unmatched": "unmatched",
    "d": "date", "date": "date",
}
_VOICE_RE = re.compile(r"^v\s*([1-5])$", re.IGNORECASE)


def parse_reply(text: str) -> list[tuple[str, str]]:
    """Parse a short outcome note into events (m, r: text, x, d, v1-5). Case-insensitive, punctuation-tolerant.

    Forms carrying text ("m: ...", "r: ...", "n: ...") consume the rest of
    their line; bare forms (m, x, d, v4) may be comma-chained.
    """
    events: list[tuple[str, str]] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = CODE_RE.sub("", line).strip()  # the code routes, it isn't an event
        if not line:
            continue
        m = re.match(r"^(m|match|matched|r|reply|n)\s*[:\-]\s*(.+)$", line, re.I)
        if not m:
            m = re.match(r"^(match|reply|matched)\s+(.+)$", line, re.I)
        if m and m.group(2).strip():
            payload = m.group(2).strip()
            # A trailing ", v5" / ", d" / ", x" is a chained event, not part
            # of what she wrote. Strip them off the end, innermost last.
            trailing: list[tuple[str, str]] = []
            while True:
                t = re.search(r"[,;]\s*(v\s*[1-5]|x|d|no|date|unmatched)\s*$",
                              payload, re.I)
                if not t:
                    break
                token = t.group(1).strip().lower()
                v = _VOICE_RE.match(token.replace(" ", ""))
                trailing.insert(0, ("voice", v.group(1)) if v
                                else (_FLAG_FORMS[token], ""))
                payload = payload[:t.start()].rstrip()
            events.append((_TEXT_FORMS[m.group(1).lower()], payload))
            events.extend(trailing)
            continue
        for token in re.split(r"[,;]+", line):
            token = token.strip().strip(".!").lower()
            if not token:
                continue
            v = _VOICE_RE.match(token)
            if v:
                events.append(("voice", v.group(1)))
            elif token in _FLAG_FORMS:
                events.append((_FLAG_FORMS[token], ""))
            elif token:
                events.append(("note", token))
    return events


def apply_events(send_id: str, events: list[tuple[str, str]]) -> str:
    """Apply parsed events to a send. Returns a one-line ack summary."""
    done: list[str] = []
    with _lock:
        data = _load()
        s = data["sends"].get(send_id)
        if not s:
            return ""
        for kind, payload in events:
            if kind in ("matched", "match_text"):
                s["matched"] = True
                s["matched_at"] = s.get("matched_at") or _now()
                s["unmatched_by_her"] = False
                s["resolved"] = False
                done.append("matched")
                if kind == "match_text":
                    s["replied"] = True
                    s["replied_at"] = _now()
                    s["her_first_message"] = payload
                    done.append("replied")
            elif kind == "reply_text":
                s["matched"] = True
                s["matched_at"] = s.get("matched_at") or _now()
                s["replied"] = True
                s["replied_at"] = _now()
                s["her_first_message"] = payload
                done.append("replied")
            elif kind == "nudge_text":
                s["user_nudged"] = True
                s["nudge_text"] = payload
                done.append("nudge logged")
            elif kind == "unmatched":
                if s.get("matched"):
                    s["unmatched_by_her"] = True
                else:
                    s["matched"] = False
                s["resolved"] = True
                s["resolved_at"] = _now()
                done.append("unmatched")
            elif kind == "date":
                s["matched"] = True
                s["matched_at"] = s.get("matched_at") or _now()
                s["date_booked"] = True
                s["booked_at"] = _now()
                s["resolved"] = True
                s["resolved_at"] = _now()
                done.append("date booked")
            elif kind == "voice":
                s["voice_rating"] = int(payload)
                done.append(f"v{payload}")
            elif kind == "note":
                note = (s.get("user_note") or "")
                s["user_note"] = (note + " | " + payload).strip(" |")
                done.append("note logged")
        _save(data)
    name = (s.get("her_name") or "?").title()
    return f"✓ {name}: " + ", ".join(dict.fromkeys(done)) if done else ""


# ---------- resolution + reminders ----------

def resolve_stale() -> int:
    """Close records nothing was ever learned about. Returns how many closed."""
    now = datetime.now()
    closed = 0
    with _lock:
        data = _load()
        for s in data["sends"].values():
            if s.get("resolved"):
                continue
            sent = _parse_ts(s.get("sent_at"))
            matched_at = _parse_ts(s.get("matched_at"))
            if s.get("matched") and matched_at:
                if now - matched_at > timedelta(days=REPLY_DEADLINE_DAYS) and not s.get("replied"):
                    s["replied"] = False
                    s["resolved"] = True
                    s["resolved_at"] = _now()
                    closed += 1
            elif sent and now - sent > timedelta(days=MATCH_DEADLINE_DAYS):
                s["matched"] = False
                s["resolved"] = True
                s["resolved_at"] = _now()
                closed += 1
        if closed:
            _save(data)
    return closed


def open_threads(days: int = MATCH_DEADLINE_DAYS) -> list[dict]:
    """Matched but not yet reported as replied/dated — the follow-up list."""
    cutoff = datetime.now() - timedelta(days=days)
    out = []
    for s in _load()["sends"].values():
        matched_at = _parse_ts(s.get("matched_at"))
        if (s.get("matched") and not s.get("replied") and not s.get("resolved")
                and matched_at and matched_at >= cutoff):
            out.append(s)
    return sorted(out, key=lambda s: s["matched_at"])


def coverage(days: int = 14) -> dict:
    """Share of sends with any known outcome."""
    cutoff = datetime.now() - timedelta(days=days)
    total = reported = 0
    lags: list[float] = []
    for s in _load()["sends"].values():
        sent = _parse_ts(s.get("sent_at"))
        if not sent or sent < cutoff:
            continue
        total += 1
        first = None
        for key in ("matched_at", "replied_at", "booked_at"):
            ts = _parse_ts(s.get(key))
            if ts and (first is None or ts < first):
                first = ts
        if first or s.get("voice_rating") or s.get("user_note") or s.get("unmatched_by_her"):
            reported += 1
            if first:
                lags.append((first - sent).total_seconds() / 3600)
    lags.sort()
    median = lags[len(lags) // 2] if lags else None
    return {
        "days": days, "sends": total, "reported": reported,
        "coverage": (reported / total) if total else 0.0,
        "median_lag_hours": median,
    }


# ---------- free-text feedback ----------

FEEDBACK_PATH = Path(config.DEBUG_DIR) / "feedback.jsonl"
def add_feedback(summary: dict, note: str) -> None:
    """Store the user's own words about a decision. Fed back to the judge as
    calibration context; never executed as an instruction."""
    FEEDBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {"at": _now(), "note": note.strip(),
              **{k: summary.get(k) for k in
                 ("name", "decision", "verdict", "score", "attractiveness",
                  "comment", "send_id", "timestamp")}}
    with FEEDBACK_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    if summary.get("send_id"):
        with _lock:
            data = _load()
            s = data["sends"].get(summary["send_id"])
            if s:
                prev = (s.get("user_note") or "")
                s["user_note"] = (prev + " | " + note.strip()).strip(" |")
                _save(data)


def recent_feedback(limit: int = 12) -> list[dict]:
    try:
        lines = FEEDBACK_PATH.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def meta_get(key: str, default=None):
    return _load()["meta"].get(key, default)


def meta_set(key: str, value) -> None:
    with _lock:
        data = _load()
        data["meta"][key] = value
        _save(data)
