# Guía: Instalar CloudWatch Agent en EC2 para monitoreo de procesos
> Aplica a: cualquier instancia EC2 en la cuenta 3HTP Corporate (369595298303)  
> Última actualización: 2026-06-18 · Basado en AWS CloudWatch Agent v1.300+

---

## ¿Por qué algunas instancias funcionan automáticamente y otras no?

El sistema aws-monitor incluye un mecanismo de auto-provisioning que instala y configura el CloudWatch Agent automáticamente. Sin embargo, **solo actúa en instancias que SSM conoce**.

Para que SSM conozca una instancia, esta necesita:
1. **SSM Agent** instalado y corriendo en la instancia
2. **IAM Role** con la policy `AmazonSSMManagedInstanceCore`

| Situación | ¿Auto-provisioning funciona? | Acción requerida |
|-----------|------------------------------|-----------------|
| Instancia tiene SSM Agent + rol con `AmazonSSMManagedInstanceCore` + `CloudWatchAgentServerPolicy` | ✅ Sí, automático | Ninguna |
| Instancia tiene SSM Agent + rol, pero falta `CloudWatchAgentServerPolicy` | ⚠️ Parcial | Agregar `CloudWatchAgentServerPolicy` al rol (Opción A) |
| Instancia no tiene SSM Agent o no tiene IAM Role | ❌ No | Instalar SSM Agent + configurar IAM (Opción B) |

**Ejemplos reales en este ambiente:**
- **SCT-Test (Windows):** Tenía SSMInstanceProfileID con `AmazonSSMManagedInstanceCore` → auto-provisioning funcionó (faltaba `CloudWatchAgentServerPolicy`, se agregó manualmente)
- **IBMWebMethod (Linux):** Igual que SCT-Test → se agregó `CloudWatchAgentServerPolicy` manualmente
- **jpnunez-dev (Linux):** Probablemente sin SSM Agent o sin IAM Role → requiere Opción B completa

---

## Objetivo

Habilitar métricas de **procesos individuales** (CPU y memoria por proceso) en cualquier instancia EC2 para que el agente de monitoreo pueda responder preguntas como:
> *"Dame los 5 procesos que consumen más CPU y memoria en jpnunez-dev"*

---

## Opción A — La instancia aparece en SSM Fleet Manager pero sin métricas de procesos

Usar esta opción cuando la instancia **sí aparece** en SSM → Fleet Manager con estado "Online", pero el agente no responde sobre procesos.

### Paso 1 — Verificar el IAM Role de la instancia

1. **EC2** → **Instances** → seleccionar la instancia
2. Pestaña **Security** → ver el campo **IAM Role**
3. Hacer clic en el nombre del rol → se abre IAM
4. En **Permissions policies**, verificar que existen ambas:
   - `AmazonSSMManagedInstanceCore` ← para que SSM pueda gestionarla
   - `CloudWatchAgentServerPolicy` ← para que el agente envíe métricas a CloudWatch

**Si falta `CloudWatchAgentServerPolicy`** → agregar sin tocar las demás:
1. En IAM → el rol → **Add permissions** → **Attach policies**
2. Buscar `CloudWatchAgentServerPolicy` → seleccionar → **Add permissions**
3. ⚠️ No modificar ni eliminar ninguna política existente

### Paso 2 — Forzar instalación del CW Agent ahora

1. **Systems Manager** → **State Manager**
2. Buscar la asociación `monitor-cwagent-setup`
3. Seleccionarla → clic en **Apply association now**
4. Esperar 2-3 minutos

### Paso 3 — Verificar éxito

1. **Systems Manager** → **State Manager** → `monitor-cwagent-setup`
2. La columna **Status** debe mostrar **Success** para la instancia
3. Hacer clic en la ejecución → verificar que **Step 1** (install) y **Step 2** (configure) son ambos **Success**

### Paso 4 — Validar métricas en CloudWatch

1. **CloudWatch** → **Metrics** → **All metrics** → namespace **CWAgent**
2. La instancia debe aparecer con métricas `procstat cpu_usage` / `procstat_cpu_usage` y `procstat memory_rss` / `procstat_memory_rss`
3. Si aparecen → el agente de monitoreo ya puede responder sobre procesos

---

## Opción B — La instancia NO aparece en SSM Fleet Manager

Usar esta opción cuando la instancia **no aparece** en SSM → Fleet Manager. Requiere acceso SSH a la instancia o acceso a la consola EC2 (Session Manager o key pair).

### Paso B1 — Agregar IAM Role a la instancia

1. **EC2** → **Instances** → seleccionar la instancia
2. Pestaña **Security** → ver el campo **IAM Role**

**Si ya tiene un rol:**
- **IAM** → buscar el rol → **Add permissions** → **Attach policies**
- Agregar: `AmazonSSMManagedInstanceCore` + `CloudWatchAgentServerPolicy`
- ⚠️ NO eliminar ninguna política existente

**Si no tiene rol:**
1. **IAM** → **Roles** → **Create role**
2. Trusted entity: **AWS service** → **EC2**
3. Attach policies: `AmazonSSMManagedInstanceCore` + `CloudWatchAgentServerPolicy`
4. Nombre del rol: `EC2-CloudWatch-Monitor-Role`
5. **EC2** → seleccionar la instancia → **Actions** → **Security** → **Modify IAM role** → seleccionar el rol recién creado

### Paso B2 — Instalar SSM Agent en la instancia

Conectarse a la instancia vía SSH o EC2 Instance Connect y ejecutar según el SO:

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

**Windows Server:**
El SSM Agent viene preinstalado en AMIs de Windows de AWS. Verificar que está corriendo:
```powershell
Get-Service AmazonSSMAgent
```

**Verificar que SSM Agent está activo (Linux):**
```bash
sudo systemctl status amazon-ssm-agent
```
Debe mostrar `active (running)`.

### Paso B3 — Confirmar que la instancia aparece en Fleet Manager

1. **Systems Manager** → **Fleet Manager**
2. Buscar la instancia por nombre o ID — debe aparecer con estado **Online**
3. Si no aparece en 2-3 minutos: verificar que el IAM Role tiene `AmazonSSMManagedInstanceCore` y que SSM Agent está corriendo

### Paso B4 — Ejecutar la asociación

1. **Systems Manager** → **State Manager**
2. Buscar la asociación `monitor-cwagent-setup`
3. Seleccionarla → clic en **Apply association now**
4. Esperar 3-5 minutos

### Paso B5 — Validar

Igual que los pasos 3 y 4 de la Opción A.

---

## Verificación final — Preguntarle al agente de monitoreo

Una vez completados los pasos, abrir el chat del agente y preguntar:
```
Dame los 5 procesos que consumen más CPU y memoria en [nombre-instancia]
```

La respuesta debe mostrar una tabla con procesos reales, porcentaje de CPU y uso de memoria.

---

## Tiempo estimado

| Opción | Tiempo total |
|--------|-------------|
| Opción A (solo falta CloudWatchAgentServerPolicy) | 5 minutos |
| Opción B (instalación desde cero) | 15-20 minutos |

---

## Recuperación automática tras reinicios

| SO | Comportamiento tras reinicio |
|----|------------------------------|
| Linux | El CW Agent corre como servicio `systemd` — arranca automáticamente |
| Windows | El SSM schedule `rate(1 day)` reinstala y reconfigura el agente en máximo 24h |

No se requiere ninguna acción manual tras un reinicio normal de la instancia.

---

## Notas importantes

- **No se modifica ni elimina ninguna política IAM existente** — solo se agregan las necesarias
- El CloudWatch Agent solo **lee** métricas del sistema operativo, no ejecuta ni modifica procesos
- Las métricas se almacenan en CloudWatch bajo el namespace `CWAgent` — no interfiere con otros sistemas de monitoreo existentes
- El costo adicional de CloudWatch por estas métricas personalizadas es de aproximadamente **$0.30/mes por instancia** con uso normal
- Las métricas de procesos en Linux se publican como `procstat_cpu_usage` (guión bajo); en Windows como `procstat cpu_usage` (espacio) — el agente de monitoreo maneja ambos formatos automáticamente
