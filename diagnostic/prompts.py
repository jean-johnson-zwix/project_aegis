"""
prompts.py — Nova Lite prompt templates for CRAC diagnostic Lambda.

Design rationale:
  - Persona block grounds the model in DCIM cooling domain vocabulary.
  - Explicit 4-step CoT prevents the small model from skipping reasoning steps.
  - JSON-only output instruction eliminates prose that would break json.loads().
  - Context is injected as structured sections, not free-form, to reduce hallucination.
"""

_PERSONA = (
    'You are a senior CRAC (Computer Room Air Conditioning) diagnostics engineer '
    'with deep expertise in data center cooling infrastructure, thermal dynamics, '
    'psychrometrics, and predictive maintenance of precision cooling equipment.'
)

_COT_INSTRUCTION = """\
Analyze the alarm and telemetry using this exact 4-step reasoning process:
1. IDENTIFY  — What specific anomaly is observed? Which threshold or physical limit was violated?
2. RANK      — List the most probable root causes in descending order of likelihood, \
with a one-sentence rationale for each.
3. EVIDENCE  — Which specific telemetry values or edge flags support or contradict each hypothesis?
4. RECOMMEND — What is the single most important immediate action the operations team should take?\
"""

_OUTPUT_INSTRUCTION = """\
Respond ONLY with a single valid JSON object. No prose, no markdown fences, \
no commentary outside the JSON.

Required schema (all fields mandatory):
{
  "what": "<one sentence describing the observed fault>",
  "why": [
    "<most likely root cause>",
    "<second most likely root cause>"
  ],
  "evidence": [
    "<specific data point or flag that supports the diagnosis>",
    "<another data point>"
  ],
  "confidence": <float 0.0–1.0>,
  "recommended_action": "<specific, actionable recommendation for the ops team>"
}\
"""


def build_diagnostic_prompt(
    alarm_name: str,
    asset_info: dict,
    telemetry: list[dict],
    flags: list[str],
) -> str:
    """Assemble the full Nova Lite prompt for a CRAC diagnostic run."""
    asset_name = asset_info.get('assetName', 'unknown')
    attrs = asset_info.get('attributes', {})

    flags_str = ', '.join(flags) if flags else 'none'
    telemetry_str = _format_telemetry(telemetry)

    context = f"""\
## Alarm Event
Alarm name : {alarm_name}
Asset      : {asset_name}
Unit ID    : {attrs.get('unit_id', 'unknown')}
Manufacturer: {attrs.get('manufacturer', 'unknown')}
Model      : {attrs.get('model_number', 'unknown')}
Max cooling: {attrs.get('max_cooling_kw', 'unknown')} kW

## Edge Validation Flags (pre-computed at edge before cloud ingestion)
{flags_str}

## Last 30 Minutes of Telemetry (newest readings first)
{telemetry_str}"""

    return f'{_PERSONA}\n\n{_COT_INSTRUCTION}\n\n{context}\n\n{_OUTPUT_INSTRUCTION}'


def _format_telemetry(readings: list[dict]) -> str:
    """
    Group readings by property name, show up to 3 newest values each.
    Format: 'property_name  | val1 | val2 | val3'
    """
    if not readings:
        return 'No telemetry available for this window.'

    by_prop: dict[str, list] = {}
    for r in readings:
        prop = r.get('property', 'unknown')
        if prop not in by_prop:
            by_prop[prop] = []
        if len(by_prop[prop]) < 3:
            by_prop[prop].append(r.get('value'))

    col_w = 28
    header = f"{'Property':<{col_w}} | Recent values (newest → oldest)"
    sep = '-' * (col_w + 35)
    lines = [header, sep]
    for prop in sorted(by_prop):
        values_str = ' | '.join(
            f'{v:.2f}' if isinstance(v, float) else str(v)
            for v in by_prop[prop]
        )
        lines.append(f'{prop:<{col_w}} | {values_str}')
    return '\n'.join(lines)
