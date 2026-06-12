import boto3
import time

SSM_ALIAS_PARAM = '/3htp/monitor/agent-alias-id'


def wait_for_agent_prepared(client, agent_id, timeout=120):
    """Poll until agentStatus == PREPARED. Raises RuntimeError on timeout or failure."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = client.get_agent(agentId=agent_id)
            status = resp['agent']['agentStatus']
            print(f'Agent status: {status}')
            if status == 'PREPARED':
                return
            if status == 'FAILED':
                raise RuntimeError(f'Agent preparation FAILED')
        except RuntimeError:
            raise
        except Exception as e:
            print(f'get_agent error (will retry): {e}')
        time.sleep(6)
    raise RuntimeError(f'Timeout ({timeout}s) waiting for agent {agent_id} to reach PREPARED state')


def write_alias_to_ssm(alias_id, region):
    """Publish alias ID to SSM so chat proxy picks it up at runtime."""
    try:
        ssm = boto3.client('ssm', region_name=region)
        ssm.put_parameter(
            Name=SSM_ALIAS_PARAM,
            Value=alias_id,
            Type='String',
            Overwrite=True,
            Description='Bedrock Agent live alias ID — managed by AliasManagerFn',
        )
        print('SSM param updated:', SSM_ALIAS_PARAM, '=', alias_id)
    except Exception as e:
        print('WARNING: SSM put failed (non-fatal):', e)


def handler(event, context):
    """
    cr.Provider contract: return dict with PhysicalResourceId and Data.
    The framework (AliasProvider/framework-onEvent) sends it to CFN ResponseURL.
    Raising an exception marks the resource as FAILED.
    """
    p = event['ResourceProperties']
    region = p['Region']
    agent_id = p['AgentId']
    client = boto3.client('bedrock-agent', region_name=region)
    old_id = event.get('PhysicalResourceId', '')
    request_type = event['RequestType']

    if request_type in ('Create', 'Update'):
        if request_type == 'Create':
            # Clean up any existing 'live' alias left by a previous CfnAgentAlias resource.
            try:
                resp = client.list_agent_aliases(agentId=agent_id)
                for a in resp.get('agentAliasSummaries', []):
                    if a.get('agentAliasName') == 'live':
                        client.delete_agent_alias(
                            agentId=agent_id,
                            agentAliasId=a['agentAliasId'],
                        )
                        print('Cleaned up existing live alias:', a['agentAliasId'])
            except Exception as e:
                print('cleanup existing live alias (non-fatal):', e)
        elif old_id:
            # Delete the alias we previously managed so we can create a fresh one.
            # Creating (not updating) a Bedrock alias is the ONLY way to generate a
            # new version snapshot — UpdateAgentAlias cannot do this.
            try:
                client.delete_agent_alias(agentId=agent_id, agentAliasId=old_id)
                print('Deleted old alias:', old_id)
            except Exception as e:
                print('delete old alias (non-fatal):', e)

        # CRITICAL: wait for agent to reach PREPARED state before creating the alias.
        # PrepareAgent API returns while the agent is still in PREPARING state.
        # If we create the alias too early, Bedrock routes to the last fully-prepared
        # version (which may predate the latest instruction/schema changes).
        wait_for_agent_prepared(client, agent_id)

        # Create fresh alias — Bedrock snapshots the now-PREPARED DRAFT as a new version.
        r = client.create_agent_alias(
            agentId=agent_id,
            agentAliasName='live',
            description='CDK managed — auto-versioned on every deploy',
        )
        alias_id = r['agentAlias']['agentAliasId']
        print('Created alias:', alias_id)

        write_alias_to_ssm(alias_id, region)

        return {'PhysicalResourceId': alias_id, 'Data': {'AliasId': alias_id}}

    elif request_type == 'Delete':
        if old_id:
            try:
                client.delete_agent_alias(agentId=agent_id, agentAliasId=old_id)
                print('Deleted alias on stack destroy:', old_id)
            except Exception as e:
                print('delete on destroy (non-fatal):', e)
        return {'PhysicalResourceId': old_id or 'deleted'}
