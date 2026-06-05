# AWS Monitor Agent

Chat-based AWS health monitoring powered by **Amazon Bedrock Agents** + **Claude 3.5 Haiku**.  
Empresa: **3htp** · Región: **us-east-1** · Cuenta: `369595298303`

## Architecture

```
Browser (CloudFront/S3)
    │
    └─► API Gateway  POST /chat
            │
            └─► Lambda (aws-monitor-chat-proxy)
                    │
                    └─► Bedrock Agent (us.anthropic.claude-3-5-haiku-20241022-v1:0)
                                │
                                └─► Action Group Lambda (aws-monitor-agent-actions)
                                        ├─ EC2 DescribeInstances
                                        ├─ Lambda ListFunctions
                                        └─ CloudWatch DescribeAlarms / GetMetricStatistics
```

---

## Prerequisites

| Tool | Version | Notes |
|------|---------|-------|
| Node.js | ≥ 18 | |
| AWS CDK | ≥ 2.150 | `npm install -g aws-cdk` |
| Python | 3.12 | Usar `python` o `py` en Windows (no `python3`) |
| boto3 | cualquiera | `pip install boto3` |
| AWS CLI | configurado | IAM user con permisos de deploy en us-east-1 |

### Required IAM permissions for deployment

- `cloudformation:*`
- `iam:*` (crear roles para el agente y Lambda)
- `iam:GetRole` ← **requerido para CDK bootstrap**
- `lambda:*`
- `s3:*`
- `apigateway:*`
- `cloudfront:*`
- `bedrock:CreateAgent`, `bedrock:CreateAgentAlias`, `bedrock:PrepareAgent`

> `AdministratorAccess` funciona para un primer deploy en entorno de desarrollo.

### Enable Claude 3.5 Haiku in Bedrock

1. AWS Console → **Amazon Bedrock** → **Model access** (región: **us-east-1**)
2. Click **Manage model access** → habilitar **Anthropic Claude 3.5 Haiku**
3. **Save changes** — toma ~2 minutos

> En us-east-1 se usa el inference profile `us.anthropic.claude-3-5-haiku-20241022-v1:0`.  
> Si la cuenta estuvo inactiva +30 días, AWS puede marcar el modelo como Legacy — volver a habilitarlo.

---

## Deploy

```bash
# 1. Validar credenciales y permisos (debe dar 17/17 PASS antes de continuar)
python validate_aws_access.py

# 2. Instalar dependencias Node.js
npm install

# 3. Bootstrap CDK en us-east-1 (primera vez por cuenta/región — requiere iam:GetRole)
npx cdk bootstrap aws://369595298303/us-east-1

# 4. Desplegar ambos stacks
npm run deploy
```

Al terminar, CDK imprime las URLs:
```
AwsMonitorFrontendStack.ChatUiUrl = https://xxxxx.cloudfront.net
AwsMonitorFrontendStack.ApiUrl    = https://xxxxx.execute-api.us-east-1.amazonaws.com/prod/
```

### Post-deploy: obtener API Key

```bash
aws apigateway get-api-key --api-key <ApiKeyId> --include-value --region us-east-1
```

El `ApiKeyId` aparece en los Outputs del stack al terminar el deploy.

---

## Stacks

### `AwsMonitorAgentStack`

| Resource | Nombre en AWS |
|----------|--------------|
| Lambda action handler | `aws-monitor-agent-actions` |
| S3 schema bucket | `aws-monitor-schema-369595298303-us-east-1` |
| IAM Role | `AmazonBedrockExecutionRoleForAgents_AwsMonitor` |
| Bedrock Agent | `aws-monitor-agent` |
| Agent Alias | `live` |

### `AwsMonitorFrontendStack`

| Resource | Nombre en AWS |
|----------|--------------|
| Lambda proxy | `aws-monitor-chat-proxy` |
| API Gateway | `aws-monitor-chat-api` — `POST /chat` + CORS |
| S3 UI bucket | `aws-monitor-chat-ui-369595298303-us-east-1` |
| CloudFront | HTTPS, OAI, 403/404 → index.html |

---

## Agent actions

| Action | Parameters | Description |
|--------|-----------|-------------|
| `get_overall_health` | — | EC2 states + alarm count + Lambda count |
| `get_ec2_health` | `state` (optional) | All instances or filter by state |
| `get_lambda_health` | `prefix`, `hours` | Error rates, invocations, throttles |
| `get_cloudwatch_alarms` | `state` | Alarms filtered by ALARM/OK/ALL |

---

## Costs — us-east-1

| Resource | Cost at rest | Cost per use |
|----------|-------------|--------------|
| CloudFront | ~$0/mo | $0.0085 / 10K requests |
| API Gateway | ~$0/mo | $3.50 / million calls |
| Lambda (2) | ~$0/mo | $0.20 / million invocations |
| S3 (2 buckets) | < $0.01/mo | $0.023/GB stored |
| Bedrock Agent | ~$0/mo | ~$0.0008 / 1K tokens (Haiku) |
| CDKToolkit (bootstrap) | **$0/mo** | — (IAM + SSM are free) |
| **TOTAL AT REST** | **< $0.02/mo** | |

> **Key insight:** Cost risk comes from **usage** (Bedrock tokens, API calls), not from resource existence.  
> The bootstrap stack itself costs $0/month.

---

## Cleanup scripts

### Daily cleanup (recommended during development)

Eliminates all application resources to avoid accumulated usage costs:

```bash
python cleanup_deploy.py
```

Eliminates:
- Lambda functions (both)
- API Gateway
- CloudFront distribution
- S3 buckets (emptied + deleted)
- Bedrock Agent + alias

### Dry run (preview without changes)

```bash
python cleanup_deploy.py --dry-run
```

### Full environment cleanup

Only needed if you want a completely clean AWS account in us-east-1:

```bash
python cleanup_deploy.py        # remove application stacks first
python cleanup_bootstrap.py     # then remove CDKToolkit
```

> After cleanup, to redeploy: `python validate_aws_access.py && npm run deploy`

### Notes on `cdk bootstrap` and duplicate resources

- `cdk bootstrap` is **idempotent** — running it multiple times does UPDATE, not CREATE
- If bootstrap fails mid-way, CloudFormation **auto-rolls back** — no orphaned resources
- The CDKToolkit stack costs **$0/month** — no need to delete it between development sessions
- Deleting the bootstrap stack requires re-running `cdk bootstrap` before the next deploy

---

## Tear down (CDK native)

```bash
npm run destroy
```

> S3 buckets have `RemovalPolicy.DESTROY` + `autoDeleteObjects`.  
> Equivalent to `cleanup_deploy.py` but without the interactive confirmation and cost summary.

---

## Testing

```bash
# Run all 121 tests (unit + integration + e2e + TypeScript compilation check)
python run_tests.py all

# Run by suite
python run_tests.py unit
python run_tests.py integration   # includes tsc --noEmit check
python run_tests.py e2e
```

Tests require no external dependencies — run with Python stdlib only (no pip install needed).

---

## Extend

- **Add more services**: Add `def get_rds_health(...)` in `lambda/monitor-actions/index.py`, add the path to `lib/monitor-openapi.json`, redeploy.
- **Change model**: Edit `foundationModel` in `lib/monitor-agent-stack.ts` → use `us.` prefix for inference profiles in us-east-1.
- **Multi-region**: Add `region` parameter to OpenAPI schema, create boto3 clients per region, return aggregated data.
- **Production auth**: Replace API Key with Cognito User Pool + JWT Authorizer.
