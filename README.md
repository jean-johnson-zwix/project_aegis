# Project Aegis

Edge-to-cloud industrial telemetry platform on AWS IoT SiteWise + Greengrass V2. Simulates a data center CRAC cooling infrastructure, runs anomaly detection at the edge, and generates structured AI diagnostics on alarm using Amazon Nova Lite.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  EDGE (Docker — Greengrass V2 Core)                                 │
│                                                                     │
│  Modbus Sim (4 CRAC units)                                          │
│    -> validator.py  (hard-drop + soft-flag)                        │
│    -> publisher.py  (Greengrass IPC MQTT proxy)                    │
│    -> buffer.py     (SQLite WAL -- offline replay)                 │
└─────────────────────────── MQTTS ───────────────────────────────────┘
                                │
                    ┌───────────▼───────────┐
                    │  AWS IoT Core         │
                    │  IoT Topic Rules      │
                    └──┬────────────────┬───┘
                       │ SiteWise       │ Alarm condition SQL
                       │ action         │ -> alarm_evaluator Lambda
                       ▼                ▼
              ┌──────────────┐  ┌──────────────────────┐
              │  IoT         │  │  alarm_evaluator.py  │
              │  SiteWise    │  │  - BatchPutAsset     │
              │  (hot tier)  │  │    PropertyValue     │
              │              │  │    (AWS/ALARM_STATE) │
              │  4-level     │  │  - SSM: store _flags │
              │  hierarchy   │  │  - EventBridge:      │
              │  alarms      │  │    put custom event  │
              └──────────────┘  └──────────┬───────────┘
                                           │
                                           ▼
                                ┌─────────────────────┐
                                │  EventBridge rule   │
                                │  newState = Active  │
                                └──────────┬──────────┘
                                           │
                                           ▼
                                ┌─────────────────────┐
                                │  handler.py         │
                                │  - 5-min cooldown   │
                                │  - 30-min telemetry │
                                │  - Nova Lite        │
                                │  - DynamoDB write   │
                                └──────────┬──────────┘
                                           │
                                           ▼
                                ┌─────────────────────┐
                                │  DynamoDB           │
                                │  diagnostics table  │
                                └─────────────────────┘
```

---

## Asset model

| Level | Model | Key properties |
|---|---|---|
| 1 | **SiteModel** | `site_id`, `region`, `climate_zone` |
| 2 | **HallModel** | `hall_id`, `design_capacity_kw`, `target_supply_temp_c` |
| 3 | **CRACUnitModel** | attributes, measurements, transforms, metrics, alarms |

### CRACUnitModel properties

| Type | Name | Description |
|---|---|---|
| Measurement | `supply_temp_c` | Supply air temperature |
| Measurement | `return_temp_c` | Return air temperature |
| Measurement | `supply_humidity_pct` | Supply air relative humidity |
| Measurement | `fan_rpm` | Fan speed |
| Measurement | `power_draw_kw` | Unit power draw |
| Transform | `delta_t_c` | `return_temp_c - supply_temp_c` |
| Transform | `cooling_efficiency` | `delta_t_c / power_draw_kw` (°C/kW) |
| Metric | `avg_power_5min` | 5-minute tumbling average power |
| Metric | `max_supply_temp_1h` | 1-hour tumbling max supply temp |
| Metric | `total_kwh_1d` | Daily energy estimate |
| Alarm | `HighSupplyTempAlarm` | `supply_temp_c > 21°C` |
| Alarm | `FanFailureAlarm` | `fan_rpm < 100 AND power_draw_kw > 1` |
| Alarm | `EfficiencyDegradationAlarm` | `cooling_efficiency < 0.3 °C/kW` |

Property aliases: `/sitesense/{site_id}/{hall_id}/{unit_id}/{field}` — one IoT Rule routes all 4 units dynamically via substitution templates.

---

## Local setup (3 steps)

**Prerequisites:** Docker, AWS CLI configured for `us-west-2`, CDK bootstrapped.

```bash
# 1. Deploy cloud infrastructure
cd infra && npm ci
npx cdk deploy --all

# 2. Provision Greengrass edge device (one-time)
cd ../edge
bash scripts/provision-greengrass.sh
docker-compose up -d

# 3. Deploy publisher component (Windows PowerShell)
$env:SCENARIO='FanFailure'; $env:PUBLISH_INTERVAL=5
powershell -ExecutionPolicy Bypass -File scripts\deploy-component.ps1
```

Watch the pipeline:
```bash
# Edge component logs
docker exec project-aegis-greengrass tail -f /greengrass/v2/logs/com.project-aegis.publisher.log

# Alarm evaluator Lambda
MSYS_NO_PATHCONV=1 PYTHONIOENCODING=utf-8 \
  aws logs tail /aws/lambda/project-aegis-alarm-evaluator --follow --region us-west-2

# Diagnostic result
aws dynamodb scan --table-name diagnostics --region us-west-2
```

---

## Features

Every line of code in this repository is original work:

| Area | What I designed and built |
|---|---|
| SiteWise asset model | 4-level CDK hierarchy with transforms, metrics, and external alarm composite models |
| IoT Rule routing | Single rule with substitution templates handles all 4 CRAC units dynamically |
| Greengrass component | Custom Python component (Modbus sim, edge validator, SQLite buffer) running in Docker |
| Edge validation | Hard-drop rules (range, delta_t, timestamp drift) + soft-tag flags (`_flags` array) — reduces SiteWise ingest cost |
| Offline buffer | SQLite WAL with drain/replay and dead-letter table after 10 failed attempts |
| Alarm pipeline | IoT Rule SQL alarm conditions -> Lambda sets SiteWise external alarm state -> EventBridge -> diagnostic handler |
| GenAI diagnostic | Nova Lite with persona + 4-step CoT + JSON-only output; 1-retry on parse failure; `_flags` from edge as pre-computed evidence |
| IaC | All infrastructure in CDK v2 TypeScript; `cdk deploy --all` from a clean account |

---

## Cost (~$10–12/mo steady state)

| Service | Estimate |
|---|---|
| IoT SiteWise (hot tier, 4 units x 5 measurements x 1/min) | ~$4/mo |
| IoT Core MQTT messages | ~$1/mo |
| Lambda (alarm evaluator + diagnostic handler) | < $1/mo |
| Bedrock Nova Lite (alarm-gated, 5-min cooldown) | < $1/mo |
| DynamoDB (on-demand, diagnostics table) | < $1/mo |
| Greengrass (no charge for the runtime itself) | $0 |

Budget ceiling: **$40/mo**. AWS Budget alert configured at $30/day.

---

