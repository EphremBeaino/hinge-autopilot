"""Find the "Like prompt" button for the prompt the comment is about.

Hinge's data: likes on a text prompt are 47% more likely to lead to a date than likes on a
photo, and ~80% of the judge's comments answer a prompt. The judge's `target` names it in
free text ('Prompt: "My simple pleasures" (Anime, pottery...)', sometimes "crossed with"
a second item); the screen's accessibility tree has every prompt as
"Prompt: <question>. Answer: <answer>" with a clickable "Like prompt" inside its card.

find_prompt_like(target) scrolls down from the top of the profile until that prompt's card
is on screen with its button, and returns the button's center. None when the target isn't a
prompt or no prompt matches well enough (the caller falls back to photo 1).
"""

import re
import time

import adb
import chats

MAX_SCROLLS = 12
_STOP = {"prompt", "answer", "the", "a", "an", "and", "or", "of", "to", "my", "i", "im", "is", "for", "with",
         "read", "against", "crossed", "plus", "photo", "her", "you", "me", "on", "in", "it", "its", "that", "this"}


def _words(text: str) -> list[str]:
    return [w for w in re.sub(r"[^\w\s]", " ", (text or "").lower()).split() if w not in _STOP and len(w) > 1]


def _head(target: str) -> str:
    """The first item the target names (the one the comment answers); later ones are context."""
    t = re.split(r"\b(?:read against|crossed with|cross[- ]read with|plus|paired with)\b|;|\s/\s", target or "", maxsplit=1, flags=re.I)[0]
    return t


def is_prompt_target(target: str) -> bool:
    return bool(re.match(r"\s*\W*prompt\b", target or "", re.I))


def score(target: str, prompt_desc: str) -> float:
    """How well a "Prompt: Q. Answer: A" node matches the target's head: the share of the
    question's words found, or of the answer's (targets quote one or the other)."""
    m = re.match(r"Prompt:\s*(.*?)\.\s*Answer:\s*(.*)$", prompt_desc, re.S)
    q, a = (m.group(1), m.group(2)) if m else (prompt_desc, "")
    head = set(_words(_head(target)))
    if not head:
        return 0.0
    cov = lambda words: (sum(1 for w in words if w in head) / len(words)) if words else 0.0
    return max(cov(_words(q)), cov(_words(a)[:12]))


def find_prompt_like(target: str, log=print) -> tuple[int, int] | None:
    """Center of the "Like prompt" button of the prompt `target` names, scrolling down to it.
    Call with the profile scrolled to the top."""
    if not is_prompt_target(target):
        return None
    best_seen = 0.0
    for i in range(MAX_SCROLLS):
        root = chats.dump()
        nodes = list(root.walk())
        buttons = [n for n in nodes if n.desc == "Like prompt" and n.clickable]
        for p in (n for n in nodes if n.desc.startswith("Prompt:")):
            s = score(target, p.desc)
            best_seen = max(best_seen, s)
            if s < 0.6:
                continue
            btn = next((b for b in buttons if p.y1 <= b.y1 and b.y2 <= p.y2 + 5), None)
            if btn:
                log(f"  Prompt like: {p.desc[:70]!r} (match {s:.2f}) at {btn.center}")
                return btn.center
            # the right prompt, but its button is below the fold: scroll a little and look again
        adb.scroll_down()
        time.sleep(1.0)
    log(f"  Prompt like: no prompt matched the target (best {best_seen:.2f}) — liking photo 1")
    return None
