"""
Diagnóstico del Bedrock Agent — ejecutar con:
  python diagnose_agent.py
"""
import json
import boto3

PROFILE   = '3htpusa-monitor'
REGION    = 'us-east-1'
AGENT_ID  = 'YXWPV8BYMC'
S3_BUCKET = 'aws-monitor-schema-369595298303-us-east-1'
S3_KEY    = 'monitor-openapi.json'
SSM_PARAM = '/3htp/monitor/agent-alias-id'

session = boto3.Session(profile_name=PROFILE, region_name=REGION)
ba  = session.client('bedrock-agent')
s3  = session.client('s3')
ssm = session.client('ssm')

# ── 1. Schema en S3 ──────────────────────────────────────────────────
print("=== 1. Schema en S3 ===")
try:
    obj = s3.get_object(Bucket=S3_BUCKET, Key=S3_KEY)
    schema_text = obj['Body'].read().decode()
    schema = json.loads(schema_text)
    paths = list(schema.get('paths', {}).keys())
    print(f"  Paths ({len(paths)}): {paths}")
    ssm_ok = '/get_ssm_inventory' in paths
    print(f"  get_ssm_inventory: {'✅ PRESENTE' if ssm_ok else '❌ AUSENTE'}")
except Exception as e:
    print(f"  ERROR: {e}")

# ── 2. Alias actual desde SSM ────────────────────────────────────────
print("\n=== 2. Alias actual (desde SSM) ===")
try:
    alias_id = ssm.get_parameter(Name=SSM_PARAM)['Parameter']['Value']
    print(f"  Alias ID: {alias_id}")
except Exception as e:
    print(f"  ERROR leyendo SSM: {e}")
    alias_id = None

# ── 3. Versión que usa el alias ──────────────────────────────────────
print("\n=== 3. Versión del alias ===")
version = None
if alias_id:
    try:
        alias = ba.get_agent_alias(agentId=AGENT_ID, agentAliasId=alias_id)['agentAlias']
        print(f"  Status: {alias['agentAliasStatus']}")
        for r in alias.get('routingConfiguration', []):
            version = r.get('agentVersion')
            print(f"  → Versión: {version}")
    except Exception as e:
        print(f"  ERROR: {e}")

# ── 4. Action group de esa versión ───────────────────────────────────
print(f"\n=== 4. Action group de versión {version} ===")
if version:
    try:
        ags = ba.list_agent_action_groups(agentId=AGENT_ID, agentVersion=version)
        for ag in ags.get('actionGroupSummaries', []):
            ag_id = ag['actionGroupId']
            print(f"  ActionGroup: {ag['actionGroupName']} | {ag['actionGroupState']}")
            detail = ba.get_agent_action_group(
                agentId=AGENT_ID,
                agentVersion=version,
                actionGroupId=ag_id
            )['agentActionGroup']
            schema_ref = detail.get('apiSchema', {}).get('s3', {})
            if schema_ref:
                print(f"  Schema S3: s3://{schema_ref.get('s3BucketName')}/{schema_ref.get('s3ObjectKey')}")
            # Also check inline schema if present
            inline = detail.get('apiSchema', {}).get('payload')
            if inline:
                inline_paths = list(json.loads(inline).get('paths', {}).keys())
                print(f"  Schema inline paths ({len(inline_paths)}): {inline_paths}")
                print(f"  get_ssm_inventory en versión: {'✅ PRESENTE' if '/get_ssm_inventory' in inline_paths else '❌ AUSENTE'}")
    except Exception as e:
        print(f"  ERROR: {e}")

# ── 5. DRAFT action group (lo que PrepareAgent usaría ahora) ─────────
print("\n=== 5. Action group en DRAFT ===")
try:
    ags = ba.list_agent_action_groups(agentId=AGENT_ID, agentVersion='DRAFT')
    for ag in ags.get('actionGroupSummaries', []):
        ag_id = ag['actionGroupId']
        print(f"  ActionGroup: {ag['actionGroupName']} | {ag['actionGroupState']}")
        detail = ba.get_agent_action_group(
            agentId=AGENT_ID,
            agentVersion='DRAFT',
            actionGroupId=ag_id
        )['agentActionGroup']
        schema_ref = detail.get('apiSchema', {}).get('s3', {})
        inline = detail.get('apiSchema', {}).get('payload')
        if schema_ref:
            print(f"  Schema S3: s3://{schema_ref.get('s3BucketName')}/{schema_ref.get('s3ObjectKey')}")
        if inline:
            inline_paths = list(json.loads(inline).get('paths', {}).keys())
            print(f"  Schema inline paths ({len(inline_paths)}): {inline_paths}")
            print(f"  get_ssm_inventory en DRAFT: {'✅ PRESENTE' if '/get_ssm_inventory' in inline_paths else '❌ AUSENTE'}")
except Exception as e:
    print(f"  ERROR: {e}")

# ── 6. Descripción actual del agente (verificar schema hash) ─────────
print("\n=== 6. Agente (description) ===")
try:
    agent = ba.get_agent(agentId=AGENT_ID)['agent']
    print(f"  Status     : {agent['agentStatus']}")
    print(f"  Description: {agent.get('description', '')}")
    print(f"  UpdatedAt  : {agent.get('updatedAt')}")
except Exception as e:
    print(f"  ERROR: {e}")
