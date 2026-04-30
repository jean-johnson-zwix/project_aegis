import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as iot from 'aws-cdk-lib/aws-iot';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';

export interface IotStackProps extends cdk.StackProps {
  siteId: string;
}

/**
 * IotStack
 *
 * Creates:
 *   - 4 IoT Things (one per CRAC unit) — used in Phase 2 for Greengrass cert attachment.
 *     The throwaway publish_test.py script uses IAM credentials and does not need certs.
 *   - One IoT Topic Rule that:
 *       (a) SQL-filters incoming CRAC telemetry on 'sitesense/+/+/+/telemetry'
 *       (b) Routes matching messages to SiteWise via property aliases (substitution templates)
 *       (c) Routes errors to CloudWatch Logs for debugging
 *
 * IoT Rule → SiteWise routing uses property aliases built from MQTT payload fields
 * via substitution templates (${site_id}, ${hall_id}, ${unit_id}).
 * This means the single rule handles all 4 CRAC units dynamically.
 */
export class IotStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: IotStackProps) {
    super(scope, id, props);

    // -------------------------------------------------------------------------
    // 1. IoT Things (one per CRAC unit)
    // -------------------------------------------------------------------------

    const thingNames = ['crac-A1', 'crac-A2', 'crac-B1', 'crac-B2'];
    for (const name of thingNames) {
      new iot.CfnThing(this, `Thing-${name}`, {
        thingName: `project-aegis-${name}`,
      });
    }

    // -------------------------------------------------------------------------
    // 2. IAM role — allows IoT Core to call SiteWise BatchPutAssetPropertyValue
    // -------------------------------------------------------------------------

    const siteWiseRole = new iam.Role(this, 'IotToSiteWiseRole', {
      roleName: 'project-aegis-iot-to-sitewise',
      assumedBy: new iam.ServicePrincipal('iot.amazonaws.com'),
      description: 'Allows IoT Rule to put asset property values into SiteWise',
      inlinePolicies: {
        SiteWisePut: new iam.PolicyDocument({
          statements: [
            new iam.PolicyStatement({
              sid: 'BatchPutAssetPropertyValue',
              actions: ['iotsitewise:BatchPutAssetPropertyValue'],
              resources: ['*'],
            }),
          ],
        }),
      },
    });

    // -------------------------------------------------------------------------
    // 3. IAM role — allows IoT Core to write rule errors to CloudWatch Logs
    // -------------------------------------------------------------------------

    const logsRole = new iam.Role(this, 'IotRuleLogsRole', {
      roleName: 'project-aegis-iot-rule-logs',
      assumedBy: new iam.ServicePrincipal('iot.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSIoTLogging'),
      ],
    });

    const errorLogGroup = new logs.LogGroup(this, 'IotRuleErrorLogs', {
      logGroupName: '/project-aegis/iot-rule-errors',
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // -------------------------------------------------------------------------
    // 4. IoT Topic Rule
    //
    // SQL filter provides a first layer of cloud-side validation.
    // (Edge validator.py is the primary guard; this catches anything that slips through.)
    //
    // SiteWise action uses substitution templates so one rule handles all units:
    //   ${site_id}, ${hall_id}, ${unit_id}  — from MQTT payload fields
    //   ${supply_temp_c} etc.               — measurement values
    //   ${floor(timestamp() / 1000)}        — epoch seconds from message arrival time
    // -------------------------------------------------------------------------

    const measurements = [
      'supply_temp_c',
      'return_temp_c',
      'supply_humidity_pct',
      'fan_rpm',
      'power_draw_kw',
    ];

    const putAssetPropertyValueEntries: iot.CfnTopicRule.PutAssetPropertyValueEntryProperty[] =
      measurements.map((m) => ({
        entryId: m,
        propertyAlias: `/sitesense/\${site_id}/\${hall_id}/\${unit_id}/${m}`,
        propertyValues: [
          {
            value: { doubleValue: `\${${m}}` },
            timestamp: {
              timeInSeconds: '${floor(timestamp() / 1000)}',
              offsetInNanos: '0',
            },
            quality: 'GOOD',
          },
        ],
      }));

    new iot.CfnTopicRule(this, 'CracTelemetryRule', {
      ruleName: 'project_aegis_crac_telemetry',
      topicRulePayload: {
        description: 'Routes CRAC telemetry from IoT Core to SiteWise via property aliases',
        sql: "SELECT * FROM 'sitesense/+/+/+/telemetry' WHERE supply_temp_c >= -10 AND supply_temp_c <= 60 AND return_temp_c >= -10 AND return_temp_c <= 80 AND fan_rpm >= 0 AND power_draw_kw >= 0",
        awsIotSqlVersion: '2016-03-23',
        ruleDisabled: false,
        actions: [
          {
            iotSiteWise: {
              putAssetPropertyValueEntries,
              roleArn: siteWiseRole.roleArn,
            },
          },
        ],
        errorAction: {
          cloudwatchLogs: {
            logGroupName: errorLogGroup.logGroupName,
            roleArn: logsRole.roleArn,
          },
        },
      },
    });

    // -------------------------------------------------------------------------
    // 5. Stack outputs
    // -------------------------------------------------------------------------

    new cdk.CfnOutput(this, 'IotRuleName', {
      value: 'project_aegis_crac_telemetry',
      description: 'IoT Rule routing CRAC telemetry to SiteWise',
    });

    new cdk.CfnOutput(this, 'ErrorLogGroup', {
      value: errorLogGroup.logGroupName,
      description: 'CloudWatch Logs group for IoT Rule errors',
    });
  }
}
