"""Read Hinge chats off the phone.

Hinge's own data is out of reach on this phone: no root, the app isn't
debuggable and doesn't allow backups, and its traffic can't be intercepted
without patching the app. Its UI is Jetpack Compose, though, and that
exposes every visible string to Android's accessibility tree, so
`uiautomator dump` reads the chats as plain text: no screenshots, no model.

    Matches tab   one row per match: name, last message, "Your move" badge
    A chat        one node per message, labelled " Name: text. " or
                  " You: text. ", plus date separators and Sent/Read

sync() walks the match list, opens each chat, scrolls back through its
history, and writes data/chats.json. It only reads: it never types, and it
leaves Hinge on the Discover tab.

    .venv\\Scripts\\python.exe chats.py            # every chat
    .venv\\Scripts\\python.exe chats.py --limit 2  # first two only
"""

import argparse
import json
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import adb
import config

CHATS_PATH = Path(config.DEBUG_DIR) / "chats.json"
DUMP_PATH = "/sdcard/hinge_ui.xml"
MAX_HISTORY_SCROLLS = 40      # per chat; a long chat is ~2-3 messages per scroll
MAX_LIST_SCROLLS = 30

# Date separators in a chat: "Thu, Oct 8 2:05PM", "Today 3:12PM", "Yesterday 9:01AM"
_STAMP_RE = re.compile(r"^((Mon|Tue|Wed|Thu|Fri|Sat|Sun), \w{3} \d{1,2}( \d{4})?|Today|Yesterday)\b.*\d{1,2}:\d{2}\s?[AP]M$")
_MSG_RE = re.compile(r"^\s*(?P<who>[^:]{1,60}): (?P<text>.*?)\.?\s*$", re.S)
_ROW_SKIP = {"Matches", "Signals"}
# The match list's collapsible sections, top to bottom. A normal read covers "Your turn"; an extended read
# also expands and reads "Their turn" (you sent the last message) and "Hidden".
SECTIONS = ("Your turn", "Their turn", "Hidden")
_HEADER_RE = re.compile(r"^(Your turn|Their turn|Hidden) \((\d+)\)$")
# The like that opened the chat: what was liked ("Mhairi's photo", "Your photo",
# "Prompt: … Answer: …") with the comment, if any, as plain text under it.
_LIKED_RE = re.compile(r"^(?P<owner>.+?)'s (photo|video|prompt|voice prompt)\b|^Your (photo|video|prompt|voice prompt)\b|^Prompt: ", re.I)
_REACTION_RE = re.compile(r"\.?\s+(?P<who>[^.]{1,40}) (liked|loved|reacted to) this message\.?$")
_CHAT_CHROME = {"Chat", "Profile", "Ask them out", "End the chat", "Send a message", "Sent", "Read", "Delivered"}


@dataclass
class Node:
    text: str
    desc: str
    cls: str
    clickable: bool
    scrollable: bool
    x1: int
    y1: int
    x2: int
    y2: int
    children: list

    @property
    def center(self) -> tuple[int, int]:
        return (self.x1 + self.x2) // 2, (self.y1 + self.y2) // 2

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()


def _node(e: ET.Element) -> Node:
    a = e.attrib
    x1, y1, x2, y2 = map(int, re.findall(r"\d+", a.get("bounds", "[0,0][0,0]")))
    return Node(a.get("text", ""), a.get("content-desc", ""), a.get("class", ""),
                a.get("clickable") == "true", a.get("scrollable") == "true",
                x1, y1, x2, y2, [_node(c) for c in e])


def dump(retries: int = 3) -> Node:
    """The current screen's accessibility tree. uiautomator refuses while
    the UI is animating ("could not get idle state"), so retry briefly."""
    adb._pin_serial()
    last = ""
    for _ in range(retries):
        try:
            out = subprocess.run([config.adb_path(), "shell", "uiautomator", "dump", DUMP_PATH],
                                 capture_output=True, text=True, timeout=45).stdout
        except subprocess.TimeoutExpired:
            last = "uiautomator dump timed out"
            continue
        if "dumped to" in out:
            xml = subprocess.run([config.adb_path(), "exec-out", "cat", DUMP_PATH], capture_output=True, timeout=30).stdout
            try:
                return _node(ET.fromstring(xml.decode("utf-8", "replace")))
            except ET.ParseError as e:
                last = f"unparseable dump: {e}"
        else:
            last = out.strip()[-200:]
        time.sleep(1.0)
    raise RuntimeError(f"uiautomator dump failed: {last}")


def _find(root: Node, pred) -> Node | None:
    return next((n for n in root.walk() if pred(n)), None)


def _tap(n: Node) -> None:
    adb.tap(*n.center, jitter=4)


def _tab(root: Node, label: str) -> Node | None:
    """Bottom-nav tab whose description starts with `label` ("Matches, 6 new messages")."""
    return _find(root, lambda n: n.desc.split(",")[0] == label and n.y1 > config.SCREEN_HEIGHT * 0.8)


def _require_hinge() -> None:
    if not adb.is_foreground("co.hinge.app"):
        raise RuntimeError("Hinge isn't open on the phone")


# ---------- match list ----------

def _rows(root: Node) -> list[dict]:
    """Match rows on screen: a clickable full-width row whose first text is
    the name, then the last message, then an optional status line."""
    rows = []
    # the "Your move?" badge is drawn over the row but isn't inside its node
    badges = [b.center for b in root.walk() if "Your move" in b.desc]
    for n in root.walk():
        if not n.clickable or n.x2 - n.x1 < config.SCREEN_WIDTH * 0.8:
            continue
        texts = [c.text for c in n.walk() if c.text and c is not n and c.text != "Signals"]
        if len(texts) < 2 or texts[0] in _ROW_SKIP or _HEADER_RE.match(texts[0]):
            continue
        descs = [c.desc for c in n.walk() if c.desc]
        status = texts[2] if len(texts) > 2 else None
        rows.append({"name": texts[0], "preview": texts[1],
                     "status": status, "yourMove": any("Your move" in d for d in descs) or any(n.y1 <= y <= n.y2 for _, y in badges),
                     "_y": n.center[1], "_node": n})
    return rows


def _headers(root: Node) -> list[Node]:
    return sorted((n for n in root.walk() if _HEADER_RE.match(n.text or "")), key=lambda n: n.y1)


def _section(root: Node, y: int, carry: str | None = None) -> str | None:
    """Which section header ("Your turn (6)", "Their turn (3)", "Hidden (37)") a row sits under; `carry`
    is the section from the screen before, for rows whose header has scrolled off."""
    above = [h for h in _headers(root) if h.y1 < y]
    return _HEADER_RE.match(above[-1].text).group(1) if above else carry


def _toggle_section(root: Node, head: Node) -> None:
    box = next((n for n in root.walk() if n.clickable and n.y1 <= head.y1 and n.y2 >= head.y2
                and n.x2 - n.x1 > config.SCREEN_WIDTH * 0.8), head)
    _tap(box)
    time.sleep(1.5)


def _find_header(name: str):
    """Scroll until the `name` section header is on screen, high enough that the rows under it (if open)
    are visible too. Returns (screen, header) or None."""
    _scroll_list_to_top()
    pick = lambda root: next((h for h in _headers(root) if _HEADER_RE.match(h.text).group(1) == name), None)
    for _ in range(MAX_LIST_SCROLLS):
        root = dump()
        hd = pick(root)
        if hd and hd.y2 < config.SCREEN_HEIGHT * 0.86:   # above the bottom nav
            if hd.y1 > 700:  # drag it up; at the end of the list it can't move, and then nothing is under it anyway
                adb.swipe(540, hd.y1, 540, 450, 700)
                time.sleep(1.2)
                root = dump()
                hd = pick(root)
                if hd is None:
                    continue
            return root, hd
        before = [n.text for n in root.walk() if n.text]
        adb.swipe(540, 1800, 540, 900, 400)
        time.sleep(1.2)
        if [n.text for n in dump().walk() if n.text] == before:
            return None  # end of the list
    return None


def _is_open(root: Node, hd: Node) -> bool:
    """A section is expanded when a match row starts right under its header."""
    return any(hd.y2 <= r["_node"].y1 <= hd.y2 + 250 for r in _rows(root))


def set_section(name: str, want_open: bool) -> bool:
    """Open or close one section. True if it had to be toggled."""
    found = _find_header(name)
    if not found:
        return False
    root, hd = found
    if int(_HEADER_RE.match(hd.text).group(2)) == 0 or _is_open(root, hd) == want_open:
        return False
    _toggle_section(root, hd)
    return True


def expand_sections(names) -> list[str]:
    """Open the collapsed sections in `names`. Returns the ones this opened (to close them again after)."""
    opened = [n for n in names if set_section(n, True)]
    _scroll_list_to_top()
    return opened


def collapse_sections(names) -> None:
    """Close sections this read opened, leaving the list as it was."""
    for n in names:
        set_section(n, False)
    _scroll_list_to_top()


def your_turn_count() -> int:
    """How many chats are waiting on your reply ("Your turn (n)" in Matches). Hinge stops new likes at 8
    (Your Turn Limits), so a run checks this first. Leaves Hinge on Discover."""
    try:
        root = open_matches()
        n = 0
        for hd in _headers(root):
            m = _HEADER_RE.match(hd.text)
            if m.group(1) == "Your turn":
                n = int(m.group(2))
        return n
    finally:
        try:
            disc = _tab(dump(), "Discover")
            if disc:
                _tap(disc)
                time.sleep(1.5)
        except Exception:
            pass


def open_matches() -> Node:
    _require_hinge()
    root = dump()
    tab = _tab(root, "Matches")
    if tab is None:
        adb.press_back()  # maybe inside a chat or a sheet
        time.sleep(1.5)
        root = dump()
        tab = _tab(root, "Matches")
    if tab is None:
        raise RuntimeError("Couldn't find the Matches tab")
    _tap(tab)
    time.sleep(2.5)
    return dump()


def _scroll_list_to_top() -> None:
    for _ in range(MAX_LIST_SCROLLS):
        before = [r["name"] for r in _rows(dump())]
        adb.swipe(540, 700, 540, 1800, 350)
        time.sleep(1.0)
        if [r["name"] for r in _rows(dump())] == before:
            return


def read_match_list(sections=SECTIONS[:1]) -> list[dict]:
    """The matches in `sections`, top to bottom (scrolls the whole list). The list must
    already show those sections expanded (expand_sections)."""
    open_matches()
    _scroll_list_to_top()
    seen: dict[str, dict] = {}
    carry = None
    for _ in range(MAX_LIST_SCROLLS * 3):
        root = dump()
        new = 0
        for r in sorted(_rows(root), key=lambda r: r["_y"]):
            key = r["name"] + "|" + r["preview"]
            sec = _section(root, r["_y"], carry)
            if key not in seen:
                seen[key] = {k: v for k, v in r.items() if not k.startswith("_")} | {"section": sec}
                new += 1
        heads = _headers(root)
        rows = _rows(root)
        if rows:
            carry = _section(root, max(r["_y"] for r in rows), carry)
        elif heads:
            carry = _HEADER_RE.match(heads[-1].text).group(1)
        if not new:
            break
        adb.swipe(540, 1800, 540, 800, 400)
        time.sleep(1.2)
    _scroll_list_to_top()
    return [m for m in seen.values() if (m["section"] or SECTIONS[0]) in sections]


# ---------- one chat ----------

def _screen_messages(root: Node, me: str = "You") -> list[dict]:
    """Messages on screen in top-to-bottom order, each with the date
    separator above it and its Sent/Read status."""
    items = []
    nodes = list(root.walk())
    for n in nodes:
        liked = _LIKED_RE.match(n.desc or "")
        if liked and 300 < n.y1 < config.SCREEN_HEIGHT * 0.85:
            comment = next((c.text for c in nodes if c.text and not c.desc and c.text not in _CHAT_CHROME
                            and not _STAMP_RE.match(c.text) and n.y2 - 200 <= c.y1 <= n.y2 + 260
                            and c.x1 >= n.x1 - 60), "")
            mine = not (n.desc.lower().startswith("your ") or (n.desc.startswith("Prompt: ") and n.x1 < 150))
            items.append(("like", n.y1, mine, n.desc, comment))
            continue
        if _STAMP_RE.match(n.text or ""):
            items.append(("stamp", n.y1, n.text))
        elif n.text in ("Sent", "Read", "Delivered"):
            items.append(("status", n.y1, n.text))
        elif n.desc:
            m = _MSG_RE.match(n.desc)
            if m and n.y1 > 300 and n.desc.startswith(" "):
                items.append(("msg", n.y1, m.group("who").strip(), m.group("text").strip()))
    items.sort(key=lambda i: i[1])
    out, stamp = [], None
    for it in items:
        if it[0] == "stamp":
            stamp = it[2]
        elif it[0] == "status":
            if out and out[-1]["from"] == "me":
                out[-1]["status"] = it[2]
        elif it[0] == "like":
            out.append({"from": "me" if it[2] else "them", "who": me if it[2] else None, "kind": "like",
                        "liked": it[3], "text": it[4], "stamp": stamp})
        else:
            who, text = it[2], it[3]
            msg = {"from": "me" if who == me else "them", "who": who, "text": text, "stamp": stamp}
            r = _REACTION_RE.search(text)
            if r:
                msg["text"], msg["reaction"] = text[:r.start()].rstrip(), f"{r.group('who')} {r.group(2)} this"
            out.append(msg)
    return out


def _merge(older: list[dict], current: list[dict]) -> list[dict]:
    """Prepend messages from an earlier screen, using the longest overlap
    between the end of `older` and the start of `current`."""
    key = lambda m: (m["from"], m["text"])
    for k in range(min(len(older), len(current)), 0, -1):
        if [key(m) for m in older[-k:]] == [key(m) for m in current[:k]]:
            for a, b in zip(older[-k:], current[:k]):  # keep the stamp seen nearer the message
                b["stamp"] = b.get("stamp") or a.get("stamp")
            return older[:-k] + current
    return older + current


def read_open_chat(max_scrolls: int = MAX_HISTORY_SCROLLS) -> list[dict]:
    """The whole history of the chat on screen: scrolls up until nothing new."""
    msgs = _screen_messages(dump())
    for _ in range(max_scrolls):
        adb.swipe(540, 600, 540, 1700, 350)
        time.sleep(1.0)
        older = _screen_messages(dump())
        merged = _merge(older, msgs)
        if len(merged) == len(msgs):
            break
        msgs = merged
    # a stamp only appears above the first message of its group: carry it down
    last = None
    for m in msgs:
        last = m["stamp"] = m.get("stamp") or last
    return msgs


def read_chat(name: str, preview: str | None = None, sections=SECTIONS[:1]) -> dict:
    """Open the chat with `name` from the match list, read it, go back."""
    try:
        return _read_chat(name, preview)
    except LookupError:
        expand_sections(sections)
        return _read_chat(name, preview)


def _read_chat(name: str, preview: str | None) -> dict:
    for _ in range(MAX_LIST_SCROLLS):
        rows = _rows(dump())
        row = next((r for r in rows if r["name"] == name and (preview is None or r["preview"] == preview)), None) \
            or next((r for r in rows if r["name"] == name), None)
        if row:
            break
        adb.swipe(540, 1800, 540, 900, 400)
        time.sleep(1.2)
    else:
        raise LookupError(f"{name} isn't in the match list")
    _tap(row["_node"])
    opened = lambda root: _find(root, lambda n: n.text in ("Send a message", "Chat") or n.desc == "Back") is not None
    for _ in range(4):  # older chats load slower (and show a "matched for a while" banner)
        time.sleep(1.8)
        if opened(dump()):
            break
    else:
        raise RuntimeError(f"Tapped {name} but no chat opened")
    messages = read_open_chat()
    for m in messages:
        m["who"] = m["who"] or name
    back = _find(dump(), lambda n: n.desc == "Back" and n.y2 < 300)
    if back:
        _tap(back)
    else:
        adb.press_back()
    time.sleep(1.8)
    return {"name": name, "messages": messages}


# ---------- everything ----------

# ---------- chats → the bot's send records (outcomes) ----------

def _norm(text: str | None) -> str:
    return re.sub(r"\W+", " ", (text or "").lower()).strip()


def find_send(chat: dict, sends: dict | None = None) -> dict | None:
    """The like the bot sent that started this chat: matched on the opener
    text (the like card shows the exact comment), else the most recent send
    to someone with her name."""
    import outcomes
    sends = sends if sends is not None else outcomes._load()["sends"]
    opener = next((m for m in chat.get("messages") or [] if m.get("kind") == "like" and m["from"] == "me"), None)
    if opener and opener.get("text"):
        want = _norm(opener["text"])
        hit = next((s for s in sends.values() if want and _norm(s.get("comment_text")) == want), None)
        if hit:
            return hit
    name = _norm(chat.get("name"))
    named = [s for s in sends.values() if _norm(s.get("her_name")) == name]
    return max(named, key=lambda s: s.get("sent_at") or "") if named else None


def chat_key(chat: dict) -> str:
    """A chat's identity. First names repeat (two Saras), so: the name plus a short fingerprint of how the chat
    started (the opening like and comment never change), falling back to the list preview."""
    import hashlib
    first = (chat.get("messages") or [None])[0]
    seed = f"{first.get('from')}|{first.get('liked') or ''}|{first.get('text') or ''}" if first else chat.get("preview") or ""
    return f"{(chat.get('name') or '?')[:60]}#{hashlib.sha1(_norm(seed).encode()).hexdigest()[:8]}"


def find_chat(chat_list: list[dict], key: str) -> dict | None:
    """By key; a plain name works too while it's unique."""
    hit = next((c for c in chat_list if c.get("key") == key), None)
    if hit:
        return hit
    named = [c for c in chat_list if c.get("name") == key]
    return named[0] if len(named) == 1 else None


def last_key(chat: dict) -> str:
    """Identifies the latest message, so a suggestion knows when it's stale."""
    msgs = chat.get("messages") or []
    if not msgs:
        return _norm(chat.get("preview"))[:80]
    m = msgs[-1]
    return f"{m['from']}|{_norm(m.get('text'))[:80]}|{len(msgs)}"


def apply_outcomes(data: dict) -> list[str]:
    """What the chats prove, written into sends.json: being in the match list
    means she matched; a message from her means she replied (and the first
    one is her_first_message). Only ever fills in or upgrades, never undoes
    what was reported by hand."""
    import outcomes
    changed = []
    with outcomes._lock:
        store = outcomes._load()
        for chat in data["chats"]:
            s = find_send(chat, store["sends"])
            if not s:
                continue
            chat["send_id"] = s["send_id"]
            done = []
            if not s.get("matched"):
                s["matched"], s["matched_at"], s["resolved"] = True, s.get("matched_at") or outcomes._now(), False
                done.append("matched")
            msgs = chat.get("messages") or []
            first_reply = next((m for m in msgs if m["from"] == "them" and m.get("kind") != "like" and m.get("text")), None)
            if first_reply and not s.get("replied"):
                s["replied"], s["replied_at"] = True, outcomes._now()
                s["her_first_message"] = s.get("her_first_message") or first_reply["text"]
                done.append("replied")
            if done:
                s["outcome_source"] = "chats"
                changed.append(f"{(s.get('her_name') or chat['name']).title()} {'+'.join(done)}")
        if changed:
            outcomes._save(store)
    return changed


def _previous() -> dict:
    try:
        return json.loads(CHATS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"chats": []}


def _merge_reads(fresh: list[dict], covered, previous: list[dict]) -> list[dict]:
    """A normal read only sees "Your turn": keep what earlier reads saw of the other sections. A chat that was in
    a covered section but isn't any more most likely moved on (you replied, so it's "Their turn" now)."""
    keys = {c["key"] for c in fresh}
    kept = []
    for c in previous:
        c.setdefault("key", chat_key(c))
        if c["key"] in keys:
            continue
        c = dict(c)
        if (c.get("section") or SECTIONS[0]) in covered:
            last = (c.get("messages") or [{}])[-1]
            if last.get("from") != "me":
                continue  # not in the list any more and not explained by a reply: unmatched, drop it
            c["section"], c["sectionGuessed"] = "Their turn", True
        kept.append(c)
    order = {s: i for i, s in enumerate(SECTIONS)}
    return sorted(fresh + kept, key=lambda c: order.get(c.get("section") or SECTIONS[0], 0))


def sync(limit: int | None = None, progress=print, extended: bool = False) -> dict:
    """Read the match list and every chat in it; write data/chats.json.

    Normal: the "Your turn" section. Extended: also expands and reads "Their turn" and "Hidden"."""
    started = datetime.now().astimezone().isoformat()
    sections = SECTIONS if extended else SECTIONS[:1]
    opened = []
    try:
        open_matches()
        if extended:
            opened = expand_sections(sections[1:])
            if opened:
                progress(f"[chats] opened {', '.join(opened)}")
        matches = read_match_list(sections)
        progress(f"[chats] {len(matches)} matches in {', '.join(sections)}")
        chats = []
        for i, m in enumerate(matches[:limit] if limit else matches, 1):
            try:
                c = read_chat(m["name"], m["preview"], sections)
                c["readAt"] = datetime.now().astimezone().isoformat()
                c = m | c
                c["key"] = chat_key(c)
                chats.append(c)
                progress(f"[chats] {i}/{len(matches)} {m['name']}: {len(c['messages'])} messages")
            except Exception as e:
                failed = m | {"messages": None, "error": str(e)}
                failed["key"] = chat_key(failed)
                chats.append(failed)
                progress(f"[chats] {m['name']}: {e}")
                open_matches()
                _scroll_list_to_top()
        chats = _merge_reads(chats, sections, _previous().get("chats") or [])
        data = {"startedAt": started, "finishedAt": datetime.now().astimezone().isoformat(), "chats": chats,
                "extended": extended, "sections": list(sections)}
        try:
            changed = apply_outcomes(data)
            if changed:
                progress(f"[chats] outcomes updated: {', '.join(changed)}")
        except Exception as e:
            progress(f"[chats] outcomes not updated: {e!r}")
        tmp = CHATS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(CHATS_PATH)
        return data
    finally:
        try:
            if opened:
                collapse_sections(opened)
        except Exception as e:
            progress(f"[chats] couldn't close {opened}: {e!r}")
        try:  # leave the phone where the bot expects it
            disc = _tab(dump(), "Discover")
            if disc:
                _tap(disc)
        except Exception:
            pass


def main() -> None:
    p = argparse.ArgumentParser(description="Read Hinge chats off the phone")
    p.add_argument("--limit", type=int, default=None, help="Only read the first N chats")
    p.add_argument("--extended", action="store_true", help="Also read the Their turn and Hidden sections")
    args = p.parse_args()
    data = sync(args.limit, extended=args.extended)
    for c in data["chats"]:
        print(f"\n=== {c['name']} ({c.get('section')}) — {len(c['messages'] or [])} messages")
        for m in (c["messages"] or [])[-6:]:
            extra = f" (liked {m['liked']})" if m.get("kind") == "like" else f" [{m['reaction']}]" if m.get("reaction") else ""
            print(f"  [{m.get('stamp') or ''}] {m['who']}: {m['text']}{extra}")


if __name__ == "__main__":
    main()
