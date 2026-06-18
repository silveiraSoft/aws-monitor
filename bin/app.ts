#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { MonitorAgentStack } from '../lib/monitor-agent-stack';
import { ChatFrontendStack } from '../lib/chat-frontend-stack';
import { environments } from '../config/environments';

const app = new cdk.App();

// Ambiente se pasa como: cdk deploy --context env=dev|prod
// Si no se especifica, usa 'dev' por defecto.
const deployEnv = (app.node.tryGetContext('env') as string) ?? 'dev';
const envConfig = environments[deployEnv];

if (!envConfig) {
  throw new Error(
    `Ambiente desconocido: "${deployEnv}". Valores válidos: ${Object.keys(environments).join(', ')}.\n` +
    'Ejemplo: npm run deploy:dev  o  npm run deploy:prod'
  );
}

console.log(`Deploying to environment: ${deployEnv} (account: ${envConfig.account}, region: ${envConfig.region})`);

const env = {
  account: envConfig.account,
  region:  envConfig.region,
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
