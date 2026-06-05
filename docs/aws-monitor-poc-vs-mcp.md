# AWS Monitor: POC vs MCP — Análisis Comparativo

> **Propósito:** Explicar por qué la POC fue construida como solución independiente y no sobre los MCP servers de AWS disponibles, y cómo ambas capas coexisten y se complementan.  
> **Audiencia:** Cliente final + equipo técnico  
> **Fecha:** 2026-06-03

---

## 1. Resumen Ejecutivo

Existen **dos categorías de solución** para monitoreo de AWS con IA: la POC desarrollada para 3htp y los MCP Servers de AWS. No son alternativas — son herramientas para audiencias distintas con objetivos distintos.

| | POC AWS Monitor | AWS MCP Servers |
|---|---|---|
| **¿Qué es?** | Producto web deployado en la cuenta AWS del cliente | Extensiones para herramientas de desarrollo local |
| **¿Quién lo usa?** | Cualquier usuario con el enlace (técnico o no) | Desarrolladores con Claude Code o Claude Desktop instalado |
| **¿Dónde corre?** | En la nube (CloudFront + Bedrock + Lambda) | En la máquina local del desarrollador |
| **¿Requiere setup?** | No — es una URL pública HTTPS | Sí — instalación, configuración AWS CLI, credenciales locales |
| **Seguridad** | IAM Role de solo lectura, sin credenciales expuestas | Credenciales AWS en la máquina local del usuario |

---

## 2. ¿Qué Resuelve Cada Solución?

### 🏗️ POC AWS Monitor (esta solución)

- Interfaz de chat **accesible por URL** sin instalación
- Responde en **lenguaje natural** a preguntas como "¿están mis instancias EC2 activas?"
- Usable por **gerentes, soporte, operaciones** — sin conocimiento de AWS
- Deployada **dentro de la cuenta del cliente**, bajo sus políticas de seguridad
- Permisos **mínimos y fijos**: solo lectura de EC2, Lambda, CloudWatch
- **Costo predecible**: paga por uso de tokens Bedrock, no por licencia

### 🔧 AWS MCP Servers (herramientas del ecosistema)

- Extienden la capacidad de **Claude Code / Claude Desktop** en la máquina del desarrollador
- Permiten que un desarrollador diga "lista mis funciones Lambda" desde su editor
- Útiles para **workflows de desarrollo**: debug, análisis, infraestructura como código
- Requieren AWS CLI configurado localmente con credenciales activas

---

## 3. MCPs de AWS Disponibles Actualmente

### Amazon CloudWatch MCP Server
- **Repositorio:** `aws-samples/amazon-cloudwatch-mcp-server` (AWS Labs, 2026 — preview)
- **Qué hace:** Análisis de causa raíz (root cause analysis) usando datos de CloudWatch: logs, métricas, alarmas, Application Signals, X-Ray traces
- **Herramientas expuestas:** `list_metrics`, `get_metric_data`, `describe_alarms`, `get_log_events`, `start_query` (Logs Insights), análisis automático de anomalías
- **Requiere:** Claude Desktop o Claude Code + AWS credentials configuradas localmente

### AWS API MCP Server *(GA mayo 2026)*
- **Paquete:** `awslabs.aws-api-mcp-server`
- **Qué hace:** Llama **cualquier API de AWS** mediante una herramienta genérica `call_aws`. Acceso universal a todos los servicios.
- **Requiere:** Claude Code + credenciales AWS con permisos amplios
- **Riesgo:** Un desarrollador con acceso amplio puede, involuntariamente, modificar o eliminar recursos

### CloudWatch Application Signals MCP Server
- **Repositorio:** `aws-samples/amazon-cloudwatch-application-signals-mcp-server`
- **Qué hace:** APM (Application Performance Monitoring) — trazas distribuidas, SLOs, service map, latencia end-to-end
- **Audiencia:** Equipos de ingeniería de plataforma que ya usan X-Ray y Application Signals

### Otros MCPs de AWS disponibles
| MCP | Propósito |
|-----|-----------|
| `awslabs.aws-cost-analysis-mcp-server` | Análisis de costos con Cost Explorer y AWS Budgets |
| `awslabs.amazon-q-developer-mcp-server` | Q Developer en el flujo de Claude Code |
| `awslabs.eks-mcp-server` | Gestión de clusters EKS (Kubernetes) |
| `awslabs.ecs-mcp-server` | Gestión de servicios ECS (contenedores) |
| `awslabs.bedrock-kb-retrieval-mcp-server` | RAG sobre Knowledge Bases de Bedrock |

---

## 4. Desventajas de los MCPs para Usuarios Públicos y Seguridad

### ❌ Limitaciones para usuarios no técnicos

1. **Requieren instalación local** — Claude Code o Claude Desktop no son herramientas de usuario final
2. **Requieren AWS CLI configurado** — el usuario necesita conocer `aws configure`, Access Keys, regiones
3. **Sin interfaz propia** — el usuario interactúa a través del entorno de desarrollo, no de una UI de chat
4. **Sin branded experience** — no hay forma de personalizar la experiencia para el cliente
5. **Curva de aprendizaje** — un gerente de operaciones no debería necesitar instalar Node.js para ver el estado de sus instancias

### 🔐 Riesgos de seguridad en contexto empresarial

| Riesgo | Descripción |
|--------|-------------|
| **Credenciales locales** | Los MCP servers usan las credenciales AWS configuradas en la máquina del desarrollador. Si el equipo usa roles con permisos amplios (administrador, power user), el MCP hereda esos permisos |
| **AWS API MCP — acceso universal** | `call_aws` puede invocar cualquier API: `ec2:TerminateInstances`, `s3:DeleteBucket`, `iam:DeleteRole`. Un error de prompt o una inyección puede tener consecuencias destructivas |
| **Sin audit trail dedicado** | Las llamadas MCP pasan por las credenciales del desarrollador local — no hay un trail de auditoría separado por usuario de negocio |
| **Sin rate limiting** | Los MCP servers no tienen throttling por defecto — un loop o error puede generar miles de llamadas a AWS API en segundos |
| **Secretos en tránsito** | Los MCPs pueden exponer ARNs, IDs de cuenta, nombres de recursos al modelo — sin los controles del system prompt de la POC |

### ✅ La POC resuelve todos estos riesgos

- IAM Role con **7 permisos específicos** de solo lectura — imposible modificar recursos
- **System prompt con 6 restricciones** — el agente no puede revelar ARNs, account IDs, ni ejecutar comandos
- **API Key + rate limiting** — 5 req/s, 1000 req/día — sin riesgo de abuso accidental
- **Sin credenciales en el browser** — el usuario nunca ve ni maneja credenciales AWS
- **CloudTrail** registra cada llamada Lambda/EC2/CloudWatch con el ARN del role de la POC

---

## 5. Tabla: ¿Qué Significa Esto para Nuestra POC?

| Dimensión | AWS API MCP Server | CloudWatch MCP Server | POC AWS Monitor (3htp) |
|-----------|-------------------|----------------------|------------------------|
| **Audiencia** | Desarrolladores con Claude Code | Ingenieros de plataforma/SRE | Cualquier usuario con URL |
| **Instalación** | npm/pip local + AWS CLI | npm/pip local + AWS CLI | Ninguna — URL en el browser |
| **Permisos AWS** | Los del desarrollador local (potencialmente admin) | Los del desarrollador local | IAM Role fijo, solo lectura |
| **Interfaz** | Claude Code / Claude Desktop | Claude Code / Claude Desktop | Chat web con branding propio |
| **Seguridad producción** | No recomendado para usuarios externos | No recomendado para usuarios externos | Diseñado para usuarios externos |
| **Acciones destructivas posibles** | Sí (terminar instancias, borrar recursos) | Limitado (principalmente lectura) | No — imposible por diseño |
| **Contexto de conversación** | Sesión local, efímera | Sesión local, efímera | `sessionId` persistente por sesión |
| **Costo por consulta** | Tokens del plan Claude del desarrollador | Tokens del plan Claude del desarrollador | ~$0.0008/1K tokens Bedrock (cuenta AWS cliente) |
| **Customización del agente** | No — herramienta genérica | No — herramienta genérica | Sí — system prompt, restricciones, scope definido |
| **Disponibilidad en mayo 2026** | GA (mayo 2026) | Preview | ✅ Deployable hoy |

---

## 6. Por Qué la POC Existe y No Usa los MCPs de AWS

La pregunta legítima es: *"Si existen MCP servers de AWS, ¿por qué construir algo desde cero?"*

La respuesta es que **los MCP servers resuelven un problema diferente**: son extensiones para desarrolladores que trabajan en sus máquinas locales con Claude Code. La POC resuelve otro problema: **poner monitoreo inteligente en manos de usuarios no técnicos, dentro de la cuenta AWS del cliente, sin requerir ninguna instalación**.

### 6.1 Las 6 Razones Técnicas que Justifican la POC sobre los MCPs

#### Razón 1 — Audiencia incompatible
Los MCP servers requieren Claude Code o Claude Desktop instalados localmente. El usuario objetivo de la POC (gerente de TI, analista de soporte, dueño de negocio) no tiene — ni debería tener — un entorno de desarrollo configurado. La POC es una URL en el browser.

#### Razón 2 — Credenciales en la máquina del desarrollador = riesgo en producción
El AWS API MCP Server usa las credenciales AWS configuradas localmente. En un contexto de producción con múltiples usuarios, esto significa que cada persona necesita su propio set de credenciales, con el riesgo asociado de gestión, rotación y scope. La POC centraliza el acceso en un único IAM Role de solo lectura, bajo control del equipo de infraestructura.

#### Razón 3 — El AWS API MCP Server tiene acceso demasiado amplio
`call_aws` puede invocar literalmente cualquier API de AWS que los permisos del usuario permitan. Para monitoreo de solo lectura, este es un scope innecesariamente grande. La POC expone exactamente 4 herramientas (get_overall_health, get_ec2_health, get_lambda_health, get_cloudwatch_alarms) — ninguna más, ninguna menos.

#### Razón 4 — Sin branded experience ni control del scope
Los MCP servers son herramientas genéricas. No permiten definir qué puede y qué no puede hacer el agente, qué servicios monitorea, cómo responde, qué restricciones aplica. El system prompt de Bedrock Agent define el comportamiento exacto: scope, tono, restricciones de seguridad, formato de respuesta.

#### Razón 5 — Timing: el ecosistema MCP de AWS era preview cuando se diseñó la POC
El CloudWatch MCP Server está en preview. El AWS API MCP Server llegó a GA en mayo 2026 — después de que la arquitectura de la POC fue definida. Construir sobre una preview implica riesgo de breaking changes, APIs inestables y soporte limitado.

#### Razón 6 — La POC es un producto deployable, no una herramienta de desarrollo
La diferencia fundamental: los MCP servers son herramientas que un desarrollador usa en su flujo de trabajo. La POC es un producto que se instala una vez en la cuenta AWS del cliente y está disponible para toda la organización sin configuración adicional. Esta distinción de "herramienta vs. producto" es la que justifica la arquitectura completa: CloudFront, API Gateway, Bedrock Agent, Lambda actions.

---

## 7. La Evolución Natural: Las Dos Capas Coexisten

La POC y los MCP servers no son mutuamente excluyentes. La evolución natural del proyecto es **tener ambas capas operando para distintos perfiles de usuario**:

```
┌─────────────────────────────────────────────────────────────┐
│                    Capa 1 — Usuarios de Negocio             │
│                                                             │
│   Gerente / Soporte / Operaciones                           │
│         ↓                                                   │
│   Browser → CloudFront → API GW → Bedrock Agent → Lambda   │
│   [POC AWS Monitor — esta solución]                         │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                    Capa 2 — Equipo Técnico                  │
│                                                             │
│   Desarrolladores / SRE / DevOps                            │
│         ↓                                                   │
│   Claude Code + CloudWatch MCP Server                       │
│   [Root cause analysis, logs insights, X-Ray traces]        │
└─────────────────────────────────────────────────────────────┘
```

### Roadmap sugerido

| Fase | Acción | Beneficio |
|------|--------|-----------|
| **Ahora** | Deploy de la POC | Monitoreo accesible para toda la organización |
| **Corto plazo** | Equipo técnico instala CloudWatch MCP Server en Claude Code | RCA profundo para desarrolladores sin cambiar la POC |
| **Mediano plazo** | Agregar RDS y ECS a la POC | Cobertura completa de infraestructura para usuarios finales |
| **Largo plazo** | Cognito + multi-tenant en la POC | Múltiples cuentas/clientes desde una sola instancia |

---

## 8. Guía: Cómo Usar los MCPs de AWS con Claude Code (Equipo Técnico)

Esta sección es para **desarrolladores y SREs** del equipo técnico que quieren usar los MCP servers de AWS directamente desde Claude Code.

### 8.1 Prerequisitos

```bash
# 1. Tener Claude Code instalado
npm install -g @anthropic-ai/claude-code

# 2. AWS CLI configurado con credenciales de solo lectura (recomendado)
aws configure
# AWS Access Key ID: AKIA...
# AWS Secret Access Key: ...
# Default region name: us-east-1
# Default output format: json

# 3. Verificar acceso
aws sts get-caller-identity
```

### 8.2 CloudWatch MCP Server — Instalación y Uso

El CloudWatch MCP Server de AWS Labs permite análisis profundo de logs, métricas y trazas desde Claude Code.

```bash
# Instalar el MCP server globalmente
npm install -g @aws-samples/amazon-cloudwatch-mcp-server

# O con uvx (Python)
pip install amazon-cloudwatch-mcp-server
```

**Configurar en Claude Code** — agregar al archivo `~/.claude/claude_desktop_config.json` (o el equivalente de Claude Code):

```json
{
  "mcpServers": {
    "cloudwatch": {
      "command": "npx",
      "args": ["-y", "@aws-samples/amazon-cloudwatch-mcp-server"],
      "env": {
        "AWS_REGION": "us-east-1",
        "AWS_PROFILE": "default"
      }
    }
  }
}
```

**Comandos de ejemplo en Claude Code:**

```
# Ver alarmas activas
"¿Qué alarmas de CloudWatch están en estado ALARM ahora?"

# Análisis de causa raíz
"La función Lambda aws-monitor-agent-actions tuvo errores hace 30 minutos. Analiza los logs y dime qué pasó."

# Logs Insights
"Busca en los logs de /aws/lambda/aws-monitor-agent-actions los errores de las últimas 2 horas"

# Métricas
"Muéstrame las invocaciones y errores de todas las funciones Lambda en us-east-1 en las últimas 24 horas"
```

**Herramientas disponibles en el CloudWatch MCP:**

| Herramienta | Descripción |
|-------------|-------------|
| `list_metrics` | Lista métricas disponibles por namespace/dimensión |
| `get_metric_data` | Obtiene datos de métricas con estadísticas |
| `describe_alarms` | Lista alarmas y su estado actual |
| `get_log_events` | Lee eventos de un log stream específico |
| `start_query` / `get_query_results` | CloudWatch Logs Insights queries |
| `describe_log_groups` | Lista grupos de logs disponibles |
| `get_metric_statistics` | Estadísticas de una métrica con período |

### 8.3 AWS API MCP Server — Instalación y Uso

⚠️ **Advertencia de seguridad:** Este MCP tiene acceso a todas las APIs de AWS que tus credenciales permitan. Usar únicamente con credenciales de solo lectura en contextos de desarrollo.

```bash
# Instalar
npm install -g @awslabs/aws-api-mcp-server
```

**Configurar en Claude Code:**

```json
{
  "mcpServers": {
    "aws-api": {
      "command": "npx",
      "args": ["-y", "@awslabs/aws-api-mcp-server"],
      "env": {
        "AWS_REGION": "us-east-1",
        "AWS_PROFILE": "readonly"
      }
    }
  }
}
```

**Recomendación:** Crear un perfil AWS `readonly` dedicado para uso con MCPs:

```bash
# Crear perfil de solo lectura para MCPs
aws configure --profile readonly
# Usar credenciales de un usuario IAM con política ReadOnlyAccess
```

**Comandos de ejemplo:**

```
# Listar recursos
"Lista todas las funciones Lambda en us-east-1"
"¿Cuántas instancias EC2 están corriendo?"
"Muestra las alarmas de CloudWatch en estado ALARM"

# Costos (con el MCP de costos también instalado)
"¿Cuánto gasté en AWS este mes?"
```

### 8.4 Configuración Recomendada para el Equipo Técnico de 3htp

Configuración `~/.claude/claude_desktop_config.json` con ambos MCPs para el equipo técnico:

```json
{
  "mcpServers": {
    "cloudwatch": {
      "command": "npx",
      "args": ["-y", "@aws-samples/amazon-cloudwatch-mcp-server"],
      "env": {
        "AWS_REGION": "us-east-1",
        "AWS_PROFILE": "readonly"
      }
    },
    "aws-api": {
      "command": "npx",
      "args": ["-y", "@awslabs/aws-api-mcp-server"],
      "env": {
        "AWS_REGION": "us-east-1",
        "AWS_PROFILE": "readonly"
      }
    }
  }
}
```

**Política IAM mínima recomendada** para el perfil `readonly` de MCPs:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ec2:Describe*",
        "lambda:List*",
        "lambda:Get*",
        "cloudwatch:Describe*",
        "cloudwatch:Get*",
        "cloudwatch:List*",
        "logs:Describe*",
        "logs:Get*",
        "logs:Filter*",
        "logs:StartQuery",
        "logs:StopQuery",
        "xray:Get*",
        "xray:BatchGet*"
      ],
      "Resource": "*"
    }
  ]
}
```

### 8.5 Cuándo Usar Cada Herramienta — Guía de Decisión

```
¿El usuario es técnico (developer/SRE)?
├── NO → Usar la POC AWS Monitor (URL del chat)
└── SÍ → ¿Qué necesita hacer?
    ├── Vista general de salud → POC AWS Monitor (más rápido, lenguaje natural)
    ├── Análisis de logs en detalle → CloudWatch MCP Server en Claude Code
    ├── Root cause analysis de un incidente → CloudWatch MCP Server
    ├── Consulta ad-hoc de cualquier API AWS → AWS API MCP Server
    └── Escribir código o IaC relacionado → AWS API MCP Server + Claude Code
```

---

## 9. Resumen de Decisión

La POC AWS Monitor fue construida como solución independiente porque:

1. **Los MCPs son herramientas para desarrolladores** — requieren instalación, CLI, credenciales locales
2. **La POC es un producto para usuarios finales** — URL en el browser, sin setup
3. **Seguridad controlada** — IAM Role de solo lectura fijo vs. credenciales variables del desarrollador
4. **Scope definido** — 4 herramientas específicas vs. acceso universal al API de AWS
5. **Disponibilidad** — CloudWatch MCP en preview, AWS API MCP llegó a GA después del diseño de la POC
6. **Branded experience** — el agente de la POC tiene personalidad, restricciones y contexto definido

**Las dos soluciones coexisten y se complementan.** La POC sirve a toda la organización. Los MCPs potencian al equipo técnico que ya usa Claude Code como herramienta de desarrollo.

---

*Documento generado: 2026-06-03 | Proyecto: AWS Monitor Agent — 3htp | Responsable: asilveira@3htp.com*
