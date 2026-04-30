"""
handler.py — EventBridge-triggered diagnostic Lambda.

Flow:
  SiteWise alarm state → ACTIVE
  → EventBridge "IoT SiteWise Alarm State Changed"
  → this Lambda
  → 5-min cooldown check (DynamoDB)
  → BatchGetAssetPropertyValueHistory (30-min telemetry window)
  → SSM: retrieve _flags stored by alarm_evaluator
  → Nova Lite invocation (amazon.nova-lite-v1:0)
  → JSON validation + 1 retry on parse failure
  → DynamoDB write: diagnostics table

Environment variables (set by DiagnosticStack CDK):
  DIAGNOSTICS_TABLE  — DynamoDB table name
  BEDROCK_MODEL_ID   — 'amazon.nova-lite-v1:0'
  REGION             — 'us-west-2'
"""

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr, Key

from prompts import build_diagnostic_prompt

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_REGION: str = os.environ.get('REGION', 'us-west-2')
_TABLE_NAME: str = os.environ['DIAGNOSTICS_TABLE']
_MODEL_ID: str = os.environ['BEDROCK_MODEL_ID']
_COOLDOWN_MINUTES: int = 5

SITEWISE = boto3.client('iotsitewise', region_name=_REGION)
BEDROCK = boto3.client('bedrock-runtime', region_name=_REGION)
DYNAMO = boto3.resource('dynamodb', region_name=_REGION)
SSM = boto3.client('ssm', region_name=_REGION)

_table = DYNAMO.Table(_TABLE_NAME)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def handler(event: dict, context: Any) -> dict:
    logger.info('Diagnostic handler invoked: %s', json.dumps(event))

    detail: dict = event.get('detail', {})
    asset_id: str = detail.get('assetId', '')
    alarm_name: str = detail.get('alarmName', '')
    new_state: str = detail.get('newState', {}).get('stateName', '')

    if not asset_id or not alarm_name:
        logger.error('Missing assetId or alarmName in EventBridge detail')
        return {'status': 'error', 'reason': 'missing_fields'}

    if new_state != 'Active':
        logger.info('Alarm state is %r (not Active) — skipping', new_state)
        return {'status': 'skipped', 'reason': 'not_active'}

    triggered_at: datetime = datetime.now(timezone.utc)
    triggered_at_iso: str = triggered_at.isoformat()

    # 5-min per-asset per-alarm cooldown
    if _in_cooldown(asset_id, alarm_name, triggered_at):
        logger.info('Cooldown active for asset=%s alarm=%s', asset_id, alarm_name)
        return {'status': 'cooldown'}

    # Retrieve pre-computed edge flags stored by alarm_evaluator
    flags: list[str] = _get_flags(asset_id)

    # 30-min telemetry window
    telemetry: list[dict] = _fetch_telemetry(asset_id, triggered_at)

    # Asset metadata for prompt context
    asset_info: dict = _describe_asset(asset_id)

    # Build prompt and invoke Nova Lite
    prompt = build_diagnostic_prompt(
        alarm_name=alarm_name,
        asset_info=asset_info,
        telemetry=telemetry,
        flags=flags,
    )
    diagnostic = _invoke_nova_lite(prompt)

    if diagnostic is None:
        logger.error('Nova Lite returned unparseable JSON — writing error record')
        diagnostic = {
            'what': 'Diagnostic model returned invalid output',
            'why': ['Nova Lite parse failure — manual inspection required'],
            'evidence': [],
            'confidence': 0.0,
            'recommended_action': 'Review raw logs and inspect unit manually',
        }

    # Persist to DynamoDB
    ttl = int((triggered_at + timedelta(days=30)).timestamp())
    diagnostic_id = str(uuid.uuid4())
    item: dict = {
        'asset_id': asset_id,
        'triggered_at': triggered_at_iso,
        'diagnostic_id': diagnostic_id,
        'alarm_type': alarm_name,
        'what': diagnostic.get('what', ''),
        'why': diagnostic.get('why', []),
        'evidence': diagnostic.get('evidence', []),
        # DynamoDB rejects bare floats for Decimal mismatch — store as string
        'confidence': str(diagnostic.get('confidence', 0.0)),
        'recommended_action': diagnostic.get('recommended_action', ''),
        'ttl': ttl,
    }
    _table.put_item(Item=item)
    logger.info(
        'Diagnostic written: id=%s asset=%s alarm=%s',
        diagnostic_id, asset_id, alarm_name,
    )
    return {'status': 'ok', 'diagnostic_id': diagnostic_id}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _in_cooldown(asset_id: str, alarm_name: str, now: datetime) -> bool:
    """Return True if a diagnostic for this asset+alarm was written in the last 5 min."""
    cutoff = (now - timedelta(minutes=_COOLDOWN_MINUTES)).isoformat()
    response = _table.query(
        KeyConditionExpression=Key('asset_id').eq(asset_id) & Key('triggered_at').gt(cutoff),
        FilterExpression=Attr('alarm_type').eq(alarm_name),
    )
    return len(response.get('Items', [])) > 0


def _get_flags(asset_id: str) -> list[str]:
    """Retrieve _flags stored by alarm_evaluator in SSM. Returns [] if missing."""
    try:
        resp = SSM.get_parameter(Name=f'/project-aegis/latest-flags/{asset_id}')
        return json.loads(resp['Parameter']['Value'])
    except SSM.exceptions.ParameterNotFound:
        return []
    except Exception:
        logger.warning('Could not retrieve _flags from SSM', exc_info=True)
        return []


def _fetch_telemetry(asset_id: str, now: datetime) -> list[dict]:
    """
    Fetch the last 30 minutes of measurements and transforms for the asset
    using BatchGetAssetPropertyValueHistory.
    Returns a list of {property, ts, value} dicts sorted newest-first.
    """
    start = now - timedelta(minutes=30)

    # Discover property IDs by name via DescribeAsset
    try:
        asset_resp = SITEWISE.describe_asset(assetId=asset_id)
    except Exception:
        logger.warning('DescribeAsset failed for %s', asset_id, exc_info=True)
        return []

    wanted = {
        'supply_temp_c', 'return_temp_c', 'supply_humidity_pct',
        'fan_rpm', 'power_draw_kw', 'delta_t_c', 'cooling_efficiency',
    }
    prop_id_to_name: dict[str, str] = {
        p['id']: p['name']
        for p in asset_resp.get('assetProperties', [])
        if p.get('name') in wanted
    }
    if not prop_id_to_name:
        logger.warning('No matching properties found on asset %s', asset_id)
        return []

    entries = [
        {
            'entryId': prop_id,
            'assetId': asset_id,
            'propertyId': prop_id,
            'startDate': start,
            'endDate': now,
            'timeOrdering': 'DESCENDING',
            'maxResults': 5,
        }
        for prop_id in prop_id_to_name
    ]

    try:
        history_resp = SITEWISE.batch_get_asset_property_value_history(entries=entries)
    except Exception:
        logger.warning('BatchGetAssetPropertyValueHistory failed', exc_info=True)
        return []

    readings: list[dict] = []
    for entry in history_resp.get('successEntries', []):
        prop_name = prop_id_to_name.get(entry['entryId'], entry['entryId'])
        for point in entry.get('assetPropertyValueHistory', []):
            v = point.get('value', {})
            value = v.get('doubleValue') if 'doubleValue' in v else (
                v.get('integerValue') if 'integerValue' in v else v.get('stringValue')
            )
            readings.append({
                'property': prop_name,
                'ts': point['timestamp']['timeInSeconds'],
                'value': value,
            })

    return sorted(readings, key=lambda r: -r['ts'])


def _describe_asset(asset_id: str) -> dict:
    """Return {assetName, attributes} for prompt context."""
    try:
        resp = SITEWISE.describe_asset(assetId=asset_id)
        # Attributes have no alias and are STRING or DOUBLE type in the model;
        # their current values come from the asset response property list.
        attrs: dict[str, str] = {}
        for prop in resp.get('assetProperties', []):
            if prop.get('dataType') in ('STRING', 'DOUBLE') and not prop.get('alias'):
                val = prop.get('value', {})
                raw = val.get('stringValue') or val.get('doubleValue')
                if raw is not None:
                    attrs[prop['name']] = str(raw)
        return {'assetName': resp.get('assetName', ''), 'attributes': attrs}
    except Exception:
        logger.warning('DescribeAsset failed for attributes', exc_info=True)
        return {'assetName': '', 'attributes': {}}


def _invoke_nova_lite(prompt: str) -> dict | None:
    """
    Call amazon.nova-lite-v1:0 and parse the JSON response.
    On first parse failure, retry once with a stricter output reminder appended.
    Returns the parsed dict or None if both attempts fail.
    """
    suffix = ''
    for attempt in range(2):
        full_prompt = prompt + suffix
        try:
            body = json.dumps({
                'messages': [{'role': 'user', 'content': full_prompt}],
                'inferenceConfig': {'maxTokens': 1024, 'temperature': 0.1},
            })
            resp = BEDROCK.invoke_model(
                modelId=_MODEL_ID,
                body=body,
                contentType='application/json',
                accept='application/json',
            )
            text: str = json.loads(resp['body'].read())['output']['message']['content'][0]['text']

            # Extract the outermost JSON object even if the model adds surrounding text
            start = text.find('{')
            end = text.rfind('}') + 1
            if start >= 0 and end > start:
                return json.loads(text[start:end])

            raise ValueError('No JSON object found in model response')

        except (json.JSONDecodeError, ValueError, KeyError) as exc:
            logger.warning('Parse failure on attempt %d: %s', attempt + 1, exc)
            if attempt == 0:
                suffix = (
                    '\n\nIMPORTANT: Your entire response must be ONLY the JSON object. '
                    'Do not include any text before or after the braces.'
                )
        except Exception:
            logger.exception('Nova Lite invocation failed on attempt %d', attempt + 1)
            break

    return None
