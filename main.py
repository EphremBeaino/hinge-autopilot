"""The swipe loop: capture a profile -> ask the AI -> like (with a comment) or skip -> repeat.

Started by app.py (the Run page); can also run alone: python main.py --likes 5
"""

import argparse
import hashlib
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

import io

from PIL import Image, ImageFile

import adb
import config
import metrics
import like_target
import vision
from judge import finalize_comment, judge

# Windows consoles/pipes default to cp1252: an emoji or a dash in a print
# then raises mid-like and can end the session. Never let logging do that.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HINGE = "co.hinge.app"


class HingeNotForeground(RuntimeError):
    """Hinge isn't the focused app — never scroll or tap blind."""


def _min_like_score() -> int:
    """The minimum score for a like (Run page). Read per profile so a change applies mid-run."""
    try:
        return int(config.load_settings().get("minLikeScore") or 0)
    except (TypeError, ValueError):
        return 0


def require_hinge() -> None:
    """Raise unless Hinge is in the foreground. Called before every scroll
    and tap: on this phone Back from Discover exits Hinge, and gestures
    sent to the launcher open other apps."""
    if adb.is_foreground(HINGE):
        return
    time.sleep(1.0)  # focus can be briefly null during transitions
    if not adb.is_foreground(HINGE):
        raise HingeNotForeground(
            f"Hinge is not in the foreground ({adb.focused_window()!r}) — "
            f"stopping instead of acting blind"
        )


ImageFile.LOAD_TRUNCATED_IMAGES = True  # a clipped screenshot must not kill a run


def _profile_region_hash(png: bytes) -> str:
    """md5 over a cropped region of the frame, excluding status bar (clock
    ticks every minute) and bottom nav (badges flicker). Two screenshots
    of the same profile taken 60+ seconds apart should produce the same
    hash; two different profiles should always differ."""
    try:
        img = Image.open(io.BytesIO(png))
        w, h = img.size
        crop = img.crop((0, 120, w, h - 220))
        return hashlib.md5(crop.tobytes()).hexdigest()
    except (OSError, ValueError) as e:
        # Unreadable frame: return a value that can't match anything, so the
        # caller treats it as "changed" rather than crashing the session.
        print(f"  (unreadable screenshot: {e} — treating as new)")
        return f"unreadable-{time.time()}"


def capture_profile() -> list[bytes]:
    """Scroll through the current profile, returning a list of PNG frames."""
    # Check for stuck loading screen BEFORE any scrolls or interactions.
    # Saves ~3-8 wasted scroll-up swipes + avoids burning API credits
    # judging a loading screen as if it were a profile.
    require_hinge()
    initial = adb.screenshot()
    if vision.is_app_loading(initial):
        raise RuntimeError("app stuck on loading screen")

    # Defensive: new profiles load at the top, so this is just guarding
    # against the app being mid-scroll from a prior partial action. A
    # handful of swipes is enough — full 18-swipe sweep isn't needed
    # because we aren't recovering from a 7-frame scroll-down.
    scroll_back_to_top(swipes=random.randint(3, 8))

    frames = []
    frames.append(adb.screenshot())
    adb.jitter_sleep("after_screenshot")

    # Scroll until the screen stops changing (the bottom of the profile),
    # not a fixed count: a long profile needs more than FRAMES_PER_PROFILE
    # and a short one wastes frames (and judge tokens) repeating the end.
    max_frames = max(config.FRAMES_PER_PROFILE, config.FRAMES_MAX_PER_PROFILE)
    last_hash = _profile_region_hash(frames[0])
    while len(frames) < max_frames:
        require_hinge()
        adb.scroll_down()
        adb.jitter_sleep("after_scroll")
        shot = adb.screenshot()
        adb.jitter_sleep("after_screenshot")
        shot_hash = _profile_region_hash(shot)
        if shot_hash == last_hash and len(frames) >= 3:
            print(f"Reached the bottom after {len(frames)} frames.")
            break
        last_hash = shot_hash
        frames.append(shot)

    return frames


def scroll_back_to_top(swipes: int | None = None,
                       top_hash: str | None = None) -> None:
    """Scroll back to the top of the profile.

    Hinge uses momentum scrolling; each swipe's actual travel is much less
    than the gesture's pixel distance. Default `swipes` is `FRAMES_PER_PROFILE
    * 2 + 4` (18 with FRAMES=7) — enough to recover from a full scroll-down
    through the profile. Pass a smaller `swipes` for defensive top-scrolls
    where the profile is already at or near the top.
    """
    if swipes is None:
        # Enough to undo the longest capture (adaptive capture can go deeper
        # than FRAMES_PER_PROFILE); the hash check below usually stops sooner.
        swipes = max(config.FRAMES_PER_PROFILE,
                     config.FRAMES_MAX_PER_PROFILE) * 2 + 4
    print(f"Scrolling back to top (up to {swipes} swipes)...")
    # With top_hash (frame 0's region hash), stop as soon as the screen is
    # back to that exact view instead of always burning every swipe. Checks
    # start once we could plausibly be at the top again.
    min_before_check = max(1, config.FRAMES_PER_PROFILE - 2)
    for i in range(swipes):
        require_hinge()
        adb.scroll_up()
        adb.jitter_sleep("after_scroll")
        if top_hash and i + 1 >= min_before_check:
            if _profile_region_hash(adb.screenshot()) == top_hash:
                print(f"Back at top after {i + 1} swipes.")
                return
    print(f"Scrolling stopped — at top of profile.")


def do_skip() -> None:
    """Tap the X to advance to the next profile. Always taps (even in dry run);
    advancing is needed for the loop to see new profiles."""
    x, y = config.COORDS["skip_button"]
    require_hinge()
    adb.tap(x, y)
    adb.jitter_sleep("after_skip")


class StuckState(RuntimeError):
    """The app is in a state we won't guess our way out of."""


def _dismiss_compose_card_if_visible() -> None:
    """Check for a stale compose card left by a previous failed like.

    Never skip past it: the card may hold a comment for THIS profile (a
    crash between typing and sending leaves exactly that), and skipping
    throws away a profile the judge wanted. Stop and let the user clear it.
    """
    ss = adb.screenshot()
    if vision.find_send_like(ss) is not None or vision.find_send_like_anyway(ss) is not None:
        path = save_error_screenshot("stale-compose-card")
        raise StuckState(
            f"A comment box is already open (left over from an earlier like). "
            f"Close it on the phone, then run again. Screenshot: {path}"
        )


def do_like(message: str = "", top_hash: str | None = None, target: str | None = None) -> str:
    """Flow: scroll to top → tap the like button of the prompt the comment is about (else photo 1's
    heart) → type message → dismiss keyboard → find + click Send Like. Returns what was liked."""
    if config.DRY_RUN:
        do_skip()
        return "dry run"
    scroll_back_to_top(top_hash=top_hash)
    # Let the profile settle after scrolling.
    time.sleep(0.5)
    # Dismiss any stale compose card from a previous failed like.
    # (Modal overlay — scroll position doesn't affect it, so check here
    # where the screen is clean and heart detection won't false-match.)
    _dismiss_compose_card_if_visible()
    print("Scrolled to top — running OpenCV scans")

    # ── 4. The prompt the comment answers (likes on prompts lead to more dates), else photo 1 ──
    liked_on = "photo 1"
    heart_xy = None
    if target and like_target.is_prompt_target(target):
        heart_xy = like_target.find_prompt_like(target)
        if heart_xy:
            liked_on = "prompt"
        else:
            scroll_back_to_top(top_hash=top_hash)
            time.sleep(0.5)
    if heart_xy is None:
        heart_xy = vision.find_first_heart(adb.screenshot())
    if heart_xy is None:
        path = save_error_screenshot("heart-not-found")
        # A compose card covering the screen is the usual cause, and the
        # stale-card check above can miss it mid-animation.
        if vision.find_send_like(adb.screenshot()) is not None:
            raise StuckState(
                f"Couldn't find the heart: a comment box is covering the "
                f"profile. Close it on the phone, then run again. "
                f"Screenshot: {path}"
            )
        raise RuntimeError(
            f"vision: couldn't find photo-1 heart after scroll-back ({path})")
    print(f"Clicking the like on {liked_on} at {heart_xy}")
    require_hinge()
    adb.tap(*heart_xy)
    adb.jitter_sleep("after_tap")

    # ── 5. Type the message ──
    if message and not config.DRY_RUN_MESSAGE:
        # Hinge auto-focuses the comment field after the heart tap —
        # the keyboard opens on its own. Wait for the compose card
        # to settle before typing; don't tap the field (tapping away
        # would dismiss the keyboard).
        adb.jitter_sleep("after_tap")
        # ── Density check commented out (never successful) ──
        # est_send_y = heart_xy[1] + int(105 * config.SCALE_Y)
        # empty_pixels = vision.comment_field_text_pixels(
        #     adb.screenshot(), (int(config.SCREEN_WIDTH / 2), est_send_y))
        require_hinge()
        adb.input_text(message)
        # deadline = time.monotonic() + 15
        # target_pixels = empty_pixels + max(150, 20 * len(message))
        # while time.monotonic() < deadline:
        #     time.sleep(1.0)
        #     current = vision.comment_field_text_pixels(
        #         adb.screenshot(), (int(config.SCREEN_WIDTH / 2), est_send_y))
        #     if current >= target_pixels:
        #         break
        # else:
        #     print(f"WARN: typed text didn't reach expected pixel density "
        #           f"(have {current}, want {target_pixels}) — sending anyway.")

    # ── 6. Dismiss keyboard ──
    if message and not config.DRY_RUN_MESSAGE:
        adb.dismiss_keyboard_if_visible()

    # ── 7. Find and click Send Like ──
    # Dismiss keyboard first in case Hinge auto-focused the comment field
    # and the keyboard is covering Send Like.
    adb.dismiss_keyboard_if_visible()
    send_xy = vision.find_send_like(adb.screenshot())
    # A prompt like opens its comment box inline in the profile: Send Like can sit below the
    # bottom nav. Nudge the page up a little (never enough to lose the box) and look again.
    for _ in range(3):
        if send_xy is not None:
            break
        y = random.randint(1550, 1650)
        adb.swipe(random.randint(500, 580), y, random.randint(500, 580), y - random.randint(420, 520),
                  random.randint(320, 460))
        time.sleep(1.2)
        send_xy = vision.find_send_like(adb.screenshot())
    if send_xy is None:
        save_error_screenshot("send-like-not-found")
        raise RuntimeError("vision: couldn't find Send Like after heart tap")
    # The Rose button sits immediately left of "Send Priority Like"
    # (x < ~375 on 1080 wide). Never tap anything in that half.
    if send_xy[0] < config.SCREEN_WIDTH * 0.45:
        save_error_screenshot("send-like-suspicious-x")
        raise RuntimeError(f"Send Like match at {send_xy} is too far left "
                           f"(Rose button zone) — refusing to tap")
    print(f"Tapping Send Like at {send_xy} — sending like! 🎯")
    require_hinge()
    adb.tap(*send_xy)
    adb.jitter_sleep("after_like_sent")

    # Free Hinge interrupts with "Send a Rose instead?": the like only goes
    # out after "Send Like anyway". Never tap "Send a Rose" (the dark button
    # right above it) — only the matched text link, and only low on screen.
    after = adb.screenshot()
    for _ in range(3):
        anyway_xy = vision.find_send_like_anyway(after)
        if anyway_xy is None:
            break
        if anyway_xy[1] < config.SCREEN_HEIGHT * 0.80:
            save_error_screenshot("send-anyway-suspicious-y")
            raise RuntimeError(f"'Send Like anyway' match at {anyway_xy} is too high "
                               f"(Send a Rose zone) — refusing to tap")
        print(f"Tapping 'Send Like anyway' at {anyway_xy} (declining the Rose upsell)")
        require_hinge()
        adb.tap(*anyway_xy, jitter=4)
        adb.jitter_sleep("after_like_sent")
        after = adb.screenshot()

    # Proof the like actually went out: the compose card and the Rose sheet
    # must both be gone. Keep the screenshot either way so "was it sent?" is
    # answerable later.
    sent_dir = config.DEBUG_DIR / "sent"
    sent_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    still_open = (vision.find_send_like(after) is not None
                  or vision.find_send_like_anyway(after) is not None)
    path = sent_dir / f"{stamp}_{'NOT-SENT' if still_open else 'sent'}.png"
    path.write_bytes(after)
    if still_open:
        raise RuntimeError(
            f"Send Like tapped but the compose card is still open — like "
            f"probably NOT sent (see {path})"
        )
    print(f"Like confirmed sent (compose card closed) — proof: {path.name}")

    # Clean up any lingering keyboard so it doesn't carry over to the
    # next profile and block heart / Send Like detection.
    if adb.dismiss_keyboard_if_visible():
        print("Keyboard was still open after like — dismissed.")
    return liked_on


def _recover_from_dialog() -> None:
    """Attempt to dismiss a dialog by pressing back once."""
    adb.press_back()
    time.sleep(1.0)


def save_error_screenshot(context: str) -> str:
    """Capture the current screen and save to debug/errors/ for post-mortem.

    Returns the path to the saved screenshot."""
    errors_dir = config.DEBUG_DIR / "errors"
    errors_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    png = adb.screenshot()
    path = errors_dir / f"{ts}_{context}.png"
    path.write_bytes(png)
    return str(path)


def save_debug(frames: list[bytes], decision, profile_idx: int) -> str | None:
    """Saving debug artefacts must never end a session: a failure here costs
    a record, while raising costs the whole run."""
    try:
        return _save_debug(frames, decision, profile_idx)
    except Exception as e:
        print(f"save_debug failed ({e!r}) — continuing without frames on disk")
        return None


def _save_debug(frames: list[bytes], decision, profile_idx: int) -> str | None:
    if not config.SAVE_DEBUG_FRAMES:
        return None
    bucket = "liked" if decision.decision == "like" else "skipped"
    bucket_dir = config.DEBUG_DIR / bucket
    bucket_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^a-z0-9]", "", (decision.name or "unknown").lower()) or "unknown"
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = bucket_dir / f"{ts}_{profile_idx:02d}_{safe_name}"
    imgs = folder / "imgs"
    imgs.mkdir(parents=True, exist_ok=True)
    for i, png in enumerate(frames):
        (imgs / f"frame_{i:02d}.png").write_bytes(png)
    (folder / "decision.txt").write_text(

        f"name: {decision.name}\n"
        f"decision: {decision.decision}\n"
        f"confidence: {decision.confidence}\n"
        f"reasoning: {decision.reasoning}\n"
        f"message: {decision.message}\n"
        f"message_archetype: {decision.message_archetype}\n"
        f"prompt_referenced: {decision.prompt_referenced}\n"
        f"skip_reason: {decision.skip_reason}\n"
        f"skill: {decision.extra}\n"
        f"timestamp: {datetime.now().isoformat(timespec='seconds')}\n",
        # Explicit UTF-8: Windows defaults to cp1252, and the judge writes
        # real minus signs, dashes and quotes, which crashed a whole run.
        encoding="utf-8",
    )
    return folder.name


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="HingeAuto loop runner")
    p.add_argument(
        "--likes",
        type=int,
        default=None,
        help="Keep going until this many likes have been sent (instead of the "
             "randomized 5-8 session cap). --max-profiles still bounds how "
             "many profiles get judged looking for them.",
    )
    p.add_argument(
        "--max-profiles",
        type=int,
        default=None,
        help="Stop after this many profiles (overrides MAX_PROFILES_PER_SESSION "
             "for this run).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Capture the current profile (scroll gestures only), judge it, "
             "generate the comment if LIKE, print + log, and tap NOTHING. "
             "Then wait for Enter while you move to the next profile by hand. "
             "Does not relaunch Hinge or touch the nav bar.",
    )
    return p.parse_args(argv)


def _wait_for_next_profile(profile_idx: int) -> bool:
    """--dry-run: block until the user has moved to the next profile by
    hand. Returns False if they asked to quit."""
    metrics.set_status("waiting_for_next", profile_idx)
    ans = input("\n[dry-run] Move to the next profile yourself, then press "
                "Enter (q + Enter to quit): ")
    return ans.strip().lower() != "q"


def dry_run_loop() -> int:
    """Judge-only loop. Scroll gestures are the only input sent to the
    phone; no taps of any kind."""
    profiles_seen = 0
    total_cost = 0.0
    last_frame0_hash: str | None = None
    print("Run:      DRY RUN via --dry-run (scrolls only, never taps)")
    print("Open Hinge on the Discover tab with a profile showing.")
    if input("Press Enter to capture the current profile (q to quit): ").strip().lower() == "q":
        return 0

    while True:
        profiles_seen += 1
        print(f"\n--- Profile {profiles_seen} (dry run) ---")
        metrics.set_status("capturing", profiles_seen)
        t0 = time.monotonic()
        try:
            frames = capture_profile()
        except RuntimeError as e:
            print(f"Capture failed: {e}")
            profiles_seen -= 1
            if not _wait_for_next_profile(profiles_seen):
                break
            continue
        t_capture = time.monotonic() - t0

        frame0_hash = _profile_region_hash(frames[0])
        if frame0_hash == last_frame0_hash:
            print("Same profile as last time — did you move to the next one?")
            profiles_seen -= 1
            if not _wait_for_next_profile(profiles_seen):
                break
            continue
        last_frame0_hash = frame0_hash

        metrics.set_status("judging", profiles_seen)
        t1 = time.monotonic()
        try:
            decision = judge(frames)
            finalize_comment(decision)
        except Exception as e:
            print(f"Judge failed: {e!r}")
            profiles_seen -= 1
            if not _wait_for_next_profile(profiles_seen):
                break
            continue
        t_judge = time.monotonic() - t1

        print(f"Name:     {decision.name}")
        print(f"Decision: {decision.decision} ({decision.confidence})")
        print(f"Reason:   {decision.reasoning}")
        if decision.decision == "like":
            print(f"Comment:  {decision.message or '(none — plain like)'}")

        folder_rel = None
        if decision.decision != "NOT_A_PROFILE":
            folder_name = save_debug(frames, decision, profiles_seen)
            if folder_name:
                bucket = "liked" if decision.decision == "like" else "skipped"
                folder_rel = f"{bucket}/{folder_name}"
        timing = {
            "capture_seconds": round(t_capture, 2),
            "judge_seconds":   round(t_judge, 2),
            "act_seconds":     0.0,
            "total_seconds":   round(t_capture + t_judge, 2),
        }
        metrics.log_profile(profiles_seen, decision, timing,
                            folder=folder_rel, dry_run=True)
        total_cost += metrics.estimated_cost(decision.usage)
        print(f"[totals] {profiles_seen} profiles | ${total_cost:.3f}")

        if not _wait_for_next_profile(profiles_seen):
            break

    metrics.set_status("stopped", profiles_seen)
    print(f"\nDry run done. {profiles_seen} profiles judged, ${total_cost:.3f}.")
    return 0


def main() -> int:
    args = _parse_args()
    load_dotenv()
    metrics.start_heartbeat()
    if args.max_profiles is not None:
        config.MAX_PROFILES_PER_SESSION = args.max_profiles

    serial = adb.check_device()
    print(f"Connected to: {serial}")
    if args.dry_run:
        return dry_run_loop()
    print(f"Run:      {'DRY RUN (no taps)' if config.DRY_RUN else 'LIVE (will tap)'}")
    if config.DRY_RUN_MESSAGE:
        print(f"Messages:  DRY RUN (generated but not sent)")
    if args.likes is not None:
        session_like_cap = args.likes
        print(f"Target:   {session_like_cap} likes (explicit), "
              f"max profiles: {config.MAX_PROFILES_PER_SESSION}")
        if session_like_cap > config.MAX_LIKES_PER_SESSION:
            print(f"  note: above the usual per-session ceiling of "
                  f"{config.MAX_LIKES_PER_SESSION}")
    else:
        session_like_cap = random.randint(
            config.SESSION_LIKE_MIN,
            config.MAX_LIKES_PER_SESSION,
        )
        print(f"Max likes: {session_like_cap} (randomized "
              f"{config.SESSION_LIKE_MIN}-{config.MAX_LIKES_PER_SESSION}), "
              f"max profiles: {config.MAX_PROFILES_PER_SESSION}")

    if session_like_cap == 0:
        print("Cap is 0 — skipping this session.")
        return 0

    # Start from the Hinge screen the user left open — no force-relaunch
    # (a fresh launch tends to show a full-screen promo first).
    adb.wake_screen()
    try:
        require_hinge()
    except HingeNotForeground:
        print("Open Hinge on the Discover tab, then start the bot again.")
        metrics.set_status("stopped", 0)
        return 1
    # Tap the Discover tab to ensure we're on the main profile feed
    adb.tap(*config.COORDS["nav_discover"])
    time.sleep(2)

    likes_sent = 0
    skips = 0
    profiles_seen = 0
    stop_reason = None  # why the loop stopped early (shown on the Run page)
    total_cost = 0.0
    total_seconds = 0.0
    liked_profiles: list[dict] = []
    last_frame0_hash: str | None = None
    duplicate_streak = 0
    dialog_streak = 0
    cap_hit = False

    while profiles_seen < config.MAX_PROFILES_PER_SESSION:
        profiles_seen += 1
        print(f"\n--- Profile {profiles_seen} ---")

        metrics.set_status("capturing", profiles_seen)
        t0 = time.monotonic()
        try:
            frames = capture_profile()
        except RuntimeError as e:
            if "loading screen" in str(e):
                dialog_streak += 1
                print(f"\nAPP STUCK ON LOADING SCREEN (streak {dialog_streak})")
                dialog_ss = save_error_screenshot(f"loading-screen-{dialog_streak}")

                # No relaunching: wait for it to load, give up after 3 tries.
                if dialog_streak >= 3:
                    msg = (f"Loading screen persisted after {dialog_streak} "
                           f"checks.")
                    print(f"GIVING UP: {msg}")
                    break

                print(f"  Waiting for Hinge to finish loading...")
                time.sleep(8)

                profiles_seen -= 1
                last_frame0_hash = None
                duplicate_streak = 0
                continue
            raise
        t_capture = time.monotonic() - t0
        print(f"Captured {len(frames)} frames")

        # If frame 0 is identical to the previous profile's frame 0, Hinge
        # didn't advance after our last action — force-skip rather than
        # burning another ~$0.035 re-judging the same person. Escalate the
        # delay if we keep duplicating, in case Hinge needs a beat to
        # recover from an "out of likes" / popup state.
        #
        # Hash a cropped region of frame 0 (excluding status bar at top and
        # nav bar at bottom). The status-bar clock ticks every minute and
        # the nav-bar can show transient badges; both make raw-bytes md5
        # diverge across iterations even when the *profile* is identical,
        # which silently breaks duplicate detection. Cropping isolates the
        # part of the screen that actually identifies the profile.
        frame0_hash = _profile_region_hash(frames[0])
        if frame0_hash == last_frame0_hash:
            duplicate_streak += 1
            print(f"DUPLICATE: frame 0 matches previous profile "
                  f"(streak {duplicate_streak}). Force-skipping without judge.")
            try:
                do_skip()
            except Exception as e:
                print(f"Skip failed during duplicate recovery: {e!r}")
            time.sleep(min(2 + duplicate_streak * 2, 15))
            continue
        last_frame0_hash = frame0_hash
        duplicate_streak = 0

        # Random profile dwell — simulates actually reading
        dwell = random.uniform(2, 8)
        print(f"  Reading profile for {dwell:.1f}s...")
        time.sleep(dwell)

        metrics.set_status("judging", profiles_seen)
        t1 = time.monotonic()
        decision = None
        fatal_error = None
        for attempt in range(3):
            try:
                decision = judge(frames)
                finalize_comment(decision)
                break
            except Exception as e:
                err = repr(e)
                print(f"Judge attempt {attempt + 1}/3 failed: {e}")
                # Halt on errors that won't recover with a retry — burning
                # through Hinge swipes blind (force-skipping every profile
                # without a real decision) eats the daily quota and looks
                # robotic to Hinge. Saw this once when the Anthropic credit
                # balance hit zero mid-run: 124 profiles got blindly skipped
                # before we noticed.
                if any(s in err for s in (
                    "credit balance is too low",
                    "authentication_error",
                    "invalid_api_key",
                    "permission_error",
                )):
                    fatal_error = err
                    break
                if attempt < 2:
                    time.sleep(5 * (attempt + 1))
        if fatal_error is not None:
            print(f"\nFATAL judge error — halting loop instead of burning "
                  f"Hinge swipes:\n  {fatal_error}")
            if "authentication_error" in str(fatal_error) or "OAuth" in str(fatal_error):
                stop_reason = ("STOPPING: the AI key was rejected or is missing, so profiles can't be judged. "
                               "Check it on the Setup page and start the run again.")
            elif "credit balance" in str(fatal_error):
                stop_reason = "STOPPING: the AI account has no credit left, so profiles can't be judged."
            else:
                stop_reason = f"STOPPING: the judge failed ({str(fatal_error)[:160]})."
            break
        t_judge = time.monotonic() - t1
        if decision is None:
            # Never force-skip a profile that was never judged: that burns a
            # real profile from the queue on an error the user can fix.
            msg = "Judge failed 3 times — stopping instead of skipping blind."
            print(f"\n{msg}")
            save_error_screenshot(f"judge-failed-{profiles_seen}")
            stop_reason = f"STOPPING: {msg}"
            break

        # Selectivity (Run page): only like at or above the minimum score.
        min_score = _min_like_score()
        score = (decision.extra or {}).get("score")
        if decision.decision == "like" and min_score and isinstance(score, (int, float)) and score < min_score:
            print(f"Score {score} is under your minimum of {min_score}: skipping instead of liking")
            decision.decision = "skip"
            decision.skip_reason = f"below minimum score {min_score}"
            decision.message = ""
            if isinstance(decision.extra, dict):
                decision.extra["skip_reason"] = decision.skip_reason

        print(f"Name:     {decision.name}")
        print(f"Decision: {decision.decision} ({decision.confidence}) "
              f"[{decision.skip_reason if decision.decision == 'skip' else decision.message_archetype}]")
        print(f"Reason:   {decision.reasoning}")
        if decision.message:
            print(f"Message:  {decision.message}")

        # ── Dialog / popup detection & recovery ──
        if decision.decision == "NOT_A_PROFILE":
            dialog_streak += 1
            print(f"\nDIALOG DETECTED (streak {dialog_streak}): {decision.reasoning[:120]}")
            dialog_ss = save_error_screenshot(f"dialog-streak-{dialog_streak}")

            # No Back press (on this phone Back from Discover exits Hinge)
            # and no relaunch. Many promos close on their own: wait and look
            # again once, then stop and leave it for the user.
            if dialog_streak == 1:
                print(f"  Waiting 5s, then re-capturing (no taps)...")
                time.sleep(5)
            else:
                msg = (f"Popup still covering the profile after a re-check — "
                       f"stopping. Last reason: {decision.reasoning[:200]}")
                print(f"STOPPING: {msg}")
                break

            profiles_seen -= 1
            last_frame0_hash = None
            duplicate_streak = 0
            continue
        else:
            dialog_streak = 0

        folder_name = save_debug(frames, decision, profiles_seen)

        metrics.set_status("liking" if decision.decision == "like" else "skipping",
                           profiles_seen)
        t2 = time.monotonic()
        if decision.decision == "like":
            try:
                liked_on = do_like(decision.message, top_hash=frame0_hash,
                                   target=(decision.extra or {}).get("target"))
                if isinstance(decision.extra, dict):
                    decision.extra["liked_on"] = liked_on
                # Feedback loop: one send record per comment that went out.
                if decision.message:
                    import outcomes
                    send_id = outcomes.record_send(
                        name=decision.name, comment=decision.message,
                        decision_extra=decision.extra, profile_idx=profiles_seen,
                        folder=folder_name,
                    )
                    print(f"Send logged as #{send_id}")
                liked_profiles.append({
                    "name": decision.name,
                    "message": decision.message,
                    "index": profiles_seen,
                    "folder": folder_name,
                })
                likes_sent += 1
                # Don't break here: the profile still has to be logged, or
                # the last like of every run never reaches the session log. Flag it and stop at the bottom.
                if likes_sent >= session_like_cap:
                    cap_hit = True
            except (HingeNotForeground, StuckState):
                raise
            except Exception as e:
                # Don't skip: the judge wanted this profile, and skipping
                # burns it for good. Stop and let the user look at the screen.
                path = save_error_screenshot(f"do-like-failed-{profiles_seen}")
                msg = (f"Like failed on {decision.name}: {e} — stopping so the "
                       f"profile isn't skipped. Screenshot: {path}")
                print(f"\nSTOPPING: {msg}")
                break
        else:
            do_skip()
            skips += 1
        t_act = time.monotonic() - t2

        timing = {
            "capture_seconds": round(t_capture, 2),
            "judge_seconds":   round(t_judge, 2),
            "act_seconds":     round(t_act, 2),
            "total_seconds":   round(t_capture + t_judge + t_act, 2),
        }
        folder_rel = None
        if folder_name:
            bucket = "liked" if decision.decision == "like" else "skipped"
            folder_rel = f"{bucket}/{folder_name}"
        metrics.log_profile(profiles_seen, decision, timing, folder=folder_rel)
        total_cost += metrics.estimated_cost(decision.usage)
        total_seconds += timing["total_seconds"]
        metrics.print_running_totals(
            profiles_seen, likes_sent, skips, total_cost, total_seconds,
        )

        if cap_hit:
            print(f"Hit max likes cap ({session_like_cap}). Stopping.")
            break

    metrics.set_status("stopped", profiles_seen)
    if stop_reason:
        print(f"\n{stop_reason}")
    elif likes_sent < session_like_cap:
        print(f"\nRan out of profiles before the target: {likes_sent}/"
              f"{session_like_cap} likes after {profiles_seen} profiles.")
    print(f"\nDone. {likes_sent} likes sent across {profiles_seen} profiles.")


    # No cleanup: leave Hinge open and the screen on, so the next run can
    # start from Discover without unlocking the phone or relaunching Hinge
    # (a fresh launch tends to open on a full-screen promo).
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (HingeNotForeground, StuckState) as e:
        print(f"\nSTOPPED: {e}")
        try:
            print(f"Screenshot: {save_error_screenshot('hinge-not-foreground')}")
        finally:
            metrics.set_status("stopped", None, reason="Hinge left the foreground")
        sys.exit(3)
