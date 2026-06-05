#!/usr/bin/env python3
"""
AWS Monitor Agent — Deploy Cleanup (fin de día / post-pruebas)
===============================================================
Elimina los dos stacks de aplicación y todos sus recursos de us-east-1.

ESTE ES EL SCRIPT QUE DEBES EJECUTAR AL FINAL DEL DÍA para evitar gastos.

Recursos que elimina:
  AwsMonitorAgentStack:
    - Lambda: aws-monitor-agent-actions
    - S3 bucket: aws-monitor-schema-369595298303-us-east-1
    - IAM Role: AmazonBedrockExecutionRoleForAgents_AwsMonitor
    - Bedrock Agent: aws-monitor-agent + alias 'live'

  AwsMonitorFrontendStack:
    - Lambda: aws-monitor-chat-proxy
    - API Gateway: aws-monitor-chat-api
    - S3 bucket: aws-monitor-chat-ui-369595298303-us-east-1
    - CloudFront distribution

Costo estimado de recursos ACTIVOS (sin tráfico):
  - CloudFront:       ~$0/mes (sin peticiones)
  - API Gateway:      ~$0/mes (sin llamadas)
  - Lambda:           ~$0/mes (sin invocaciones)
  - S3 (2 buckets):   < $0.01/mes (solo almacenamiento ~KB)
  - Bedrock Agent:    ~$0/mes (cobra solo por tokens usados)
  TOTAL EN REPOSO:    < $0.02/mes — riesgo real es USO, no existencia

  Sin embargo, si experimentas mucho con el agente durante el desarrollo,
  ejecutar este script al final del día elimina cualquier riesgo de gasto
  acumulado por pruebas.

NO elimina:
  - Stack CDKToolkit (bootstrap) — usa cleanup_bootstrap.py para eso
  - Logs de CloudWatch (se limpian solos a los 30 días)

Usage:
    python cleanup_deploy.py            # elimina ambos stacks
    python cleanup_deploy.py --dry-run  # muestra qué eliminaría sin hacerlo
"""

import sys
import time
import getpass
import argparse

try:
    import boto3
    import botocore.exceptions
except ImportError:
    print("\n[ERROR] boto3 no instalado. Ejecuta: pip install boto3\n")
    sys.exit(1)

REGION  = "us-east-1"
ACCOUNT = "369595298303"
STACKS  = [
    "AwsMonitorFrontendStack",  # eliminar frontend primero (depende del agente)
    "AwsMonitorAgentStack",
]
BUCKETS = [
    f"aws-monitor-schema-{ACCOUNT}-{REGION}",
    f"aws-monitor-chat-ui-{ACCOUNT}-{REGION}",
]


# ── Colores ──────────────────────────────────────────────────────────────────
def green(t):  return f"\033[92m{t}\033[0m"
def red(t):    return f"\033[91m{t}\033[0m"
def yellow(t): return f"\033[93m{t}\033[0m"
def bold(t):   return f"\033[1m{t}\033[0m"
def dim(t):    return f"\033[2m{t}\033[0m"


def get_credentials():
    print(bold("\n  AWS Monitor Agent — Deploy Cleanup"))
    print(f"  Región: {REGION}  |  Stacks: {', '.join(STACKS)}\n")
    print("  Elimina todos los recursos del agente para evitar gastos en desarrollo.")
    print(dim("  Para volver a desplegar: npm run deploy\n"))
    key_id = input("  AWS Access Key ID     : ").strip()
    secret = getpass.getpass("  AWS Secret Access Key : ")
    return boto3.Session(
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
        region_name=REGION,
    )


def stack_exists(cfn, stack_name):
    try:
        resp = cfn.describe_stacks(StackName=stack_name)
        status = resp["Stacks"][0]["StackStatus"]
        return status not in ("DELETE_COMPLETE",)
    except botocore.exceptions.ClientError:
        return False


def empty_bucket(s3, bucket_name, dry_run=False):
    """Vacía el bucket S3 para que CloudFormation pueda eliminarlo."""
    try:
        s3.head_bucket(Bucket=bucket_name)
    except botocore.exceptions.ClientError:
        print(f"    {yellow('⚠')} Bucket {bucket_name} no existe — omitido.")
        return

    print(f"    Vaciando {bucket_name}...", end="", flush=True)
    if dry_run:
        print(f" {dim('[DRY-RUN — no se ejecuta]')}")
        return

    # Eliminar versiones y markers
    paginator = s3.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket_name):
        objs = []
        for obj in page.get("Versions", []):
            objs.append({"Key": obj["Key"], "VersionId": obj["VersionId"]})
        for obj in page.get("DeleteMarkers", []):
            objs.append({"Key": obj["Key"], "VersionId": obj["VersionId"]})
        if objs:
            s3.delete_objects(Bucket=bucket_name, Delete={"Objects": objs})

    # Eliminar objetos sin versión
    paginator2 = s3.get_paginator("list_objects_v2")
    for page in paginator2.paginate(Bucket=bucket_name):
        objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if objs:
            s3.delete_objects(Bucket=bucket_name, Delete={"Objects": objs})

    print(f" {green('vacío')}")


def delete_stack(cfn, stack_name, dry_run=False):
    """Elimina un stack CloudFormation y espera a que complete."""
    if not stack_exists(cfn, stack_name):
        print(f"  {yellow('⚠')} Stack {stack_name} no existe — omitido.")
        return True

    print(f"\n  Eliminando {bold(stack_name)}...")
    if dry_run:
        print(f"  {dim('[DRY-RUN — no se ejecuta]')}")
        return True

    cfn.delete_stack(StackName=stack_name)

    print("  Progreso", end="", flush=True)
    start = time.time()
    while True:
        time.sleep(8)
        elapsed = int(time.time() - start)
        print(f"\r  Progreso [{elapsed}s]", end="", flush=True)
        try:
            resp = cfn.describe_stacks(StackName=stack_name)
            status = resp["Stacks"][0]["StackStatus"]
            if status == "DELETE_COMPLETE":
                print(f"\r  {green('✔')} {stack_name} eliminado ({elapsed}s)")
                return True
            if "FAILED" in status or "ROLLBACK" in status:
                print(f"\r  {red('✘')} {stack_name} — estado: {status}")
                reasons = [
                    r.get("ResourceStatusReason", "")
                    for r in cfn.describe_stack_events(StackName=stack_name)["StackEvents"]
                    if "FAILED" in r.get("ResourceStatus", "")
                ]
                for r in reasons[:3]:
                    print(f"      {red('→')} {r}")
                return False
        except botocore.exceptions.ClientError as e:
            if "does not exist" in str(e):
                print(f"\r  {green('✔')} {stack_name} eliminado ({elapsed}s)")
                return True
            raise


def print_cost_summary():
    print(f"\n  {bold('Estimación de costos — us-east-1')}")
    print("  ─────────────────────────────────────────────────")
    rows = [
        ("CloudFront",        "~$0/mes",      "sin peticiones"),
        ("API Gateway",       "~$0/mes",      "sin llamadas"),
        ("Lambda (2)",        "~$0/mes",      "sin invocaciones"),
        ("S3 (2 buckets)",    "< $0.01/mes",  "solo almacenamiento ~KB"),
        ("Bedrock Agent",     "~$0/mes",      "cobra por tokens usados"),
        ("CDKToolkit (boot)", "~$0/mes",      "IAM + SSM son gratis"),
    ]
    for name, cost, note in rows:
        print(f"  {'%-24s' % name} {yellow('%-14s' % cost)} {dim(note)}")
    print(f"  {'─'*49}")
    print(f"  {'%-24s' % 'TOTAL EN REPOSO'} {yellow('< $0.02/mes')}")
    print(f"\n  {dim('El gasto real viene del USO: tokens Bedrock (~$0.0008/1K tokens),')}")
    print(f"  {dim('peticiones API Gateway ($3.50/millón), invocaciones Lambda ($0.20/millón).')}")
    print()


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    session = get_credentials()

    # Verificar identidad
    sts = session.client("sts")
    try:
        identity = sts.get_caller_identity()
        print(f"\n  {green('✔')} Credenciales válidas — ARN: {identity['Arn']}")
    except botocore.exceptions.ClientError as e:
        print(f"\n  {red('✘')} Credenciales inválidas: {e}")
        sys.exit(1)

    print_cost_summary()

    if args.dry_run:
        print(f"  {yellow('[DRY-RUN]')} Simulando eliminación — no se modifica nada.\n")
    else:
        print(f"  {yellow('ADVERTENCIA:')} Se eliminarán los siguientes recursos en {REGION}:")
        for s in STACKS:
            print(f"    - Stack: {s}")
        for b in BUCKETS:
            print(f"    - S3 bucket: {b} (vaciado + eliminado)")
        print(f"\n  Para volver a desplegar: {dim('npm run deploy')}")
        confirm = input("\n  ¿Confirmar eliminación? (escribe 'si' para continuar): ").strip().lower()
        if confirm != "si":
            print("  Operación cancelada.")
            sys.exit(0)

    s3  = session.client("s3",             region_name=REGION)
    cfn = session.client("cloudformation", region_name=REGION)

    # 1. Vaciar buckets primero (CDK no puede eliminarlos con contenido)
    print(f"\n  Vaciando buckets S3...")
    for bucket in BUCKETS:
        empty_bucket(s3, bucket, dry_run=args.dry_run)

    # 2. Eliminar stacks en orden
    results = {}
    for stack in STACKS:
        results[stack] = delete_stack(cfn, stack, dry_run=args.dry_run)

    # Resumen final
    print(f"\n  {'─'*50}")
    all_ok = all(results.values())
    if args.dry_run:
        print(f"  {bold('DRY-RUN completado.')} Sin cambios en AWS.")
    elif all_ok:
        print(f"  {green(bold('✔ Limpieza completada.'))} Todos los recursos eliminados.")
        print(f"  Para volver a desplegar: {dim('python validate_aws_access.py && npm run deploy')}")
    else:
        failed = [s for s, ok in results.items() if not ok]
        print(f"  {red('✘ Limpieza incompleta.')} Stacks con error: {', '.join(failed)}")
        print(f"  Revisa CloudFormation en la consola de AWS ({REGION}).")
    print()


if __name__ == "__main__":
    main()
