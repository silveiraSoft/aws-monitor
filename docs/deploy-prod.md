# Guía de Despliegue a Producción — aws-monitor

> Fecha de creación: 2026-06-18  
> Ambiente dev: cuenta `369595298303` · perfil `3htpusa-monitor`  
> Ambiente prod: cuenta `PROD_ACCOUNT_ID` (pendiente) · perfil `aws-monitor-prod`

---

## Contexto del sistema multi-ambiente

El proyecto maneja dos ambientes via scripts npm y un archivo de configuración centralizado:

| Archivo | Qué hace |
|---------|----------|
| `config/environments.ts` | Define account ID, región y perfil por ambiente |
| `package.json` (scripts) | `deploy:dev` / `deploy:prod` / `destroy:dev` / `destroy:prod` |
| `bin/app.ts` | Lee el contexto `--context env=dev\|prod` y aplica la config |

---

## Pasos para el primer deploy a PROD

### 1. Crear usuario IAM en la cuenta de producción

En AWS Console de la **cuenta PROD**:

1. IAM → Users → **Create user** → nombre: `aws-monitor-deploy`
2. Attach policies directamente:
   - `AWSCloudFormationFullAccess`
   - `AmazonBedrockFullAccess`
   - `AWSLambda_FullAccess`
   - `AmazonAPIGatewayAdministrator`
   - `AmazonS3FullAccess`
   - `CloudWatchFullAccess`
   - `AmazonSSMFullAccess`
   - `IAMFullAccess` *(necesario para que CDK cree roles)*
   - `CloudFrontFullAccess`
3. Security credentials → **Create access key** → Application running outside AWS
4. Guardar el `Access Key ID` y `Secret Access Key`

### 2. Agregar el perfil en tu máquina Windows

Edita `C:\Users\adalb\.aws\credentials` y agrega al final:

```ini
[aws-monitor-prod]
aws_access_key_id     = AKIA...TU_KEY_PROD
aws_secret_access_key = TU_SECRET_PROD
region                = us-east-1
```

### 3. Actualizar el account ID en `config/environments.ts`

```typescript
prod: {
  account: '111122223333',   // <-- reemplazar con el account ID real de producción
  region:  'us-east-1',
  profile: 'aws-monitor-prod',
},
```

El account ID de 12 dígitos aparece en AWS Console → esquina superior derecha, bajo el nombre de la cuenta.

### 4. Validar acceso a la cuenta PROD

```bash
python validate_aws_access.py
```

Cuando pida credenciales, ingresar las de la cuenta PROD. Debe dar **17/19 PASS** (los 2 WARN de IAM SimulatePrincipalPolicy son no bloqueantes).

### 5. Habilitar el modelo Bedrock en la cuenta PROD (solo una vez)

AWS Console (cuenta PROD) → Amazon Bedrock → **Model access** (región us-east-1):

- Buscar **Claude Haiku** → seleccionar **Anthropic Claude Haiku 4.5**
- Click **Request model access** → confirmar

⚠️ Sin este paso el deploy funciona pero el agente falla al intentar responder.

### 6. Bootstrap CDK en la cuenta PROD (solo la primera vez)

```bash
npx cdk bootstrap aws://PROD_ACCOUNT_ID/us-east-1 --profile aws-monitor-prod
```

Reemplaza `PROD_ACCOUNT_ID` con el account ID real. Esto crea el stack `CDKToolkit` en la cuenta — solo se ejecuta una vez por cuenta/región.

### 7. Deploy a producción

```bash
npm run deploy:prod
```

Al terminar imprime las URLs del ambiente de producción:
```
AwsMonitorFrontendStack.ChatUiUrl = https://xxxxx.cloudfront.net
AwsMonitorFrontendStack.ApiUrl    = https://xxxxx.execute-api.us-east-1.amazonaws.com/prod/
```

### 8. Obtener la API Key de producción

```bash
aws apigateway get-api-keys --include-values --profile aws-monitor-prod --region us-east-1 --query "items[?name=='aws-monitor-api-key'].value" --output text
```

Esta key va en el panel de configuración del chat (botón ⚙️ en la UI).

---

## Comandos de referencia por ambiente

| Acción | Dev | Prod |
|--------|-----|------|
| Deploy | `npm run deploy` | `npm run deploy:prod` |
| Destroy | `npm run destroy` | `npm run destroy:prod` |
| Validar acceso | `python validate_aws_access.py` | igual, con creds PROD |
| Bootstrap | `npx cdk bootstrap aws://369595298303/us-east-1 --profile 3htpusa-monitor` | `npx cdk bootstrap aws://PROD_ACCOUNT_ID/us-east-1 --profile aws-monitor-prod` |

---

## Diferencias entre DEV y PROD a tener en cuenta

| Ítem | DEV (actual) | PROD (recomendado) |
|------|-------------|-------------------|
| Auth UI | API Key via panel ⚙️ | Cognito User Pool + JWT Authorizer |
| Rate limit | 1000 req/día | Revisar Usage Plan según uso esperado |
| CORS | ALL_ORIGINS | Restringir al dominio CloudFront de prod |
| Modelo LLM | Claude Haiku 4.5 | Considerar Claude Sonnet para mejor calidad |
| Logs | 30 días | Ajustar según política de retención de la empresa |
| CW Agent | Auto-provisioned via SSM | Mismo mecanismo — validar que instancias tienen SSM Agent |

---

## Rotación de API Key (cada 90 días en PROD)

```bash
# 1. Crear nueva key en API Gateway
aws apigateway create-api-key --name aws-monitor-api-key-v2 --enabled --profile aws-monitor-prod

# 2. Asociar al Usage Plan existente
aws apigateway create-usage-plan-key \
  --usage-plan-id <PLAN_ID> \
  --key-id <NUEVA_KEY_ID> \
  --key-type API_KEY \
  --profile aws-monitor-prod

# 3. Actualizar la key en la UI del chat (botón ⚙️)

# 4. Eliminar la key anterior
aws apigateway delete-api-key --api-key <KEY_ID_ANTERIOR> --profile aws-monitor-prod
```

---

## Limpieza de producción (si aplica)

```bash
# Elimina todos los recursos AWS de producción creados por CDK
npm run destroy:prod

# Para limpieza total (incluyendo bootstrap CDK de la cuenta PROD)
python cleanup_deploy.py     # adaptar para perfil prod si es necesario
```
