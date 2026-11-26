"""Hinge Autopilot: the local web app. Run it and open http://127.0.0.1:8765

    python app.py [--port 8765] [--no-browser]

Everything stays on this computer: the app listens on 127.0.0.1 only, the data lives in data/
and the only outside calls are to the AI provider you pick (and Google, once, to download adb).
"""

import argparse
import io
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

import adb
import chats
import config
import insights
import outcomes
import suggest
import taste

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

DATA = config.DATA_DIR
WEB = config.BASE_DIR / "web"
LOG_PATH = DATA / "session_log.jsonl"
STATUS_PATH = DATA / "live_status.json"
RUN_STATE_PATH = DATA / "run.json"
RUN_LOG = DATA / "last_run.log"
SCHEDULE_PATH = DATA / "schedule.json"
SUGGEST_LOG = DATA / "suggestion_log.jsonl"
HINGE = "co.hinge.app"
MAX_LIKES = 50

settings = config.load_settings()


def save_settings() -> None:
    config.save_settings(settings)


# ================================================================ setup: adb

class AdbInstaller:
    URLS = {"Windows": "windows", "Darwin": "darwin", "Linux": "linux"}

    def __init__(self):
        self.state = {"state": "idle"}

    def start(self) -> dict:
        if self.state["state"] == "downloading":
            return self.state
        self.state = {"state": "downloading", "progress": 0}
        threading.Thread(target=self._work, daemon=True).start()
        return self.state

    def _work(self):
        try:
            osname = self.URLS.get(platform.system(), "linux")
            url = f"https://dl.google.com/android/repository/platform-tools-latest-{osname}.zip"
            with urlopen(url, timeout=60) as r:
                total = int(r.headers.get("Content-Length") or 0)
                buf = io.BytesIO()
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    buf.write(chunk)
                    if total:
                        self.state["progress"] = round(buf.tell() * 100 / total)
            config.TOOLS_DIR.mkdir(exist_ok=True)
            with zipfile.ZipFile(buf) as z:
                z.extractall(config.TOOLS_DIR)
            if os.name != "nt":
                for f in ("adb", "fastboot"):
                    p = config.TOOLS_DIR / "platform-tools" / f
                    if p.exists():
                        p.chmod(0o755)
            self.state = {"state": "done"}
        except Exception as e:
            self.state = {"state": "failed", "error": str(e)}


adb_installer = AdbInstaller()


def adb_info() -> dict:
    path = config.adb_path()
    try:
        out = subprocess.run([path, "version"], capture_output=True, text=True, timeout=15).stdout
        version = out.splitlines()[0] if out else ""
        return {"found": True, "path": path, "version": version}
    except (OSError, subprocess.TimeoutExpired):
        return {"found": False, "path": path}


def phone_info() -> dict:
    """What's plugged in. Also records the screen size the bot scales its taps to."""
    if not adb_info()["found"]:
        return {"state": "no_adb", "devices": []}
    try:
        devs = adb.devices()
    except Exception as e:
        return {"state": "error", "error": str(e), "devices": []}
    ready = [d for d in devs if d["state"] == "device"]
    if not ready:
        if any(d["state"] == "unauthorized" for d in devs):
            return {"state": "unauthorized", "devices": devs}
        return {"state": "none", "devices": devs}
    d = ready[0]
    info = {"state": "connected", "devices": devs, "serial": d["serial"], "model": d["model"]}
    os.environ.setdefault("ANDROID_SERIAL", d["serial"])
    try:
        w, h = adb.screen_size()
        info["screen"] = [w, h]
        if settings.get("screen") != [w, h]:
            settings["screen"] = [w, h]
            save_settings()
            config.apply_screen(w, h)
        info["hinge"] = adb.has_package(HINGE)
    except Exception as e:
        info["error"] = str(e)
    return info


def setup_status() -> dict:
    profile = config.load_profile()
    keys = settings.get("api_keys") or {}
    provider = settings.get("provider")
    return {
        "adb": adb_info(), "adbInstall": adb_installer.state, "phone": phone_info(),
        "ai": {"provider": provider, "model": settings.get("model") or config.DEFAULT_MODELS.get(provider),
               "hasKey": provider == "claude_code" or bool(keys.get(provider)),
               "keyHint": ("..." + keys[provider][-4:]) if keys.get(provider) else None,
               "tested": settings.get("aiTestedAt")},
        "profile": {"done": bool(profile.get("name") and profile.get("tone")), "name": profile.get("name")},
        "defaults": config.DEFAULT_MODELS,
    }


def set_ai(body: dict) -> dict:
    import llm
    provider = body.get("provider")
    if provider not in llm.PROVIDERS:
        raise ValueError("Pick a provider")
    key = (body.get("key") or "").strip() or (settings.get("api_keys") or {}).get(provider, "")
    model = (body.get("model") or "").strip() or config.DEFAULT_MODELS[provider]
    if provider != "claude_code" and not key:
        raise ValueError("Paste your API key")
    try:
        answer = llm.test_key(provider, key, model)
    except Exception as e:
        raise ValueError(f"The key didn't work: {str(e)[:300]}")
    if provider != "claude_code":
        settings.setdefault("api_keys", {})[provider] = key
    settings.update(provider=provider, model=model, aiTestedAt=datetime.now().astimezone().isoformat())
    save_settings()
    return {"ok": True, "answer": answer[:80]}


PROFILE_FIELDS = {"name", "age", "city", "job", "about", "facts", "never_mention", "interested_in", "looking_for",
                  "age_min", "age_max", "looks", "green_flags", "dealbreakers", "tone", "emoji", "languages",
                  "sample_messages", "max_words"}


def set_profile(body: dict) -> dict:
    p = {**config.load_profile(), **{k: v for k, v in body.items() if k in PROFILE_FIELDS}}
    for k in ("facts", "never_mention", "green_flags", "dealbreakers", "languages", "sample_messages"):
        if isinstance(p.get(k), str):
            p[k] = [x.strip(" -*\t") for x in p[k].splitlines() if x.strip(" -*\t")]
    for k in ("age", "age_min", "age_max", "max_words"):
        try:
            p[k] = int(p[k]) if p.get(k) not in (None, "") else None
        except (TypeError, ValueError):
            p[k] = None
    config.PROFILE_PATH.write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


# ================================================================ images

def _jpeg(src, width: int, quality: int = 72, privacy: bool = False, header: bool = False) -> bytes:
    from PIL import Image
    with Image.open(io.BytesIO(src) if isinstance(src, bytes) else src) as im:
        im = im.convert("RGB")
        if privacy:
            im = blur_photos(im, header)
        if im.width > width:
            im = im.resize((width, max(1, round(im.height * width / im.width))), Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, "JPEG", quality=quality, optimize=True)
    return out.getvalue()


def blur_photos(im, header: bool = False):
    """Privacy mode: blur the photo areas of a Hinge screenshot and keep the text (prompts, comments) readable.
    A row belongs to a photo when most of its pixels differ from the page background."""
    import numpy as np
    from PIL import ImageFilter
    a = np.asarray(im).astype(np.int16)
    h, w, _ = a.shape
    bg = np.median(a[:, max(2, w // 60)], axis=0)  # the left margin column is page background
    x0, x1 = int(w * 0.08), int(w * 0.92)
    diff = np.abs(a[:, x0:x1] - bg).max(axis=2) > 28
    photo_rows = diff.mean(axis=1) > 0.55
    runs, start = [], None
    for y, v in enumerate(list(photo_rows) + [False]):
        if v and start is None:
            start = y
        elif not v and start is not None:
            if y - start > h * 0.04:
                runs.append((start, y))
            start = None
    out = im.copy()
    for y0, y1 in runs:
        y0, y1 = max(0, y0 - 6), min(h, y1 + 6)
        box = (0, y0, w, y1)
        out.paste(im.crop(box).filter(ImageFilter.GaussianBlur(radius=max(18, w // 30))), box)
    soft = ImageFilter.GaussianBlur(radius=max(14, w // 45))
    if header:
        # the first screen of a profile: the name sits between the filter chips and the first big photo
        # (a thin promo banner above the name can look like a photo, so only count runs below 20%)
        first = next((y0 for y0, _ in runs if h * 0.2 < y0 < h * 0.45), int(h * 0.3))
        box = (0, int(h * 0.075), w, first)
    else:
        # later screens: Hinge pins a bar with the name under the status bar
        box = (0, int(h * 0.035), w, int(h * 0.1))
    out.paste(out.crop(box).filter(soft), box)
    return out


def _frames(folder: str) -> list[Path]:
    if not re.match(r"^[a-z]+/[^/\\]{1,150}$", folder or ""):
        return []
    imgs = Path(os.path.normpath(DATA / folder / "imgs"))  # not resolve(): liked/ may be a link to another disk
    if DATA not in imgs.parents or not imgs.is_dir():
        return []
    return sorted(imgs.glob("*.png"))


# ================================================================ session log + profiles

def _log_lines() -> list[str]:
    if not LOG_PATH.exists():
        return []
    text = LOG_PATH.read_text(encoding="utf-8-sig")
    return text.split("\n")[:text.count("\n")]


def profiles() -> dict:
    sends = insights._sends_by_folder()
    taste_r = taste.ratings()
    out = []
    for idx, line in enumerate(_log_lines()):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        folder = r.get("folder") or ""
        sk = r.get("skill") or {}
        o = insights.outcome_of(sends.get(folder.split("/")[-1])) if r.get("decision") == "like" else None
        out.append({"seq": idx + 1, "at": r.get("timestamp"), "folder": folder, "name": r.get("name"),
                    "decision": r.get("decision"), "dryRun": r.get("dry_run"), "message": r.get("message"),
                    "verdict": sk.get("verdict"), "score": sk.get("score"), "looks": sk.get("attractiveness"),
                    "age": sk.get("her_age"), "rationale": sk.get("rationale") or r.get("reasoning"),
                    "hook": sk.get("hook"), "machine": sk.get("machine"), "variation": sk.get("variation"),
                    "target": sk.get("target") or r.get("prompt_referenced"), "candidates": sk.get("candidates"),
                    "skeleton": sk.get("skeleton"), "flags": sk.get("flags"), "skipReason": r.get("skip_reason"),
                    "likedOn": sk.get("liked_on"), "model": r.get("model"), "cost": r.get("estimated_cost_usd"),
                    "frames": len(_frames(folder)) if folder else 0,
                    "outcome": o, "status": insights.status_of(o) if r.get("decision") == "like" else None,
                    "rating": (taste_r.get(folder) or {}).get("rating")})
    return {"records": out}


def report_outcome(folder: str, text: str) -> dict:
    text = (text or "").strip()
    if not text:
        raise ValueError("Nothing to report")
    base = folder.split("/")[-1]
    send = next((s for s in outcomes._load()["sends"].values() if (s.get("folder") or "").split("/")[-1] == base), None)
    events = outcomes.parse_reply(text)
    if send and any(e[0] != "note" for e in events):
        return {"ack": outcomes.apply_events(send["send_id"], events)}
    rec = next((json.loads(l) for l in reversed(_log_lines()) if l.strip() and f'"{folder}"' in l), None)
    sk = (rec or {}).get("skill") or {}
    outcomes.add_feedback({"name": (rec or {}).get("name"), "decision": (rec or {}).get("decision"),
                           "verdict": sk.get("verdict"), "score": sk.get("score"), "comment": (rec or {}).get("message"),
                           "send_id": (send or {}).get("send_id"), "timestamp": (rec or {}).get("timestamp")}, text)
    return {"ack": "Noted. The judge reads your recent notes as calibration."}


# ================================================================ runs

def _live() -> dict | None:
    try:
        st = json.loads(STATUS_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    beat = st.pop("heartbeat", None)
    try:
        st["alive"] = (datetime.now() - datetime.fromisoformat(beat)).total_seconds() < 15 and st.get("state") != "stopped"
    except (TypeError, ValueError):
        st["alive"] = False
    return st


def run_log_tail(n: int = 4) -> str:
    try:
        return "\n".join(RUN_LOG.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-n:])
    except OSError:
        return ""


def run_outcome_note() -> str:
    try:
        lines = RUN_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    marks = ("Ran out of profiles", "GIVING UP:", "STOPPING:", "STOPPED:", "Hit max likes cap", "Open Hinge")
    hits = [l.strip() for l in lines if any(m in l for m in marks)]
    done = [l.strip() for l in lines if l.startswith("Done.")]
    return "\n".join(x for x in (done[-1] if done else "", hits[-1] if hits else "") if x)


class Runner:
    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()
        try:
            self.state = json.loads(RUN_STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.state = None
        if self.state and self.state.get("state") == "running":
            self._finish(None, "The app restarted while the run was going")

    def _save(self):
        RUN_STATE_PATH.write_text(json.dumps(self.state, indent=1), encoding="utf-8")

    def _counts(self) -> dict:
        recs = []
        for line in _log_lines()[(self.state or {}).get("logStart", 0):]:
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        sent = [r for r in recs if r.get("decision") == "like" and not r.get("dry_run")]
        return {"profiles": len(recs), "likes": len(sent), "cost": round(sum(r.get("estimated_cost_usd") or 0 for r in recs), 3)}

    def busy_elsewhere(self) -> bool:
        live = _live()
        return bool(live and live.get("alive")) and self.proc is None

    def start(self, likes: int) -> dict:
        with self.lock:
            if self.proc is not None:
                raise ValueError("A run is already going.")
            if chat_reader.busy():
                raise ValueError("The phone is busy reading chats. Try again in a minute.")
            if not config.load_profile().get("name"):
                raise ValueError("Finish the Setup questions first.")
            adb.check_device()
            phone_info()
            wake_hinge()
            waiting = chats.your_turn_count()
            if waiting >= 8:
                raise ValueError(f"{waiting} chats are waiting for your reply and Hinge stops new likes at 8. "
                                 f"Reply to a few, then run again.")
            cmd = [sys.executable, "-u", "main.py", "--likes", str(likes),
                   "--max-profiles", str(min(150, max(10, likes * 6)))]
            log = RUN_LOG.open("w", encoding="utf-8", errors="replace")
            env = {**os.environ, "PYTHONIOENCODING": "utf-8", "HA_DATA_DIR": str(DATA)}
            self.proc = subprocess.Popen(cmd, cwd=str(config.BASE_DIR), env=env, stdout=log, stderr=subprocess.STDOUT)
            self.state = {"id": uuid.uuid4().hex[:10], "state": "running", "likesWanted": likes, "pid": self.proc.pid,
                          "startedAt": datetime.now().astimezone().isoformat(), "logStart": len(_log_lines()),
                          "dryRun": bool(settings.get("dryRun"))}
            self._save()
            threading.Thread(target=self._wait, args=(self.proc,), daemon=True).start()
            return self.state

    def _wait(self, proc):
        code = proc.wait()
        with self.lock:
            if self.proc is proc:
                self.proc = None
                if self.state.get("state") == "running":
                    self._finish(code)

    def _finish(self, code, note=None):
        stopped = self.state.get("stopRequested")
        self.state.update(state="stopped" if stopped else "finished" if code == 0 else "failed", exitCode=code,
                          finishedAt=datetime.now().astimezone().isoformat(),
                          note=note or (run_outcome_note() if code == 0 else run_log_tail(4)), **self._counts())
        self._save()
        if self.state["state"] == "finished" and settings.get("readAfterRun"):
            threading.Timer(5, lambda: chat_reader.start_quietly("after a run")).start()

    def stop(self) -> dict:
        with self.lock:
            if self.proc is None:
                raise ValueError("Nothing is running.")
            proc = self.proc
            self.state["stopRequested"] = True
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        return {"stopped": True}

    def snapshot(self):
        run = dict(self.state) if self.state else None
        if run and run["state"] == "running":
            run.update(self._counts())
        return run


def wake_hinge() -> None:
    adb.wake_screen()
    if not adb.is_foreground(HINGE):
        adb.launch_app(HINGE)
        time.sleep(8)
    if not adb.is_foreground(HINGE):
        raise ValueError("Couldn't bring Hinge to the front. Is the phone unlocked?")


class ChatReader:
    STUCK_MIN = 30

    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"state": "idle"}
        try:
            d = json.loads(chats.CHATS_PATH.read_text(encoding="utf-8"))
            self.state.update(finishedAt=d.get("finishedAt"), count=len(d.get("chats") or []))
        except (OSError, json.JSONDecodeError):
            pass

    def busy(self) -> bool:
        if self.state["state"] != "reading":
            return False
        try:
            age = (datetime.now().astimezone() - datetime.fromisoformat(self.state["startedAt"])).total_seconds() / 60
        except (KeyError, TypeError, ValueError):
            return True
        if age > self.STUCK_MIN:
            self.state = {**self.state, "state": "failed", "error": f"Stuck for {age:.0f} minutes"}
            return False
        return True

    def start(self, limit=None, extended=False) -> dict:
        with self.lock:
            if self.busy():
                raise ValueError("Already reading the chats.")
            if runner.proc is not None or runner.busy_elsewhere():
                raise ValueError("A run is using the phone. Read the chats when it ends.")
            adb.check_device()
            phone_info()
            self.state = {**self.state, "state": "reading", "startedAt": datetime.now().astimezone().isoformat(),
                          "progress": "Opening Matches", "error": None, "extended": extended}
        threading.Thread(target=self._read, args=(limit, extended), daemon=True).start()
        return self.state

    def _read(self, limit, extended):
        def progress(line):
            print(line, flush=True)
            self.state["progress"] = line.replace("[chats] ", "")
        try:
            wake_hinge()
            data = chats.sync(limit, progress=progress, extended=extended)
            self.state = {"state": "idle", "finishedAt": data["finishedAt"], "count": len(data["chats"])}
            if extended:
                outcomes.resolve_stale()
            if settings.get("autoSuggest"):
                suggester.queue_due(data["chats"])
        except Exception as e:
            print(f"[chats] failed: {e!r}", flush=True)
            self.state = {**self.state, "state": "failed", "error": str(e), "progress": None}

    def start_quietly(self, why):
        try:
            self.start()
            print(f"[chats] reading {why}", flush=True)
        except Exception as e:
            print(f"[chats] not reading {why}: {e}", flush=True)


class Suggester:
    def __init__(self):
        self.lock = threading.Lock()
        self.queue: list[tuple[str, str | None]] = []
        self.current = None
        self.last_error = None
        threading.Thread(target=self._work, daemon=True).start()

    def state(self):
        return {"current": self.current, "queue": [n for n, _ in self.queue], "lastError": self.last_error}

    def add(self, key, note=None):
        with self.lock:
            self.queue = [(n, x) for n, x in self.queue if n != key] + [(key, note)]
        return self.state()

    def queue_due(self, chat_list):
        have = suggest.all_suggestions()
        for c in chat_list:
            if c.get("error") or not c.get("messages") or c.get("section") == "Hidden":
                continue
            key = c.get("key") or chats.chat_key(c)
            if (have.get(key) or {}).get("forLast") != chats.last_key(c):
                self.add(key)

    def _work(self):
        while True:
            with self.lock:
                item = self.queue.pop(0) if self.queue else None
            if not item:
                time.sleep(2)
                continue
            key, note = item
            self.current = key
            try:
                data = json.loads(chats.CHATS_PATH.read_text(encoding="utf-8"))
                chat = chats.find_chat(data["chats"], key)
                if chat is None:
                    raise ValueError(f"{key} isn't in the last chat read")
                r = suggest.suggest(chat, note)
                with SUGGEST_LOG.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
                self.last_error = None
            except Exception as e:
                print(f"[suggest] {key} failed: {e!r}", flush=True)
                self.last_error = {"name": key, "error": str(e)[:300]}
            finally:
                self.current = None


class TasteBuilder:
    def __init__(self):
        self.state = {"state": "idle"}

    def info(self):
        t = taste.taste()
        return {**self.state, "ratings": len(taste.ratings()), "builtAt": t.get("at"), "text": t.get("taste_text"),
                "disagreements": t.get("disagreements"), "every": taste.BUILD_EVERY}

    def start(self):
        if self.state["state"] != "building":
            self.state = {"state": "building"}
            threading.Thread(target=self._work, daemon=True).start()
        return self.info()

    def _work(self):
        try:
            taste.build(lambda l: print(f"[taste] {l}", flush=True))
            self.state = {"state": "idle"}
        except Exception as e:
            self.state = {"state": "failed", "error": str(e)[:300]}


class DailyRun:
    """runSessions runs a day starting at runAt (+ i*runGapMin), each shifted by a random 0-runRandomMin
    minutes so no start time repeats."""
    GRACE_H = 3

    def __init__(self):
        try:
            self.plan = json.loads(SCHEDULE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.plan = {}

    def _save(self):
        SCHEDULE_PATH.write_text(json.dumps(self.plan, indent=1), encoding="utf-8")

    def _pick(self, day, i):
        import random
        h, m = map(int, settings["runAt"].split(":"))
        at = datetime.combine(day, datetime.min.time()).replace(hour=h, minute=m).astimezone()
        at += timedelta(minutes=i * int(settings.get("runGapMin") or 120) + random.randint(0, int(settings.get("runRandomMin") or 0)),
                        seconds=random.randint(0, 59))
        return at.isoformat()

    def _new(self, day, keep=None):
        keep = keep or []
        n = int(settings.get("runSessions") or 1)
        return {"date": day.isoformat(), "slots": [keep[i] if i < len(keep) and keep[i].get("state") in ("started", "skipped")
                                                   else {"at": self._pick(day, i), "state": "planned"} for i in range(n)]}

    def today(self):
        day = datetime.now().astimezone().date()
        if self.plan.get("date") != day.isoformat() or not self.plan.get("slots"):
            self.plan = self._new(day)
            self._save()
        return self.plan

    def replan(self):
        day = datetime.now().astimezone().date()
        self.plan = self._new(day, self.plan.get("slots") if self.plan.get("date") == day.isoformat() else None)
        self._save()

    def tick(self):
        if not settings.get("autoRun"):
            return
        now = datetime.now().astimezone()
        for slot in self.today()["slots"]:
            if slot["state"] != "planned":
                continue
            at = datetime.fromisoformat(slot["at"])
            if now < at:
                return
            if now > at + timedelta(hours=self.GRACE_H):
                slot.update(state="skipped", note=slot.get("lastError") or "its time had passed")
                self._save()
                continue
            if runner.proc is not None or chat_reader.busy():
                slot["lastError"] = "the phone was busy; trying again every minute"
                self._save()
                return
            try:
                st = runner.start(int(settings.get("runLikes") or 8))
                slot.update(state="started", startedAt=now.isoformat(), runId=st.get("id"), lastError=None)
            except Exception as e:
                slot["lastError"] = str(e)[:240]
                if "waiting for your reply" in str(e):
                    slot.update(state="skipped", note=str(e)[:240])
            self._save()
            return

    def state(self):
        plan = self.today() if settings.get("autoRun") else self.plan
        slots = plan.get("slots") or []
        nxt = next((x for x in slots if x.get("state") == "planned"), None)
        return {"enabled": bool(settings.get("autoRun")), "slots": slots, "next": nxt.get("at") if nxt else None}


runner = Runner()
chat_reader = ChatReader()
suggester = Suggester()
taste_builder = TasteBuilder()
schedule = DailyRun()


def _scheduler():
    while True:
        time.sleep(60)
        try:
            schedule.tick()
        except Exception as e:
            print(f"[schedule] {e!r}", flush=True)
        hours = settings.get("readEveryHours") or 0
        if not hours or chat_reader.busy() or runner.proc is not None:
            continue
        last = chat_reader.state.get("finishedAt") or chat_reader.state.get("startedAt")
        try:
            age = (datetime.now().astimezone() - datetime.fromisoformat(last)).total_seconds() if last else 1e9
        except ValueError:
            age = 1e9
        if age >= hours * 3600:
            chat_reader.start_quietly(f"on the {hours} h timer")


SETTING_RULES = {
    "readAfterRun": bool, "autoSuggest": bool, "autoRun": bool, "dryRun": bool, "privacy": bool, "useLessons": bool,
    "readEveryHours": (0, 48), "runRandomMin": (0, 240), "runSessions": (1, 3), "runGapMin": (45, 300),
    "minLikeScore": (1, 15), "runLikes": (1, MAX_LIKES), "runAt": "time", "effort": ("low", "medium", "high"),
}


def set_settings(body: dict) -> dict:
    for k, v in body.items():
        rule = SETTING_RULES.get(k)
        if rule is None:
            raise ValueError(f"Unknown setting {k}")
        if rule is bool and not isinstance(v, bool):
            raise ValueError(f"{k} must be true or false")
        if isinstance(rule, tuple) and isinstance(rule[0], int) and (not isinstance(v, int) or not rule[0] <= v <= rule[1]):
            raise ValueError(f"{k} must be {rule[0]}-{rule[1]}")
        if isinstance(rule, tuple) and isinstance(rule[0], str) and v not in rule:
            raise ValueError(f"{k} must be one of {', '.join(rule)}")
        if rule == "time" and not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", str(v)):
            raise ValueError("runAt must be HH:MM")
        settings[k] = v
    save_settings()
    if {"runAt", "runRandomMin", "autoRun", "runSessions", "runGapMin"} & set(body):
        schedule.replan()
    return public_settings()


def public_settings() -> dict:
    return {k: v for k, v in settings.items() if k != "api_keys"}


def chats_with_suggestions() -> dict:
    try:
        data = json.loads(chats.CHATS_PATH.read_text(encoding="utf-8"))
    except OSError:
        return {"chats": [], "finishedAt": None}
    have = suggest.all_suggestions()
    sends = outcomes._load()["sends"]
    for c in data["chats"]:
        c.setdefault("key", chats.chat_key(c))
        s = have.get(c["key"]) or have.get(c["name"])
        if s:
            c["suggestion"] = {**s, "stale": s.get("forLast") != chats.last_key(c)}
        send = sends.get(c.get("send_id") or "") or chats.find_send(c, sends)
        if send:
            c["opener"] = {"comment": send.get("comment_text"), "target": send.get("target_text"),
                           "hook": send.get("hook_test"), "machine": send.get("machine"), "folder": send.get("folder")}
    return data


def state() -> dict:
    return {"run": runner.snapshot(), "live": _live(), "chats": chat_reader.state, "suggest": suggester.state(),
            "schedule": schedule.state(), "taste": taste_builder.info(), "settings": public_settings(),
            "profileDone": bool(config.load_profile().get("name")), "logLines": len(_log_lines())}


# ================================================================ HTTP

TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _send(self, code, body: bytes, ctype: str, cache: bool = False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"), "application/json; charset=utf-8")

    def _handle(self, fn):
        try:
            self._json(200, fn())
        except ValueError as e:
            self._json(400, {"error": str(e)})
        except Exception as e:
            print(f"[app] {self.path}: {e!r}", flush=True)
            self._json(500, {"error": f"{type(e).__name__}: {e}"})

    def do_GET(self):
        u = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        num = lambda k, d, hi, lo=0: max(lo, min(hi, int(qs.get(k) or d)))
        p = u.path
        if p == "/api/state":
            return self._handle(state)
        if p == "/api/setup":
            return self._handle(setup_status)
        if p == "/api/profile":
            return self._handle(config.load_profile)
        if p == "/api/profiles":
            return self._handle(profiles)
        if p == "/api/chats":
            return self._handle(chats_with_suggestions)
        if p == "/api/insights":
            return self._handle(insights.insights)
        if p == "/api/runlog":
            def runlog():
                try:
                    lines = RUN_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    lines = []
                return {"lines": lines[-num("lines", 200, 2000):]}
            return self._handle(runlog)
        if p == "/api/screen":
            try:
                img = _jpeg(adb.screenshot(), num("w", 480, 1080, 160), 70, privacy=bool(settings.get("privacy")), header=True)
                return self._send(200, img, "image/jpeg")
            except Exception as e:
                return self._json(503, {"error": str(e)})
        if p == "/api/frame":
            frames = _frames(qs.get("folder", ""))
            i = num("i", 0, 100)
            if i >= len(frames):
                return self._json(404, {"error": "no such frame"})
            img = _jpeg(frames[i], num("w", 540, 1080, 120), 74, privacy=bool(settings.get("privacy")), header=(i == 0))
            return self._send(200, img, "image/jpeg", cache=False)
        # static
        rel = "index.html" if p in ("/", "") else p.lstrip("/")
        f = (WEB / rel).resolve()
        if WEB.resolve() in f.parents and f.is_file():
            return self._send(200, f.read_bytes(), TYPES.get(f.suffix, "application/octet-stream"))
        self._json(404, {"error": "not found"})

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "bad JSON"})
        p = urlparse(self.path).path
        routes = {
            "/api/setup/adb": adb_installer.start,
            "/api/setup/ai": lambda: set_ai(body),
            "/api/profile": lambda: set_profile(body),
            "/api/settings": lambda: set_settings(body),
            "/api/run": lambda: runner.start(_likes(body.get("likes"))),
            "/api/stop": runner.stop,
            "/api/chats/sync": lambda: chat_reader.start(body.get("limit"), bool(body.get("extended"))),
            "/api/suggest": lambda: suggester.add(str(body.get("key") or "").strip() or _bad("key is required"),
                                                  str(body.get("note") or "").strip()[:1000] or None),
            "/api/taste/rate": lambda: (_rate(body), taste_builder.info())[1],
            "/api/taste/build": taste_builder.start,
            "/api/outcome": lambda: report_outcome(str(body.get("folder", "")), str(body.get("text", ""))),
        }
        fn = routes.get(p)
        if fn is None:
            return self._json(404, {"error": "not found"})
        self._handle(fn)


def _bad(msg):
    raise ValueError(msg)


def _likes(v):
    if not isinstance(v, int) or not 1 <= v <= MAX_LIKES:
        raise ValueError(f"Pick 1-{MAX_LIKES} likes")
    return v


def _rate(body):
    folder, rating = str(body.get("folder") or ""), str(body.get("rating") or "")
    if not re.match(r"^[a-z]+/[^/\\]{1,150}$", folder):
        raise ValueError("bad folder")
    r = taste.rate(folder, rating)
    if r.get("due"):
        taste_builder.start()


def main():
    ap = argparse.ArgumentParser(description="Hinge Autopilot")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    threading.Thread(target=_scheduler, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"Hinge Autopilot is running: {url}  (data in {DATA})", flush=True)
    if not args.no_browser:
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
