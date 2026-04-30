"""
validator.py — Edge-side CRAC telemetry validation.

Per BUILD.md §8, implements two tiers:

Hard drop rules (physically impossible or malformed):
  Any field failing a hard check causes the reading to be silently dropped.
  The caller increments droppedInvalidCount. Return value is None.

Soft-tag rules (suspicious but publishable):
  Readings that pass hard checks may receive flags in the `_flags` array.
  IoT Rule SQL does not filter on _flags; SiteWise ignores it.
  The Diagnostic Lambda reads `_flags` as pre-computed edge evidence when
  building its root-cause narrative. See docs/edge-validation.md.

Stateful: maintains a per-unit, per-field rolling window (10 samples) to
detect rate-of-change spikes exceeding 3 standard deviations.
"""

import datetime
import logging
from collections import deque
from typing import Any

log = logging.getLogger("validator")

# ---------------------------------------------------------------------------
# Hard drop thresholds — physically possible operating range for CRAC units
# ---------------------------------------------------------------------------
HARD_RANGES: dict[str, tuple[float, float]] = {
    "supply_temp_c":       (-10.0,  60.0),
    "return_temp_c":       (-10.0,  80.0),
    "supply_humidity_pct": (  0.0, 100.0),
    "fan_rpm":             (  0.0, 5000.0),
    "power_draw_kw":       (  0.0,  50.0),
}

TIMESTAMP_TOLERANCE_S: int = 600  # ±10 minutes

# Spike detection parameters
SPIKE_WINDOW: int = 10     # rolling sample count
SPIKE_SIGMA: float = 3.0   # threshold in standard deviations
MIN_WINDOW_FOR_SPIKE: int = 3  # need at least this many samples before flagging

NUMERIC_FIELDS: list[str] = list(HARD_RANGES.keys())


class Validator:
    """
    Stateful edge validator.

    One instance per publisher process. Maintains per-unit rolling history
    for spike detection. Thread-safety not required (single-threaded publisher).
    """

    def __init__(self) -> None:
        # unit_id → field → deque[float]
        self._history: dict[str, dict[str, deque]] = {}

    def validate(self, reading: dict[str, Any]) -> dict[str, Any] | None:
        """
        Validate one CRAC reading.

        Returns:
            The reading dict with a `_flags` list added (may be empty), or
            None if the reading fails any hard-drop rule.
        """
        unit_id: str = reading.get("unit_id", "unknown")
        flags: list[str] = []

        # -------------------------------------------------------------------
        # Hard drop: timestamp within ±10 minutes of now
        # -------------------------------------------------------------------
        ts_str: str = reading.get("ts", "")
        try:
            ts = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            now = datetime.datetime.now(datetime.timezone.utc)
            age_s = abs((now - ts).total_seconds())
            if age_s > TIMESTAMP_TOLERANCE_S:
                log.warning(
                    "Hard drop %s: stale timestamp age=%.0fs (ts=%s)",
                    unit_id, age_s, ts_str,
                )
                return None
        except (ValueError, AttributeError):
            log.warning("Hard drop %s: unparseable timestamp %r", unit_id, ts_str)
            return None

        # -------------------------------------------------------------------
        # Hard drop: per-field range checks (Input Register values)
        # -------------------------------------------------------------------
        for field, (lo, hi) in HARD_RANGES.items():
            raw = reading.get(field)
            if raw is None:
                log.warning("Hard drop %s: missing field %s", unit_id, field)
                return None
            value = float(raw)
            if not (lo <= value <= hi):
                log.warning(
                    "Hard drop %s: %s=%.2f out of range [%.1f, %.1f]",
                    unit_id, field, value, lo, hi,
                )
                return None

        # -------------------------------------------------------------------
        # Hard drop: delta_t must be ≥ 0 (return air must be warmer than supply)
        # Physically impossible for a working CRAC unit to deliver return air
        # colder than its supply air — indicates sensor swap or data corruption.
        # -------------------------------------------------------------------
        delta_t = float(reading["return_temp_c"]) - float(reading["supply_temp_c"])
        if delta_t < 0:
            log.warning(
                "Hard drop %s: delta_t=%.2f°C < 0 (return=%.2f supply=%.2f)",
                unit_id, delta_t, reading["return_temp_c"], reading["supply_temp_c"],
            )
            return None

        # -------------------------------------------------------------------
        # Soft tag: fan stall suspect
        # fan_rpm < 100 AND power_draw_kw > 1 kW means the compressor is running
        # but the fan has stopped — motor seized, belt snapped, or controller fault.
        # -------------------------------------------------------------------
        if float(reading["fan_rpm"]) < 100 and float(reading["power_draw_kw"]) > 1.0:
            flags.append("fan_stall_suspect")
            log.info(
                "Soft flag %s: fan_stall_suspect (rpm=%.0f kw=%.2f)",
                unit_id, reading["fan_rpm"], reading["power_draw_kw"],
            )

        # -------------------------------------------------------------------
        # Soft tag: rate-of-change spike
        # Flag any field that jumps > 3σ beyond its 10-sample rolling mean.
        # Uses population std dev (biased) — sufficient for this window size.
        # -------------------------------------------------------------------
        history = self._history.setdefault(unit_id, {})
        for field in NUMERIC_FIELDS:
            value = float(reading[field])
            window: deque = history.setdefault(field, deque(maxlen=SPIKE_WINDOW))

            if len(window) >= MIN_WINDOW_FOR_SPIKE:
                mean = sum(window) / len(window)
                variance = sum((x - mean) ** 2 for x in window) / len(window)
                std = variance ** 0.5
                if std > 0 and abs(value - mean) > SPIKE_SIGMA * std:
                    flag = f"spike_suspect:{field}"
                    flags.append(flag)
                    log.info(
                        "Soft flag %s: %s (value=%.2f mean=%.2f std=%.2f)",
                        unit_id, flag, value, mean, std,
                    )

            window.append(value)

        # -------------------------------------------------------------------
        # Return validated reading with _flags appended
        # _flags is a pass-through field: not filtered by IoT Rule SQL,
        # not ingested by SiteWise, but read by the Diagnostic Lambda.
        # -------------------------------------------------------------------
        result = dict(reading)
        result["_flags"] = flags
        return result
