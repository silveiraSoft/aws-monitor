# AWS Monitor Agent — Explicación Técnica de Arquitectura

> **Audiencia:** Desarrolladores y arquitectos que necesitan entender y explicar la solución
> **Nivel:** Técnico intermedio-avanzado
> **Última actualización:** 2026-06-11 — SSM Inventory integrado (7 acciones), Claude Haiku 4.5, 281 tests pasando, 14 checks validate_aws_access.py
> **Complementa:** `aws-monitor-presentacion.md` (versión de negocio) · `aws-monitor-dev-guide.pptx` (visual)

---

## Visión general del flujo

```
Usuario (browser)
     │
     │ HTTPS POST /chat  +  x-api-key
     ▼
┌─────────────────────────────────────────────────┐
│  CloudFront + S3                                │
│  HTML/JS estático — CDN global                  │
└─────────────────────────┬───────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────┐
│  API Gateway REST                               │
│  • API Key validation                           │
│  • Rate limiting (5 req/s, 1000/día)            │
│  • CORS                                         │
└─────────────────────────┬───────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────┐
│  Lambda Proxy  (aws-monitor-chat-proxy)         │
│  Python 3.12 — código inline en CDK             │
│  • Genera sessionId                             │
│  • Llama bedrock-agent-runtime.invoke_agent()   │
│  • Consume stream de respuesta                  │
└─────────────────────────┬───────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────┐
│  Amazon Bedrock Agent  (aws-monitor-agent)      │
│  alias: live                                    │
│  ┌──────────────────────────────────────────┐   │
│  │  Claude Haiku 4.5                        │   │
│  │  us.anthropic.claude-haiku-4-5-20251001  │   │
│  │  • Razonamiento ReAct                    │   │
│  │  • Selección automática de herramienta   │   │
│  │  • Síntesis de respuesta natural         │   │
│  └──────────────────────────────────────────┘   │
│  + OpenAPI schema (desde S3)                    │
│  + System prompt con 6 restricciones            │
└─────────────────────────┬───────────────────────┘
                          │  Invoca acción
                          ▼
┌─────────────────────────────────────────────────┐
│  Lambda Actions  (aws-monitor-agent-actions)    │
│  Python 3.12 — 7 acciones de monitoreo          │
│  Todas aceptan parámetro opcional "region"      │
│  • get_overall_health  (region)                 │
│  • get_ec2_health      (state, region)          │
│  • get_lambda_health   (prefix, hours, region)  │
│  • get_cloudwatch_alarms (state, region)        │
│  • get_logs_analysis   (log_group, region)      │
│  • get_xray_traces     (hours, region)          │
│  • get_ssm_inventory   (instance_id, type, region)│
└──────┬──────┬──────┬──────┬──────┬──────┬──────┘
       │      │      │      │      │      │
       ▼      ▼      ▼      ▼      ▼      ▼
  AWS EC2   CW    Lambda   CW    X-Ray  SSM
  Describe Alarms  List   Logs   Trace  Inv.
  Inst.    +Met.  +Metr. Insig. Summ.  (Fleet)
  (any region — boto3 per-request client)
```

---

## Qué puede responder el agente — capacidad actual

Esta tabla refleja el estado tras la implementación de `get_logs_analysis`, `get_xray_traces` y `get_ssm_inventory`:

| Pregunta del usuario | Antes (4 acciones) | Ahora (7 acciones) |
|---|---|---|
| ¿Cuántos errores tuve? | ✅ | ✅ |
| ¿Qué dice el mensaje de error? | ❌ | ✅ `get_logs_analysis` |
| ¿Desde cuándo empezó el problema? | Parcial (métricas) | ✅ exacto, en logs |
| ¿En qué servicio del flujo falla? | ❌ | ✅ `get_xray_traces` |
| ¿El problema es en mi código o en una dependencia? | ❌ | Parcial → ✅ |
| ¿Cuánto tarda cada paso de la request? | ❌ | ✅ `duration_s` por trace |
| ¿Qué errores son los más frecuentes? | ❌ | ✅ eventos con `@message` |
| ¿Qué SO tiene la instancia? | ❌ | ✅ `get_ssm_inventory` |
| ¿Qué apps y versiones están instaladas? | ❌ | ✅ `get_ssm_inventory` |
| ¿Qué configuración de red tiene la instancia? | ❌ | ✅ `get_ssm_inventory` |

**Ejemplo concreto de salto de capacidad:**

```
# Antes:
Usuario: "La función payment-processor tiene 15% de errores, ¿qué pasa?"
Agente:  "Confirmo: 15% error rate en las últimas 2 horas."   ← se detenía aquí

# Ahora, con Logs Insights:
Agente:  "Encontré 47 errores. El mensaje más frecuente es:
          'Connection timeout to RDS endpoint db.prod.internal:5432'
          Ocurre desde las 14:23. Posible causa: la base de datos
          no acepta nuevas conexiones."

# Ahora, con X-Ray (microservicios):
Agente:  "El trace muestra: checkout-service → payment-processor → fraud-detector.
          El 94% de los errores ocurren en fraud-detector (timeout 3s).
          checkout-service y payment-processor están sanos."
```

---

## Capa 1 — Frontend: CloudFront + S3

### Qué es

Un archivo `index.html` estático con JavaScript puro alojado en un bucket S3. CloudFront actúa como CDN frente a ese bucket.

### Cómo funciona

- **S3 sin acceso público directo** — el bucket tiene `BlockPublicAccess` activado. El acceso es exclusivamente a través de CloudFront mediante una **Origin Access Identity (OAI)**. Cualquier intento de acceso directo al bucket retorna `403 Forbidden`.
- **CloudFront termina el TLS** — el bucket sirve HTTP internamente, pero el usuario siempre accede por HTTPS.
- **Redirección SPA** — cualquier ruta que no exista redirige a `index.html` con código 200. Configurado en los `errorResponses` del CDK.
- **Sin frameworks** — HTML/JS puro, sin React ni dependencias. Reduce superficie de ataque y elimina build steps.

### Qué hace el frontend

1. Captura el texto del usuario en un `<input>`
2. Hace `fetch('POST /chat', { body: mensaje, headers: { 'x-api-key': KEY } })`
3. Espera la respuesta JSON `{ "response": "texto" }`
4. Renderiza el texto en el chat

### Por qué este diseño

El frontend no tiene ninguna credencial AWS ni lógica de negocio. Es solo una interfaz. Toda la inteligencia y el acceso a AWS está en el backend. Si el HTML fuera comprometido, el atacante no ganaría acceso a AWS.

---

## Capa 2 — API Gateway REST

### Qué es

Un endpoint HTTPS gestionado por AWS. Expone exactamente **un método**: `POST /chat`.

### Controles de seguridad implementados

| Control | Componente | Configuración | Efecto |
|---|---|---|---|
| API Key | API Gateway | `apiKeyRequired: true` | Sin header `x-api-key` → `403` antes de llegar al código |
| Rate limit sostenido | API Gateway | 5 req/s | Requests adicionales → `429 Too Many Requests` |
| Burst | API Gateway | 10 req | Permite picos cortos |
| Cuota diaria | API Gateway | 1,000 req/día | Protege contra abuso prolongado |
| Stage throttle | API Gateway | 20 req/s | Segunda línea de defensa |
| CORS | API Gateway | `ALL_ORIGINS` (POC) | ⚠️ Pendiente restringir a dominio CloudFront post-deploy |
| Security headers | CloudFront | `ResponseHeadersPolicy` | CSP, X-Frame-Options DENY, HSTS 1 año, XSS-Protection, Referrer-Policy |
| S3 buckets | S3 | `BlockPublicAccess.BLOCK_ALL` | Acceso solo vía OAI (CloudFront) o service principal |
| Session ID | Frontend | `crypto.randomUUID()` | ID criptográficamente seguro (con fallback a `Math.random()`) |
| Validación inputs | Lambda Actions | regex + límites de longitud | Rechaza `log_group` con metacaracteres, queries >2048 chars → HTTP 400 |
| System prompt | Bedrock Agent | 6 restricciones explícitas | No revela configs internas, no ejecuta acciones destructivas |
| IAM | Roles | Mínimo privilegio (solo lectura) | Lambda actions solo puede leer EC2/Lambda/CloudWatch/Logs/X-Ray |

### Por qué REST API y no HTTP API

HTTP API es ~70% más barato, pero **REST API tiene soporte nativo de API Keys + Usage Plans**. Para producción con usuarios autenticados via Cognito, se puede migrar a HTTP API. El cambio es de ~30 minutos en el CDK.

### Cómo obtener el valor de la API Key post-deploy

```bash
aws apigateway get-api-key \
  --api-key <ApiKeyId output del CDK> \
  --include-value \
  --region us-east-1
```

---

## Capa 3 — Lambda Proxy (`aws-monitor-chat-proxy`)

### Qué es

Una función Lambda Python 3.12 definida como **código inline en el CDK** (el código Python está embebido directamente en `chat-frontend-stack.ts`, no en un archivo separado).

### Flujo interno

```python
# 1. Recibe el body del POST
body = json.loads(event["body"])
message = body["message"]

# 2. Genera sessionId para mantener contexto de conversación
session_id = str(uuid.uuid4())

# 3. Invoca el Bedrock Agent
response = bedrock_runtime.invoke_agent(
    agentId=AGENT_ID,           # Variable de entorno inyectada por CDK
    agentAliasId=AGENT_ALIAS_ID,
    sessionId=session_id,
    inputText=message
)

# 4. Consume el stream de eventos (EventStream)
completion = ""
for event in response["completion"]:
    if "chunk" in event:
        completion += event["chunk"]["bytes"].decode()

# 5. Retorna la respuesta
return { "statusCode": 200, "body": json.dumps({"response": completion}) }
```

### Por qué existe esta Lambda intermediaria

Bedrock no tiene un endpoint HTTP público con CORS. No se puede llamar Bedrock directamente desde el browser porque:
1. Requeriría exponer credenciales AWS en el cliente (crítico de seguridad)
2. No tiene soporte CORS nativo
3. La autenticación es via IAM Signature v4, no via API Key simple

Esta Lambda actúa como proxy seguro usando su **IAM Role** (`ChatLambdaRole`), que tiene el único permiso `bedrock:InvokeAgent`.

---

## Capa 4 — Amazon Bedrock Agent

### Qué es

El servicio de AWS que orquesta el agente de IA. No es una simple llamada a un LLM — es un **motor de razonamiento con bucle de acción (ReAct loop)**.

### El ciclo de razonamiento interno (ReAct)

```
┌──────────────────────────────────────────────────────┐
│  1. THINK: Claude recibe el mensaje + las herramientas│
│     disponibles del schema OpenAPI                   │
│     → "El usuario pregunta por salud general.         │
│        Debo llamar get_overall_health"               │
├──────────────────────────────────────────────────────┤
│  2. ACT: Bedrock invoca la Lambda de acciones        │
│     con el evento:                                   │
│     { "apiPath": "/get_overall_health" }             │
├──────────────────────────────────────────────────────┤
│  3. OBSERVE: Bedrock recibe el JSON de respuesta     │
│     y se lo pasa a Claude como contexto adicional    │
├──────────────────────────────────────────────────────┤
│  4. RESPOND: Claude genera la respuesta final        │
│     en lenguaje natural con los datos recibidos      │
└──────────────────────────────────────────────────────┘
```

Este ciclo puede repetirse: Claude puede decidir llamar a 2 o 3 herramientas antes de responder. Ejemplo: detecta Lambda con error_rate > 5% → automáticamente llama `get_logs_analysis` en su log group para buscar la causa raíz.

### El alias `live`

El alias permite **blue/green deployments**: se puede crear una nueva versión del agente, testearla, y cambiar `live` sin downtime ni cambios en el código del Lambda proxy.

### El schema OpenAPI en S3

Bedrock necesita saber qué herramientas existen. El archivo `lib/monitor-openapi.json` describe las 6 acciones con sus parámetros. CDK lo sube a S3 privado durante el deploy. Bedrock lo lee y lo incluye en el contexto del LLM.

### El system prompt

Define scope, herramientas disponibles, comportamiento multi-región y 6 restricciones de seguridad:
1. `OUT OF SCOPE` — solo EC2, Lambda, CloudWatch, Logs, X-Ray, SSM Inventory
2. `NO DESTRUCTIVE ACTIONS` — no sugiere terminar/eliminar/modificar recursos
3. `NO INTERNAL CONFIG DISCLOSURE` — no revela ARNs, account IDs ni env vars
4. `NO PROMPT INJECTION` — bloquea intentos de override del system prompt
5. `NO CREDENTIALS OR SECRETS` — nunca solicita ni maneja Access Keys
6. `NO CODE EXECUTION` — no genera comandos para ejecutar en nombre del usuario

**Comportamiento proactivo configurado en el system prompt:** cuando una Lambda tiene `error_rate_pct > 5%`, el agente llama automáticamente `get_logs_analysis` en su log group sin que el usuario lo pida.

---

## Capa 5 — Lambda Action Group (`aws-monitor-agent-actions`)

### Qué es

La Lambda que ejecuta las llamadas reales a las APIs de AWS. Es el único componente con acceso a los datos de infraestructura. Archivo: `lambda/monitor-actions/index.py`.

### Routing de eventos

```python
ACTIONS = {
    "get_ec2_health":        get_ec2_health,
    "get_lambda_health":     get_lambda_health,
    "get_cloudwatch_alarms": get_cloudwatch_alarms,
    "get_overall_health":    get_overall_health,
    "get_logs_analysis":     get_logs_analysis,
    "get_xray_traces":       get_xray_traces,
    "get_ssm_inventory":     get_ssm_inventory,
}

def handler(event, context):
    action = event.get("apiPath", "").lstrip("/")
    fn = ACTIONS.get(action)
    if not fn:
        return err("Unknown action", 404)
    return fn(event)
```

### Implementación multi-región — patrón `_make_clients`

Todas las acciones crean sus clientes boto3 **por request** (no a nivel de módulo), lo que permite consultar cualquier región en cada llamada:

```python
DEFAULT_REGION = os.environ.get("REGION", "us-east-1")

VALID_REGIONS = {
    "us-east-1", "us-east-2", "us-west-1", "us-west-2",
    "eu-west-1", "eu-west-2", "eu-west-3", "eu-central-1",
    "eu-north-1", "eu-south-1", "ap-northeast-1", "ap-southeast-1",
    "ap-south-1", "sa-east-1", "ca-central-1", "af-south-1",
    # ... 29 regiones en total
}

def _make_clients(region: str) -> dict:
    """Crea boto3 clients para la región solicitada."""
    return {
        "ec2":         boto3.client("ec2",         region_name=region),
        "lambda":      boto3.client("lambda",      region_name=region),
        "cloudwatch":  boto3.client("cloudwatch",  region_name=region),
        "logs":        boto3.client("logs",        region_name=region),
        "xray":        boto3.client("xray",        region_name=region),
    }

def _resolve_region(event) -> tuple:
    """Extrae y valida la región del evento. Retorna (region, None) o (None, error_response)."""
    region = (get_param(event, "region") or DEFAULT_REGION).strip().lower()
    if region not in VALID_REGIONS:
        return None, err(f"Invalid region '{region}'. Valid examples: us-east-1, eu-west-1, ap-southeast-1", code=400)
    return region, None

# Patrón en cada acción:
def get_ec2_health(event):
    region, region_err = _resolve_region(event)
    if region_err:
        return region_err
    clients = _make_clients(region)
    ec2 = clients["ec2"]
    # ... consulta usando ec2 con la región correcta
    return ok({"region": region, "summary": ..., "instances": ...})
```

**¿Por qué clientes por request y no módulo-nivel?**
- Lambda reutiliza el contexto entre invocaciones. Un cliente módulo-nivel quedaría fijado a la región de la primera llamada.
- El overhead de crear 5 clientes boto3 es ~25-50ms — despreciable frente al timeout de 30s de la Lambda.

**Cómo el agente Bedrock maneja la multi-región:**
El Bedrock Agent usa el ciclo ReAct. Si el usuario pide comparar dos regiones, el agente llama la misma herramienta dos veces con distintos parámetros `region`, acumula los resultados y sintetiza la respuesta:

```
Usuario: "Compara las EC2 de us-east-1 y eu-west-1"
→ Bedrock llama: get_ec2_health(region=us-east-1)
→ Bedrock llama: get_ec2_health(region=eu-west-1)
→ Claude sintetiza ambos resultados en una respuesta comparativa
```

### El formato de respuesta obligatorio (contrato Bedrock 1.0)

```python
def ok(body):
    return {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": "MonitorActions",
            "httpStatusCode": 200,
            "responseBody": {
                "application/json": {
                    "body": json.dumps(body)  # ← DEBE ser string, no dict
                }
            }
        }
    }
```

**Nota crítica:** `body` debe ser un **string JSON** (`json.dumps()`), no un diccionario Python. Desviarse de esto causa error silencioso en Bedrock.

### Las 6 acciones en detalle

#### `get_overall_health` — visión consolidada
Combina EC2 + Lambda + CloudWatch en una sola respuesta. Usa paginators en ambas llamadas. Lógica: 0 alarmas → `healthy`, < 5 → `degraded`, ≥ 5 → `critical`.

#### `get_ec2_health` — estado de instancias
```python
# Parámetro: state (running/stopped/terminated/all)
paginator = ec2.get_paginator("describe_instances")
# Retorna: id, name, type, state, az, private_ip, public_ip, launch_time
```

#### `get_lambda_health` — métricas de funciones
```python
# Parámetros: prefix (filtro), hours (1-168, default 24)
# Para cada función: invocations, errors, error_rate_pct, avg_duration_ms, throttles
# Lógica de salud: critical si error_rate >= 10%, warning si > 5%
period = max(60, raw_seconds - (raw_seconds % 60))  # CloudWatch requiere múltiplo de 60
```

#### `get_cloudwatch_alarms` — alarmas activas
```python
# Parámetro: state (ALARM/OK/INSUFFICIENT_DATA/ALL, default ALARM)
# Incluye MetricAlarms y CompositeAlarms
# Retorna: name, state, metric, threshold, comparison, updated_at
```

#### `get_logs_analysis` — diagnóstico en logs ← nuevo

Ejecuta una query Logs Insights sobre un log group y retorna los eventos coincidentes.

```python
# Parámetros:
#   log_group  (requerido) — ej: /aws/lambda/payment-processor
#   hours      (1-24, default 1)
#   query      (opcional) — query Logs Insights; default filtra ERROR/Exception/WARN

# Query default:
DEFAULT_LOGS_QUERY = """
fields @timestamp, @message
| filter @message like /ERROR|Exception|WARN|error/
| sort @timestamp desc
| limit 20
"""

# Flujo:
start_resp = logs_client.start_query(
    logGroupName=log_group,
    startTime=int(start_time.timestamp()),
    endTime=int(end_time.timestamp()),
    queryString=query_string,
    limit=50,
)
query_id = start_resp["queryId"]

# Polling hasta completar (timeout 25s)
while time.time() < deadline:
    resp = logs_client.get_query_results(queryId=query_id)
    if resp["status"] in ("Complete", "Failed", "Cancelled", "Timeout"):
        break
    time.sleep(1)

# Respuesta:
{
    "log_group": "/aws/lambda/payment-processor",
    "period_hours": 1,
    "total_events": 47,
    "error_events": 47,
    "statistics": { "recordsScanned": 1200, ... },
    "events": [
        { "timestamp": "2026-06-03 14:23:01.000", 
          "message": "Connection timeout to RDS endpoint db.prod.internal:5432" },
        ...
    ]
}
```

**Errores manejados:**
- `ResourceNotFoundException` → HTTP 404 (log group no existe)
- Timeout de query → HTTP 504 (query no completó en 25s)

#### `get_xray_traces` — trazas distribuidas ← nuevo

Obtiene resúmenes de trazas X-Ray para identificar cuellos de botella y fallas en flujos multi-servicio.

```python
# Parámetros:
#   hours             (1-6, default 1)
#   filter_expression (opcional) — ej: "fault = true", "service(\"checkout\")"

paginator = xray.get_paginator("get_trace_summaries")
for page in paginator.paginate(
    StartTime=start_time,
    EndTime=end_time,
    Sampling=False,
    FilterExpression=filter_expression,  # solo si se especificó
):
    for trace in page["TraceSummaries"]:
        entry = {
            "trace_id":       trace["Id"],
            "duration_s":     round(trace["Duration"], 3),
            "response_time_s": round(trace["ResponseTime"], 3),
            "has_error":      trace["HasError"],   # 4xx — error del cliente
            "has_fault":      trace["HasFault"],   # 5xx — error del servidor (tu código)
            "has_throttle":   trace["HasThrottle"],
            "http":           { "url": ..., "status": ..., "method": ... },
            "service_ids":    [{"Name": s["Name"], "Type": s["Type"]} for s in trace["ServiceIds"]],
        }

# Respuesta:
{
    "period_hours": 1,
    "total_traces": 234,
    "summary": {
        "errors": 3,          # 4xx
        "faults": 12,         # 5xx — los críticos
        "throttles": 0,
        "avg_duration_s": 0.847,
        "p99_duration_s": 3.210,
        "health": "warning"
    },
    "traces": [ ... ]  # máx 100 trazas
}
```

**Distinción `has_fault` vs `has_error`:**
- `has_fault = true` → HTTP 5xx — falla en tu código o dependencia (crítico)
- `has_error = true` → HTTP 4xx — error del cliente (puede ser esperado)

**Error manejado:** `AccessDeniedException` → HTTP 403 (falta permiso X-Ray en el IAM role).

#### `get_ssm_inventory` — inventario de software e instancias gestionadas ← nuevo

Consulta AWS Systems Manager para obtener información de inventario de las instancias EC2 que tienen SSM Agent instalado.

```python
# Parámetros:
#   instance_id     (opcional) — filtrar por ID de instancia específica
#   inventory_type  (opcional) — uno de: AWS:InstanceInformation (default),
#                    AWS:Application, AWS:AWSComponent, AWS:Network,
#                    AWS:WindowsUpdate, AWS:PatchSummary, ALL
#   region          (opcional) — default us-east-1

# Crea cliente SSM directamente (no via _make_clients)
ssm = boto3.client("ssm", region_name=region)

# Paso 1: obtener instancias gestionadas
paginator = ssm.get_paginator("describe_instance_information")
# Paso 2: obtener inventario
paginator2 = ssm.get_paginator("get_inventory")

# Respuesta (con instancias):
{
    "region": "us-east-1",
    "managed_instance_count": 3,
    "inventory_type_queried": "AWS:InstanceInformation",
    "instances": [
        {
            "instance_id": "i-0abc123",
            "computer_name": "ip-10-0-0-5",
            "platform_type": "Linux",
            "platform_name": "Amazon Linux 2",
            "platform_version": "2",
            "agent_version": "3.2.0",
            "ip_address": "10.0.0.5",
            "ping_status": "Online",
            "last_ping": "2026-06-11T10:30:00Z"
        }
    ],
    "inventory": [ ... ],   # datos específicos del inventory_type
    "note": "SSM Inventory requiere SSM Agent instalado y rol AmazonSSMManagedInstanceCore."
}

# Respuesta (sin instancias):
{
    "managed_instance_count": 0,
    "message": "No managed instances found. Ensure SSM Agent is installed and running, and the EC2 instance has the AmazonSSMManagedInstanceCore IAM role."
}
```

**Nota crítica de implementación:** `get_ssm_inventory` crea su cliente `boto3.client("ssm")` directamente en lugar de usar `_make_clients()`. Esto permite un patrón de mock distinto en los tests (patch de `index.boto3` en lugar de los clients preexistentes).

**Prerequisitos en infraestructura:**
- SSM Agent instalado y corriendo en la EC2 (pre-instalado en Amazon Linux 2/2023 y Windows Server 2016+)
- IAM Role de la EC2 con política `AmazonSSMManagedInstanceCore`
- Conectividad con endpoints SSM (internet o VPC endpoints)

**Tipos de inventario soportados:**

| Tipo | Qué devuelve |
|---|---|
| `AWS:InstanceInformation` (default) | SO, versión, agente SSM, IP, estado |
| `AWS:Application` | Apps instaladas: nombre, versión, publicador |
| `AWS:AWSComponent` | Componentes AWS: CloudWatch Agent, SSM Agent, etc. |
| `AWS:Network` | Interfaces de red: IP, MAC, gateway, DNS |
| `AWS:WindowsUpdate` | Actualizaciones de Windows pendientes/instaladas |
| `AWS:PatchSummary` | Resumen de parches: instalados, pendientes, fallidos |
| `ALL` | Consulta todos los tipos disponibles |

### Por qué paginators en todas las acciones

```python
# ❌ Sin paginator — pierde datos con >100 instancias
response = ec2.describe_instances()

# ✅ Con paginator — correcto, recorre todas las páginas
paginator = ec2.get_paginator("describe_instances")
for page in paginator.paginate():
    # procesa todas las páginas
```

Este fue uno de los 6 bugs críticos corregidos durante el desarrollo.

---

## IAM — El modelo de seguridad

Cada componente tiene un IAM Role independiente con mínimos permisos:

```
ChatLambdaRole
└── bedrock:InvokeAgent  (solo puede invocar el agente)

ActionLambdaRole  (solo lectura — NUNCA puede modificar recursos)
├── ec2:DescribeInstances, ec2:DescribeInstanceStatus
├── lambda:ListFunctions, lambda:GetFunctionConfiguration
├── cloudwatch:DescribeAlarms, cloudwatch:GetMetricStatistics, cloudwatch:ListMetrics
├── logs:DescribeLogGroups, logs:DescribeLogStreams, logs:GetLogEvents
├── logs:StartQuery, logs:GetQueryResults, logs:StopQuery   ← Logs Insights
├── xray:GetTraceSummaries, xray:BatchGetTraces             ← X-Ray
└── ssm:DescribeInstanceInformation, ssm:ListInventoryEntries, ssm:GetInventory  ← SSM Inventory

BedrockAgentRole
├── bedrock:InvokeModel  (solo Haiku en us-east-1)
└── s3:GetObject         (solo el bucket del schema)
```

**Principio de mínimo privilegio:** si la Lambda de acciones fuera comprometida, el atacante solo puede leer datos de monitoreo. No puede crear, modificar ni eliminar ningún recurso de AWS.

---

## Auditoría de seguridad — estado actual (2026-06-04)

Esta sección documenta el análisis de seguridad aplicado al proyecto, los controles implementados, las vulnerabilidades corregidas y los puntos pendientes para producción.

### Nivel de seguridad: ✅ Adecuado para POC / Producción limitada

| Capa | Estado | Detalle |
|---|---|---|
| Credenciales AWS | ✅ Seguro | IAM Roles, sin access keys en código ni variables de entorno |
| Acceso al chat | ✅ Seguro | API Key + rate limiting + cuota diaria |
| Datos en tránsito | ✅ Seguro | HTTPS enforced (CloudFront + API GW) |
| Datos en reposo | ✅ Seguro | S3 Block Public Access, sin datos sensibles almacenados |
| Headers HTTP | ✅ Seguro | CSP, HSTS, X-Frame-Options DENY, XSS-Protection |
| Validación de inputs | ✅ Seguro | Todos los parámetros Lambda validados con límites y regex |
| Session ID | ✅ Seguro | `crypto.randomUUID()` — criptográficamente seguro |
| System prompt | ✅ Seguro | 6 restricciones explícitas — anti-injection, anti-disclosure |
| CORS | ⚠️ Parcial | `ALL_ORIGINS` en POC — restringir a dominio CloudFront post-deploy |
| Autenticación usuarios | ⚠️ Pendiente | API Key compartida — producción requiere Cognito + JWT |
| CloudTrail | ⚠️ Opcional | No habilitado — recomendado para entornos productivos |

### Vulnerabilidades corregidas en auditoría (2026-06-04)

| # | Severidad | Componente | Vulnerabilidad | Corrección aplicada |
|---|---|---|---|---|
| 1 | 🔴 Alta | `index.py` — `get_logs_analysis` | `log_group` sin validación de formato: aceptaba metacaracteres shell (`$()`, `&&`, `\|`) | Validación regex `^[a-zA-Z0-9_./#-]+$` + límite 512 chars → HTTP 400 si inválido |
| 2 | 🟡 Media | `index.py` — `get_logs_analysis` | Parámetro `query` (Logs Insights) sin límite de longitud | Límite 2048 chars → HTTP 400 si excede |
| 3 | 🟡 Media | `index.py` — `get_xray_traces` | Parámetro `filter_expression` sin límite de longitud | Límite 2048 chars → HTTP 400 si excede |
| 4 | 🟡 Media | `chat-frontend-stack.ts` | Session ID generado con `Math.random()` — no criptográfico, predecible | Reemplazado por `crypto.randomUUID()` con fallback |
| 5 | 🟡 Media | `chat-frontend-stack.ts` | CloudFront sin security headers HTTP | `ResponseHeadersPolicy`: CSP, HSTS 1 año, X-Frame-Options DENY, XSS-Protection, Referrer-Policy |

### Archivos truncados corregidos (2026-06-04)

Durante la auditoría se detectaron dos archivos truncados en el sistema de archivos:

| Archivo | Síntoma | Causa | Corrección |
|---|---|---|---|
| `lib/chat-frontend-stack.ts` | Cortado a mitad de `sendMessage()` — `tsc` reportaba "Unterminated template literal" | Sincronización incompleta entre Windows y Linux mount | Reconstruido con el bloque faltante; TypeScript compila sin errores |
| `bin/app.ts` | Cortado después de 14 líneas — faltaban ambas instancias de stack | Operación Edit truncó el archivo en sesión anterior | Restaurado con ambas instancias `MonitorAgentStack` y `ChatFrontendStack` |

### Validación de inputs — detalle técnico

Todos los parámetros de entrada de las 7 acciones Lambda están validados antes de usarlos:

```python
# log_group: formato estricto + longitud máxima
if len(log_group) > MAX_LOG_GROUP_LEN:           # 512 chars
    return err("...", code=400)
if not re.match(r'^[a-zA-Z0-9_./#\-][a-zA-Z0-9_./#\-]*$', log_group):
    return err("...invalid characters...", code=400)

# query y filter_expression: solo longitud
if len(raw_query) > MAX_QUERY_LEN:               # 2048 chars
    return err("...", code=400)

# prefix: truncado silencioso (no rechaza — compatible con versiones anteriores)
prefix = (get_param(event, "prefix") or "")[:MAX_PREFIX_LEN]  # 128 chars

# region: validación contra set de 29 regiones conocidas
if region not in VALID_REGIONS:
    return err("Invalid region '...'", code=400)
```

### Pendiente para producción

> ⚠️ **CORS: acción requerida post-deploy**
>
> Actualmente `allowOrigins: apigw.Cors.ALL_ORIGINS` permite que cualquier dominio llame al API. Esto reduce el impacto de la API Key como única barrera. **Después del deploy**, restringir al dominio CloudFront asignado:
>
> ```typescript
> // En lib/chat-frontend-stack.ts, reemplazar:
> allowOrigins: apigw.Cors.ALL_ORIGINS,
> // Por:
> allowOrigins: [`https://${distribution.distributionDomainName}`],
> ```
>
> Esto requiere conocer el dominio CloudFront antes de deployar, que es un problema de huevo-gallina. La solución limpia es ejecutar el deploy en dos pasos: primero CloudFront, luego API GW con el dominio conocido. O usar un dominio custom fijo.

---

## CDK — Infraestructura como Código

### Los dos stacks

```typescript
// bin/app.ts
const agentStack    = new AwsMonitorAgentStack(app, 'AwsMonitorAgentStack');
const frontendStack = new AwsMonitorFrontendStack(app, 'AwsMonitorFrontendStack', {
    agentId:      agentStack.agentId,
    agentAliasId: agentStack.agentAliasId,
});
```

**¿Por qué dos stacks?** El agente stack (Bedrock + Lambda de acciones) cambia poco. El frontend stack (UI + API GW) puede cambiar frecuentemente. Se pueden destruir y recrear independientemente.

### Outputs post-deploy

```
AwsMonitorAgentStack.AgentId         = XXXXXXXX
AwsMonitorAgentStack.AgentAliasId    = live
AwsMonitorFrontendStack.ChatUiUrl    = https://d1xxx.cloudfront.net
AwsMonitorFrontendStack.ApiUrl       = https://xxx.execute-api.us-east-1.amazonaws.com/prod/
AwsMonitorFrontendStack.ApiKeyId     = abc123
```

---

## Flujo completo — 14 pasos en ~3 segundos

```
 1. Usuario escribe en el browser y presiona Enter
 2. JavaScript hace POST /chat al API Gateway con header x-api-key
 3. API Gateway valida la key (si falla → 403, ni llega a Lambda)
 4. API Gateway invoca Lambda proxy
 5. Lambda proxy llama bedrock-agent-runtime.invoke_agent()
 6. Bedrock envía a Claude: mensaje + herramientas disponibles + system prompt
 7. Claude razona: "invocar get_overall_health"
 8. Bedrock construye el evento { apiPath: "/get_overall_health" }
 9. Bedrock invoca Lambda de acciones
10. Lambda llama EC2, CloudWatch y Lambda APIs
11. Lambda formatea y retorna el JSON con envelope Bedrock 1.0
12. Bedrock pasa los datos a Claude: "genera respuesta en lenguaje natural"
13. Claude escribe la respuesta final
14. Bedrock → Lambda proxy → API GW → browser
```

**Tiempo total: 2-4 segundos** — dominado por latencia de Bedrock/Claude, no por las APIs de AWS.

**Ciclo extendido con diagnóstico (cuando hay error):**
```
Paso 7b: Claude detecta Lambda con error_rate > 5%
         → decide invocar get_logs_analysis automáticamente
Paso 8b: { apiPath: "/get_logs_analysis", parameters: [{ log_group: "/aws/lambda/X" }] }
Pasos 9-13 se repiten → Claude incluye causa raíz en la respuesta
```

---

## Decisiones técnicas clave

| Decisión | Por qué |
|---|---|
| Prefijo `us.` en modelo Bedrock | Obligatorio en us-east-1 — inference profile, no model ID directo |
| Lambda proxy en lugar de llamada directa | Bedrock no tiene CORS; credenciales no pueden estar en el browser |
| Código proxy inline en CDK | Función trivial, no necesita tests independientes |
| Lambda actions en archivo externo | Permite 162 tests locales + debugging |
| Dos stacks CDK separados | Agent stack cambia poco; frontend stack puede cambiar frecuentemente |
| REST API en lugar de HTTP API | Soporte nativo de API Keys; migrar a HTTP API es ~30 min cuando se use Cognito |
| Paginators obligatorios | Sin ellos, cuentas con >100 instancias/funciones pierden datos |
| Logs Insights con polling | La API es asíncrona — `start_query` + `get_query_results` con timeout 25s |
| X-Ray `Sampling: False` | Obtiene todas las trazas del período, no una muestra estadística |

### ¿Por qué Claude Haiku 4.5 y no Sonnet?

- **Más rápido** — respuestas en 1-2s vs 3-5s con Sonnet
- **Más económico** — ~$0.001/consulta vs ~$0.005 con Sonnet
- **Suficiente para monitoreo** — las preguntas operacionales no requieren razonamiento profundo
- **Versión más reciente** — Haiku 4.5 (`us.anthropic.claude-haiku-4-5-20251001-v1:0`) es el modelo activo en us-east-1; Claude 3.5 Haiku y Claude 3.7 Sonnet quedaron marcados como Legacy

Para análisis de causa raíz complejos con múltiples servicios interrelacionados → upgrade a Claude Sonnet 4.5 cambiando `foundationModel` en `monitor-agent-stack.ts`.

### ¿Por qué inference profile (`us.` prefijo)?

En `us-east-1`, AWS requiere inference profiles para modelos Claude:

```typescript
// ❌ Incorrecto en us-east-1
foundationModel: "anthropic.claude-haiku-4-5-20251001-v1:0"

// ✅ Correcto en us-east-1 (inference profile cross-region)
foundationModel: "us.anthropic.claude-haiku-4-5-20251001-v1:0"
```

Adicionalmente, la IAM policy del BedrockAgentRole debe usar el ARN completo con account ID (requisito para inference profiles con cross-region):

```typescript
// ✅ ARN correcto para inference profile
`arn:aws:bedrock:us-east-1:369595298303:inference-profile/us.anthropic.claude-haiku-4-5-20251001-v1:0`
// (nota: account ID 369595298303 es obligatorio en el ARN)
```

---

## Extensibilidad — cómo agregar un nuevo servicio

Ejemplo: agregar monitoreo de RDS.

```python
# 1. Agregar función en lambda/monitor-actions/index.py
def get_rds_health(event):
    rds = boto3.client("rds", region_name=REGION)
    paginator = rds.get_paginator("describe_db_instances")
    instances = []
    for page in paginator.paginate():
        for db in page["DBInstances"]:
            instances.append({
                "id":     db["DBInstanceIdentifier"],
                "status": db["DBInstanceStatus"],
                "engine": db["Engine"],
                "class":  db["DBInstanceClass"],
            })
    return ok({"instances": instances, "total": len(instances)})

# 2. Registrar en el dict ACTIONS
ACTIONS["get_rds_health"] = get_rds_health
```

```json
// 3. Agregar en lib/monitor-openapi.json
"/get_rds_health": {
  "get": {
    "operationId": "getRdsHealth",
    "description": "Returns health status of RDS instances",
    "responses": { "200": { "description": "Success" } }
  }
}
```

```bash
# 4. Deploy — el agente toma el nuevo schema automáticamente
npm run deploy
```

**Tiempo estimado: menos de 1 hora por servicio nuevo.**

Servicios candidatos para la siguiente iteración: RDS, ECS/EKS, DynamoDB, S3, ALB/CloudFront.

---

## Suite de tests — 281 tests, 0 fallos

```bash
python run_tests.py all
# 281 tests OK (unit=200+, e2e=42, integration=39)
```

| Tipo | Archivo | Tests | Qué valida |
|---|---|---|---|
| Unit | `tests/unit/test_monitor_actions.py` | ~107 | 7 acciones (incl. SSM), multi-región, envelope Bedrock 1.0, error handling, Logs, X-Ray |
| Unit | `tests/unit/test_frontend_html.py` | ~85 | HTML del frontend — config panel, multi-región, sessionStorage, badge SSM |
| Integration | `tests/integration/test_validator.py` | 39 | `validate_aws_access.py` — 14 checks: identity, IAM, Bedrock, Logs, X-Ray, SSM |
| E2E | `tests/e2e/test_bedrock_contract.py` | 42 | Contrato Bedrock 1.0, multi-región, seguridad, SSM Inventory, diagnóstico |

**Tests multi-región incluidos en `TestRegionValidation`:**
- Región válida retorna HTTP 200 con campo `region` en respuesta
- Región inválida retorna HTTP 400 con mensaje de error descriptivo
- Sin parámetro → default `us-east-1`
- Case insensitive: `US-EAST-1` → normalizado a `us-east-1`
- Las 7 acciones respetan la región pasada

**Tests de escenario de diagnóstico en E2E** (clase `TestDiagnosticScenarios`):

| Test | Gap que valida |
|---|---|
| `test_gap1_log_events_contain_message_field` | `get_logs_analysis` retorna campo `message` — el agente puede decir qué dice el error |
| `test_gap2_log_events_contain_timestamp_field` | `get_logs_analysis` retorna `timestamp` — el agente puede decir desde cuándo |
| `test_gap3_xray_traces_expose_service_ids` | `get_xray_traces` retorna `service_ids` — identifica el servicio exacto que falla |
| `test_gap4_has_fault_vs_has_error_distinction` | Distingue 5xx (tu código) de 4xx (cliente) |
| `test_gap5_traces_expose_duration_per_trace` | Retorna `duration_s` por trace — identifica cuellos de botella |
| `test_gap5_summary_includes_avg_and_p99_duration` | Summary con avg y p99 sobre todas las trazas |
| `test_gap6_error_event_count_is_returned` | Retorna `total_events` y `error_events` para cuantificar impacto |
| `test_gap6_events_list_allows_frequency_analysis` | Lista de eventos con `message` para detectar errores más frecuentes |
| `test_gap7_checkout_rca_via_xray_identifies_fault_service` | Escenario RCA multi-servicio: X-Ray identifica `fraud-detector` como causa raíz |
| `test_gap7_logs_rca_reveals_error_message_and_timing` | Escenario RCA con logs: mensaje de error + timestamp exactos |

**No requiere pip install** — usa Python stdlib puro (`unittest` + `unittest.mock`). Stubs de boto3 inyectados en `sys.modules` antes del discovery.

---

## Costos — referencia técnica

Esta solución tiene costo casi cero en reposo. El modelo es 100% pay-per-use.

| Recurso | Costo en reposo | Costo por uso |
|---|---|---|
| CloudFront | ~$0/mes | $0.0085/10K peticiones |
| API Gateway | ~$0/mes | $3.50/millón de llamadas |
| Lambda (2) | ~$0/mes | $0.20/millón de invocaciones |
| S3 (2 buckets) | < $0.01/mes | $0.023/GB almacenado |
| Bedrock Haiku | ~$0/mes | ~$0.001/consulta (tokens in + out) |
| **TOTAL EN REPOSO** | **< $0.02/mes** | |

**Escenario equipo de 5 personas, 20 consultas/día:** ~$1.70/mes.

Comparado con SaaS de monitoreo (Datadog ~$15-30/host/mes, New Relic ~$25/usuario/mes): esta solución es **>99% más económica** para equipos que ya tienen sus datos en AWS.

---

## Bugs críticos corregidos durante el desarrollo

| # | Bug | Fix aplicado |
|---|---|---|
| 1 | `get_ec2_health` sin paginator → instancias faltantes | `ec2.get_paginator("describe_instances")` |
| 2 | `hours` no validado como entero | `try/except ValueError → err(400)` |
| 3 | CloudWatch `Period < 60` posible | `period = max(60, raw_seconds - raw_seconds % 60)` |
| 4 | `get_cloudwatch_alarms` sin validación de estado | `VALID_ALARM_STATES` set + check → err(400) |
| 5 | `get_overall_health` sin paginators | Paginators EC2 y Lambda |
| 6 | Threshold `error_rate > 10` (boundary) | Cambiado a `>= 10` |

---

## Pendientes para producción

### Bloqueantes pre-deploy
- Habilitar **Claude Haiku 4.5** en AWS Console → Bedrock → Model access (us-east-1)
- Rotar Access Key (`AKIA...REDACTED` — inactiva, crear nueva)
- `python validate_aws_access.py` → confirmar 14/14 PASS

### Deploy
```bash
npm install
npx cdk bootstrap aws://369595298303/us-east-1
npm run deploy
```

### Post-deploy
```bash
aws apigateway get-api-key --api-key <ApiKeyId> --include-value --region us-east-1
```

### Producción con usuarios externos
- Reemplazar API Key por **Cognito User Pool + JWT Authorizer**
- Habilitar **X-Ray active tracing** en Lambdas para que `get_xray_traces` tenga datos reales
- Habilitar **SSM Agent + AmazonSSMManagedInstanceCore** en instancias EC2 para que `get_ssm_inventory` retorne datos
- Revisar Usage Plan (1,000 req/día puede ser insuficiente)

### Opcionales
- Upgrade a **Claude Sonnet 4.5** para análisis de causa raíz más profundo
- Migrar REST API → **HTTP API** (costo -70%)
- Agregar monitoreo de **RDS, ECS, DynamoDB**

---

## Archivos de referencia del proyecto

| Archivo | Propósito |
|---|---|
| `docs/aws-monitor-presentacion.md` | Versión de negocio — valor, costos, roadmap para el cliente |
| `docs/aws-monitor-arquitectura-tecnica.md` | Este documento — implementación técnica capa por capa |
| `docs/aws-monitor-dev-guide.pptx` | Documentación visual (slides) para demos y presentaciones |
| `docs/aws-monitor-poc-vs-mcp.md` | Análisis comparativo: qué agrega Logs Insights y X-Ray |
| `lib/monitor-agent-stack.ts` | CDK: Bedrock Agent + Lambda actions + IAM + S3 schema |
| `lib/chat-frontend-stack.ts` | CDK: API Gateway + Lambda proxy + S3 + CloudFront |
| `lambda/monitor-actions/index.py` | Handler Python: 7 acciones de monitoreo |
| `lib/monitor-openapi.json` | Schema OpenAPI 3.0 — describe las 7 herramientas a Bedrock |
| `docs/ssm-inventory.md` | Guía SSM: prerrequisitos, tipos de inventario, troubleshooting |
| `validate_aws_access.py` | 14 checks pre-deploy de credenciales y permisos (incl. SSM) |
| `run_tests.py` | Runner: `python run_tests.py all` → 281 tests |

---

*Actualizado 2026-06-11 · AWS Monitor Agent — Arquitectura Técnica · Multi-región (29 regiones) · 7 acciones · 281 tests · 3htp*
