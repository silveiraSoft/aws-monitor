# Guía: Instalar CloudWatch Agent en EC2 para monitoreo de procesos
> Instancia objetivo: **IBMWebMethod** (Linux) · Cuenta: 3HTP Corporate (369595298303)  
> Última actualización: 2026-06-18 · Basado en AWS CloudWatch Agent v1.300+

---

## Objetivo

Habilitar métricas de **procesos individuales** (CPU y memoria por proceso) en la instancia IBMWebMethod para que el agente de monitoreo pueda responder preguntas como:
> *"Dame los 5 procesos que consumen más CPU y memoria en IBMWebMethod"*

---

## Opción A — Automática vía SSM (recomendada, sin SSH)

Esta opción funciona si IBMWebMethod tiene SSM Agent activo. El sistema aws-monitor hace todo automáticamente.

### Paso 1 — Verificar que IBMWebMethod está en SSM Fleet Manager

1. Ir a **AWS Console** → buscar **Systems Manager**
2. En el menú izquierdo: **Fleet Manager**
3. Buscar `IBMWebMethod` en la lista de nodos

**Si aparece con estado "Online"** → continuar con Paso 2.  
**Si no aparece** → ir a la Opción B más abajo.

### Paso 2 — Verificar que el IAM Role de la instancia tiene los permisos necesarios

1. **EC2** → **Instances** → seleccionar `IBMWebMethod`
2. Pestaña **Security** → ver el campo **IAM Role**
3. Hacer clic en el nombre del rol → se abre IAM
4. En **Permissions policies**, verificar que existe alguna de estas:
   - `AmazonSSMManagedInstanceCore` ← para que SSM pueda gestionarla
   - `CloudWatchAgentServerPolicy` ← para que el agente envíe métricas

**Si ambas políticas existen** → el sistema aws-monitor las instalará automáticamente en el próximo deploy (`npm run deploy`). No se necesita ninguna acción manual adicional.

**Si falta `CloudWatchAgentServerPolicy`** → agregar sin quitar las existentes:
1. En IAM → el rol de la instancia → **Add permissions** → **Attach policies**
2. Buscar `CloudWatchAgentServerPolicy` → seleccionar → **Add permissions**
3. ⚠️ No modificar ni eliminar ninguna política existente

### Paso 3 — Forzar la instalación ahora (sin esperar el schedule semanal)

1. **Systems Manager** → **State Manager**
2. Buscar la asociación `monitor-cwagent-setup`
3. Seleccionarla → clic en **Apply association now**
4. Esperar 2-3 minutos

### Paso 4 — Verificar que el agente está corriendo

1. **Systems Manager** → **State Manager** → `monitor-cwagent-setup`
2. La columna **Status** debe mostrar **Success** para IBMWebMethod
3. Hacer clic en la ejecución → verificar que **Step 1** (install) y **Step 2** (configure) son ambos **Success**

### Paso 5 — Validar métricas en CloudWatch

1. **CloudWatch** → **Metrics** → **All metrics**
2. Buscar namespace **CWAgent**
3. Debe aparecer `IBMWebMethod` con métricas `procstat cpu_usage` y `procstat memory_rss`
4. Si aparecen → el agente de monitoreo ya puede responder sobre procesos

---

## Opción B — Manual vía SSM Run Command (si NO tiene SSM Agent)

Si IBMWebMethod no aparece en Fleet Manager, instalar SSM Agent primero y luego el CloudWatch Agent.

### Paso B1 — Instalar SSM Agent en la instancia (requiere acceso SSH o acceso a la consola EC2)

Conectarse a IBMWebMethod y ejecutar:

**Amazon Linux 2 / Amazon Linux 2023:**
```bash
sudo dnf install -y amazon-ssm-agent
sudo systemctl enable amazon-ssm-agent
sudo systemctl start amazon-ssm-agent
```

**Ubuntu 20.04 / 22.04 / 24.04:**
```bash
sudo snap install amazon-ssm-agent --classic
sudo systemctl enable snap.amazon-ssm-agent.amazon-ssm-agent.service
sudo systemctl start snap.amazon-ssm-agent.amazon-ssm-agent.service
```

**Verificar que SSM Agent está activo:**
```bash
sudo systemctl status amazon-ssm-agent
```

Debe mostrar `active (running)`.

### Paso B2 — Agregar permisos IAM a la instancia

1. **EC2** → **Instances** → seleccionar `IBMWebMethod`
2. **Actions** → **Security** → **Modify IAM role**
3. Si ya tiene un rol:
   - Ir a **IAM** → buscar el rol → **Add permissions** → **Attach policies**
   - Agregar: `AmazonSSMManagedInstanceCore` y `CloudWatchAgentServerPolicy`
   - ⚠️ NO eliminar ninguna política existente
4. Si no tiene rol:
   - **IAM** → **Roles** → **Create role**
   - Trusted entity: **AWS service** → **EC2**
   - Attach policies: `AmazonSSMManagedInstanceCore` + `CloudWatchAgentServerPolicy`
   - Nombre del rol: `EC2-CloudWatch-Monitor-Role`
   - Volver a EC2 → **Modify IAM role** → seleccionar el rol recién creado

### Paso B3 — Instalar CloudWatch Agent vía SSM Run Command

Una vez que IBMWebMethod aparece en Fleet Manager:

1. **Systems Manager** → **Run Command** → **Run command**
2. **Command document**: buscar `AWS-ConfigureAWSPackage`
3. Parámetros:
   - Action: `Install`
   - Name: `AmazonCloudWatchAgent`
4. **Targets**: seleccionar `IBMWebMethod` manualmente
5. Clic en **Run** → esperar que el status sea **Success**

### Paso B4 — Configurar el CloudWatch Agent

1. **Systems Manager** → **Run Command** → **Run command**
2. **Command document**: buscar `AmazonCloudWatch-ManageAgent`
3. Parámetros:
   - Action: `configure`
   - Mode: `ec2`
   - Optional Configuration Source: `ssm`
   - Optional Configuration Location: `/AmazonCloudWatch-aws-monitor/config`
   - Optional Restart: `yes`
4. **Targets**: seleccionar `IBMWebMethod`
5. Clic en **Run** → esperar **Success**

### Paso B5 — Validar

Igual que el Paso 5 de la Opción A: verificar namespace `CWAgent` en CloudWatch con métricas de procesos de IBMWebMethod.

---

## Verificación final — Preguntarle al agente de monitoreo

Una vez completados los pasos, abrir el chat del agente en:
```
https://[URL-CloudFront].cloudfront.net
```

Y preguntar:
```
Dame los 5 procesos que consumen más CPU y memoria en IBMWebMethod
```

La respuesta debe mostrar una tabla con procesos reales, porcentaje de CPU y uso de memoria.

---

## Tiempo estimado

| Opción | Tiempo total |
|--------|-------------|
| Opción A (SSM ya activo) | 5 minutos |
| Opción B (instalación desde cero) | 15-20 minutos |

---

## Notas importantes

- **No se modifica ni elimina ninguna política IAM existente** — solo se agregan las necesarias
- El CloudWatch Agent solo **lee** métricas del sistema operativo, no ejecuta ni modifica procesos
- Las métricas se almacenan en CloudWatch bajo el namespace `CWAgent` — no interfiere con otros sistemas de monitoreo existentes
- El costo adicional de CloudWatch por estas métricas personalizadas es de aproximadamente **$0.30/mes por instancia** con uso normal
