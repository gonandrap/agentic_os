"""The researcher's quota forecast for a decision-order experiment: parse, compute, render (docs/superpowers/specs/2026-11-10-decision-order-experiments.md §5)"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

MAX_RUNS = 10000
MAX_NOTES = 1000
#: Points of the 5-hour window kept free for everything else the fleet runs
HEADROOM_POINTS = 10.0
UNAVAILABLE = "unavailable"
#: Null when the meter is unavailable
MEASURED_FIELDS = ("points_per_run", "window_share_pct", "window_used_pct", "resets_at",
                   "max_safe_runs_now", "fits_now", "dollars_per_point")


class ForecastError(ValueError):
    """A forecast that cannot be accepted; `problems` lists every reason at once"""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def _count(raw: Any) -> bool:
    return isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0


def parse_forecast(doc: Any) -> dict[str, Any]:
    """The researcher's input, normalised; raises `ForecastError` listing every problem"""
    if not isinstance(doc, Mapping):
        raise ForecastError(["the forecast must be a JSON object"])
    problems: list[str] = []
    runs = doc.get("runs")
    if not (isinstance(runs, int) and not isinstance(runs, bool) and 1 <= runs <= MAX_RUNS):
        problems.append(f"`runs` must be a whole number from 1 to {MAX_RUNS}")
    model = doc.get("model")
    if not isinstance(model, str) or not model.strip():
        problems.append("`model` must be a non-empty model id")
    per_run = doc.get("per_run")
    if not isinstance(per_run, Mapping):
        problems.append("`per_run` must be an object with input_tokens, output_tokens "
                        "and minutes")
        per_run = {}
    else:
        for name in ("input_tokens", "output_tokens"):
            if not _count(per_run.get(name)):
                problems.append(f"`per_run.{name}` must be a whole number >= 0")
        minutes = per_run.get("minutes")
        if (isinstance(minutes, bool) or not isinstance(minutes, (int, float))
                or not math.isfinite(minutes) or minutes < 0):
            problems.append("`per_run.minutes` must be a number >= 0")
    notes = doc.get("notes", "")
    if not isinstance(notes, str):
        problems.append("`notes` must be a string")
    elif len(notes) > MAX_NOTES:
        problems.append(f"`notes` is {len(notes)} characters; at most {MAX_NOTES}")
    if problems:
        raise ForecastError(problems)
    return {"runs": runs, "model": model.strip(),
            "per_run": {"input_tokens": per_run["input_tokens"],
                        "output_tokens": per_run["output_tokens"],
                        "minutes": float(per_run["minutes"])},
            "notes": notes.strip()}


def run_usd(forecast: Mapping[str, Any]) -> float:
    """One run at the per-model list pricing `jarvis cost` uses"""
    from . import usage

    per = forecast["per_run"]
    return usage.priced(forecast["model"], input=per["input_tokens"],
                        output=per["output_tokens"]).list_cost_usd


def compute(forecast: Mapping[str, Any],
            meter: Mapping[str, Any] | None) -> dict[str, Any]:
    """`forecast` plus what the OS measures"""
    per = forecast["per_run"]
    runs = int(forecast["runs"])
    one = run_usd(forecast)
    out: dict[str, Any] = {
        **forecast,
        "total_tokens": runs * (per["input_tokens"] + per["output_tokens"]),
        "minutes_total": round(runs * per["minutes"], 2),
        "est_usd": round(runs * one, 4),
        **dict.fromkeys(MEASURED_FIELDS),
    }
    rate = float((meter or {}).get("dollars_per_point") or 0.0)
    if meter is None or meter.get("used_pct") is None or rate <= 0:
        return {**out, "meter": UNAVAILABLE}
    used = float(meter["used_pct"])
    points = one / rate
    free = 100.0 - used - HEADROOM_POINTS
    safe = max(0, math.floor(free / points)) if points > 0 else None
    out.update(meter="available", dollars_per_point=rate,
               points_per_run=round(points, 4),
               window_share_pct=round(runs * points, 2),
               window_used_pct=used, resets_at=meter.get("resets_at"),
               max_safe_runs_now=safe,
               fits_now=(runs <= safe) if safe is not None else free >= 0)
    return out


def reason(do_id: str, n: int, computed: Mapping[str, Any]) -> str:
    """The do's attention reason; names the user's go verb"""
    go = f"jarvis do experiment {do_id} --go"
    if computed.get("meter") == UNAVAILABLE:
        return (f"experiment {n} forecast: {computed['runs']} runs, "
                f"~${computed['est_usd']:.2f} (usage meter unavailable) — {go}")
    return (f"experiment {n} forecast: {computed['runs']} runs, "
            f"~{computed['window_share_pct']:g}% of the 5h window "
            f"(now {computed['window_used_pct']:g}% used) — {go}")


def render(computed: Mapping[str, Any]) -> str:
    """The forecast as the notification body and `do show` text"""
    per = computed["per_run"]
    lines = [
        f"{computed['runs']} runs of {computed['model']}: "
        f"{per['input_tokens']} in / {per['output_tokens']} out tokens, "
        f"{per['minutes']:g} min each",
        f"total: {computed['total_tokens']} tokens, {computed['minutes_total']:g} min, "
        f"~${computed['est_usd']:.2f} at list price",
    ]
    if computed.get("meter") == UNAVAILABLE:
        lines.append("5h window: usage meter unavailable (no sample or no OAuth token)")
    else:
        fits = "fits now" if computed["fits_now"] else "does NOT fit now"
        lines.append(
            f"5h window: ~{computed['window_share_pct']:g}% "
            f"({computed['points_per_run']:g} per run), {computed['window_used_pct']:g}% "
            f"used; at most {computed['max_safe_runs_now']} runs with "
            f"{HEADROOM_POINTS:g} points headroom — {fits}")
    if computed.get("notes"):
        lines.append(f"notes: {computed['notes']}")
    return "\n".join(lines)
