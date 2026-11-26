"""Image-based detection for UI elements whose position varies per profile.

Uses OpenCV template matching for reliable detection immune to profile
photo colors and Hinge UI updates.
"""

import io
from pathlib import Path

import config

import cv2
import numpy as np
from PIL import Image


# Scale factor: resolution-independent constants are defined for 1080px
# (Pixel 10 reference width) and scaled at import time.
_S = config.SCALE_X

# Template images (one-time load). Hinge draws the like buttons differently
# depending on the subscription, so each element has one template per look:
#   *.jpg        HingeX: double-heart icon, dark "Send Priority Like"
#   *.basic.jpg  free Hinge: single-heart icon, light "Send Like"
# Every variant is tried; the best match above the threshold wins.
_TEMPLATE_DIR = Path(__file__).parent / "templates"
_HEART_FILES = ("heart_template.jpg", "heart_template.basic.jpg")
_SENDLIKE_FILES = ("sendlike_template.jpg", "sendlike_template.basic.jpg")
_heart_templates: list[tuple[str, np.ndarray]] = []
_sendlike_templates: list[tuple[str, np.ndarray]] = []

_SENDLIKE_CONFIDENCE = 0.85
_HEART_CONFIDENCE = 0.85


def _load_templates() -> None:
    """Lazy-load template images from disk. Called once per session."""
    for files, out in ((_HEART_FILES, _heart_templates), (_SENDLIKE_FILES, _sendlike_templates)):
        if out:
            continue
        for name in files:
            path = _TEMPLATE_DIR / name
            img = cv2.imread(str(path), cv2.IMREAD_COLOR) if path.exists() else None
            if img is not None:
                out.append((name, img))


def _png_to_ndarray(png: bytes) -> np.ndarray | None:
    """Decode PNG bytes to BGR numpy array (OpenCV format). None when the
    bytes aren't a decodable image (truncated adb screenshot)."""
    if not png:
        return None
    return cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)


def _png_to_array(png: bytes) -> np.ndarray:
    """Decode PNG bytes to RGB numpy array (PIL format)."""
    return np.array(Image.open(io.BytesIO(png)).convert("RGB"))


# ============================================================
# find_send_like — OpenCV template matching
# ============================================================

def find_send_like(png: bytes) -> tuple[int, int] | None:
    """Locate the 'Send Like' button via OpenCV template matching.

    Searches the right half of the screen. Returns (x, y) center of the
    best match, or None if confidence is below threshold or the template
    file is missing.
    """
    _load_templates()
    if not _sendlike_templates:
        print("  Send Like: no template file — skipping")
        return None

    screen = _png_to_ndarray(png)
    if screen is None:
        print("  Send Like: unreadable screenshot — skipping")
        return None
    scr_h, scr_w = screen.shape[:2]

    # Compose card sits in right half, middle 50% vertically
    y0 = int(scr_h * 0.25)
    y1 = int(scr_h * 0.75)
    x_crop = int(scr_w * 0.25)
    roi = screen[y0:y1, x_crop:]

    best = (-1.0, None, None)  # (conf, center, template name)
    for name, tmpl in _sendlike_templates:
        tmpl_h, tmpl_w = tmpl.shape[:2]
        if roi.shape[0] < tmpl_h or roi.shape[1] < tmpl_w:
            continue
        result = cv2.matchTemplate(roi, tmpl, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        if max_val > best[0]:
            best = (max_val, (x_crop + max_loc[0] + tmpl_w // 2, y0 + max_loc[1] + tmpl_h // 2), name)

    max_val, center, name = best
    if center is not None and max_val >= _SENDLIKE_CONFIDENCE:
        print(f"  Send Like: template match at {center} conf={max_val:.3f} ({name})")
        return center

    print(f"  Send Like: not found (best conf={max_val:.3f} < {_SENDLIKE_CONFIDENCE})")
    return None


# ============================================================
# find_send_like_anyway — free Hinge's "Send a Rose instead?" sheet
# ============================================================

_ANYWAY_CONFIDENCE = 0.85


def find_send_like_anyway(png: bytes) -> tuple[int, int] | None:
    """Locate "Send Like anyway" on the Rose upsell sheet that free Hinge
    shows after Send Like. The sheet's dark "Send a Rose" button sits just
    above it; only this text link is ever returned."""
    path = _TEMPLATE_DIR / "rose_upsell_anyway.jpg"
    tmpl = cv2.imread(str(path), cv2.IMREAD_COLOR) if path.exists() else None
    screen = _png_to_ndarray(png)
    if tmpl is None or screen is None:
        return None
    scr_h, scr_w = screen.shape[:2]
    y0 = int(scr_h * 0.60)  # the sheet is bottom-anchored
    roi = screen[y0:, :]
    tmpl_h, tmpl_w = tmpl.shape[:2]
    result = cv2.matchTemplate(roi, tmpl, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    if max_val < _ANYWAY_CONFIDENCE:
        return None
    center = (max_loc[0] + tmpl_w // 2, y0 + max_loc[1] + tmpl_h // 2)
    print(f"  Rose upsell: 'Send Like anyway' at {center} conf={max_val:.3f}")
    return center


# ============================================================
# find_first_heart — OpenCV template matching
# ============================================================

def find_first_heart(png: bytes) -> tuple[int, int] | None:
    """Locate the heart icon on photo 1 via OpenCV template matching.

    Searches the right ~40% of the screen across the full height.
    Finds all heart matches above confidence and returns the top-most
    one (smallest Y = highest on screen = photo 1).
    """
    _load_templates()
    if not _heart_templates:
        print("  Heart vision: no template file — skipping")
        return None

    screen = _png_to_ndarray(png)
    if screen is None:
        print("  Heart vision: unreadable screenshot — skipping")
        return None
    scr_h, scr_w = screen.shape[:2]

    x0 = int(scr_w * 0.60)
    x1 = scr_w
    y0 = int(scr_h * 0.10)
    y1 = int(scr_h * 0.90)
    roi = screen[y0:y1, x0:x1]

    # Every match above threshold, from every variant; the top-most one
    # (smallest Y = highest on screen) is photo 1.
    hits = []  # (cy, cx, conf, name)
    best_seen = 0.0
    for name, tmpl in _heart_templates:
        tmpl_h, tmpl_w = tmpl.shape[:2]
        if roi.shape[0] < tmpl_h or roi.shape[1] < tmpl_w:
            continue
        result = cv2.matchTemplate(roi, tmpl, cv2.TM_CCOEFF_NORMED)
        best_seen = max(best_seen, float(result.max()))
        ys, xs = np.where(result >= _HEART_CONFIDENCE)
        for my, mx in zip(ys, xs):
            hits.append((y0 + my + tmpl_h // 2, x0 + mx + tmpl_w // 2, float(result[my, mx]), name))

    if not hits:
        print(f"  Heart vision: not found (best conf={best_seen:.3f} < {_HEART_CONFIDENCE})")
        return None

    # The top-most hit is the edge of a cluster of near-matches: take the
    # strongest match within one icon height of it.
    top = min(h[0] for h in hits)
    cy, cx, conf, name = max((h for h in hits if h[0] - top < 40 * _S), key=lambda h: h[2])
    print(f"  Heart vision: top match at ({cx}, {cy}) conf={conf:.3f} ({name}, "
          f"{len(hits)} total above threshold)")
    return (int(cx), int(cy))


# ============================================================
# Comment field / input helpers
# ============================================================

def comment_field_text_pixels(png: bytes, send_like_xy: tuple[int, int]) -> int:
    """Count dark (text) pixels in the comment input area above Send Like."""
    arr = _png_to_array(png)
    sx, sy = send_like_xy
    y0 = max(0, sy - int(230 * _S))
    y1 = max(0, sy - int(60 * _S))
    x0 = max(0, sx - int(350 * _S))
    x1 = min(arr.shape[1], sx + int(350 * _S))
    region = arr[y0:y1, x0:x1]
    if region.size == 0:
        return 0
    dark = (region.max(axis=-1) < 130).sum()
    return int(dark)


def find_comment_input(send_like_xy: tuple[int, int]) -> tuple[int, int]:
    """Comment input sits at a fixed offset above the Send Like button."""
    _, send_y = send_like_xy
    return (int(540 * _S), send_y - int(171 * _S))


# ============================================================
# is_app_loading — detect stuck loading / splash screen
# ============================================================

def is_app_loading(png: bytes) -> bool:
    """Check if Hinge is stuck on the loading/splash screen.

    The loading screen is a white backdrop with a small animated logo
    in the center — the animation creates some pixel variation at
    center, but the overwhelming majority of the content area is still
    near-white. A loaded profile has a photo card, text overlays, and
    UI buttons that fill most of the screen with non-white pixels.

    White ratio alone is NOT enough: a studio photo on a white backdrop
    reads as 78% white and got judged as a loading screen three times in a
    row, ending the session. The decisive signal is Hinge's bottom nav bar,
    which is dark (mean ~36) on every in-app screen and absent while the app
    is still loading. Both must agree.
    """
    try:
        im = np.array(Image.open(io.BytesIO(png)).convert("L"))
    except (OSError, ValueError):
        return False  # unreadable frame: don't claim the app is loading
    h, w = im.shape
    if h < 1500 or w < 950:
        return False

    # Full content area: exclude status bar (~y=0-150) and nav bar
    # (~bottom 250px). The animated logo occupies a small fraction of
    # this region — it won't push white_ratio below the threshold.
    content = im[150:h - 250, 50:w - 50]
    if content.size == 0:
        return False

    white_ratio = (content > 230).mean()
    if white_ratio <= 0.75:
        return False

    # Bottom nav strip, above the Android buttons: dark whenever Hinge's UI
    # is up, bright/white on the splash.
    nav = im[h - 260:h - 170, :]
    nav_is_dark = nav.size > 0 and nav.mean() < 120
    if nav_is_dark:
        print(f"  (white-looking screen ({white_ratio:.0%}) but the nav bar "
              f"is up — treating as a real profile)")
        return False
    return True
