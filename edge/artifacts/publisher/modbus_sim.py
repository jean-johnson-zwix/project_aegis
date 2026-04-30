"""
modbus_sim.py — CRAC unit Modbus register simulator.

Simulates the two classes of Modbus registers used in CRAC units:

  Holding Registers (Function Code 03 read / FC 16 write — read/write):
    These are operator-configurable setpoints. In a real deployment a BMS or
    DCIM controller would write to these to adjust unit behaviour.
      - supply_temp_setpoint   (HR 40001)
      - fan_speed_setpoint_pct (HR 40002)

  Input Registers (Function Code 04 — read-only):
    These are live sensor measurements. The Greengrass publisher reads these
    on each polling cycle and publishes them upstream.
      - supply_temp_c          (IR 30001)
      - return_temp_c          (IR 30002)
      - supply_humidity_pct    (IR 30003)
      - fan_rpm                (IR 30004)
      - power_draw_kw          (IR 30005)

Only Input Registers are published to SiteWise. Holding Register values
influence the simulated Input Register output (e.g. fan_speed_setpoint
shifts the fan_rpm baseline).
"""

import math
import random
import datetime
from typing import Any


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_hour_fraction() -> float:
    """Return fractional hour-of-day (0.0–24.0) in local time."""
    now = datetime.datetime.now()
    return now.hour + now.minute / 60.0 + now.second / 3600.0


def _daily_sine(hour: float, amplitude: float, phase_hours: float = 14.0) -> float:
    """Sine wave over 24 h, peaking at phase_hours (default 2 PM = peak load)."""
    return amplitude * math.sin(2 * math.pi * (hour - phase_hours) / 24.0)


def _noise(std: float) -> float:
    return random.gauss(0.0, std)


# ---------------------------------------------------------------------------
# Holding Registers (setpoints — configurable, influence sensor output)
# ---------------------------------------------------------------------------

def _read_holding_registers(unit: dict[str, Any]) -> dict[str, float]:
    """
    HR 40001–40002: operator setpoints.
    In this simulation they are fixed per unit config but could be updated
    dynamically to simulate BMS commands.
    """
    baseline = unit["baseline"]
    return {
        "supply_temp_setpoint": baseline["supply_temp_c"],       # HR 40001
        "fan_speed_setpoint_pct": 100.0,                          # HR 40002 — full speed
    }


# ---------------------------------------------------------------------------
# Input Registers (sensors — read-only, published to SiteWise)
# ---------------------------------------------------------------------------

def _read_input_registers(
    unit: dict[str, Any],
    holding: dict[str, float],
    scenario_overrides: dict[str, Any],
    hour: float,
) -> dict[str, float]:
    """
    IR 30001–30005: live sensor measurements.

    Generation logic:
      supply_temp_c  — setpoint + daily load wave + noise
      return_temp_c  — supply + delta_t (influenced by load)
      humidity       — relatively stable with small fluctuations
      fan_rpm        — setpoint-driven + noise
      power_draw_kw  — correlated with cooling load
    """
    baseline = unit["baseline"]

    # Daily load cycle: data centres peak ~2 PM, valley ~4 AM
    load_factor = _daily_sine(hour, amplitude=1.0)  # -1 to +1

    # --- IR 30001: supply_temp_c ---
    supply_temp_c = (
        holding["supply_temp_setpoint"]
        + load_factor * 0.8          # ±0.8°C diurnal drift
        + _noise(0.15)
    )

    # --- IR 30002: return_temp_c ---
    delta_t = baseline["delta_t_c"] + load_factor * 1.5 + _noise(0.3)
    return_temp_c = supply_temp_c + max(delta_t, 0.5)

    # --- IR 30003: supply_humidity_pct ---
    supply_humidity_pct = baseline["supply_humidity_pct"] + _noise(1.2)

    # --- IR 30004: fan_rpm ---
    speed_fraction = holding["fan_speed_setpoint_pct"] / 100.0
    fan_rpm = baseline["fan_rpm"] * speed_fraction + _noise(30.0)

    # --- IR 30005: power_draw_kw ---
    # Power scales with cooling load and fan speed
    power_draw_kw = (
        baseline["power_draw_kw"]
        + load_factor * 1.5
        + (speed_fraction - 1.0) * 2.0
        + _noise(0.4)
    )

    readings = {
        "supply_temp_c": round(supply_temp_c, 2),
        "return_temp_c": round(return_temp_c, 2),
        "supply_humidity_pct": round(max(0.0, min(100.0, supply_humidity_pct)), 1),
        "fan_rpm": round(max(0.0, fan_rpm), 0),
        "power_draw_kw": round(max(0.0, power_draw_kw), 2),
    }

    # Apply scenario overrides AFTER normal generation so partial overrides work
    readings.update(scenario_overrides)

    return readings


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def next_reading(unit: dict[str, Any], scenario: str | None = None) -> dict[str, Any]:
    """
    Generate one telemetry reading for the given CRAC unit.

    Args:
        unit:     Unit config dict (one entry from crac_units.yaml).
        scenario: Optional scenario name from anomaly_scenarios.yaml.
                  Pass None or 'Normal' for baseline generation.

    Returns:
        Dict with SiteWise-ready fields plus routing metadata.
        Only Input Register values are included (supply_temp_c etc.).
        Holding Register setpoints are NOT published — they are internal state.
    """
    hour = _now_hour_fraction()
    holding = _read_holding_registers(unit)

    # Load scenario overrides (caller is responsible for passing the right dict)
    overrides: dict[str, Any] = {}
    if scenario and scenario != "Normal":
        # overrides are injected by publisher.py after loading anomaly_scenarios.yaml
        # this function accepts a pre-resolved overrides dict via the scenario param
        # when scenario is a dict (publisher.py usage) vs a string (direct usage)
        pass

    if isinstance(scenario, dict):
        overrides = scenario

    ir = _read_input_registers(unit, holding, overrides, hour)

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "site_id": unit.get("site_id", "phx-dc-01"),
        "hall_id": unit["hall_id"],
        "unit_id": unit["unit_id"],
        "ts": ts,
        **ir,
    }
