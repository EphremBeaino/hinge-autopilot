"""Thin wrapper around `adb` commands."""

import os
import random
import shlex
import subprocess
import time

import config


def _pin_serial() -> None:
    """Point every adb call (ours and child processes') at the one device
    that's online. A stale emulator listed as "offline" otherwise makes plain
    `adb` fail intermittently with "more than one device/emulator"."""
    if os.environ.get("ANDROID_SERIAL"):
        return
    try:
        out = subprocess.run([config.adb_path(), "devices"], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return
    online = [l.split("\t")[0] for l in out.splitlines()[1:] if l.endswith("\tdevice")]
    if len(online) == 1:
        os.environ["ANDROID_SERIAL"] = online[0]


ADB_TIMEOUT = 60  # seconds; a hung adb call must fail, not freeze the caller forever (Oct 23-25 hang)


def _run(args: list[str], capture: bool = False) -> bytes | None:
    _pin_serial()
    cmd = [config.adb_path()] + args
    if capture:
        result = subprocess.run(cmd, capture_output=True, check=True, timeout=ADB_TIMEOUT)
        return result.stdout
    subprocess.run(cmd, check=True, timeout=ADB_TIMEOUT)
    return None


def check_device() -> str:
    """Raise if no device is connected. Return the serial of the first device."""
    out = _run(["devices"], capture=True).decode()
    lines = [l for l in out.splitlines()[1:] if l.strip() and "\tdevice" in l]
    if not lines:
        raise RuntimeError(
            "No Android phone found. Plug it in with USB debugging on and accept the prompt on the phone."
        )
    return lines[0].split("\t")[0]


def screenshot(retries: int = 3) -> bytes:
    """Return PNG bytes of the current screen.

    `exec-out screencap` occasionally returns a truncated PNG (the stream is
    cut mid-transfer). A short PNG blows up later in PIL, so validate the
    IEND trailer here and re-take instead."""
    last = b""
    for attempt in range(retries):
        last = _run(["exec-out", "screencap", "-p"], capture=True) or b""
        if len(last) > 1024 and last.startswith(b"\x89PNG") and last.rstrip().endswith(b"\xaeB`\x82"):
            return last
        print(f"adb: truncated screenshot ({len(last)} bytes), "
              f"retry {attempt + 1}/{retries}")
        time.sleep(0.6)
    return last  # hand back whatever we got; callers guard their own parsing


def tap(x: int, y: int, jitter: int = 10) -> None:
    """Tap at (x, y) with ±`jitter` px randomness."""
    x += random.randint(-jitter, jitter)
    y += random.randint(-jitter, jitter)
    _run(["shell", "input", "tap", str(x), str(y)])


def input_text(text: str) -> None:
    """Type `text` into the currently focused field via `adb shell input text`.

    Single-quotes the payload with shlex so $, `, ", and spaces survive the
    device shell. Caller should keep text plain ASCII — emoji and the chars
    \\ " $ ` should be filtered upstream (the model is instructed to avoid
    them) since `input text` itself only sends keyevents.
    """
    quoted = shlex.quote(text)
    try:
        _run(["shell", f"input text {quoted}"])
    except subprocess.CalledProcessError as e:
        # Some devices/IME implementations crash `input text` with a
        # NullPointerException (Moto e20 / Gboard). When this happens
        # the keyboard is in a stuck state — dismiss it so the rest
        # of the pipeline can recover.
        print(f"adb: input_text failed (device IME crash) — dismissing"
              f" keyboard\n    text={text!r}")
        try:
            _run(["shell", "input", "keyevent", "4"])
        except subprocess.CalledProcessError:
            pass


def swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
    _run(["shell", "input", "swipe",
          str(x1), str(y1), str(x2), str(y2), str(duration_ms)])


def _human_swipe(sx: int, sy: int, dy: int) -> None:
    """One scroll gesture that never repeats exactly: the start point moves by a few dozen pixels,
    the finger drifts a little sideways, and the length and speed vary. The length stays within
    85-115% of the calibrated step, so consecutive screenshots still overlap and nothing is skipped."""
    c = config.COORDS
    x0 = sx + random.randint(-60, 60)
    y0 = sy + random.randint(-50, 50)
    length = int(dy * random.uniform(0.85, 1.15))
    x1 = x0 + random.randint(-25, 25)
    base = c["scroll_duration_ms"]
    swipe(x0, y0, x1, y0 + length, random.randint(int(base * 0.75), int(base * 1.35)))


def scroll_down() -> None:
    c = config.COORDS
    _human_swipe(int(c["scroll_from"][0]), int(c["scroll_from"][1]), -(int(c["scroll_from"][1]) - int(c["scroll_to"][1])))


def scroll_up() -> None:
    c = config.COORDS
    _human_swipe(int(c["scroll_to"][0]), int(c["scroll_to"][1]), int(c["scroll_from"][1]) - int(c["scroll_to"][1]))


def jitter_sleep(key: str) -> None:
    lo, hi = config.DELAYS[key]
    time.sleep(random.uniform(lo, hi))


def is_screen_awake() -> bool:
    """Check if the device screen is on."""
    out = _run(["shell", "dumpsys", "power"], capture=True).decode()
    return "mWakefulness=Awake" in out


def wake_screen() -> None:
    """Turn the screen on if it's off."""
    if not is_screen_awake():
        _run(["shell", "input", "keyevent", "26"])
        time.sleep(1.0)


def force_stop_app(package: str) -> None:
    """Force-stop the app so the next launch starts fresh."""
    _run(["shell", "am", "force-stop", package])
    time.sleep(0.5)


def dismiss_keyboard_if_visible() -> bool:
    """Dismiss the soft keyboard if it's currently visible.

    Uses dumpsys input_method to check mInputShown, then sends a single
    KEYCODE_BACK only when the IME is confirmed visible. Safe to call
    when the keyboard isn't up — does nothing and returns False.
    """
    out = _run(["shell", "dumpsys", "input_method"], capture=True)
    if out and b"mInputShown=true" in out:
        _run(["shell", "input", "keyevent", "4"])  # KEYCODE_BACK
        time.sleep(0.3)
        return True
    return False


def focused_window() -> str:
    """The `mCurrentFocus` line from dumpsys window (e.g.
    'Window{... u0 co.hinge.app/co.hinge.app.ui.AppActivity}')."""
    out = _run(["shell", "dumpsys", "window"], capture=True).decode(errors="ignore")
    for line in out.splitlines():
        if "mCurrentFocus" in line:
            return line.strip()
    return ""


def is_foreground(package: str) -> bool:
    """True if `package` owns the focused window."""
    return f" {package}/" in focused_window()


def press_back() -> None:
    """Press the Android back button once."""
    _run(["shell", "input", "keyevent", "4"])


def launch_app(package: str) -> None:
    """Launch an Android app by package name. Force-stops first for a clean slate."""
    force_stop_app(package)
    _run(["shell", "monkey", "-p", package, "1"])
    time.sleep(3.0)


def go_home() -> None:
    """Send the home key."""
    _run(["shell", "input", "keyevent", "3"])
    time.sleep(0.5)


def turn_screen_off() -> None:
    """Turn the screen off."""
    _run(["shell", "input", "keyevent", "26"])


def devices() -> list[dict]:
    """Every device adb sees: [{serial, state, model}]. state: device | unauthorized | offline."""
    out = subprocess.run([config.adb_path(), "devices", "-l"], capture_output=True, text=True, timeout=15).stdout
    found = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        model = next((p.split(":", 1)[1] for p in parts[2:] if p.startswith("model:")), "")
        found.append({"serial": parts[0], "state": parts[1], "model": model.replace("_", " ")})
    return found


def screen_size() -> tuple[int, int]:
    """Physical (or overridden) screen size from `wm size`."""
    out = _run(["shell", "wm", "size"], capture=True).decode()
    sizes = [l.split(":")[-1].strip() for l in out.splitlines() if "size" in l.lower()]
    w, h = (int(x) for x in sizes[-1].split("x"))
    return w, h


def has_package(package: str) -> bool:
    out = _run(["shell", "pm", "list", "packages", package], capture=True).decode()
    return f"package:{package}" in out
