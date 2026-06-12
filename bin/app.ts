#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { MonitorAgentStack } from '../lib/monitor-agent-stack';
import { ChatFrontendStack } from '../lib/chat-frontend-stack';

const app = new cdk.App();

const account = process.env.CDK_DEFAULT_ACCOUNT;
if (!account) {
  throw new Error(
    'CDK_DEFAULT_ACCOUNT is not set. Run: export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text) ' +
    'and ensure your AWS credentials are configured before running cdk synth/deploy.'
  );
}

const env = {
  account,
  region: 'us-east-1',
};

const agentStack = new MonitorAgentStack(app, 'AwsMonitorAgentStack', { env });

// agentAliasId is NOT passed — ChatFrontendStack imports it via Fn.importValue('AwsMonitorAgentAliasId').
// This prevents CDK from auto-generating a cross-stack export tied to the alias resource
// logical ID, which would break deploys whenever the alias resource is replaced.
const frontendStack = new ChatFrontendStack(app, 'AwsMonitorFrontendStack', {
  env,
  agentId: agentStack.agentId,
});
frontendStack.addDependency(agentStack);
