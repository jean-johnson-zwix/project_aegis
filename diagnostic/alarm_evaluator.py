"""
alarm_evaluator.py — Sets a SiteWise external alarm state to Active when an
IoT Core Topic Rule detects an alarm condition in CRAC telemetry.

Triggered by: three IoT Topic Rules (one per alarm type) in DiagnosticStack.

Each rule adds to the MQTT payload:
  unit_id    — from topic(4)   e.g. 'crac-A1'
  hall_id    — from topic(3)   e.g. 'hall-A'
  alarm_name — literal string  e.g. 'FanFailureAlarm'
  ts_ms      — timestamp()     epoch milliseconds

Environment variables (set by DiagnosticStack CDK):
  CRAC_UNIT_MODEL_ID  — SiteWise CRACUnitModel asset model ID
  ASSET_ID_CRAC_A1    — SiteWise asset IDs for each unit
  ASSET_ID_CRAC_A2
  ASSET_ID_CRAC_B1
  ASSET_ID_CRAC_B2
"""

import json
import logging
import os
import time

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_REGION = 'us-west-2'
SITEWISE = boto3.client('iotsitewise', region_name=_REGION)
SSM = boto3.client('ssm', region_name=_REGION)
EVENTS = boto3.client('events', region_name=_REGION)

MODEL_ID: str = os.environ['CRAC_UNIT_MODEL_ID']

# Maps unit_id (from MQTT topic) → SiteWise asset ID (from CDK exports)
ASSET_MAP: dict[str, str] = {
    'crac-A1': os.environ['ASSET_ID_CRAC_A1'],
    'crac-A2': os.environ['ASSET_ID_CRAC_A2'],
    'crac-B1': os.environ['ASSET_ID_CRAC_B1'],
    'crac-B2': os.environ['ASSET_ID_CRAC_B2'],
}

# Cold-start cache: alarm composite model name → AWS/ALARM_STATE property ID
_alarm_prop_cache: dict[str, str] = {}


def _load_alarm_property_ids() -> dict[str, str]:
    """
    Call DescribeAssetModel once per Lambda cold start to discover the
    AWS/ALARM_STATE property ID for each alarm composite model.
    Property IDs are SiteWise-assigned UUIDs not known at CDK synth time.
    """
    global _alarm_prop_cache
    if _alarm_prop_cache:
        return _alarm_prop_cache

    response = SITEWISE.describe_asset_model(assetModelId=MODEL_ID)
    cache: dict[str, str] = {}
    for cm in response.get('assetModelCompositeModels', []):
        alarm_name: str = cm['name']  # e.g. 'FanFailureAlarm'
        for prop in cm.get('properties', []):
            if prop['name'] == 'AWS/ALARM_STATE':
                cache[alarm_name] = prop['id']
                break

    _alarm_prop_cache = cache
    logger.info('Loaded alarm property IDs for: %s', list(cache.keys()))
    return cache


def handler(event: dict, context) -> None:
    """
    Receive enriched MQTT payload from IoT Rule, set SiteWise alarm state to Active,
    and persist _flags in SSM for the downstream diagnostic Lambda.
    """
    logger.info('Alarm evaluator invoked: %s', json.dumps(event))

    unit_id: str = event.get('unit_id', '')
    alarm_name: str = event.get('alarm_name', '')
    flags: list = event.get('_flags', [])
    ts_ms: int = event.get('ts_ms', int(time.time() * 1000))
    ts_seconds: int = ts_ms // 1000

    asset_id = ASSET_MAP.get(unit_id)
    if not asset_id:
        logger.error('Unknown unit_id %r — no asset mapping', unit_id)
        return

    alarm_prop_ids = _load_alarm_property_ids()
    prop_id = alarm_prop_ids.get(alarm_name)
    if not prop_id:
        logger.error('No AWS/ALARM_STATE property found for alarm %r', alarm_name)
        return

    # Write ACTIVE state to SiteWise — this triggers the EventBridge event
    # that wakes the diagnostic Lambda.
    alarm_state = json.dumps({'stateName': 'Active'})
    try:
        SITEWISE.batch_put_asset_property_value(
            entries=[{
                'entryId': f'{asset_id}-{alarm_name}-{ts_seconds}',
                'assetId': asset_id,
                'propertyId': prop_id,
                'propertyValues': [{
                    'value': {'stringValue': alarm_state},
                    'timestamp': {'timeInSeconds': ts_seconds, 'offsetInNanos': 0},
                    'quality': 'GOOD',
                }],
            }]
        )
        logger.info('Alarm %s set Active for asset %s', alarm_name, asset_id)
    except Exception:
        logger.exception('Failed to set alarm state')
        raise

    # Emit custom EventBridge event so the diagnostic Lambda is triggered.
    # SiteWise external alarms set via BatchPutAssetPropertyValue do NOT
    # automatically emit "IoT SiteWise Alarm State Changed" EventBridge events
    # (that event type is for IoT Events-backed alarms only). We emit an
    # equivalent custom event from the same structure so handler.py is unchanged.
    try:
        response = EVENTS.put_events(Entries=[{
            'Source': 'project-aegis.alarms',
            'DetailType': 'IoT SiteWise Alarm State Changed',
            'Detail': json.dumps({
                'assetId': asset_id,
                'alarmName': alarm_name,
                'propertyId': prop_id,
                'newState': {'stateName': 'Active'},
                'oldState': {'stateName': 'Normal'},
            }),
            'EventBusName': 'default',
        }])
        if response.get('FailedEntryCount', 0):
            logger.error('EventBridge put_events failed entries: %s', response.get('Entries'))
            raise RuntimeError('EventBridge put_events reported failed entries')
        logger.info('EventBridge alarm event emitted for asset %s alarm %s', asset_id, alarm_name)
    except Exception:
        logger.exception('Failed to emit EventBridge event')
        raise

    # Persist _flags in SSM so the diagnostic Lambda can include them in the
    # Nova Lite prompt as pre-computed edge evidence.
    if flags:
        try:
            SSM.put_parameter(
                Name=f'/project-aegis/latest-flags/{asset_id}',
                Value=json.dumps(flags),
                Type='String',
                Overwrite=True,
            )
            logger.info('Stored _flags for asset %s: %s', asset_id, flags)
        except Exception:
            logger.warning('Could not persist _flags to SSM', exc_info=True)
