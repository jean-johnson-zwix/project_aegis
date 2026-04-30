import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import * as greengrassv2 from 'aws-cdk-lib/aws-greengrassv2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as iot from 'aws-cdk-lib/aws-iot';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import { Construct } from 'constructs';

/**
 * GreengrassStack
 *
 * Creates:
 *   1. IoT Thing for the Greengrass core device (separate from the 4 CRAC unit Things).
 *   2. Token Exchange IAM role — Greengrass devices assume this via IoT credentials
 *      to call AWS services (CloudWatch Logs, S3 artifact downloads).
 *   3. IoT Role Alias — maps the IAM role to an IoT credential endpoint alias.
 *   4. IoT Policy for the core device — governs what the nucleus can do over MQTT
 *      and which Greengrass service APIs it can call.
 *   5. S3 artifact bucket — hosts component Python files + config YAML files.
 *      BucketDeployment uploads them at `cdk deploy` time.
 *   6. CfnComponentVersion — registers com.project-aegis.publisher v1.0.0 with
 *      inline recipe referencing the S3 artifact URIs.
 *   7. CfnDeployment — targets the core device Thing; delivers the component
 *      automatically when the nucleus connects and checks in.
 *
 * Local dev workflow (no S3 required):
 *   The `edge/recipe.yaml` + `edge/scripts/deploy-component.sh` use file:// URIs
 *   and `greengrass-cli deployment create` to deploy locally without touching S3.
 *   The CDK stack is the production path; local is for fast iteration.
 */
export class GreengrassStack extends cdk.Stack {
  public readonly coreThingName: string;
  public readonly tokenExchangeRoleAlias: string;

  constructor(scope: Construct, id: string, props: cdk.StackProps) {
    super(scope, id, props);

    this.coreThingName = 'project-aegis-greengrass-core';
    this.tokenExchangeRoleAlias = 'ProjectAegisGreengrassTokenExchangeAlias';

    // -------------------------------------------------------------------------
    // 1. IoT Thing — Greengrass core device
    //    Separate from the 4 CRAC unit Things in IotStack.
    //    This Thing represents the Docker container running the nucleus.
    // -------------------------------------------------------------------------

    const coreThing = new iot.CfnThing(this, 'GreengrassCoreThing', {
      thingName: this.coreThingName,
    });

    const coreThingArn = this.formatArn({
      service: 'iot',
      resource: 'thing',
      resourceName: this.coreThingName,
    });

    // -------------------------------------------------------------------------
    // 2. Token Exchange IAM role
    //    Greengrass uses IoT credentials (device cert) to assume this role via
    //    the credentials endpoint, then makes AWS SDK calls as this role.
    //    Permissions: CloudWatch Logs (structured metrics) + S3 (artifact downloads).
    // -------------------------------------------------------------------------

    const tokenExchangeRole = new iam.Role(this, 'TokenExchangeRole', {
      roleName: 'project-aegis-greengrass-token-exchange',
      assumedBy: new iam.ServicePrincipal('credentials.iot.amazonaws.com'),
      description:
        'Greengrass V2 token exchange: grants edge device access to CloudWatch Logs and S3 artifacts',
    });

    // CloudWatch Logs — structured METRIC log lines from publisher.py
    tokenExchangeRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'CloudWatchLogs',
        actions: [
          'logs:CreateLogGroup',
          'logs:CreateLogStream',
          'logs:PutLogEvents',
          'logs:DescribeLogStreams',
        ],
        resources: [`arn:aws:logs:${this.region}:${this.account}:log-group:/project-aegis/*`],
      }),
    );

    // -------------------------------------------------------------------------
    // 3. IoT Role Alias
    //    The core device IoT policy allows AssumeRoleWithCertificate against this alias.
    // -------------------------------------------------------------------------

    new iot.CfnRoleAlias(this, 'TokenExchangeRoleAlias', {
      roleAlias: this.tokenExchangeRoleAlias,
      roleArn: tokenExchangeRole.roleArn,
      credentialDurationSeconds: 3600,
    });

    // -------------------------------------------------------------------------
    // 4. IoT Policy for Greengrass core device
    //    Attached manually to the device certificate during provisioning
    //    (see edge/scripts/provision-greengrass.sh).
    //
    //    Covers:
    //      - iot:Connect  → nucleus MQTT connection (clientId = thingName)
    //      - iot:Publish/Subscribe/Receive → Greengrass system topics + CRAC telemetry
    //      - iot:AssumeRoleWithCertificate → token exchange for AWS SDK calls
    //      - greengrass:* → service-side deployment, artifact, and connectivity APIs
    // -------------------------------------------------------------------------

    const iotTopicBase = `arn:aws:iot:${this.region}:${this.account}:topic`;
    const iotTopicFilterBase = `arn:aws:iot:${this.region}:${this.account}:topicfilter`;

    new iot.CfnPolicy(this, 'GreengrassCoreiOtPolicy', {
      policyName: 'project-aegis-greengrass-core-policy',
      policyDocument: {
        Version: '2012-10-17',
        Statement: [
          // MQTT connection — client ID must match the Thing name
          {
            Effect: 'Allow',
            Action: ['iot:Connect'],
            Resource: `arn:aws:iot:${this.region}:${this.account}:client/${this.coreThingName}`,
          },
          // Greengrass nucleus system topics ($aws/things/<thingName>/*)
          {
            Effect: 'Allow',
            Action: ['iot:Publish', 'iot:Subscribe', 'iot:Receive'],
            Resource: [
              `${iotTopicBase}/$aws/things/${this.coreThingName}/*`,
              `${iotTopicFilterBase}/$aws/things/${this.coreThingName}/*`,
            ],
          },
          // CRAC telemetry topics — published by the custom component via IPC MQTT proxy
          {
            Effect: 'Allow',
            Action: ['iot:Publish', 'iot:Subscribe', 'iot:Receive'],
            Resource: [
              `${iotTopicBase}/sitesense/*`,
              `${iotTopicFilterBase}/sitesense/*`,
            ],
          },
          // Token exchange — allows the nucleus to assume the IAM role via IoT creds endpoint
          {
            Effect: 'Allow',
            Action: ['iot:AssumeRoleWithCertificate'],
            Resource: `arn:aws:iot:${this.region}:${this.account}:rolealias/${this.tokenExchangeRoleAlias}`,
          },
          // Greengrass service APIs — deployment polling, artifact download, connectivity
          {
            Effect: 'Allow',
            Action: [
              'greengrass:GetComponentVersionArtifact',
              'greengrass:ResolveComponentCandidates',
              'greengrass:GetDeploymentConfiguration',
              'greengrass:ListThingGroupsForCoreDevice',
              'greengrass:UpdateCoreDeviceConnectivityInfo',
              'greengrass:PutCertificateAuthorities',
              'greengrass:VerifyClientDeviceIoTCertificateAssociation',
              'greengrass:GetConnectivityInfo',
            ],
            Resource: '*',
          },
        ],
      },
    });

    // -------------------------------------------------------------------------
    // 5. S3 artifact bucket
    //    Hosts component artifacts for cloud deployment.
    //    BucketDeployment uploads publisher.py, modbus_sim.py, validator.py,
    //    buffer.py, requirements.txt, crac_units.yaml, anomaly_scenarios.yaml
    //    at `cdk deploy` time via a Lambda-backed Custom Resource.
    // -------------------------------------------------------------------------

    const artifactBucket = new s3.Bucket(this, 'ArtifactBucket', {
      // Account-scoped name avoids conflicts
      bucketName: `project-aegis-gg-artifacts-${this.account}`,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      enforceSSL: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      versioned: false,
    });

    // Allow the token exchange role to download component artifacts
    artifactBucket.grantRead(tokenExchangeRole);

    const artifactPrefix = 'com.project-aegis.publisher/1.0.0';

    // Upload Python component files
    const publisherDeployment = new s3deploy.BucketDeployment(
      this,
      'UploadPublisherArtifacts',
      {
        sources: [
          s3deploy.Source.asset(
            path.join(__dirname, '../../edge/artifacts/publisher'),
          ),
        ],
        destinationBucket: artifactBucket,
        destinationKeyPrefix: artifactPrefix,
        prune: false,
      },
    );

    // Upload config files (crac_units.yaml, anomaly_scenarios.yaml) to same prefix
    // so they land at {artifacts:path}/crac_units.yaml inside the component.
    const configDeployment = new s3deploy.BucketDeployment(
      this,
      'UploadConfigArtifacts',
      {
        sources: [
          s3deploy.Source.asset(
            path.join(__dirname, '../../edge/config'),
          ),
        ],
        destinationBucket: artifactBucket,
        destinationKeyPrefix: artifactPrefix,
        prune: false,
      },
    );

    // -------------------------------------------------------------------------
    // 6. Greengrass Component Version
    //    Inline recipe references S3 artifact URIs.
    //    The component version is created after artifacts are uploaded (explicit dep).
    //
    //    accessControl in DefaultConfiguration grants the component permission
    //    to call aws.greengrass#PublishToIoTCore via the IPC MQTT proxy.
    // -------------------------------------------------------------------------

    const bucketName = artifactBucket.bucketName;

    const componentRecipe = {
      RecipeFormatVersion: '2020-01-25',
      ComponentName: 'com.project-aegis.publisher',
      ComponentVersion: '1.0.0',
      ComponentDescription:
        'CRAC telemetry publisher: Modbus simulator, edge validator, SQLite offline buffer. Publishes via Greengrass IPC MQTT proxy → IoT Core → SiteWise.',
      ComponentPublisher: 'project-aegis',
      ComponentDependencies: {
        'aws.greengrass.Nucleus': {
          VersionRequirement: '>=2.9.0',
          DependencyType: 'SOFT',
        },
      },
      ComponentConfiguration: {
        DefaultConfiguration: {
          publishIntervalSeconds: '60',
          bufferDbPath: '/greengrass/v2/work/com.project-aegis.publisher/buffer.db',
          accessControl: {
            'aws.greengrass.ipc.mqttproxy': {
              'com.project-aegis.publisher:mqttproxy:1': {
                policyDescription: 'Publish CRAC telemetry readings to IoT Core',
                operations: ['aws.greengrass#PublishToIoTCore'],
                resources: ['sitesense/*'],
              },
            },
            'aws.greengrass.ipc.pubsub': {
              'com.project-aegis.publisher:pubsub:1': {
                policyDescription: 'Local Greengrass pub/sub for inter-component messaging',
                operations: [
                  'aws.greengrass#PublishToTopic',
                  'aws.greengrass#SubscribeToTopic',
                ],
                resources: ['*'],
              },
            },
          },
        },
      },
      Manifests: [
        {
          Platform: { os: 'linux' },
          Lifecycle: {
            Install: {
              Script: `pip3 install -r {artifacts:path}/requirements.txt --quiet`,
              Timeout: 120,
            },
            Run: {
              Script: [
                'export PUBLISH_INTERVAL_SECONDS={configuration:/publishIntervalSeconds}',
                'export BUFFER_DB_PATH={configuration:/bufferDbPath}',
                'export CONFIG_DIR={artifacts:path}',
                'python3 -u {artifacts:path}/publisher.py',
              ].join('\n'),
              RequiresPrivilege: false,
            },
          },
          Artifacts: [
            { URI: `s3://${bucketName}/${artifactPrefix}/publisher.py`, Unarchive: 'NONE' },
            { URI: `s3://${bucketName}/${artifactPrefix}/modbus_sim.py`, Unarchive: 'NONE' },
            { URI: `s3://${bucketName}/${artifactPrefix}/validator.py`, Unarchive: 'NONE' },
            { URI: `s3://${bucketName}/${artifactPrefix}/buffer.py`, Unarchive: 'NONE' },
            { URI: `s3://${bucketName}/${artifactPrefix}/requirements.txt`, Unarchive: 'NONE' },
            { URI: `s3://${bucketName}/${artifactPrefix}/crac_units.yaml`, Unarchive: 'NONE' },
            { URI: `s3://${bucketName}/${artifactPrefix}/anomaly_scenarios.yaml`, Unarchive: 'NONE' },
          ],
        },
      ],
    };

    const publisherComponent = new greengrassv2.CfnComponentVersion(
      this,
      'PublisherComponentVersion',
      {
        inlineRecipe: JSON.stringify(componentRecipe),
      },
    );

    // Ensure S3 artifacts are uploaded before the component version is registered
    publisherComponent.node.addDependency(publisherDeployment);
    publisherComponent.node.addDependency(configDeployment);

    // -------------------------------------------------------------------------
    // 7. Greengrass Deployment
    //    Targets the core device Thing directly.
    //    When the nucleus starts and connects, it picks up this deployment
    //    and installs + runs com.project-aegis.publisher automatically.
    //
    //    NOTE: The deployment will enter FAILED state until the core device
    //    registers (i.e. the nucleus first connects). This is expected.
    //    Once the device registers, the deployment transitions to COMPLETED.
    // -------------------------------------------------------------------------

    const deployment = new greengrassv2.CfnDeployment(this, 'EdgeDeployment', {
      targetArn: coreThingArn,
      deploymentName: 'project-aegis-edge-deployment',
      components: {
        'com.project-aegis.publisher': {
          componentVersion: '1.0.0',
          configurationUpdate: {
            merge: JSON.stringify({ publishIntervalSeconds: '60' }),
          },
        },
      },
    });

    deployment.node.addDependency(publisherComponent);
    deployment.node.addDependency(coreThing);

    // -------------------------------------------------------------------------
    // Outputs
    // -------------------------------------------------------------------------

    new cdk.CfnOutput(this, 'CoreThingName', {
      value: this.coreThingName,
      description: 'Greengrass core device Thing name',
    });

    new cdk.CfnOutput(this, 'TokenExchangeRoleAliasName', {
      value: this.tokenExchangeRoleAlias,
      description: 'IoT role alias for Greengrass token exchange',
    });

    new cdk.CfnOutput(this, 'ArtifactBucketName', {
      value: artifactBucket.bucketName,
      description: 'S3 bucket for Greengrass component artifacts',
    });

    new cdk.CfnOutput(this, 'GreengrassCorePolicyName', {
      value: 'project-aegis-greengrass-core-policy',
      description:
        'IoT policy to attach to the core device certificate (see edge/scripts/provision-greengrass.sh)',
    });

    new cdk.CfnOutput(this, 'ProvisioningCommand', {
      value: 'cd edge && bash scripts/provision-greengrass.sh',
      description: 'Run this after cdk deploy to provision the core device certificate',
    });
  }
}
