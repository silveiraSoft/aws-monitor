#!/usr/bin/env python3
"""
AWS Monitor Agent — Bootstrap Cleanup
======================================
Elimina el stack CDKToolkit (cdk bootstrap) y todos sus recursos de us-east-1.

Recursos que elimina:
  - CloudFormation stack: CDKToolkit
  - S3 bucket: cdk-hnb659fds-assets-369595298303-us-east-1 (vaciado + eliminado)
  - IAM Roles creados por bootstrap (4 roles con prefijo cdk-hnb659fds-*)
  - SSM Parameter: /cdk-bootstrap/hnb659fds/version

Cuándo usar:
  - Cuando quieras limpiar completamente el entorno CDK de us-east-1
  - No es necesario ejecutarlo al final de cada día (bootstrap no genera gasto)
  - Ejecutar ANTES de cleanup_deploy.py si vas a limpiar todo

Costo del bootstrap: ~$0/mes (IAM + SSM son gratis; S3 solo cobra por contenido)

Usage:
    python cleanup_bootstrap.py
"""

import sys
import time
import getpass

try:
    import boto3
    import botocore.exceptions
except ImportError:
    print("\n[ERROR] boto3 no instalado. Ejecuta: pip install boto3\n")
    sys.exit(1)

REGION       = "us-east-1"
ACCOUNT      = "369595298303"
STACK_NAME   = "CDKToolkit"
BUCKET_NAME  = f"cdk-hnb659fds-assets-{ACCOUNT}-{REGION}"


# ── Colores ──────────────────────────────────────────────────────────────────
def green(t):  return f"\033[92m{t}\033[0m"
def red(t):    return f"\033[91m{t}\033[0m"
def yellow(t): return f"\033[93m{t}\033[0m"
def bold(t):   return f"\033[1m{t}\033[0m"


def get_credentials():
    print(bold("\n  AWS Monitor Agent — Bootstrap Cleanup"))
    print(f"  Región: {REGION}  |  Stack: {STACK_NAME}\n")
    print("  Este script elimina el stack CDKToolkit y sus recursos.")
    print("  Costo del bootstrap: ~$0/mes — solo limpia si necesitas un entorno fresco.\n")
    key_id = input("  AWS Access Key ID     : ").strip()
    secret = getpass.getpass("  AWS Secret Access Key : ")
    return boto3.Session(
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
        region_name=REGION,
    )


def empty_and_delete_bucket(s3, bucket_name):
    """Vacía y elimina el bucket S3 del bootstrap."""
    print(f"\n  Vaciando bucket {bucket_name}...")
    try:
        # Eliminar todos los objetos (incluye versiones si hay versionado)
        paginator = s3.get_paginator("list_object_versions")
        pages = paginator.paginate(Bucket=bucket_name)
        delete_list = []
        for page in pages:
            for obj in page.get("Versions", []):
                delete_list.append({"Key": obj["Key"], "VersionId": obj["VersionId"]})
            for obj in page.get("DeleteMarkers", []):
                delete_list.append({"Key": obj["Key"], "VersionId": obj["VersionId"]})
            if delete_list:
                s3.delete_objects(Bucket=bucket_name, Delete={"Objects": delete_list})
                delete_list = []

        # Eliminar objetos sin versión
        paginator2 = s3.get_paginator("list_objects_v2")
        for page in paginator2.paginate(Bucket=bucket_name):
            objects = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if objects:
                s3.delete_objects(Bucket=bucket_name, Delete={"Objects": objects})

        s3.delete_bucket(Bucket=bucket_name)
        print(f"  {green('✔')} Bucket eliminado: {bucket_name}")
    except botocore.exceptions.ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "NoSuchBucket":
            print(f"  {yellow('⚠')} Bucket no existe (ya fue eliminado o nunca se creó)")
        else:
            print(f"  {red('✘')} Error eliminando bucket: {e}")


def delete_stack(cfn, stack_name):
    """Elimina el stack CloudFormation y espera a que termine."""
    print(f"\n  Eliminando stack CloudFormation: {stack_name}...")
    try:
        cfn.describe_stacks(StackName=stack_name)
    except botocore.exceptions.ClientError:
        print(f"  {yellow('⚠')} Stack {stack_name} no existe — nada que eliminar.")
        return True

    try:
        cfn.delete_stack(StackName=stack_name)
    except botocore.exceptions.ClientError as e:
        print(f"  {red('✘')} No se pudo iniciar eliminación: {e}")
        return False

    print("  Esperando eliminación", end="", flush=True)
    while True:
        time.sleep(5)
        print(".", end="", flush=True)
        try:
            resp = cfn.describe_stacks(StackName=stack_name)
            status = resp["Stacks"][0]["StackStatus"]
            if "COMPLETE" in status and "DELETE" in status:
                print(f"\n  {green('✔')} Stack eliminado.")
                return True
            if "FAILED" in status:
                print(f"\n  {red('✘')} Eliminación fallida. Status: {status}")
                return False
        except botocore.exceptions.ClientError as e:
            if "does not exist" in str(e):
                print(f"\n  {green('✔')} Stack eliminado.")
                return True
            raise


def main():
    session = get_credentials()

    # Verificar identidad
    sts = session.client("sts")
    try:
        identity = sts.get_caller_identity()
        print(f"\n  {green('✔')} Credenciales válidas — ARN: {identity['Arn']}")
    except botocore.exceptions.ClientError as e:
        print(f"\n  {red('✘')} Credenciales inválidas: {e}")
        sys.exit(1)

    # Confirmación
    print(f"\n  {yellow('ADVERTENCIA:')} Se eliminarán los siguientes recursos en {REGION}:")
    print(f"    - CloudFormation stack: {STACK_NAME}")
    print(f"    - S3 bucket: {BUCKET_NAME}")
    print(f"    - IAM Roles con prefijo: cdk-hnb659fds-*")
    print(f"    - SSM Parameter: /cdk-bootstrap/hnb659fds/version")
    confirm = input("\n  ¿Confirmar eliminación? (escribe 'si' para continuar): ").strip().lower()
    if confirm != "si":
        print("  Operación cancelada.")
        sys.exit(0)

    s3  = session.client("s3", region_name=REGION)
    cfn = session.client("cloudformation", region_name=REGION)

    # 1. Vaciar y eliminar bucket primero (CloudFormation no puede eliminar bucket con contenido)
    empty_and_delete_bucket(s3, BUCKET_NAME)

    # 2. Eliminar stack (también elimina IAM Roles y SSM Parameter)
    success = delete_stack(cfn, STACK_NAME)

    print()
    if success:
        print(f"  {green(bold('Limpieza completada.'))} El entorno CDK de {REGION} está limpio.")
        print(f"  Para volver a usar CDK ejecuta: npx cdk bootstrap aws://{ACCOUNT}/{REGION}")
    else:
        print(f"  {red('Limpieza incompleta.')} Revisa la consola de CloudFormation en {REGION}.")
    print()


if __name__ == "__main__":
    main()
