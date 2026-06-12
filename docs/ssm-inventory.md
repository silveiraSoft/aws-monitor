# SSM Inventory — Guía de Implementación

**Fecha:** 2026-06-11  
**Autor:** aws-monitor-agent  
**Estado:** Implementado y testeado

---

## ¿Qué es SSM Inventory?

AWS Systems Manager Inventory recopila automáticamente metadatos de los servidores administrados:
- **Sistema operativo:** distribución, versión del kernel, arquitectura
- **Aplicaciones instaladas:** nombre, versión, publisher, fecha de instalación
- **Componentes AWS:** CloudWatch Agent, SSM Agent, AWS CLI
- **Configuración de red:** interfaces, IPs, DNS, gateway
- **Parches:** estado de compliance de parches (Windows/Linux)

Es un servicio **gratuito** — solo se paga si se usa SSM Patch Manager o Session Manager con EC2 Instance Connect.

---

## Prerrequisitos (obligatorios)

Para que una instancia EC2 aparezca en SSM Inventory, se necesitan **tres condiciones**:

### 1. SSM Agent instalado y ejecutándose
```bash
# Verificar en la instancia EC2
sudo systemctl status amazon-ssm-agent

# Instalar si no está (Amazon Linux 2)
sudo yum install -y amazon-ssm-agent
sudo systemctl enable amazon-ssm-agent
sudo systemctl start amazon-ssm-agent

# Instalar en Ubuntu
sudo snap install amazon-ssm-agent --classic
sudo systemctl enable snap.amazon-ssm-agent.amazon-ssm-agent.service
sudo systemctl start snap.amazon-ssm-agent.amazon-ssm-agent.service
```

### 2. IAM Role con `AmazonSSMManagedInstanceCore`
Adjuntar el AWS managed policy a la instancia EC2:
```
Policy ARN: arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
```
En CDK:
```typescript
ec2Role.addManagedPolicy(
  iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonSSMManagedInstanceCore')
);
```

### 3. Conectividad con endpoints SSM
La instancia debe alcanzar estos endpoints (sin Internet Gateway se necesitan VPC Endpoints):
- `ssm.us-east-1.amazonaws.com`
- `ssmmessages.us-east-1.amazonaws.com`
- `ec2messages.us-east-1.amazonaws.com`

---

## Tipos de inventario disponibles

| Tipo | Descripción | Tiempo para aparecer |
|------|-------------|---------------------|
| `AWS:InstanceInformation` | SO, kernel, agent version, IP (default) | ~5 min |
| `AWS:Application` | Apps instaladas: nombre, versión, publisher | ~30 min |
| `AWS:AWSComponent` | CloudWatch Agent, SSM Agent, AWS CLI | ~30 min |
| `AWS:Network` | Interfaces de red, IPs, MAC, DNS | ~30 min |
| `AWS:WindowsUpdate` | Windows Update KB instalados | ~1 hora (solo Windows) |
| `AWS:PatchSummary` | Resumen de parches: installed/missing/failed | ~1 hora |
| `AWS:PatchCompliance` | Detalle por parche | ~1 hora |
| `AWS:ComplianceSummary` | Estado de compliance general | ~1 hora |
| `ALL` | Combina: InstanceInformation + Application + AWSComponent + Network | — |

---

## Uso en el Agente de Monitoreo

### Preguntas que el agente puede responder

```
¿Qué sistema operativo tienen mis instancias EC2?
¿Qué versión de Python está instalada en i-0abc123?
¿Qué aplicaciones tienen instaladas mis servidores?
Muéstrame el estado de los parches en us-east-1
¿Qué componentes AWS están instalados en mis instancias?
¿Cuál es la configuración de red de la instancia i-0abc123?
```

### Parámetros de la acción `get_ssm_inventory`

| Parámetro | Tipo | Requerido | Default | Descripción |
|-----------|------|-----------|---------|-------------|
| `instance_id` | string | No | (todos) | Filtrar por instancia específica (ej: `i-0abc123`) |
| `inventory_type` | enum | No | `AWS:InstanceInformation` | Tipo de inventario a consultar |
| `region` | string | No | `us-east-1` | Región AWS a consultar |

### Ejemplos de respuesta

**`AWS:InstanceInformation` (default):**
```json
{
  "region": "us-east-1",
  "managed_instance_count": 2,
  "inventory_type_queried": "AWS:InstanceInformation",
  "instances": [
    {
      "instance_id": "i-0abc123def456",
      "computer_name": "ip-10-0-1-5.ec2.internal",
      "platform_type": "Linux",
      "platform_name": "Amazon Linux 2",
      "platform_version": "2",
      "agent_version": "3.2.985.0",
      "ip_address": "10.0.1.5",
      "ping_status": "Online",
      "last_ping": "2026-06-11T14:30:00+00:00",
      "association_status": "Success",
      "resource_type": "ManagedInstance"
    }
  ],
  "inventory": [],
  "note": "SSM Inventory requires: (1) SSM Agent running, (2) AmazonSSMManagedInstanceCore role, (3) Inventory collection configured."
}
```

**`AWS:Application`:**
```json
{
  "inventory": [
    {
      "instance_id": "i-0abc123def456",
      "inventory_type": "AWS:Application",
      "type_label": "installed applications",
      "count": 47,
      "entries": [
        {"Name": "python3", "Version": "3.9.16", "Publisher": "Amazon"},
        {"Name": "nginx", "Version": "1.22.1", "Publisher": "nginx.org"},
        {"Name": "nodejs", "Version": "18.12.0", "Publisher": "Node.js Foundation"}
      ]
    }
  ]
}
```

---

## Configurar Inventory Collection

SSM Inventory requiere que se configure explícitamente en SSM. Hay dos formas:

### Opción A: Quick Setup (recomendado para empezar)
1. AWS Console → Systems Manager → Quick Setup
2. Seleccionar "Host Management"
3. Activar "Collect inventory from your instances every 30 minutes"
4. Aplicar a todas las instancias o a un grupo específico

### Opción B: State Manager Association (control fino)
```bash
aws ssm create-association \
  --name "AWS-GatherSoftwareInventory" \
  --targets "Key=InstanceIds,Values=*" \
  --schedule-expression "rate(30 minutes)" \
  --parameters '{}' \
  --region us-east-1
```

---

## Costos

| Concepto | Costo |
|----------|-------|
| SSM Inventory collection | **Gratis** |
| SSM Agent en EC2 | **Gratis** |
| API calls (DescribeInstanceInformation, GetInventory) | **Gratis** |
| SSM Patch Manager (opcional) | $0.0178/instancia/mes |
| SSM Session Manager (opcional) | $0.00005/minuto |

**Conclusión: SSM Inventory + este agente = $0 en costo de SSM.**

---

## Troubleshooting

### "No SSM-managed instances found"

1. Verificar que SSM Agent esté corriendo:
   ```bash
   sudo systemctl status amazon-ssm-agent
   ```

2. Verificar que el IAM Role tenga `AmazonSSMManagedInstanceCore`:
   ```bash
   aws iam list-attached-role-policies --role-name <EC2-ROLE-NAME>
   ```

3. Verificar conectividad con SSM:
   ```bash
   curl -I https://ssm.us-east-1.amazonaws.com
   ```

4. Revisar logs del SSM Agent:
   ```bash
   sudo tail -100 /var/log/amazon/ssm/amazon-ssm-agent.log
   ```

### "Inventory is empty for managed instances"

La instancia aparece como managed pero no tiene datos de inventario aún:
- Configurar Inventory Collection vía Quick Setup o State Manager Association
- Esperar 30 minutos después de configurar
- Verificar que el tipo de inventario solicitado sea compatible con el SO (ej: `AWS:WindowsUpdate` solo en Windows)

### El agente responde "SSM Agent must be running"

El agente Bedrock incluye esta explicación automáticamente cuando no encuentra instancias managed. Es el comportamiento correcto — el sistema prompt instruye al agente a siempre explicar los prerrequisitos cuando SSM Inventory devuelve cero instancias.

---

## Permisos IAM requeridos (Action Lambda)

Agregados en `lib/monitor-agent-stack.ts`:
```
ssm:DescribeInstanceInformation   — Listar instancias administradas por SSM
ssm:ListInventoryEntries          — Listar entradas de inventario por tipo
ssm:GetInventory                  — Obtener datos de inventario completos
```

Todos son permisos de **solo lectura** — el agente no puede modificar configuración SSM.

---

## Arquitectura de la integración

```
Usuario: "¿Qué apps tiene i-0abc123?"
  └─► Bedrock Agent → Claude Haiku 4.5
        └─► get_ssm_inventory(instance_id="i-0abc123", inventory_type="AWS:Application")
              └─► Lambda aws-monitor-agent-actions
                    └─► ssm.describe_instance_information() — verificar que está managed
                    └─► ssm.get_inventory(TypeName="AWS:Application") — obtener apps
                    └─► Respuesta: lista de apps con versiones
```
