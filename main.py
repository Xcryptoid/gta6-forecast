"""Forecast GTA VI's launch-day Twitch audience, hour by hour.

For each hour of the release day after the first, this program writes five
quantiles (5%, 25%, 50%, 75%, 95%) of the concurrent viewers in the game's
Twitch category. It learns the shape of a launch day from past big game
launches supplied in the case file, then scales that shape by the case's own
hour-1 print.

How the shape is learned:
  * Every comparable launch contributes its release-day hourly curve: the
    viewers in hours 1..24 of its UTC release day.
  * A launch is kept only when its UTC hour 1 is a real unlock hour -- its
    hour-1 viewers are at least 3% of that day's peak, and at least 12 of
    the 24 hours are present. Launches that unlocked all at once later in
    the day (a 14:00Z global unlock, say) have a near-empty hour 1 and a
    huge later jump, so their ratios would poison the pool.
  * The target game's own category history is left out: it is pre-release
    traffic, not a launch day.
  * For each hour, the forecast comes from the log-ratios
    ln(viewers_h / viewers_1) across the kept launches, weighted by size
    similarity: a launch whose hour-1 audience is close to this case's
    hour-1 print counts most, with weight
    exp(-|ln(launch hour 1) - ln(p1)| / 0.5). Big launches behave
    differently from small ones, so size-matched launches are the best
    teachers.
  * The five quantiles are the weighted empirical quantiles of those
    log-ratios at the case's levels, exponentiated and scaled by p1. The
    contest scores on the log scale, so log space is the natural scale.
  * When no launch qualifies for an hour, the program falls back to
    holding at the hour-1 print with a spread that widens like a random
    walk through the day.

Standard library only, no network, deterministic: the same input bytes
always produce the same output bytes.
"""

import json
import math
import os
from datetime import datetime
from pathlib import Path

INPUT_DIR = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))

# A comparable's hour-1 size must be within this many log units of the
# case's hour-1 print to carry full weight.
SIZE_SCALE = 0.5
# Hour 1 must reach at least this share of the launch's own day peak to
# count as a genuine unlock hour.
HOUR1_MIN_SHARE = 0.03
# A launch needs at least this many release-day hours present.
DAY_MIN_HOURS = 12
# Fallback spread: log units per square-root hour.
FALLBACK_SIGMA = 0.15
# Standard normal quantiles at the scored levels, for the fallback.
NORMAL_Q = {0.05: -1.645, 0.25: -0.674, 0.5: 0.0, 0.75: 0.674, 0.95: 1.645}


def parse_when(text):
    """ISO 8601 UTC timestamp -> aware datetime."""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def safe_log(value):
    """Natural log of a viewer count; the contest floors counts at 1."""
    return math.log(max(1.0, float(value)))


def day_curve(launch):
    """Hourly viewers for hours 1..24 of a launch's UTC release day.

    The source samples sparsely for small categories, so only hours with
    an actual point are returned; nothing is interpolated.
    """
    start = parse_when(launch["day_start"])
    curve = {}
    for point in launch.get("hourly", []):
        viewers = point.get("viewers")
        if viewers is None:
            continue
        hour = int((parse_when(point["from"]) - start).total_seconds() // 3600) + 1
        if 1 <= hour <= 24:
            curve[hour] = viewers
    return curve


def usable_launches(case):
    """(shape, hour-1 viewers) for every comparable that can teach us.

    Shape maps each forecast hour to ln(viewers_h / viewers_1).
    """
    own_name = (case.get("game") or {}).get("name")
    kept = []
    for launch in case.get("comparables", []):
        if launch.get("name") == own_name:
            continue
        curve = day_curve(launch)
        if 1 not in curve or len(curve) < DAY_MIN_HOURS:
            continue
        if curve[1] < HOUR1_MIN_SHARE * max(curve.values()):
            continue
        first = max(1.0, float(curve[1]))
        shape = {
            hour: safe_log(curve[hour]) - math.log(first)
            for hour in range(2, 25)
            if hour in curve
        }
        if shape:
            kept.append((shape, first))
    return kept


def weighted_quantile(pairs, level):
    """Weighted quantile of (value, weight) pairs at the given level.

    The pairs are sorted by value and the quantile is the first value
    whose cumulative weight reaches the cutoff -- an actual launch's
    ratio, not a blend of two. Interpolating between launches was
    tried and scored worse: a blend can land where no launch ever
    sat, while the step keeps each quantile on a real observed ratio.
    """
    total = sum(weight for _, weight in pairs)
    if total <= 0:
        return None
    cutoff = level * total
    cumulative = 0.0
    for value, weight in sorted(pairs):
        cumulative += weight
        if cumulative >= cutoff:
            return value
    return sorted(pairs)[-1][0]


def round_half_up(value):
    """Round to 2 decimals, halves up; whole numbers come back as ints."""
    scaled = value * 100
    whole = math.floor(scaled)
    if scaled - whole >= 0.5:
        whole += 1
    rounded = whole / 100
    return int(rounded) if rounded.is_integer() else rounded


def forecast_one_hour(p1, launches, hour, levels):
    """Five quantiles of one hour's viewer count."""
    pairs = []
    for shape, launch_first in launches:
        if hour not in shape:
            continue
        gap = abs(math.log(launch_first) - math.log(p1))
        pairs.append((shape[hour], math.exp(-gap / SIZE_SCALE)))
    if pairs:
        raw = [p1 * math.exp(weighted_quantile(pairs, level)) for level in levels]
    else:
        # Nothing to learn from: hold at the hour-1 print, spread widening
        # like a random walk as the day goes on.
        spread = FALLBACK_SIGMA * math.sqrt(hour - 1)
        raw = [p1 * math.exp(NORMAL_Q.get(level, 0.0) * spread) for level in levels]
    cleaned = []
    floor = 0.0
    for value in raw:
        value = min(max(value, 1.0), 1_000_000_000.0)
        value = max(value, floor)
        cleaned.append(round_half_up(value))
        floor = cleaned[-1]
    return cleaned


def forecast_case(case):
    """Forecast every hour the case asks for, anchored on its hour-1 print."""
    levels = case.get("levels") or [0.05, 0.25, 0.5, 0.75, 0.95]
    first_row = next(
        (row for row in case.get("known", []) if row.get("hour") == 1), None
    )
    if first_row is None:
        raise ValueError("case has no hour-1 print")
    p1 = max(1.0, float(first_row["viewers"]))
    launches = usable_launches(case)
    rows = [
        {"hour": row["hour"], "q": forecast_one_hour(p1, launches, row["hour"], levels)}
        for row in case.get("forecast", [])
    ]
    return {"hours": rows}


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(INPUT_DIR.glob("*.in")):
        case = json.loads(path.read_text(encoding="utf-8"))
        forecast = forecast_case(case)
        text = json.dumps(forecast, separators=(",", ":")) + "\n"
        (OUTPUT_DIR / (path.stem + ".out")).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
