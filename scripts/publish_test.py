"""
publish_test.py — Phase 1 throwaway script.

Publishes simulated CRAC telemetry directly to AWS IoT Core via boto3
(HTTPS, no certificate required — uses your IAM credentials).

The IoT Rule picks up each message and routes it to SiteWise.
Use this to verify the end-to-end pipeline before Greengrass is set up.

Usage:
    python scripts/publish_test.py [--iterations N] [--interval S] [--scenario NAME]

Examples:
    python scripts/publish_test.py                          # 5 readings, 1/min, normal
    python scripts/publish_test.py --iterations 10 --interval 5
    python scripts/publish_test.py --scenario FanFailure    # inject anomaly
"""

import argparse
import json
import os
import sys
import time

import boto3
import yaml

# Import modbus_sim from the edge directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'edge', 'artifacts', 'publisher'))
from modbus_sim import next_reading  # noqa: E402

REGION = 'us-west-2'
CONFIG_PATH = os.path.join(os.path.dirname(__file__), '..', 'edge', 'config', 'crac_units.yaml')
SCENARIOS_PATH = os.path.join(os.path.dirname(__file__), '..', 'edge', 'config', 'anomaly_scenarios.yaml')
TOPIC_TEMPLATE = 'sitesense/{site_id}/{hall_id}/{unit_id}/telemetry'


def load_config() -> tuple[list[dict], dict]:
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    site_id = cfg['site_id']
    units = cfg['units']
    for u in units:
        u['site_id'] = site_id

    with open(SCENARIOS_PATH) as f:
        scenarios = yaml.safe_load(f)

    return units, scenarios


def get_iot_data_client():
    iot_mgmt = boto3.client('iot', region_name=REGION)
    endpoint = iot_mgmt.describe_endpoint(endpointType='iot:Data-ATS')['endpointAddress']
    print(f"[init] IoT endpoint: {endpoint}")
    return boto3.client('iot-data', region_name=REGION, endpoint_url=f'https://{endpoint}')


def publish_reading(client, reading: dict) -> None:
    topic = TOPIC_TEMPLATE.format(
        site_id=reading['site_id'],
        hall_id=reading['hall_id'],
        unit_id=reading['unit_id'],
    )
    payload = json.dumps(reading)
    client.publish(topic=topic, payload=payload, qos=0)
    print(f"  → {topic}  supply={reading['supply_temp_c']}°C  fan={reading['fan_rpm']}rpm  pwr={reading['power_draw_kw']}kW")


def main() -> None:
    parser = argparse.ArgumentParser(description='Publish test CRAC telemetry to IoT Core')
    parser.add_argument('--iterations', type=int, default=5, help='Number of publish rounds (default: 5)')
    parser.add_argument('--interval', type=float, default=60.0, help='Seconds between rounds (default: 60)')
    parser.add_argument('--scenario', type=str, default=None,
                        help='Anomaly scenario name: HighSupplyTemp | FanFailure | EfficiencyDegradation')
    args = parser.parse_args()

    units, scenarios = load_config()

    scenario_overrides: dict = {}
    if args.scenario:
        if args.scenario not in scenarios['scenarios']:
            print(f"[error] Unknown scenario '{args.scenario}'. Available: {list(scenarios['scenarios'].keys())}")
            sys.exit(1)
        scenario_overrides = scenarios['scenarios'][args.scenario].get('overrides', {})
        print(f"[scenario] {args.scenario}: {scenarios['scenarios'][args.scenario]['description'].strip()}")

    client = get_iot_data_client()

    for i in range(args.iterations):
        print(f"\n[round {i + 1}/{args.iterations}]")
        for unit in units:
            reading = next_reading(unit, scenario=scenario_overrides if scenario_overrides else None)
            publish_reading(client, reading)

        if i < args.iterations - 1:
            print(f"  sleeping {args.interval}s...")
            time.sleep(args.interval)

    print("\n[done] All readings published. Check SiteWise console for live data.")


if __name__ == '__main__':
    main()
