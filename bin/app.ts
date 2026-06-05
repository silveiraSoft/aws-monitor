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

new ChatFrontendStack(app, 'AwsMonitorFrontendStack', {
  env,
  agentId: agentStack.agentId,
  agentAliasId: agentStack.agentAliasId,
});
