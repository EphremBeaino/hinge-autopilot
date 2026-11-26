"""Runtime configuration.

Everything a user can change lives in data/settings.json and is edited from the
web UI (python app.py). Nothing here needs hand editing. Set HA_DATA_DIR to keep
the data somewhere else (several phones, a demo copy, ...).
"""

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("HA_DATA_DIR") or BASE_DIR / "data").resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DEBUG_DIR = DATA_DIR  # older modules call it this
SETTINGS_PATH = DATA_DIR / "settings.json"
PROFILE_PATH = DATA_DIR / "profile.json"
TOOLS_DIR = BASE_DIR / "tools"

DEFAULTS = {
    # AI provider: anthropic | openai | gemini | claude_code (the logged in `claude` CLI, no key)
    "provider": "anthropic",
    "model": "",                 # "" = the provider's default below
    "effort": "medium",          # Claude only: low | medium | high
    "api_keys": {},              # provider -> key, stored on this PC only
    # automation (the Schedule page)
    "readAfterRun": True, "readEveryHours": 0, "autoSuggest": True,
    "autoRun": False, "runAt": "19:00", "runRandomMin": 60, "runLikes": 8,
    "runSessions": 1, "runGapMin": 120, "minLikeScore": 6,
    "dryRun": False,             # judge and log, never like
    "privacy": False,            # blur photos and shorten names in the UI (for screenshots)
    "screen": None,              # [w, h] of the phone, detected on connect
    "adbPath": "",
}

DEFAULT_MODELS = {
    "anthropic": "claude-opus-5-5",
    "claude_code": "claude-opus-5-5",
    "openai": "gpt-5.1",
    "gemini": "gemini-3.5-flash",
}


def load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    return {**DEFAULTS, **data}


def save_settings(s: dict) -> None:
    tmp = SETTINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1), encoding="utf-8")
    tmp.replace(SETTINGS_PATH)


def load_profile() -> dict:
    try:
        return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


_s = load_settings()
PROVIDER = _s["provider"]
MODEL = _s["model"] or DEFAULT_MODELS.get(PROVIDER, "claude-opus-5-5")
EFFORT = _s["effort"]
DRY_RUN = bool(_s["dryRun"])
DRY_RUN_MESSAGE = False
MODE_NAME = "profile"
CLAUDE_CLI_TIMEOUT_S = 300

# ---------- session pacing ----------
MAX_LIKES_PER_SESSION = 8
SESSION_LIKE_MIN = 5
MAX_PROFILES_PER_SESSION = 100
COMMENT_MAX_WORDS = 40
COMMENT_MAX_CHARS = 300
FRAMES_PER_PROFILE = 7
FRAMES_MAX_PER_PROFILE = 11
SAVE_DEBUG_FRAMES = True

# ---------- screen + coordinates ----------
# Measured on a 1080x2400 phone and scaled to the connected phone's size.
BASE_W, BASE_H = 1080, 2400
_BASE_COORDS = {
    "skip_button":    (121, 1983),
    "heart_photo_1":  (930, 1354),
    "send_like_button": (694, 1308),
    "comment_input":  (500, 1130),
    "compose_close":  (975, 203),
    "scroll_from":    (540, 1688),
    "scroll_to":      (540, 698),
    "nav_discover":   (118, 2187),
    "nav_standouts":  (324, 2187),
    "nav_likes_you":  (540, 2187),
    "nav_matches":    (756, 2187),
    "nav_self_pfp":   (972, 2187),
}
SCREEN_WIDTH, SCREEN_HEIGHT = BASE_W, BASE_H
SCALE_X = SCALE_Y = 1.0
COORDS: dict = {}


def apply_screen(w: int, h: int) -> None:
    g = globals()
    g["SCREEN_WIDTH"], g["SCREEN_HEIGHT"] = int(w), int(h)
    g["SCALE_X"], g["SCALE_Y"] = w / BASE_W, h / BASE_H
    g["COORDS"] = {k: (round(x * w / BASE_W), round(y * h / BASE_H)) for k, (x, y) in _BASE_COORDS.items()}
    g["COORDS"]["scroll_duration_ms"] = 350


apply_screen(*(_s.get("screen") or (BASE_W, BASE_H)))

DELAYS = {
    "after_scroll":     (0.6, 1.0),
    "after_screenshot": (0.2, 0.4),
    "after_tap":        (0.8, 1.4),
    "after_like_sent":  (2.5, 4.0),
    "after_skip":       (1.5, 2.5),
}


def adb_path() -> str:
    """adb from the setting, else the copy the setup page downloads into tools/, else PATH."""
    s = load_settings()
    if s.get("adbPath") and Path(s["adbPath"]).exists():
        return s["adbPath"]
    local = TOOLS_DIR / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb")
    if local.exists():
        return str(local)
    return "adb"
