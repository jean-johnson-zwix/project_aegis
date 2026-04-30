import * as path from 'path';

import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as iot from 'aws-cdk-lib/aws-iot';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { Construct } from 'constructs';

/**
 * DiagnosticStack — Phase 3
 *
 * Alarm detection and GenAI diagnostic pipeline:
 *
 *   MQTT telemetry
 *     → IoT Topic Rules (per-alarm condition SQL)
 *     → AlarmEvaluator Lambda  (sets SiteWise AWS/ALARM_STATE = Active)
 *     → SiteWise emits EventBridge "IoT SiteWise Alarm State Changed"
 *     → EventBridge rule (newState = Active)
 *     → DiagnosticHandler Lambda
 *         - 5-min per-asset cooldown (DynamoDB)
 *         - BatchGetAssetPropertyValueHistory (30-min window)
 *         - _flags from SSM (written by AlarmEvaluator)
 *         - Nova Lite (amazon.nova-lite-v1:0)
 *         - Write structured diagnostic to DynamoDB
 *
 * External alarms (AWS/ALARM_TYPE = EXTERNAL) are used rather than IoT Events
 * because they allow programmatic state writes from Lambda without the circular
 * property-ID dependency that IoT Events alarm models would introduce.
 *
 * Alarm condition thresholds (matching anomaly_scenarios.yaml):
 *   HighSupplyTemp      : supply_temp_c > 21.0°C  (= target 18°C + 3°C margin)
 *   FanFailure          : fan_rpm < 100 AND power_draw_kw > 1
 *   EfficiencyDegradation: (return_temp_c - supply_temp_c) / power_draw_kw < 0.3 °C/kW
 */
export class DiagnosticStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: cdk.StackProps) {
    super(scope, id, props);

    // Cross-stack imports from SiteWiseStack
    const cracUnitModelId = cdk.Fn.importValue('ProjectAegis-CRACUnitModelId');
    const cracA1AssetId   = cdk.Fn.importValue('ProjectAegis-CracA1AssetId');
    const cracA2AssetId   = cdk.Fn.importValue('ProjectAegis-CracA2AssetId');
    const cracB1AssetId   = cdk.Fn.importValue('ProjectAegis-CracB1AssetId');
    const cracB2AssetId   = cdk.Fn.importValue('ProjectAegis-CracB2AssetId');

    // -------------------------------------------------------------------------
    // 1. DynamoDB — diagnostics table
    // -------------------------------------------------------------------------

    const diagnosticsTable = new dynamodb.Table(this, 'DiagnosticsTable', {
      tableName: 'diagnostics',
      partitionKey: { name: 'asset_id',    type: dynamodb.AttributeType.STRING },
      sortKey:      { name: 'triggered_at', type: dynamodb.AttributeType.STRING },
      timeToLiveAttribute: 'ttl',
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // GSI for querying diagnostics by alarm_type across all assets
    diagnosticsTable.addGlobalSecondaryIndex({
      indexName: 'alarm_type-index',
      partitionKey: { name: 'alarm_type',   type: dynamodb.AttributeType.STRING },
      sortKey:      { name: 'triggered_at', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // -------------------------------------------------------------------------
    // 2. AlarmEvaluator Lambda — sets SiteWise external alarm state
    // -------------------------------------------------------------------------

    const evaluatorRole = new iam.Role(this, 'AlarmEvaluatorRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
      ],
    });
    evaluatorRole.addToPolicy(new iam.PolicyStatement({
      sid: 'SiteWiseAlarmWrite',
      actions: ['iotsitewise:DescribeAssetModel', 'iotsitewise:BatchPutAssetPropertyValue'],
      resources: ['*'],
    }));
    evaluatorRole.addToPolicy(new iam.PolicyStatement({
      sid: 'SsmFlagsWrite',
      actions: ['ssm:PutParameter'],
      resources: [`arn:aws:ssm:us-west-2:${this.account}:parameter/project-aegis/latest-flags/*`],
    }));
    evaluatorRole.addToPolicy(new iam.PolicyStatement({
      sid: 'EventBridgePutAlarmEvent',
      actions: ['events:PutEvents'],
      resources: [`arn:aws:events:us-west-2:${this.account}:event-bus/default`],
    }));

    const alarmEvaluatorFn = new lambda.Function(this, 'AlarmEvaluator', {
      functionName: 'project-aegis-alarm-evaluator',
      description: 'Sets SiteWise external alarm state to Active on IoT Rule trigger',
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'alarm_evaluator.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../diagnostic')),
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      role: evaluatorRole,
      environment: {
        CRAC_UNIT_MODEL_ID: cracUnitModelId,
        ASSET_ID_CRAC_A1:   cracA1AssetId,
        ASSET_ID_CRAC_A2:   cracA2AssetId,
        ASSET_ID_CRAC_B1:   cracB1AssetId,
        ASSET_ID_CRAC_B2:   cracB2AssetId,
      },
    });

    // Allow IoT Core to invoke the evaluator Lambda directly
    alarmEvaluatorFn.addPermission('AllowIoTInvoke', {
      principal: new iam.ServicePrincipal('iot.amazonaws.com'),
      action: 'lambda:InvokeFunction',
    });

    // -------------------------------------------------------------------------
    // 3. IoT Topic Rules — alarm condition detection
    // -------------------------------------------------------------------------

    const alarmRules: Array<{ id: string; name: string; sql: string }> = [
      {
        id: 'FanFailureAlarmRule',
        name: 'project_aegis_alarm_fan_failure',
        // IoT Rule SQL: Note topic() index is 1-based; topic segment layout:
        //   sitesense / phx-dc-01 / hall-A / crac-A1 / telemetry
        //      1            2          3        4          5
        sql: `SELECT topic(3) AS hall_id, topic(4) AS unit_id, \
timestamp() AS ts_ms, 'FanFailureAlarm' AS alarm_name, * \
FROM 'sitesense/phx-dc-01/+/+/telemetry' \
WHERE fan_rpm < 100 AND power_draw_kw > 1`,
      },
      {
        id: 'HighSupplyTempAlarmRule',
        name: 'project_aegis_alarm_high_supply_temp',
        sql: `SELECT topic(3) AS hall_id, topic(4) AS unit_id, \
timestamp() AS ts_ms, 'HighSupplyTempAlarm' AS alarm_name, * \
FROM 'sitesense/phx-dc-01/+/+/telemetry' \
WHERE supply_temp_c > 21.0`,
      },
      {
        id: 'EfficiencyDegradationAlarmRule',
        name: 'project_aegis_alarm_efficiency_degradation',
        // Guard against division by zero with power_draw_kw > 0.
        // Condition mirrors EfficiencyDegradationAlarm threshold (0.3 °C/kW default).
        sql: `SELECT topic(3) AS hall_id, topic(4) AS unit_id, \
timestamp() AS ts_ms, 'EfficiencyDegradationAlarm' AS alarm_name, * \
FROM 'sitesense/phx-dc-01/+/+/telemetry' \
WHERE power_draw_kw > 0 AND (return_temp_c - supply_temp_c) / power_draw_kw < 0.3`,
      },
    ];

    for (const rule of alarmRules) {
      new iot.CfnTopicRule(this, rule.id, {
        ruleName: rule.name,
        topicRulePayload: {
          sql: rule.sql,
          awsIotSqlVersion: '2016-03-23',
          ruleDisabled: false,
          actions: [{ lambda: { functionArn: alarmEvaluatorFn.functionArn } }],
        },
      });
    }

    // -------------------------------------------------------------------------
    // 4. DiagnosticHandler Lambda — telemetry fetch + Nova Lite + DynamoDB write
    // -------------------------------------------------------------------------

    const diagnosticRole = new iam.Role(this, 'DiagnosticHandlerRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
      ],
    });
    diagnosticRole.addToPolicy(new iam.PolicyStatement({
      sid: 'SiteWiseTelemetryRead',
      actions: [
        'iotsitewise:DescribeAsset',
        'iotsitewise:BatchGetAssetPropertyValueHistory',
        'iotsitewise:ListAssetProperties',
      ],
      resources: ['*'],
    }));
    diagnosticRole.addToPolicy(new iam.PolicyStatement({
      sid: 'BedrockNovaLiteOnly',
      actions: ['bedrock:InvokeModel'],
      // Scope to Nova Lite foundation model only
      resources: ['arn:aws:bedrock:us-west-2::foundation-model/amazon.nova-lite-v1:0'],
    }));
    diagnosticRole.addToPolicy(new iam.PolicyStatement({
      sid: 'DynamoReadWrite',
      actions: ['dynamodb:PutItem', 'dynamodb:GetItem', 'dynamodb:Query'],
      resources: [
        diagnosticsTable.tableArn,
        `${diagnosticsTable.tableArn}/index/*`,
      ],
    }));
    diagnosticRole.addToPolicy(new iam.PolicyStatement({
      sid: 'SsmFlagsRead',
      actions: ['ssm:GetParameter'],
      resources: [`arn:aws:ssm:us-west-2:${this.account}:parameter/project-aegis/latest-flags/*`],
    }));

    const diagnosticHandlerFn = new lambda.Function(this, 'DiagnosticHandler', {
      functionName: 'project-aegis-diagnostic-handler',
      description: 'Fetches telemetry, calls Nova Lite, writes structured diagnostic to DynamoDB',
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../diagnostic')),
      timeout: cdk.Duration.seconds(60),
      memorySize: 512,
      role: diagnosticRole,
      environment: {
        DIAGNOSTICS_TABLE: diagnosticsTable.tableName,
        BEDROCK_MODEL_ID: 'amazon.nova-lite-v1:0',
        REGION: 'us-west-2',
      },
    });

    // -------------------------------------------------------------------------
    // 5. EventBridge rule — alarm Active → DiagnosticHandler
    //
    // SiteWise external alarms set via BatchPutAssetPropertyValue do NOT emit
    // the native "IoT SiteWise Alarm State Changed" event (that fires only for
    // IoT Events-backed alarms). The AlarmEvaluator Lambda emits an equivalent
    // custom event from source 'project-aegis.alarms' with the same detail
    // structure, so the handler.py parsing is unchanged.
    //
    // 5-minute per-asset cooldown is enforced inside the handler Lambda by
    // querying DynamoDB for recent diagnostics.
    // -------------------------------------------------------------------------

    const alarmActiveRule = new events.Rule(this, 'SiteWiseAlarmActiveRule', {
      ruleName: 'project-aegis-sitewise-alarm-active',
      description: 'Triggers diagnostic Lambda when AlarmEvaluator signals an alarm is Active',
      eventPattern: {
        source: ['project-aegis.alarms'],
        detailType: ['IoT SiteWise Alarm State Changed'],
        detail: {
          newState: { stateName: ['Active'] },
        },
      },
    });
    alarmActiveRule.addTarget(new targets.LambdaFunction(diagnosticHandlerFn));

    // -------------------------------------------------------------------------
    // 6. Stack outputs
    // -------------------------------------------------------------------------

    new cdk.CfnOutput(this, 'DiagnosticsTableName', {
      value: diagnosticsTable.tableName,
      exportName: 'ProjectAegis-DiagnosticsTableName',
      description: 'DynamoDB table for CRAC diagnostics (PK: asset_id, SK: triggered_at)',
    });
    new cdk.CfnOutput(this, 'AlarmEvaluatorArn', {
      value: alarmEvaluatorFn.functionArn,
      exportName: 'ProjectAegis-AlarmEvaluatorArn',
    });
    new cdk.CfnOutput(this, 'DiagnosticHandlerArn', {
      value: diagnosticHandlerFn.functionArn,
      exportName: 'ProjectAegis-DiagnosticHandlerArn',
    });
  }
}
