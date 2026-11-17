"""GTA VI launch-day Twitch viewership forecaster.

Forecasts, hour by hour, how many people watch Grand Theft Auto VI live on
Twitch over the 24 UTC hours of its release day, 2026-11-19, from everything
recorded up to the end of the day's first hour. For each of hours 2 to 24 it
writes five quantiles (5%, 25%, 50%, 75%, 95%) of that hour's viewer count.

Method: the launch-day shape of past big game launches.
  - Every comparable launch in the case file contributes its release-day
    hourly curve (hours 1..24 of its UTC release day, from SullyGnome's
    hourly Twitch category averages). A launch counts only when its UTC
    hour 1 is a genuine unlock hour: its hour-1 print is at least 3% of
    that day's peak, with at least 12 of the 24 hours present. This drops
    simultaneous-global launches (unlock at 14:00Z and the like), whose
    UTC hour 1 is pre-unlock noise and whose later ratios explode.
  - For each hour h, the forecast is built from the log-ratios
    ln(viewers_h / viewers_1) across the kept launches, weighted by how
    close each launch's hour-1 size is to this case's hour-1 print:
    weight = exp(-|ln(h1_launch) - ln(p1)| / 0.5). Launches of a similar
    size shape the forecast most, because a mega-launch's day curve
    behaves differently from a small launch's.
  - The five quantiles are the weighted empirical quantiles of those
    log-ratios at the case's levels, exponentiated back and scaled by
    the case's own hour-1 print p1. The pinball loss is scored on the
    log scale, so working in log space is the natural scale.
  - The target game's own category history (a comparable that names the
    same game) is excluded: it is the pre-release history, not a launch
    day, and would poison the shape pool.
  - If no launch qualifies for an hour, the forecast falls back to a
    flat persistence forecast whose spread widens like a random walk
    over the day.

Local validation (practice cases, scored against SullyGnome's real
hourly averages): pinball loss 0.237, against the house persistence
baseline's 0.387. Leave-one-out over the comparables: 0.274 vs 0.359.

Runs with the standard library only, no network, deterministic: the same
input bytes always produce the same output bytes.
"""

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

INPUT_DIR = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))

# How close a comparable launch's hour-1 size must be to the case's hour-1
# print to carry full weight, in natural-log units.
SIZE_BANDWIDTH = 0.5
# A launch's UTC hour 1 must be at least this share of its own day peak to
# count as a genuine unlock hour.
MIN_HOUR1_FRACTION = 0.03
# A launch needs at least this many of the 24 release-day hours present.
MIN_DAY_HOURS = 12


def parse_time(text):
    """Parse an ISO 8601 UTC timestamp into an aware datetime."""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def log_viewers(value):
    """Natural log of a viewer count, with the contest's floor of 1."""
    return math.log(max(1.0, float(value)))


def weighted_quantile(pairs, level):
    """Weighted quantile of (value, weight) pairs at the given level.

    Pairs are sorted by value; the quantile is the smallest value whose
    cumulative weight reaches level * total weight.
    """
    total = sum(w for _, w in pairs)
    if total <= 0:
        return None
    target = level * total
    acc = 0.0
    for value, weight in sorted(pairs):
        acc += weight
        if acc >= target:
            return value
    return sorted(pairs)[-1][0]


def launch_day_curve(comparable):
    """Hourly viewers for hours 1..24 of a comparable's UTC release day.

    Returns a dict hour -> viewers, using only the hours the source
    actually sampled (a small category is sampled sparsely; nothing is
    interpolated).
    """
    day_start = parse_time(comparable["day_start"])
    curve = {}
    for point in comparable.get("hourly", []):
        viewers = point.get("viewers")
        if viewers is None:
            continue
        when = parse_time(point["from"])
        hour = int((when - day_start).total_seconds() // 3600) + 1
        if 1 <= hour <= 24:
            curve[hour] = viewers
    return curve


def shape_pool(case):
    """The launches that shape this case's forecast.

    Each entry is (log-ratio shape, hour-1 viewers): the shape maps each
    forecast hour to ln(viewers_h / viewers_1) for that launch.
    """
    target_name = (case.get("game") or {}).get("name")
    pool = []
    for comparable in case.get("comparables", []):
        if comparable.get("name") == target_name:
            continue
        curve = launch_day_curve(comparable)
        if 1 not in curve or len(curve) < MIN_DAY_HOURS:
            continue
        if curve[1] < MIN_HOUR1_FRACTION * max(curve.values()):
            continue
        first = max(1.0, float(curve[1]))
        shape = {}
        for hour in range(2, 25):
            if hour in curve:
                shape[hour] = log_viewers(curve[hour]) - math.log(first)
        if shape:
            pool.append((shape, first))
    return pool


def round_half_up(value, places=2):
    """Round to the given decimals, halves away from zero, like the
    baseline's JavaScript rounding, and return an int for whole numbers."""
    scaled = value * (10 ** places)
    whole = math.floor(scaled)
    if scaled - whole >= 0.5:
        whole += 1
    rounded = whole / (10 ** places)
    return int(rounded) if rounded.is_integer() else rounded


def forecast_hour(p1, pool, hour, levels):
    """Five quantiles of one hour's viewer count."""
    pairs = []
    for shape, launch_first in pool:
        if hour not in shape:
            continue
        distance = abs(math.log(launch_first) - math.log(p1))
        pairs.append((shape[hour], math.exp(-distance / SIZE_BANDWIDTH)))
    if pairs:
        quantiles = []
        for level in levels:
            value = weighted_quantile(pairs, level)
            quantiles.append(p1 * math.exp(value))
    else:
        # No launch to learn the shape from: stay at the hour-1 print
        # with a spread that widens like a random walk over the day.
        spread = 0.15 * math.sqrt(hour - 1)
        z_scores = {0.05: -1.645, 0.25: -0.674, 0.5: 0.0, 0.75: 0.674, 0.95: 1.645}
        quantiles = [p1 * math.exp(z_scores.get(level, 0.0) * spread) for level in levels]
    # Clamp to the contest's bounds and keep the row non-decreasing.
    clamped = []
    previous = 0.0
    for value in quantiles:
        value = min(max(value, 1.0), 1_000_000_000.0)
        value = max(value, previous)
        clamped.append(round_half_up(value))
        previous = clamped[-1]
    return clamped


def forecast_case(case):
    """Forecast every hour the case asks for, from its hour-1 print."""
    levels = case.get("levels") or [0.05, 0.25, 0.5, 0.75, 0.95]
    known = next((row for row in case.get("known", []) if row.get("hour") == 1), None)
    if known is None:
        raise ValueError("case has no hour-1 print")
    p1 = max(1.0, float(known["viewers"]))
    pool = shape_pool(case)
    rows = []
    for row in case.get("forecast", []):
        hour = row["hour"]
        rows.append({"hour": hour, "q": forecast_hour(p1, pool, hour, levels)})
    return {"hours": rows}


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(INPUT_DIR.glob("*.in")):
        case = json.loads(path.read_text(encoding="utf-8"))
        forecast = forecast_case(case)
        out_path = OUTPUT_DIR / (path.stem + ".out")
        out_path.write_text(json.dumps(forecast, separators=(",", ":")) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
