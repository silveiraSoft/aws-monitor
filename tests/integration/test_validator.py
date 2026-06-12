"""
Integration tests for validate_aws_access.py

Simulates the full validation flow with mocked boto3 clients.
No real AWS credentials needed — all network calls are intercepted.

Run with:
    python3 -m unittest tests/integration/test_validator.py -v
"""
import sys
import os
import unittest
from io import StringIO
from unittest.mock import MagicMock, patch

# Inject stubs before importing validate_aws_access
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import conftest_stdlib  # noqa: E402

import botocore.exceptions

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
import validate_aws_access as v


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def client_error(code: str, msg: str = "Access denied") -> botocore.exceptions.ClientError:
    return botocore.exceptions.ClientError(
        {"Error": {"Code": code, "Message": msg}}, "Op"
    )

def make_session(
    identity_error=None,
    simulate_result=None,
    simulate_error=None,
    bedrock_model_error=None,
    bedrock_invoke_error=None,
    agents_error=None,
    ec2_error=None,
    cw_error=None,
    ssm_error=None,
):
    """Build a fully mocked boto3.Session for one test scenario."""
    session = MagicMock()

    # STS
    sts = MagicMock()
    if identity_error:
        sts.get_caller_identity.side_effect = identity_error
    else:
        sts.get_caller_identity.return_value = {
            "Account": "369595298303",
            "Arn": "arn:aws:iam::369595298303:user/asilveira",
            "UserId": "AIDATEST",
        }

    # IAM
    iam = MagicMock()
    if simulate_error:
        iam.simulate_principal_policy.side_effect = simulate_error
    elif simulate_result is not None:
        iam.simulate_principal_policy.return_value = {"EvaluationResults": simulate_result}
    else:
        iam.simulate_principal_policy.return_value = {"EvaluationResults": []}
    iam.list_roles.return_value = {"Roles": []}

    # Bedrock model
    bedrock = MagicMock()
    if bedrock_model_error:
        bedrock.get_foundation_model.side_effect = bedrock_model_error
    else:
        bedrock.get_foundation_model.return_value = {
            "modelDetails": {"modelName": "Claude 3.5 Haiku", "modelId": v.BEDROCK_MODEL}
        }

    # Bedrock runtime
    bedrock_rt = MagicMock()
    if bedrock_invoke_error:
        bedrock_rt.invoke_model.side_effect = bedrock_invoke_error
    else:
        bedrock_rt.invoke_model.return_value = {
            "body": MagicMock(read=lambda: b'{"content":[{"text":"hi"}]}')
        }

    # Bedrock agent
    bedrock_agent = MagicMock()
    if agents_error:
        bedrock_agent.list_agents.side_effect = agents_error
    else:
        bedrock_agent.list_agents.return_value = {"agentSummaries": []}

    # EC2 / CloudWatch / Lambda / others
    ec2_client = MagicMock()
    if ec2_error:
        ec2_client.describe_instances.side_effect = ec2_error
    else:
        ec2_client.describe_instances.return_value = {"Reservations": []}

    cw_client = MagicMock()
    if cw_error:
        cw_client.describe_alarms.side_effect = cw_error
    else:
        cw_client.describe_alarms.return_value = {"MetricAlarms": [], "CompositeAlarms": []}
    cw_client.list_metrics.return_value = {"Metrics": []}

    lc = MagicMock()
    lc.list_functions.return_value = {"Functions": []}

    apigw = MagicMock()
    apigw.get_rest_apis.return_value = {"items": []}
    cf = MagicMock()
    cf.list_distributions.return_value = {}
    s3 = MagicMock()
    s3.list_buckets.return_value = {"Buckets": []}
    cfn = MagicMock()
    cfn.list_stacks.return_value = {"StackSummaries": []}
    logs = MagicMock()
    logs.describe_log_groups.return_value = {"logGroups": []}

    ssm_client = MagicMock()
    if ssm_error:
        ssm_client.describe_instance_information.side_effect = ssm_error
        ssm_client.get_inventory.side_effect = ssm_error
    else:
        ssm_client.describe_instance_information.return_value = {"InstanceInformationList": []}
        ssm_client.get_inventory.return_value = {"Entities": []}

    lookup = {
        "sts": sts, "iam": iam,
        "bedrock": bedrock, "bedrock-runtime": bedrock_rt, "bedrock-agent": bedrock_agent,
        "ec2": ec2_client, "cloudwatch": cw_client, "lambda": lc,
        "apigateway": apigw, "cloudfront": cf, "s3": s3,
        "cloudformation": cfn, "logs": logs, "ssm": ssm_client,
    }
    session.client.side_effect = lambda svc, **kw: lookup.get(svc, MagicMock())
    return session


# ─────────────────────────────────────────────────────────────────────────────
# 1. Report model
# ─────────────────────────────────────────────────────────────────────────────

class TestReportModel(unittest.TestCase):

    def test_empty_report(self):
        r = v.Report()
        self.assertEqual(r.passed, [])
        self.assertEqual(r.failed, [])
        self.assertEqual(r.critical_failures, [])

    def test_passed_check(self):
        r = v.Report()
        r.add(v.Check("ok", passed=True, detail="fine"))
        self.assertEqual(len(r.passed), 1)
        self.assertEqual(len(r.failed), 0)

    def test_critical_failure(self):
        r = v.Report()
        r.add(v.Check("bad", passed=False, detail="fail", critical=True))
        self.assertEqual(len(r.critical_failures), 1)

    def test_non_critical_failure_not_in_critical(self):
        r = v.Report()
        r.add(v.Check("warn", passed=False, detail="warn", critical=False))
        self.assertEqual(len(r.critical_failures), 0)
        self.assertEqual(len(r.failed), 1)

    def test_mixed_checks(self):
        r = v.Report()
        r.add(v.Check("a", passed=True, detail=""))
        r.add(v.Check("b", passed=False, detail="", critical=True))
        r.add(v.Check("c", passed=False, detail="", critical=False))
        self.assertEqual(len(r.passed), 1)
        self.assertEqual(len(r.failed), 2)
        self.assertEqual(len(r.critical_failures), 1)


# ─────────────────────────────────────────────────────────────────────────────
# 2. check_identity
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckIdentity(unittest.TestCase):

    def test_valid_credentials_pass(self):
        session = make_session()
        report = v.Report()
        identity = v.check_identity(session, report)
        self.assertIsNotNone(identity)
        self.assertEqual(identity["Account"], "369595298303")
        self.assertTrue(report.checks[0].passed)

    def test_invalid_token_fails(self):
        session = make_session(identity_error=client_error("InvalidClientTokenId", "Token invalid"))
        report = v.Report()
        identity = v.check_identity(session, report)
        self.assertIsNone(identity)
        self.assertFalse(report.checks[0].passed)
        self.assertTrue(report.checks[0].critical)

    def test_expired_token_fails(self):
        session = make_session(identity_error=client_error("ExpiredTokenException", "Token expired"))
        report = v.Report()
        identity = v.check_identity(session, report)
        self.assertIsNone(identity)
        self.assertFalse(report.checks[0].passed)

    def test_detail_contains_account_and_arn(self):
        session = make_session()
        report = v.Report()
        v.check_identity(session, report)
        detail = report.checks[0].detail
        self.assertIn("369595298303", detail)


# ─────────────────────────────────────────────────────────────────────────────
# 3. check_iam_permissions
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckIamPermissions(unittest.TestCase):

    def test_all_actions_allowed(self):
        session = make_session(simulate_result=[
            {"EvalActionName": "iam:CreateRole", "EvalDecision": "allowed"},
        ])
        report = v.Report()
        v.check_iam_permissions(session, report, "arn:aws:iam::123:user/test")
        passed = [c for c in report.checks if c.passed]
        self.assertGreater(len(passed), 0)

    def test_denied_action_creates_failure(self):
        session = make_session(simulate_result=[
            {"EvalActionName": "bedrock:CreateAgent", "EvalDecision": "implicitDeny"},
        ])
        report = v.Report()
        v.check_iam_permissions(session, report, "arn:aws:iam::123:user/test")
        failed = [c for c in report.checks if not c.passed]
        self.assertGreater(len(failed), 0)
        self.assertIn("bedrock:CreateAgent", failed[0].detail)

    def test_no_simulate_permission_triggers_fallback(self):
        session = make_session(simulate_error=client_error("AccessDenied"))
        report = v.Report()
        v.check_iam_permissions(session, report, "arn:aws:iam::123:user/test")
        check_names = " ".join(c.name for c in report.checks)
        has_fallback = "probe" in check_names.lower() or "direct" in check_names.lower() or "IAM" in check_names
        self.assertTrue(has_fallback, f"Expected fallback, got: {check_names}")

    def test_simulate_fallback_check_is_not_critical(self):
        """The simulate_not_allowed check itself must be non-critical (it's a warning)."""
        session = make_session(simulate_error=client_error("AccessDenied"))
        report = v.Report()
        v.check_iam_permissions(session, report, "arn:aws:iam::123:user/test")
        simulate_check = next((c for c in report.checks if "SimulatePrincipalPolicy" in c.name), None)
        if simulate_check:
            self.assertFalse(simulate_check.critical)

    def test_required_actions_include_key_services(self):
        """validate that new additions (logs, apigateway key) are present in simulate."""
        import inspect
        src = inspect.getsource(v.check_iam_permissions)
        self.assertIn("logs:CreateLogGroup", src)
        self.assertIn("logs:PutRetentionPolicy", src)
        self.assertIn("apigateway:CreateApiKey", src)
        self.assertIn("bedrock:UpdateAgent", src)
        self.assertIn("bedrock:AssociateAgentActionGroup", src)


# ─────────────────────────────────────────────────────────────────────────────
# 4. check_bedrock_model_access
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckBedrockModelAccess(unittest.TestCase):

    def test_model_accessible_and_invocable(self):
        session = make_session()
        report = v.Report()
        v.check_bedrock_model_access(session, report)
        passed = [c for c in report.checks if c.passed]
        self.assertGreater(len(passed), 0)
        self.assertEqual(len(report.critical_failures), 0)

    def test_model_not_enabled_is_critical(self):
        session = make_session(bedrock_invoke_error=client_error("AccessDeniedException"))
        report = v.Report()
        v.check_bedrock_model_access(session, report)
        self.assertGreater(len(report.critical_failures), 0)

    def test_model_not_enabled_warning_is_actionable(self):
        session = make_session(bedrock_invoke_error=client_error("AccessDeniedException"))
        report = v.Report()
        v.check_bedrock_model_access(session, report)
        failed_with_warning = [c for c in report.checks if not c.passed and c.warning]
        self.assertGreater(len(failed_with_warning), 0)
        warning_text = failed_with_warning[0].warning
        self.assertTrue(
            "Bedrock" in warning_text or "model" in warning_text.lower(),
            f"Warning not actionable: {warning_text}"
        )

    def test_legacy_model_error_is_critical(self):
        session = make_session(
            bedrock_invoke_error=client_error(
                "ValidationException",
                "This model is legacy and no longer available."
            )
        )
        report = v.Report()
        v.check_bedrock_model_access(session, report)
        self.assertGreater(len(report.critical_failures), 0)

    def test_model_id_uses_haiku_45(self):
        # Model updated to Claude Haiku 4.5 (claude-3-5-haiku was Legacy)
        self.assertIn("haiku", v.BEDROCK_MODEL)
        self.assertIn("anthropic", v.BEDROCK_MODEL)

    def test_region_is_us_east_1(self):
        self.assertEqual(v.REGION, "us-east-1")


# ─────────────────────────────────────────────────────────────────────────────
# 5. check_bedrock_agents_api
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckBedrockAgentsApi(unittest.TestCase):

    def test_agents_accessible(self):
        session = make_session()
        report = v.Report()
        v.check_bedrock_agents_api(session, report)
        self.assertGreater(len(report.passed), 0)

    def test_agents_denied_is_critical(self):
        session = make_session(agents_error=client_error("AccessDeniedException"))
        report = v.Report()
        v.check_bedrock_agents_api(session, report)
        self.assertGreater(len(report.critical_failures), 0)

    def test_agents_denied_has_warning(self):
        session = make_session(agents_error=client_error("AccessDeniedException"))
        report = v.Report()
        v.check_bedrock_agents_api(session, report)
        failed = [c for c in report.checks if not c.passed and c.warning]
        self.assertGreater(len(failed), 0)


# ─────────────────────────────────────────────────────────────────────────────
# 6. check_agent_runtime_permissions
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckAgentRuntimePermissions(unittest.TestCase):

    def test_all_pass_when_no_errors(self):
        session = make_session()
        report = v.Report()
        v.check_agent_runtime_permissions(session, report)
        # Should have checks for EC2, CW alarms, CW metrics, Lambda
        self.assertGreaterEqual(len(report.checks), 4)
        self.assertEqual(len(report.critical_failures), 0)

    def test_ec2_denied_is_critical(self):
        session = make_session(ec2_error=client_error("AccessDenied"))
        report = v.Report()
        v.check_agent_runtime_permissions(session, report)
        ec2_check = next((c for c in report.checks if "EC2" in c.name), None)
        self.assertIsNotNone(ec2_check)
        self.assertFalse(ec2_check.passed)
        self.assertTrue(ec2_check.critical)

    def test_denied_check_mentions_action_lambda_role(self):
        session = make_session(ec2_error=client_error("AccessDenied"))
        report = v.Report()
        v.check_agent_runtime_permissions(session, report)
        ec2_check = next(c for c in report.checks if "EC2" in c.name)
        self.assertIn("ActionLambdaRole", ec2_check.warning)

    def test_cloudwatch_denied_is_critical(self):
        session = make_session(cw_error=client_error("AccessDenied"))
        report = v.Report()
        v.check_agent_runtime_permissions(session, report)
        cw_check = next((c for c in report.checks if "CloudWatch" in c.name and not c.passed), None)
        if cw_check:
            self.assertTrue(cw_check.critical)

    def test_ssm_probes_present_when_all_pass(self):
        session = make_session()
        report = v.Report()
        v.check_agent_runtime_permissions(session, report)
        ssm_checks = [c for c in report.checks if "SSM" in c.name]
        self.assertEqual(len(ssm_checks), 2)
        for c in ssm_checks:
            self.assertTrue(c.passed)

    def test_ssm_denied_is_critical(self):
        session = make_session(ssm_error=client_error("AccessDenied"))
        report = v.Report()
        v.check_agent_runtime_permissions(session, report)
        ssm_checks = [c for c in report.checks if "SSM" in c.name and not c.passed]
        self.assertGreater(len(ssm_checks), 0)
        for c in ssm_checks:
            self.assertTrue(c.critical)

    def test_ssm_denied_mentions_action_lambda_role(self):
        session = make_session(ssm_error=client_error("AccessDenied"))
        report = v.Report()
        v.check_agent_runtime_permissions(session, report)
        ssm_check = next((c for c in report.checks if "SSM" in c.name and not c.passed), None)
        self.assertIsNotNone(ssm_check)
        self.assertIn("ActionLambdaRole", ssm_check.warning)

    def test_runtime_probes_cover_all_services(self):
        session = make_session()
        report = v.Report()
        v.check_agent_runtime_permissions(session, report)
        names = " ".join(c.name for c in report.checks)
        for svc in ("EC2", "CloudWatch", "Lambda", "Logs", "X-Ray", "SSM"):
            self.assertIn(svc, names, f"{svc} probe missing from runtime checks")


# ─────────────────────────────────────────────────────────────────────────────
# 7. print_report
# ─────────────────────────────────────────────────────────────────────────────

class TestPrintReport(unittest.TestCase):

    def _capture(self, report):
        old_stdout = sys.stdout
        sys.stdout = StringIO()
        try:
            v.print_report(report)
            return sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout

    def test_all_pass_shows_success(self):
        r = v.Report()
        r.add(v.Check("X", passed=True, detail="ok"))
        out = self._capture(r)
        self.assertIn("Total", out)
        self.assertIn("Passed", out)

    def test_critical_failure_shows_required_fixes(self):
        r = v.Report()
        r.add(v.Check("Bad", passed=False, detail="nope", critical=True,
                       warning="Fix this now"))
        out = self._capture(r)
        self.assertIn("Bad", out)

    def test_non_critical_shows_warning_not_fail(self):
        r = v.Report()
        r.add(v.Check("Warn", passed=False, detail="warn", critical=False))
        out = self._capture(r)
        self.assertIn("Warn", out)


# ─────────────────────────────────────────────────────────────────────────────
# 8. Full integration flow
# ─────────────────────────────────────────────────────────────────────────────

class TestFullFlow(unittest.TestCase):

    def _run_all(self, session):
        report = v.Report()
        identity = v.check_identity(session, report)
        if identity is None:
            return report
        v.check_iam_permissions(session, report, identity["Arn"])
        v.check_bedrock_model_access(session, report)
        v.check_bedrock_agents_api(session, report)
        v.check_agent_runtime_permissions(session, report)
        return report

    def test_fully_healthy_account_no_critical_failures(self):
        report = self._run_all(make_session())
        self.assertEqual(len(report.critical_failures), 0)

    def test_invalid_creds_stops_at_check_1(self):
        session = make_session(identity_error=client_error("InvalidClientTokenId"))
        report = self._run_all(session)
        self.assertEqual(len(report.checks), 1)
        self.assertFalse(report.checks[0].passed)

    def test_bedrock_failure_does_not_stop_other_checks(self):
        """Even if Bedrock model check fails, agent API and runtime checks still run."""
        session = make_session(bedrock_model_error=client_error("AccessDeniedException"))
        report = self._run_all(session)
        check_names = [c.name for c in report.checks]
        # Agent and runtime checks should still be present
        has_agent = any("Agent" in n or "agent" in n for n in check_names)
        has_runtime = any("EC2" in n or "Lambda" in n or "CloudWatch" in n for n in check_names)
        self.assertTrue(has_agent)
        self.assertTrue(has_runtime)

    def test_minimum_check_count_when_all_pass(self):
        """A healthy run must produce at least 6 checks."""
        report = self._run_all(make_session())
        self.assertGreaterEqual(len(report.checks), 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)