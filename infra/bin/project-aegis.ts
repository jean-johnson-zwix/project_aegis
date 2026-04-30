#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { SiteWiseStack } from '../lib/sitewise-stack';
import { IotStack } from '../lib/iot-stack';
import { GreengrassStack } from '../lib/greengrass-stack';
import { DiagnosticStack } from '../lib/diagnostic-stack';

const app = new cdk.App();

const env: cdk.Environment = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_DEFAULT_REGION ?? 'us-west-2',
};

const siteWiseStack = new SiteWiseStack(app, 'ProjectAegisSiteWiseStack', {
  env,
  description: 'Project Aegis — SiteWise asset models, assets, and hierarchy',
});

new IotStack(app, 'ProjectAegisIotStack', {
  env,
  description: 'Project Aegis — IoT Things and routing rule to SiteWise',
  siteId: siteWiseStack.siteId,
});

new GreengrassStack(app, 'ProjectAegisGreengrassStack', {
  env,
  description:
    'Project Aegis — Greengrass V2 core device, token exchange role, component version, and deployment',
});

new DiagnosticStack(app, 'ProjectAegisDiagnosticStack', {
  env,
  description:
    'Project Aegis — Alarm evaluator, Nova Lite diagnostic Lambda, DynamoDB diagnostics table, EventBridge rule',
});
