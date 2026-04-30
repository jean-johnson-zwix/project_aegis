"""
publisher.py — Greengrass V2 custom component: com.project-aegis.publisher

Main publish loop. Per BUILD.md §8:

  1. Load CRAC unit configs from crac_units.yaml.
  2. Every publishIntervalSeconds, for each unit:
       a. Generate a reading via modbus_sim.next_reading().
       b. Validate via validator.validate(). Drop invalid; count droppedInvalid.
       c. Try to publish via Greengrass IPC MQTT proxy → IoT Core.
       d. On publish failure: buffer.enqueue(reading, topic).
  3. Every tick: buffer.drain(max_n=100) — replay buffered readings, oldest first.
  4. Every 30 s: emit structured METRIC log lines for CloudWatch Logs metric filters.

Configuration (env vars injected by the Greengrass recipe lifecycle script):
  PUBLISH_INTERVAL_SECONDS  default 60
  BUFFER_DB_PATH            default /greengrass/v2/work/com.project-aegis.publisher/buffer.db
  CONFIG_DIR                default <script directory>  (all artifacts land in the same dir)
  ACTIVE_SCENARIO           optional — name from anomaly_scenarios.yaml (for demo injection)
"""

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Local modules — same directory as publisher.py in the Greengrass artifacts path
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent))
import modbus_sim
import validator as _validator_mod
import buffer as _buffer_mod

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("publisher")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PUBLISH_INTERVAL_S: int = int(os.environ.get("PUBLISH_INTERVAL_SECONDS", "60"))
BUFFER_DB_PATH: str = os.environ.get(
    "BUFFER_DB_PATH",
    "/greengrass/v2/work/com.project-aegis.publisher/buffer.db",
)
# In Greengrass, the recipe sets CONFIG_DIR={artifacts:path} so all artifacts
# (including crac_units.yaml and anomaly_scenarios.yaml) are co-located with publisher.py.
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", str(Path(__file__).parent)))

ACTIVE_SCENARIO: str | None = os.environ.get("ACTIVE_SCENARIO", None)

METRIC_EMIT_INTERVAL_S: int = 30
DRAIN_MAX_N: int = 100


# ---------------------------------------------------------------------------
# Greengrass IPC client
# ---------------------------------------------------------------------------
def _make_ipc_client() -> Any | None:
    """
    Connect to the Greengrass nucleus IPC socket.
    Returns None (with a warning) if the socket is not available — this allows
    the component to run in local dev mode outside a nucleus, buffering everything.
    """
    try:
        from awsiot.greengrasscoreipc.clientv2 import GreengrassCoreIPCClientV2  # type: ignore
        client = GreengrassCoreIPCClientV2()
        log.info("Greengrass IPC client connected")
        return client
    except Exception as exc:
        log.warning(
            "Greengrass IPC unavailable (%s) — all readings will be buffered", exc
        )
        return None


# ---------------------------------------------------------------------------
# MQTT publish
# ---------------------------------------------------------------------------
def _publish_mqtt(ipc_client: Any | None, topic: str, payload: dict) -> bool:
    """
    Publish payload JSON to IoT Core via Greengrass IPC MQTT proxy.
    Returns True on success, False on any failure.
    """
    if ipc_client is None:
        return False
    try:
        from awsiot.greengrasscoreipc.model import QOS  # type: ignore

        ipc_client.publish_to_iot_core(
            topic_name=topic,
            qos=QOS.AT_LEAST_ONCE,
            payload=json.dumps(payload).encode("utf-8"),
        )
        return True
    except Exception as exc:
        log.warning("MQTT publish failed (topic=%s): %s", topic, exc)
        return False


# ---------------------------------------------------------------------------
# Structured metric emission
# ---------------------------------------------------------------------------
def _emit_metrics(
    published: int,
    buffered: int,
    dropped: int,
    avg_latency_ms: float,
) -> None:
    """
    Write structured METRIC lines to stdout.
    CloudWatch Logs metric filters extract these into custom CloudWatch metrics:
      - Namespace: ProjectAegis/Edge
      - Dimension: ComponentName=com.project-aegis.publisher

    Filter patterns (configured in observability-stack.ts):
      [prefix="METRIC", kv, ...]
    """
    print(f"METRIC publishedCount={published} unit=Count", flush=True)
    print(f"METRIC bufferedCount={buffered} unit=Count", flush=True)
    print(f"METRIC droppedInvalidCount={dropped} unit=Count", flush=True)
    print(f"METRIC publishLatencyMs={avg_latency_ms:.1f} unit=Milliseconds", flush=True)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------
def _load_config() -> tuple[str, list[dict], dict[str, dict]]:
    """
    Load CRAC unit config and anomaly scenarios.
    Returns (site_id, units, scenarios) where scenarios maps name → override dict.
    """
    with open(CONFIG_DIR / "crac_units.yaml") as f:
        crac_cfg = yaml.safe_load(f)
    with open(CONFIG_DIR / "anomaly_scenarios.yaml") as f:
        scenario_cfg = yaml.safe_load(f)

    site_id: str = crac_cfg["site_id"]
    units: list[dict] = crac_cfg["units"]
    scenarios: dict[str, dict] = {
        name: data.get("overrides", {})
        for name, data in scenario_cfg["scenarios"].items()
    }
    return site_id, units, scenarios


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main() -> None:
    log.info("com.project-aegis.publisher starting")
    log.info("  publishIntervalSeconds=%d", PUBLISH_INTERVAL_S)
    log.info("  bufferDbPath=%s", BUFFER_DB_PATH)
    log.info("  configDir=%s", CONFIG_DIR)
    log.info("  activeScenario=%s", ACTIVE_SCENARIO or "Normal")

    Path(BUFFER_DB_PATH).parent.mkdir(parents=True, exist_ok=True)

    site_id, units, scenarios = _load_config()
    log.info("Loaded %d CRAC units for site %s", len(units), site_id)

    buf = _buffer_mod.Buffer(BUFFER_DB_PATH)
    val = _validator_mod.Validator()
    ipc_client = _make_ipc_client()

    # Rolling counters — reset every METRIC_EMIT_INTERVAL_S
    published_count = 0
    dropped_count = 0
    latency_total_ms = 0.0
    latency_samples = 0
    last_metric_emit = time.monotonic()

    while True:
        tick_start = time.monotonic()

        # Resolve scenario overrides for this tick (same scenario applied to all units
        # for simplicity; real BMS would allow per-unit overrides).
        scenario_overrides: dict | None = None
        active = os.environ.get("ACTIVE_SCENARIO", ACTIVE_SCENARIO)
        if active and active != "Normal":
            scenario_overrides = scenarios.get(active)
            if scenario_overrides is None:
                log.warning("Unknown scenario %r — using Normal", active)

        for unit in units:
            unit_id: str = unit["unit_id"]
            hall_id: str = unit["hall_id"]

            reading = modbus_sim.next_reading(unit, scenario_overrides)

            # Edge validation — hard drops return None, soft tags add _flags
            validated = val.validate(reading)
            if validated is None:
                dropped_count += 1
                log.debug("Dropped invalid reading from %s", unit_id)
                continue

            topic = f"sitesense/{site_id}/{hall_id}/{unit_id}/telemetry"

            t0 = time.monotonic()
            success = _publish_mqtt(ipc_client, topic, validated)
            elapsed_ms = (time.monotonic() - t0) * 1000.0

            if success:
                published_count += 1
                latency_total_ms += elapsed_ms
                latency_samples += 1
                log.info("Published %s → %s (%.0f ms)", unit_id, topic, elapsed_ms)
            else:
                buf.enqueue(validated, topic)
                log.warning(
                    "Buffered reading for %s (queue=%d)", unit_id, buf.count()
                )

        # Drain buffer — publish oldest buffered readings
        drained = buf.drain(
            ipc_client=ipc_client,
            publish_fn=_publish_mqtt,
            max_n=DRAIN_MAX_N,
        )
        if drained > 0:
            published_count += drained
            log.info("Drained %d buffered readings (queue=%d)", drained, buf.count())

        # Emit CloudWatch metrics every 30 s
        now = time.monotonic()
        if now - last_metric_emit >= METRIC_EMIT_INTERVAL_S:
            avg_latency = (
                latency_total_ms / latency_samples if latency_samples else 0.0
            )
            _emit_metrics(published_count, buf.count(), dropped_count, avg_latency)
            published_count = 0
            dropped_count = 0
            latency_total_ms = 0.0
            latency_samples = 0
            last_metric_emit = now

        # Sleep until next tick
        elapsed = time.monotonic() - tick_start
        sleep_s = max(0.0, PUBLISH_INTERVAL_S - elapsed)
        log.debug("Tick complete in %.2fs, sleeping %.2fs", elapsed, sleep_s)
        time.sleep(sleep_s)


if __name__ == "__main__":
    main()
