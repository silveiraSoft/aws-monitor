"""
Profile Attacher — CloudFormation Custom Resource

SAFETY RULES (never violate):
  - NEVER modify existing IAM roles or policies
  - NEVER replace or remove existing instance profiles
  - NEVER detach/disassociate anything
  - On Delete event: no-op (idempotent cleanup handled by IAM/EC2 separately)
  - Only ec2:AssociateIamInstanceProfile on instances with IamInstanceProfile == None
"""
import boto3
import json


def handler(event, context):
    request_type = event['RequestType']

    # Delete: never remove profiles — instances may depend on them after stack destroy
    if request_type == 'Delete':
        print("Delete event — no action taken (profiles are never removed by this resource).")
        return {'PhysicalResourceId': event.get('PhysicalResourceId', 'profile-attacher-noop')}

    p = event['ResourceProperties']
    region = p['Region']
    profile_arn = p['ProfileArn']
    profile_name = p['ProfileName']

    ec2 = boto3.client('ec2', region_name=region)

    # Fetch all non-terminated instances (pending/running/stopping/stopped)
    instances = []
    paginator = ec2.get_paginator('describe_instances')
    for page in paginator.paginate(Filters=[
        {'Name': 'instance-state-name', 'Values': ['pending', 'running', 'stopping', 'stopped']}
    ]):
        for r in page['Reservations']:
            for i in r['Instances']:
                instances.append(i)

    attached = []          # profile successfully attached by us
    already_has = []       # already has a profile — NEVER touched
    failed = []            # attach attempted but failed

    for inst in instances:
        iid = inst['InstanceId']
        name = next((t['Value'] for t in inst.get('Tags', []) if t['Key'] == 'Name'), '')
        state = inst['State']['Name']
        existing_profile = inst.get('IamInstanceProfile')

        if existing_profile:
            # Instance already has a profile — record for manual guidance, do NOT touch
            already_has.append({
                'instance_id': iid,
                'name': name or iid,
                'state': state,
                'existing_profile_arn': existing_profile.get('Arn', 'unknown'),
            })
        else:
            # Safe to attach: instance has NO profile whatsoever
            try:
                ec2.associate_iam_instance_profile(
                    IamInstanceProfile={'Arn': profile_arn, 'Name': profile_name},
                    InstanceId=iid,
                )
                attached.append({'instance_id': iid, 'name': name or iid, 'state': state})
                print(f"ATTACHED profile to {iid} ({name}) [{state}]")
            except ec2.exceptions.ClientError as e:
                code = e.response['Error']['Code']
                msg = e.response['Error']['Message']
                # IncorrectState: instance may be in a transient state — not fatal
                failed.append({'instance_id': iid, 'name': name or iid, 'error': f"{code}: {msg}"})
                print(f"FAILED to attach to {iid}: {code}: {msg}")
            except Exception as e:
                failed.append({'instance_id': iid, 'name': name or iid, 'error': str(e)})
                print(f"FAILED to attach to {iid}: {e}")

    # Print structured summary for CloudWatch Logs
    summary = {
        'attached_count': len(attached),
        'attached': attached,
        'needs_manual_action_count': len(already_has),
        'needs_manual_action': already_has,
        'failed_count': len(failed),
        'failed': failed,
    }
    print(json.dumps(summary, indent=2))

    if already_has:
        print("\n=== MANUAL ACTION REQUIRED ===")
        print("These instances already have an IAM profile and were NOT modified.")
        print("To enable CloudWatch Agent metrics, add BOTH of these AWS managed policies")
        print("to the existing IAM role attached to each instance:")
        print("  1. AmazonSSMManagedInstanceCore   (allows SSM to connect)")
        print("  2. CloudWatchAgentServerPolicy    (allows CW Agent to publish metrics)")
        print("")
        print("Steps: AWS Console → IAM → Roles → [role name] → Attach policies → search and attach both.")
        print("")
        for i in already_has:
            role_hint = i['existing_profile_arn'].split('/')[-1] if '/' in i['existing_profile_arn'] else 'unknown'
            print(f"  Instance: {i['instance_id']} ({i['name']}) "
                  f"[{i['state']}] — Profile ARN: {i['existing_profile_arn']} — Role hint: {role_hint}")

    # CloudFormation Data attributes (max ~4KB per attribute)
    needs_json = json.dumps(already_has)[:3900]

    return {
        'PhysicalResourceId': f"profile-attacher-{region}",
        'Data': {
            'AttachedCount': str(len(attached)),
            'NeedsManualActionCount': str(len(already_has)),
            'NeedsManualAction': needs_json,
        },
    }
