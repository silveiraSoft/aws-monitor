#!/usr/bin/env python3
"""
AWS Monitor Agent - Pre-deployment Access Validator
====================================================
Validates that your AWS credentials have all permissions required to deploy
the aws-monitor CDK project (Bedrock Agent + Lambda + API GW + CloudFront).

Usage:
    python validate_aws_access.py

The script will prompt for your AWS Access Key ID and Secret Access Key.
Nothing is stored or sent anywhere — credentials are used only for this session.
"""

import sys
import json
import getpass
import textwrap
from dataclasses import dataclass, field
from typing import Optional

# ── Dependency check ──────────────────────────────────────────────────────────
try:
    import boto3
    import botocore.exceptions
except ImportError:
    print("\n[ERROR] boto3 is not installed. Run:  pip install boto3\n")
    sys.exit(1)

REGION = "us-east-1"
# Claude Haiku 4.5 — modelo activo más económico (2026-06-05)
BEDROCK_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

# ── Result model ──────────────────────────────────────────────────────────────
@dataclass
class Check:
    name: str
    passed: bool
    detail: str
    critical: bool = True
    warning: Optional[str] = None

@dataclass
class Report:
    checks: list = field(default_factory=list)

    def add(self, check: Check):
        self.checks.append(check)

    @property
    def passed(self):   return [c for c in self.checks if c.passed]
    @property
    def failed(self):   return [c for c in self.checks if not c.passed]
    @property
    def critical_failures(self): return [c for c in self.failed if c.critical]


# ── ANSI colors ───────────────────────────────────────────────────────────────
def green(s):  return f"\033[92m{s}\033[0m"
def red(s):    return f"\033[91m{s}\033[0m"
def yellow(s): return f"\033[93m{s}\033[0m"
def bold(s):   return f"\033[1m{s}\033[0m"
def dim(s):    return f"\033[2m{s}\033[0m"

PASS = green("✔ PASS")
FAIL = red("✘ FAIL")
WARN = yellow("⚠ WARN")

# ── Credential input ──────────────────────────────────────────────────────────
def prompt_credentials() -> dict:
    print()
    print(bold("━━━ AWS Credentials ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"))
    print(dim("  Credentials are used only in memory for this validation session."))
    print()

    key_id = input("  AWS Access Key ID     : ").strip()
    if not key_id:
        print(red("  Access Key ID cannot be empty."))
        sys.exit(1)

    secret = getpass.getpass("  AWS Secret Access Key : ").strip()
    if not secret:
        print(red("  Secret Access Key cannot be empty."))
        sys.exit(1)

    return {"aws_access_key_id": key_id, "aws_secret_access_key": secret}


def make_session(creds: dict) -> boto3.Session:
    return boto3.Session(
        aws_access_key_id=creds["aws_access_key_id"],
        aws_secret_access_key=creds["aws_secret_access_key"],
        region_name=REGION,
    )


# ── Individual checks ─────────────────────────────────────────────────────────

def check_identity(session: boto3.Session, report: Report):
    """Verify the credentials are valid and get caller identity."""
    sts = session.client("sts")
    try:
        identity = sts.get_caller_identity()
        acct = identity["Account"]
        arn  = identity["Arn"]
        report.add(Check(
            name="Credentials valid (STS GetCallerIdentity)",
            passed=True,
            detail=f"Account: {acct}  |  ARN: {arn}",
        ))
        return identity
    except botocore.exceptions.ClientError as e:
        report.add(Check(
            name="Credentials valid (STS GetCallerIdentity)",
            passed=False,
            detail=str(e),
            critical=True,
        ))
        return None


def check_iam_permissions(session: boto3.Session, report: Report, caller_arn: str):
    """
    Simulate IAM actions needed to deploy the CDK stacks.
    Uses iam:SimulatePrincipalPolicy — requires the calling identity to have
    iam:SimulatePrincipalPolicy permission itself, or we fall back to direct calls.
    """
    iam = session.client("iam")

    required_actions = [
        # IAM
        "iam:CreateRole",
        "iam:AttachRolePolicy",
        "iam:PutRolePolicy",
        "iam:PassRole",
        "iam:GetRole",
        # Lambda
        "lambda:CreateFunction",
        "lambda:GetFunction",
        "lambda:AddPermission",
        "lambda:UpdateFunctionCode",
        # API Gateway
        "apigateway:POST",
        "apigateway:PUT",
        "apigateway:GET",
        # CloudFront
        "cloudfront:CreateDistribution",
        "cloudfront:CreateCloudFrontOriginAccessIdentity",
        # S3 (CDK bootstrap + schema bucket)
        "s3:CreateBucket",
        "s3:PutBucketPolicy",
        "s3:PutObject",
        # CloudFormation (CDK)
        "cloudformation:CreateStack",
        "cloudformation:UpdateStack",
        "cloudformation:DescribeStacks",
        # CloudWatch Logs (log retention feature)
        "logs:CreateLogGroup",
        "logs:PutRetentionPolicy",
        "logs:DeleteRetentionPolicy",
        # API Gateway — API Key + Usage Plan (security hardening)
        "apigateway:CreateApiKey",
        "apigateway:UpdateUsagePlan",
        # Bedrock
        "bedrock:CreateAgent",
        "bedrock:GetAgent",
        "bedrock:UpdateAgent",
        "bedrock:CreateAgentAlias",
        "bedrock:PrepareAgent",
        "bedrock:AssociateAgentActionGroup",
    ]

    try:
        resp = iam.simulate_principal_policy(
            PolicySourceArn=caller_arn,
            ActionNames=required_actions,
            ResourceArns=["*"],
        )

        denied = [
            r["EvalActionName"]
            for r in resp["EvaluationResults"]
            if r["EvalDecision"] != "allowed"
        ]

        if not denied:
            report.add(Check(
                name="IAM SimulatePrincipalPolicy — all deployment actions",
                passed=True,
                detail=f"All {len(required_actions)} required actions are allowed.",
            ))
        else:
            report.add(Check(
                name="IAM SimulatePrincipalPolicy — all deployment actions",
                passed=False,
                detail=f"Denied actions ({len(denied)}): {', '.join(denied)}",
                critical=True,
                warning="Ask your admin to grant these actions or attach AdministratorAccess for deployment.",
            ))

    except botocore.exceptions.ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("AccessDenied", "AccessDeniedException"):
            report.add(Check(
                name="IAM SimulatePrincipalPolicy",
                passed=False,
                detail="No permission to run iam:SimulatePrincipalPolicy — falling back to direct probes.",
                critical=False,
                warning="Add iam:SimulatePrincipalPolicy to run a full permission simulation.",
            ))
            _check_permissions_direct(session, report)
        else:
            report.add(Check(
                name="IAM SimulatePrincipalPolicy",
                passed=False,
                detail=str(e),
                critical=False,
            ))


def _check_permissions_direct(session: boto3.Session, report: Report):
    """Fallback: probe each service with a lightweight read call."""
    probes = [
        ("IAM — ListRoles",          lambda: session.client("iam").list_roles(MaxItems=1)),
        ("Lambda — ListFunctions",   lambda: session.client("lambda", region_name=REGION).list_functions(MaxItems=1)),
        ("API Gateway — GetRestApis",lambda: session.client("apigateway", region_name=REGION).get_rest_apis(limit=1)),
        ("CloudFront — ListDistributions", lambda: session.client("cloudfront").list_distributions()),
        ("S3 — ListBuckets",         lambda: session.client("s3").list_buckets()),
        ("CloudFormation — ListStacks",lambda: session.client("cloudformation", region_name=REGION).list_stacks(StackStatusFilter=["CREATE_COMPLETE"])),
        # New: log retention permission (added 2026-06-02 for logRetention feature)
        ("CloudWatch Logs — DescribeLogGroups", lambda: session.client("logs", region_name=REGION).describe_log_groups(limit=1)),
        # New: Bedrock Agents create access
        ("Bedrock — ListAgents",     lambda: session.client("bedrock-agent", region_name=REGION).list_agents(maxResults=1)),
    ]

    for name, probe in probes:
        try:
            probe()
            report.add(Check(
                name=f"Direct probe: {name}",
                passed=True,
                detail="Read access confirmed.",
                critical=False,
            ))
        except botocore.exceptions.ClientError as e:
            code = e.response["Error"]["Code"]
            denied = code in ("AccessDenied", "AccessDeniedException", "UnauthorizedAccess")
            report.add(Check(
                name=f"Direct probe: {name}",
                passed=not denied,
                detail=f"{code}: {e.response['Error']['Message']}",
                critical=denied,
            ))


def check_bedrock_model_access(session: boto3.Session, report: Report):
    """Check if the configured Bedrock model/inference profile is accessible."""
    is_inference_profile = BEDROCK_MODEL.startswith("us.") or BEDROCK_MODEL.startswith("eu.") or BEDROCK_MODEL.startswith("ap.")

    if not is_inference_profile:
        bedrock = session.client("bedrock", region_name=REGION)
        try:
            resp = bedrock.get_foundation_model(modelIdentifier=BEDROCK_MODEL)
            model_info = resp.get("modelDetails", {})
            name = model_info.get("modelName", BEDROCK_MODEL)
            report.add(Check(
                name=f"Bedrock model exists: {name}",
                passed=True,
                detail=f"Model ID: {BEDROCK_MODEL}  |  Status: available",
            ))
        except botocore.exceptions.ClientError as e:
            code = e.response["Error"]["Code"]
            report.add(Check(
                name=f"Bedrock model exists: {BEDROCK_MODEL}",
                passed=False,
                detail=f"{code}: {e.response['Error']['Message']}",
                critical=True,
                warning=f"Go to AWS Console → Bedrock → Model access → Request access for: {BEDROCK_MODEL}",
            ))
            return
    else:
        report.add(Check(
            name=f"Bedrock inference profile: {BEDROCK_MODEL}",
            passed=True,
            detail="Inference profile — skipping get_foundation_model, testing invocation directly.",
        ))

    # 2. Check model invocation access (dry-run with minimal tokens)
    bedrock_rt = session.client("bedrock-runtime", region_name=REGION)
    try:
        bedrock_rt.invoke_model(
            modelId=BEDROCK_MODEL,
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
            }),
        )
        report.add(Check(
            name=f"Bedrock InvokeModel — {BEDROCK_MODEL}",
            passed=True,
            detail=f"Model invocation succeeded ({REGION}).",
        ))
    except botocore.exceptions.ClientError as e:
        code = e.response["Error"]["Code"]
        msg  = e.response["Error"]["Message"]
        legacy = "legacy" in msg.lower()
        denied = "not have access" in msg.lower() or code in ("AccessDeniedException",)
        if legacy or denied:
            report.add(Check(
                name=f"Bedrock InvokeModel — {BEDROCK_MODEL}",
                passed=False,
                detail=f"{code}: {msg}",
                critical=True,
                warning=(
                    "Model access not granted or model is Legacy.\n"
                    f"  → AWS Console → Amazon Bedrock → Model access ({REGION})\n"
                    f"  → Request access for: {BEDROCK_MODEL}"
                ),
            ))
        else:
            report.add(Check(
                name=f"Bedrock InvokeModel — {BEDROCK_MODEL}",
                passed=False,
                detail=f"{code}: {msg}",
                critical=True,
            ))


def check_agent_runtime_permissions(session: boto3.Session, report: Report):
    """
    Verify read permissions the agent's action Lambda needs at runtime.
    These are separate from deploy permissions — they belong to the Lambda IAM Role,
    but we validate them here to confirm the account/region has no SCP blocks.
    """
    runtime_probes = [
        (
            "EC2 — DescribeInstances (agent runtime)",
            lambda: session.client("ec2", region_name=REGION).describe_instances(MaxResults=5),
            "ec2:DescribeInstances needed by get_ec2_health action",
        ),
        (
            "CloudWatch — DescribeAlarms (agent runtime)",
            lambda: session.client("cloudwatch", region_name=REGION).describe_alarms(MaxRecords=1),
            "cloudwatch:DescribeAlarms needed by get_cloudwatch_alarms action",
        ),
        (
            "CloudWatch — GetMetricStatistics (agent runtime)",
            lambda: session.client("cloudwatch", region_name=REGION).list_metrics(Namespace="AWS/Lambda"),
            "cloudwatch:ListMetrics / GetMetricStatistics needed by get_lambda_health action",
        ),
        (
            "Lambda — ListFunctions (agent runtime)",
            lambda: session.client("lambda", region_name=REGION).list_functions(MaxItems=1),
            "lambda:ListFunctions needed by get_lambda_health action",
        ),
        (
            "CloudWatch Logs — DescribeLogGroups (agent runtime)",
            lambda: session.client("logs", region_name=REGION).describe_log_groups(limit=1),
            "logs:StartQuery / logs:GetQueryResults needed by get_logs_analysis action",
        ),
        (
            "X-Ray — GetTraceSummaries (agent runtime)",
            lambda: session.client("xray", region_name=REGION).get_trace_summaries(
                StartTime=__import__('datetime').datetime.utcnow() - __import__('datetime').timedelta(hours=1),
                EndTime=__import__('datetime').datetime.utcnow(),
                Sampling=False,
            ),
            "xray:GetTraceSummaries needed by get_xray_traces action",
        ),
    ]

    for name, probe, hint in runtime_probes:
        try:
            probe()
            report.add(Check(
                name=name,
                passed=True,
                detail="Runtime read access confirmed from deploying identity.",
                critical=False,
            ))
        except botocore.exceptions.ClientError as e:
            code = e.response["Error"]["Code"]
            denied = code in ("AccessDenied", "AccessDeniedException", "UnauthorizedAccess")
            report.add(Check(
                name=name,
                passed=not denied,
                detail=f"{code}: {e.response['Error']['Message']}",
                critical=denied,
                warning=(
                    f"{hint}\n"
                    "  → Check Service Control Policies (SCPs) in AWS Organizations.\n"
                    "  → The Lambda IAM Role (ActionLambdaRole) needs this permission."
                ) if denied else None,
            ))


def check_bedrock_agents_api(session: boto3.Session, report: Report):
    """Check ListAgents permission (basic Bedrock Agents API access)."""
    bedrock_agents = session.client("bedrock-agent", region_name=REGION)
    try:
        bedrock_agents.list_agents(maxResults=1)
        report.add(Check(
            name="Bedrock Agents API — ListAgents",
            passed=True,
            detail=f"Bedrock Agents API accessible in {REGION}.",
        ))
    except botocore.exceptions.ClientError as e:
        code = e.response["Error"]["Code"]
        report.add(Check(
            name="Bedrock Agents API — ListAgents",
            passed=False,
            detail=f"{code}: {e.response['Error']['Message']}",
            critical=True,
            warning="Add bedrock:ListAgents and bedrock:CreateAgent to your IAM policy.",
        ))


# ── Report printer ────────────────────────────────────────────────────────────
def print_report(report: Report):
    width = 70
    print()
    print(bold("━" * width))
    print(bold("  AWS Monitor Agent — Pre-deployment Validation Report"))
    print(bold("━" * width))
    print()

    for check in report.checks:
        status = PASS if check.passed else (WARN if not check.critical else FAIL)
        print(f"  {status}  {bold(check.name)}")
        if check.detail:
            for line in textwrap.wrap(check.detail, width=60):
                print(f"          {dim(line)}")
        if check.warning and not check.passed:
            for line in check.warning.split("\n"):
                print(f"          {yellow('-> ' + line)}")
        print()

    print("━" * width)
    total   = len(report.checks)
    passed  = len(report.passed)
    failed  = len(report.failed)
    crit    = len(report.critical_failures)

    print(f"  Total: {total}  |  {green(f'Passed: {passed}')}  |  {red(f'Failed: {failed}')}  |  Critical: {crit}")
    print()

    if crit == 0 and failed == 0:
        print(green("  All checks passed! Your account is ready to deploy."))
        print()
        print(dim("  Next step:"))
        print(dim("    cd aws-monitor"))
        print(dim("    npm install"))
        print(dim(f"    npx cdk bootstrap aws://YOUR_ACCOUNT_ID/{REGION}"))
        print(dim("    npm run deploy"))
    elif crit == 0:
        print(yellow("  Some non-critical checks failed. Deploy may still work."))
        print(yellow("    Review warnings above before proceeding."))
    else:
        print(red(f"  {crit} critical issue(s) must be resolved before deploying."))
        print()
        print(bold("  Required fixes:"))
        for c in report.critical_failures:
            print(f"    - {c.name}")
            if c.warning:
                for line in c.warning.split("\n"):
                    print(f"      {yellow(line)}")

    print("━" * width)
    print()


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print()
    print(bold("  AWS Monitor Agent — Pre-deployment Validator"))
    print(dim(f"  Region: {REGION}  |  Model: {BEDROCK_MODEL}"))

    creds   = prompt_credentials()
    session = make_session(creds)
    report  = Report()

    print()
    print(bold("  Running checks..."))
    print()

    # 1. Identity
    identity = check_identity(session, report)
    if identity is None:
        print_report(report)
        sys.exit(1)

    caller_arn = identity["Arn"]

    # 2. IAM permissions simulation
    check_iam_permissions(session, report, caller_arn)

    # 3. Bedrock model access
    check_bedrock_model_access(session, report)

    # 4. Bedrock Agents API
    check_bedrock_agents_api(session, report)

    # 5. Agent runtime permissions (EC2, CloudWatch, Lambda read)
    check_agent_runtime_permissions(session, report)

    print_report(report)
    sys.exit(0 if not report.critical_failures else 1)


if __name__ == "__main__":
    main()
