import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as apigw from 'aws-cdk-lib/aws-apigateway';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import { Construct } from 'constructs';
import path from 'node:path';
import * as fs from 'node:fs';

interface ChatFrontendProps extends cdk.StackProps {
  agentId: string;
  // agentAliasId is NOT passed as a prop. It is imported by name from the AgentStack
  // export 'AwsMonitorAgentAliasId'. This avoids a CDK auto-generated cross-stack
  // export whose name changes whenever the alias resource logical ID changes, which
  // would cause CloudFormation to refuse the deploy (export in use by this stack).
}

export class ChatFrontendStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: ChatFrontendProps) {
    super(scope, id, props);

    const { agentId } = props;

    // ── 1. Lambda: Chat proxy to Bedrock Agent ────────────────────────────
    const chatLambdaRole = new iam.Role(this, 'ChatLambdaRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
      ],
      inlinePolicies: {
        BedrockInvoke: new iam.PolicyDocument({
          statements: [
            new iam.PolicyStatement({
              actions: ['bedrock:InvokeAgent'],
              // Alias ID is read at runtime from SSM — use wildcard here
              resources: [
                `arn:aws:bedrock:${this.region}:${this.account}:agent/${agentId}`,
                `arn:aws:bedrock:${this.region}:${this.account}:agent-alias/${agentId}/*`,
              ],
            }),
            new iam.PolicyStatement({
              actions: ['ssm:GetParameter'],
              // Allow reading the alias ID stored by AliasManagerFn after each deploy
              resources: [`arn:aws:ssm:${this.region}:${this.account}:parameter/3htp/monitor/agent-alias-id`],
            }),
          ],
        }),
      },
    });

    // Security: retain logs 30 days only (cost + data hygiene)
    const chatLogGroup = new logs.LogGroup(this, 'ChatProxyLogGroup', {
      logGroupName: '/aws/lambda/aws-monitor-chat-proxy',
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const chatLambda = new lambda.Function(this, 'ChatProxyLambda', {
      functionName: 'aws-monitor-chat-proxy',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromInline(this.chatProxyCode(agentId)),
      role: chatLambdaRole,
      timeout: cdk.Duration.seconds(29),
      memorySize: 256,
      description: 'Proxy between chat UI and Bedrock Agent',
      environment: {
        AGENT_ID: agentId,
        REGION: this.region,
        // Forces Lambda cold start on every deploy so it reads the new alias
        // from SSM immediately — no 5-minute cache wait after npm run deploy.
        DEPLOY_TIME: Date.now().toString(),
      },
      logGroup: chatLogGroup,
    });

    // ── 2. API Gateway ────────────────────────────────────────────────────
    const api = new apigw.RestApi(this, 'ChatApi', {
      restApiName: 'aws-monitor-chat-api',
      description: 'Chat API for AWS Monitor Agent',
      defaultCorsPreflightOptions: {
        allowOrigins: apigw.Cors.ALL_ORIGINS,
        allowMethods: ['POST', 'OPTIONS'],
        allowHeaders: ['Content-Type', 'X-Session-Id', 'x-api-key'],
      },
      deployOptions: {
        stageName: 'prod',
        loggingLevel: apigw.MethodLoggingLevel.ERROR,
        // Security: throttle at stage level as a hard ceiling
        throttlingRateLimit: 20,
        throttlingBurstLimit: 10,
      },
      // Security: API Key required — protects against unauthorized invocations
      // and controls Bedrock costs in case of abuse.
      apiKeySourceType: apigw.ApiKeySourceType.HEADER,
    });

    const chatResource = api.root.addResource('chat');
    chatResource.addMethod('POST', new apigw.LambdaIntegration(chatLambda, {
      proxy: true,
      timeout: cdk.Duration.seconds(29),
    }), {
      // Require API Key on this method
      apiKeyRequired: true,
    });

    // Security: API Key + Usage Plan with rate limiting
    const apiKey = api.addApiKey('MonitorApiKey', {
      apiKeyName: 'aws-monitor-chat-key',
      description: 'API Key for AWS Monitor chat UI — rotate every 90 days',
    });

    const usagePlan = api.addUsagePlan('MonitorUsagePlan', {
      name: 'aws-monitor-usage-plan',
      description: 'Rate limiting: 10 req/s burst, 5 req/s sustained, 1000/day',
      throttle: {
        rateLimit: 5,    // sustained requests/second
        burstLimit: 10,  // max concurrent burst
      },
      quota: {
        limit: 1000,                        // max requests per day
        period: apigw.Period.DAY,
      },
    });
    usagePlan.addApiStage({ stage: api.deploymentStage });
    usagePlan.addApiKey(apiKey);

    // Output the API Key ID so the developer can retrieve the value from AWS Console
    new cdk.CfnOutput(this, 'ApiKeyId', {
      value: apiKey.keyId,
      description: 'API Key ID — retrieve value: aws apigateway get-api-key --api-key <id> --include-value',
    });

    // ── 3. S3 + CloudFront for Chat UI ────────────────────────────────────
    const siteBucket = new s3.Bucket(this, 'ChatUiBucket', {
      bucketName: `aws-monitor-chat-ui-${this.account}-${this.region}`,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
    });

    const oai = new cloudfront.OriginAccessIdentity(this, 'OAI', {
      comment: 'OAI for aws-monitor chat UI',
    });

    siteBucket.grantRead(oai);

    // Security: response headers policy — adds security headers to all CloudFront responses
    const securityHeadersPolicy = new cloudfront.ResponseHeadersPolicy(this, 'SecurityHeaders', {
      responseHeadersPolicyName: 'aws-monitor-security-headers',
      securityHeadersBehavior: {
        contentSecurityPolicy: {
          contentSecurityPolicy: "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; connect-src 'self' https://*.execute-api.us-east-1.amazonaws.com; frame-ancestors 'none';",
          override: true,
        },
        frameOptions: {
          frameOption: cloudfront.HeadersFrameOption.DENY,
          override: true,
        },
        referrerPolicy: {
          referrerPolicy: cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
          override: true,
        },
        strictTransportSecurity: {
          accessControlMaxAge: cdk.Duration.seconds(31536000),
          includeSubdomains: true,
          override: true,
        },
        xssProtection: {
          protection: true,
          modeBlock: true,
          override: true,
        },
        contentTypeOptions: { override: true },
      },
    });

    const distribution = new cloudfront.Distribution(this, 'ChatDistribution', {
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessIdentity(siteBucket, {
          originAccessIdentity: oai,
        }),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        responseHeadersPolicy: securityHeadersPolicy,
      },
      defaultRootObject: 'index.html',
      errorResponses: [
        { httpStatus: 403, responseHttpStatus: 200, responsePagePath: '/index.html' },
        { httpStatus: 404, responseHttpStatus: 200, responsePagePath: '/index.html' },
      ],
    });

    // Write the frontend HTML with the API URL baked in
    const frontendHtml = this.buildFrontendHtml(api.url);

    new s3deploy.BucketDeployment(this, 'UiDeployment', {
      sources: [s3deploy.Source.data('index.html', frontendHtml)],
      destinationBucket: siteBucket,
      distribution,
      distributionPaths: ['/*'],
    });

    // ── Outputs ───────────────────────────────────────────────────────────
    new cdk.CfnOutput(this, 'ChatUiUrl', {
      value: `https://${distribution.distributionDomainName}`,
      description: 'Chat UI URL',
    });

    new cdk.CfnOutput(this, 'ApiUrl', {
      value: api.url,
      description: 'API Gateway URL',
    });
  }

  private chatProxyCode(agentId: string): string {
    return `
import json
import boto3
import uuid
import os
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AGENT_ID = os.environ.get('AGENT_ID', '${agentId}')
REGION = os.environ.get('REGION', 'us-east-1')

bedrock_agent = boto3.client('bedrock-agent-runtime', region_name=REGION)
_ssm = boto3.client('ssm', region_name=REGION)

# Cache alias ID per execution environment with 5-min TTL.
# AliasManagerFn writes /3htp/monitor/agent-alias-id to SSM after each deploy.
_alias_id_cache = None
_alias_id_ts = 0.0

def get_alias_id():
    import time
    global _alias_id_cache, _alias_id_ts
    now = time.time()
    if _alias_id_cache and (now - _alias_id_ts) < 300:
        return _alias_id_cache
    try:
        resp = _ssm.get_parameter(Name='/3htp/monitor/agent-alias-id')
        _alias_id_cache = resp['Parameter']['Value']
        _alias_id_ts = now
        logger.info("Loaded alias ID from SSM: %s", _alias_id_cache)
    except Exception as e:
        logger.warning("SSM get_alias_id failed: %s", e)
        if not _alias_id_cache:
            _alias_id_cache = os.environ.get('AGENT_ALIAS_ID', '')
    return _alias_id_cache

CORS_HEADERS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': 'Content-Type,X-Session-Id',
    'Access-Control-Allow-Methods': 'POST,OPTIONS',
    'Content-Type': 'application/json',
}

def handler(event, context):
    if event.get('httpMethod') == 'OPTIONS':
        return {'statusCode': 200, 'headers': CORS_HEADERS, 'body': ''}

    try:
        body = json.loads(event.get('body', '{}'))
        message = body.get('message', '').strip()
        session_id = (
            event.get('headers', {}).get('X-Session-Id') or
            body.get('sessionId') or
            str(uuid.uuid4())
        )

        if not message:
            return {
                'statusCode': 400,
                'headers': CORS_HEADERS,
                'body': json.dumps({'error': 'message is required'}),
            }

        logger.info("Agent invoke: session=%s msg=%s", session_id, message[:100])

        resp = bedrock_agent.invoke_agent(
            agentId=AGENT_ID,
            agentAliasId=get_alias_id(),
            sessionId=session_id,
            inputText=message,
        )

        # Stream the response chunks
        full_response = ''
        for event_chunk in resp['completion']:
            if 'chunk' in event_chunk:
                chunk_data = event_chunk['chunk']
                if 'bytes' in chunk_data:
                    full_response += chunk_data['bytes'].decode('utf-8')

        return {
            'statusCode': 200,
            'headers': CORS_HEADERS,
            'body': json.dumps({
                'response': full_response,
                'sessionId': session_id,
            }),
        }

    except Exception as e:
        logger.exception("Chat proxy error")
        return {
            'statusCode': 500,
            'headers': CORS_HEADERS,
            'body': json.dumps({'error': str(e)}),
        }
`;
  }

  private buildFrontendHtml(_apiUrl: string): string {
    // HTML is loaded from frontend.html to avoid TypeScript template literal escaping issues.
    // The API URL is configured at runtime via the settings panel in the UI.
    return fs.readFileSync(path.join(__dirname, 'frontend.html'), 'utf-8');
  }
}