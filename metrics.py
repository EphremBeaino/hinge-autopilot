"""Per-profile structured logging for the autopilot loop.

Each profile run produces one JSONL line in `debug/session_log.jsonl`
with timing, token usage, decision, and message metadata. Append-only
and machine-parseable so chart-making downstream is trivial.
"""

import json
import os
import threading
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import config


# Pricing per 1M tokens (USD), for the cost shown on the Run page. Unknown models count as $0.
MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-5-5":   {"input_tokens": 4.00, "output_tokens": 20.00, "cache_creation_input_tokens": 5.00, "cache_read_input_tokens": 0.20},
    "claude-sonnet-5-5": {"input_tokens": 2.00, "output_tokens": 10.00, "cache_creation_input_tokens": 2.50, "cache_read_input_tokens": 0.10},
    "claude-haiku-5-5":  {"input_tokens": 0.10, "output_tokens": 0.50},
    "claude-opus-5":     {"input_tokens": 5.00, "output_tokens": 25.00, "cache_creation_input_tokens": 6.25, "cache_read_input_tokens": 0.50},
    "gemini-3.5-flash":  {"input_tokens": 1.50, "output_tokens": 9.00},
    "gemini-3.1-flash-lite": {"input_tokens": 0.25, "output_tokens": 1.50},
}

_ACTIVE_MODEL = config.MODEL
_PRICING = MODEL_PRICING.get(_ACTIVE_MODEL, {"input_tokens": 0.0, "output_tokens": 0.0})


def estimated_cost(usage: dict[str, int]) -> float:
    """Dollar estimate from a usage dict returned by judge()."""
    if not usage:
        return 0.0
    # claude_code backend: the CLI reports its own (API-equivalent) cost.
    if usage.get("cli_cost_usd"):
        return float(usage["cli_cost_usd"])
    return sum(
        usage.get(k, 0) * price / 1_000_000
        for k, price in _PRICING.items()
    )


_status: dict[str, Any] = {}
_status_lock = threading.Lock()


def _write_status() -> None:
    path = config.DEBUG_DIR / "live_status.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**_status, "heartbeat": datetime.now().isoformat(timespec="seconds")}
    tmp = path.with_suffix(".tmp")
    # Windows refuses the swap while a reader (the web UI) has the file open: retry briefly, and a
    # missed status update never stops a run
    for attempt in range(6):
        try:
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(path)
            return
        except OSError:
            time.sleep(0.05 * (attempt + 1))


def set_status(state: str, profile_idx: int | None = None, **extra: Any) -> None:
    """Overwrite debug/live_status.json so the dashboard can show what the
    loop is doing right now (capturing / judging / acting / waiting)."""
    with _status_lock:
        _status.clear()
        _status.update(
            state=state,
            profile_idx=profile_idx,
            timestamp=datetime.now().isoformat(timespec="seconds"),
            pid=os.getpid(),
            **extra,
        )
        _write_status()


def start_heartbeat(interval_s: float = 5.0) -> None:
    """Refresh the status file's `heartbeat` every few seconds from a daemon
    thread. When the bot process dies the heartbeat stops, and the
    dashboard shows it as offline."""
    def _beat() -> None:
        while True:
            time.sleep(interval_s)
            with _status_lock:
                if _status:
                    try:
                        _write_status()
                    except OSError:
                        pass

    threading.Thread(target=_beat, name="status-heartbeat", daemon=True).start()


def log_profile(
    profile_idx: int,
    decision,
    timing: dict[str, float],
    log_path: Path | None = None,
    folder: str | None = None,
    dry_run: bool = False,
) -> None:
    """Append a JSONL record for one profile.

    `folder` is the debug frames folder relative to DEBUG_DIR (e.g.
    "liked/20260921-230102_01_lauren"), used by the dashboard."""
    if log_path is None:
        log_path = config.DEBUG_DIR / "session_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    record = {
        "profile_idx": profile_idx,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "dry_run": dry_run,
        "folder": folder,
        "mode": "profile",
        "model": (decision.usage or {}).get("model") or _ACTIVE_MODEL,
        "name": decision.name,
        "decision": decision.decision,
        "confidence": decision.confidence,
        "reasoning": decision.reasoning,
        "message": decision.message,
        "message_length": len(decision.message),
        "message_archetype": decision.message_archetype,
        "premade_id": decision.premade_id,
        "prompt_referenced": decision.prompt_referenced,
        "skip_reason": decision.skip_reason,
        "skill": getattr(decision, "extra", {}) or None,
        "timing": timing,
        "tokens": decision.usage,
        "estimated_cost_usd": round(estimated_cost(decision.usage), 5),
    }

    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def print_running_totals(
    profiles_seen: int,
    likes_sent: int,
    skips: int,
    total_cost: float,
    total_seconds: float,
) -> None:
    """One-line summary printed every loop iteration."""
    avg_cost = total_cost / profiles_seen if profiles_seen else 0
    avg_time = total_seconds / profiles_seen if profiles_seen else 0
    like_rate = likes_sent / profiles_seen if profiles_seen else 0
    model_tag = _ACTIVE_MODEL or "?"
    print(
        f"[totals] {profiles_seen} profiles | {likes_sent} likes "
        f"({like_rate:.0%}) | {skips} skips | "
        f"${total_cost:.3f} (~${avg_cost:.4f}/profile) | "
        f"avg {avg_time:.1f}s/profile | {model_tag}"
    )
