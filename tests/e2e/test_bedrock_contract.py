"""
E2E contract tests — Lambda handler <-> Bedrock Agent event protocol

Validates the complete round-trip contract as Amazon Bedrock Agents executes it.
All clients are mocked via _make_clients.
"""
import json
import sys
import os
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import conftest_stdlib  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../lambda/monitor-actions"))
import index  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

def bedrock_event(api_path, parameters=None, include_agent_block=True):
    evt = {
        "messageVersion": "1.0",
        "actionGroup": "MonitorActions",
        "apiPath": "/" + api_path.lstrip("/"),
        "httpMethod": "GET",
        "parameters": parameters or [],
        "requestBody": {},
        "sessionAttributes": {},
        "promptSessionAttributes": {},
        "inputText": "What is the health of my infrastructure?",
        "sessionId": "abc-123",
    }
    if include_agent_block:
        evt["agent"] = {
            "name": "aws-monitor-agent",
            "id": "AGENTID01",
            "alias": "live",
            "version": "DRAFT",
        }
    return evt


def assert_contract(response, expected_status=200):
    assert isinstance(response, dict), "Response is not a dict"
    assert response.get("messageVersion") == "1.0", f"messageVersion wrong: {response.get('messageVersion')}"
    resp = response["response"]
    assert resp["actionGroup"] == "MonitorActions", f"actionGroup wrong: {resp['actionGroup']}"
    assert resp["httpStatusCode"] == expected_status, f"Expected {expected_status}, got {resp['httpStatusCode']}"
    raw = resp["responseBody"]["application/json"]["body"]
    assert isinstance(raw, str), f"body must be str, got {type(raw)}"
    body = json.loads(raw)
    assert isinstance(body, dict), "body must be dict after JSON parse"
    return body


def make_paginator(pages):
    p = MagicMock()
    p.paginate.return_value = pages
    return p


def build_clients(ec2_pages=None, cw_pages=None, lambda_pages=None, alarm_count=0,
                  logs_events=None, xray_summaries=None):
    ec2 = MagicMock()
    ec2.get_paginator.return_value = make_paginator(ec2_pages or [{"Reservations": []}])

    cw = MagicMock()
    alarms = [{"AlarmName": f"a{i}", "AlarmDescription": "", "StateValue": "ALARM",
                "StateReason": "", "MetricName": "CPU", "Namespace": "AWS/EC2",
                "Threshold": 80.0, "ComparisonOperator": "GT",
                "StateUpdatedTimestamp": datetime.now(timezone.utc)}
               for i in range(alarm_count)]
    cw.get_paginator.return_value = make_paginator(
        cw_pages or [{"MetricAlarms": alarms, "CompositeAlarms": []}]
    )
    cw.get_metric_statistics.return_value = {"Datapoints": []}

    lc = MagicMock()
    lc.get_paginator.return_value = make_paginator(lambda_pages or [{"Functions": []}])

    logs = MagicMock()
    logs.start_query.return_value = {"queryId": "q-contract-test"}
    _events = logs_events or []
    logs.get_query_results.return_value = {
        "status": "Complete",
        "results": [[{"field": "@timestamp", "value": "t"}, {"field": "@message", "value": e}] for e in _events],
        "statistics": {"recordsMatched": len(_events)},
    }

    xray = MagicMock()
    xray.get_paginator.return_value = make_paginator(
        [{"TraceSummaries": xray_summaries or []}]
    )

    return {"ec2": ec2, "lambda": lc, "cloudwatch": cw, "logs": logs, "xray": xray}


# ─────────────────────────────────────────────────────────────────────────────
# Bedrock 1.0 envelope contract
# ─────────────────────────────────────────────────────────────────────────────

class TestBedrockEnvelopeContract(unittest.TestCase):
    def setUp(self):
        self.clients = build_clients()

    def _invoke(self, path, params=None):
        with patch.object(index, "_make_clients", return_value=self.clients):
            return index.handler(bedrock_event(path, params), None)

    def test_messageVersion_is_1_0(self):
        r = self._invoke("get_overall_health")
        self.assertEqual(r["messageVersion"], "1.0")

    def test_actionGroup_is_MonitorActions(self):
        r = self._invoke("get_overall_health")
        self.assertEqual(r["response"]["actionGroup"], "MonitorActions")

    def test_apiPath_echoed_back(self):
        r = self._invoke("get_ec2_health")
        self.assertEqual(r["response"]["apiPath"], "/get_ec2_health")

    def test_responseBody_content_type(self):
        r = self._invoke("get_overall_health")
        self.assertIn("application/json", r["response"]["responseBody"])

    def test_body_is_string(self):
        r = self._invoke("get_overall_health")
        raw = r["response"]["responseBody"]["application/json"]["body"]
        self.assertIsInstance(raw, str)

    def test_body_parses_to_dict(self):
        r = self._invoke("get_overall_health")
        raw = r["response"]["responseBody"]["application/json"]["body"]
        self.assertIsInstance(json.loads(raw), dict)

    def test_200_on_success(self):
        r = self._invoke("get_overall_health")
        self.assertEqual(r["response"]["httpStatusCode"], 200)

    def test_404_on_unknown_path(self):
        r = index.handler(bedrock_event("get_rds_health"), None)
        assert_contract(r, 404)

    def test_400_on_invalid_region(self):
        r = self._invoke("get_overall_health", [{"name": "region", "value": "mars-1"}])
        self.assertEqual(r["response"]["httpStatusCode"], 400)

    def test_response_includes_region_field(self):
        r = self._invoke("get_overall_health")
        body = assert_contract(r)
        self.assertIn("region", body)


# ─────────────────────────────────────────────────────────────────────────────
# Multi-region contract
# ─────────────────────────────────────────────────────────────────────────────

class TestMultiRegionContract(unittest.TestCase):
    def _invoke(self, path, region, extra_params=None):
        params = [{"name": "region", "value": region}] + (extra_params or [])
        clients = build_clients()
        with patch.object(index, "_make_clients", return_value=clients) as mock_mc:
            r = index.handler(bedrock_event(path, params), None)
        return r, mock_mc

    def test_eu_west_1_passed_to_make_clients(self):
        _, mock_mc = self._invoke("get_overall_health", "eu-west-1")
        mock_mc.assert_called_once_with("eu-west-1")

    def test_ap_northeast_1_in_response(self):
        r, _ = self._invoke("get_ec2_health", "ap-northeast-1")
        body = assert_contract(r)
        self.assertEqual(body["region"], "ap-northeast-1")

    def test_sa_east_1_in_response(self):
        r, _ = self._invoke("get_cloudwatch_alarms", "sa-east-1")
        body = assert_contract(r)
        self.assertEqual(body["region"], "sa-east-1")

    def test_us_west_2_lambda_health(self):
        r, mock_mc = self._invoke("get_lambda_health", "us-west-2")
        mock_mc.assert_called_once_with("us-west-2")
        body = assert_contract(r)
        self.assertEqual(body["region"], "us-west-2")

    def test_invalid_region_contract(self):
        params = [{"name": "region", "value": "not-a-region"}]
        r = index.handler(bedrock_event("get_overall_health", params), None)
        body = assert_contract(r, 400)
        self.assertIn("error", body)

    def test_default_region_us_east_1_when_absent(self):
        clients = build_clients()
        with patch.object(index, "_make_clients", return_value=clients) as mock_mc:
            index.handler(bedrock_event("get_overall_health"), None)
        mock_mc.assert_called_once_with("us-east-1")

    def test_logs_analysis_cross_region(self):
        params = [
            {"name": "region", "value": "eu-central-1"},
            {"name": "log_group", "value": "/aws/lambda/eu-fn"},
        ]
        clients = build_clients()
        with patch.object(index, "_make_clients", return_value=clients) as mock_mc:
            r = index.handler(bedrock_event("get_logs_analysis", params), None)
        mock_mc.assert_called_once_with("eu-central-1")
        body = assert_contract(r)
        self.assertEqual(body["region"], "eu-central-1")

    def test_xray_traces_cross_region(self):
        params = [{"name": "region", "value": "ap-southeast-1"}]
        clients = build_clients()
        with patch.object(index, "_make_clients", return_value=clients) as mock_mc:
            r = index.handler(bedrock_event("get_xray_traces", params), None)
        mock_mc.assert_called_once_with("ap-southeast-1")
        body = assert_contract(r)
        self.assertEqual(body["region"], "ap-southeast-1")


# ─────────────────────────────────────────────────────────────────────────────
# Security contract
# ─────────────────────────────────────────────────────────────────────────────

class TestSecurityContract(unittest.TestCase):
    def setUp(self):
        self.clients = build_clients()

    def _invoke(self, path, params=None):
        with patch.object(index, "_make_clients", return_value=self.clients):
            return index.handler(bedrock_event(path, params), None)

    def test_no_credentials_in_response(self):
        r = self._invoke("get_overall_health")
        body_str = r["response"]["responseBody"]["application/json"]["body"]
        for keyword in ("AccessKey", "SecretKey", "password", "secret"):
            self.assertNotIn(keyword, body_str)

    def test_no_arn_in_response(self):
        r = self._invoke("get_overall_health")
        body_str = r["response"]["responseBody"]["application/json"]["body"]
        self.assertNotIn("arn:aws:iam", body_str)

    def test_invalid_action_does_not_expose_internals(self):
        r = index.handler(bedrock_event("get_iam_roles"), None)
        body_str = r["response"]["responseBody"]["application/json"]["body"]
        self.assertNotIn("Traceback", body_str)

    def test_injection_attempt_in_region_param(self):
        r = self._invoke("get_overall_health", [{"name": "region", "value": "'; DROP TABLE--"}])
        self.assertEqual(r["response"]["httpStatusCode"], 400)

    def test_opt_in_region_error_handled_gracefully(self):
        clients = build_clients()
        clients["ec2"].get_paginator.side_effect = Exception("AuthFailure: not opted in")
        # Call handler directly (not self._invoke) to avoid inner patch overriding outer patch
        with patch.object(index, "_make_clients", return_value=clients):
            r = index.handler(bedrock_event("get_ec2_health", [{"name": "region", "value": "ap-east-1"}]), None)
        self.assertIn(r["response"]["httpStatusCode"], (403, 500))
        body = json.loads(r["response"]["responseBody"]["application/json"]["body"])
        self.assertIn("error", body)


# ─────────────────────────────────────────────────────────────────────────────
# Complete flow scenarios (ReAct loop simulation)
# ─────────────────────────────────────────────────────────────────────────────

class TestCompleteFlowScenarios(unittest.TestCase):
    def _invoke(self, path, params=None, clients=None):
        c = clients or build_clients()
        with patch.object(index, "_make_clients", return_value=c):
            return index.handler(bedrock_event(path, params), None)

    def test_scenario_general_status_check(self):
        """Bedrock calls get_overall_health first on 'how is my infra?'"""
        r = self._invoke("get_overall_health")
        body = assert_contract(r)
        for key in ("overall_status", "ec2", "active_alarms", "lambda_functions", "region"):
            self.assertIn(key, body)

    def test_scenario_ec2_filter_running(self):
        """Agent filters running instances after user asks about active servers"""
        r = self._invoke("get_ec2_health", [{"name": "state", "value": "running"}])
        assert_contract(r)

    def test_scenario_lambda_errors_then_logs(self):
        """Agent calls get_lambda_health, finds errors, then calls get_logs_analysis"""
        lc = MagicMock()
        lc.get_paginator.return_value = make_paginator([{"Functions": [
            {"FunctionName": "api-fn", "Runtime": "python3.12",
             "MemorySize": 256, "Timeout": 30, "State": "Active"}
        ]}])
        cw = MagicMock()
        cw.get_metric_statistics.side_effect = [
            {"Datapoints": [{"Sum": 100}]},
            {"Datapoints": [{"Sum": 15}]},
            {"Datapoints": []},
            {"Datapoints": []},
        ]
        c = build_clients()
        c["lambda"] = lc
        c["cloudwatch"] = cw
        r = self._invoke("get_lambda_health", clients=c)
        body = assert_contract(r)
        fn = body["functions"][0]
        self.assertEqual(fn["metrics"]["health"], "critical")

        # Second call: agent queries logs for the failing function
        r2 = self._invoke("get_logs_analysis", [{"name": "log_group", "value": "/aws/lambda/api-fn"}])
        assert_contract(r2)

    def test_scenario_cross_region_comparison(self):
        """Agent calls same tool twice for different regions (ReAct multi-call)"""
        clients_us = build_clients()
        clients_eu = build_clients()
        with patch.object(index, "_make_clients", return_value=clients_us) as mc1:
            r1 = index.handler(bedrock_event("get_ec2_health", [{"name": "region", "value": "us-east-1"}]), None)
        with patch.object(index, "_make_clients", return_value=clients_eu) as mc2:
            r2 = index.handler(bedrock_event("get_ec2_health", [{"name": "region", "value": "eu-west-1"}]), None)
        mc1.assert_called_once_with("us-east-1")
        mc2.assert_called_once_with("eu-west-1")
        assert_contract(r1)
        assert_contract(r2)

    def test_scenario_alarms_all_regions(self):
        """User asks for alarms in a specific region"""
        r = self._invoke("get_cloudwatch_alarms", [
            {"name": "region", "value": "ap-northeast-1"},
            {"name": "state", "value": "ALL"},
        ])
        body = assert_contract(r)
        self.assertEqual(body["region"], "ap-northeast-1")

    def test_scenario_xray_with_filter(self):
        r = self._invoke("get_xray_traces", [
            {"name": "filter_expression", "value": "fault = true"},
            {"name": "hours", "value": "2"},
        ])
        body = assert_contract(r)
        self.assertEqual(body["period_hours"], 2)

    def test_scenario_without_agent_block(self):
        """Bedrock sometimes omits the agent block in test invocations"""
        c = build_clients()
        with patch.object(index, "_make_clients", return_value=c):
            r = index.handler(bedrock_event("get_overall_health", include_agent_block=False), None)
        assert_contract(r)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestSsmInventoryContract(unittest.TestCase):
    """E2E contract tests for get_ssm_inventory — Bedrock 1.0 envelope + security."""

    def _invoke(self, params=None):
        from unittest.mock import MagicMock

        def make_paginator(pages):
            pag = MagicMock()
            pag.paginate.return_value = iter(pages)
            return pag

        ssm = MagicMock()
        inst_pag = make_paginator([{"InstanceInformationList": [{
            "InstanceId": "i-0abc123", "ComputerName": "ip-10-0-0-1",
            "PlatformType": "Linux", "PlatformName": "Amazon Linux 2",
            "PlatformVersion": "2", "AgentVersion": "3.2.0",
            "IPAddress": "10.0.0.1", "PingStatus": "Online",
            "LastPingDateTime": __import__('datetime').datetime(2026, 6, 1, 0, 0, 0),
            "AssociationStatus": "Success", "ResourceType": "ManagedInstance",
        }]}])
        inv_pag = make_paginator([{"Entities": []}])

        def pag_side_effect(op):
            return inst_pag if op == "describe_instance_information" else inv_pag

        ssm.get_paginator.side_effect = pag_side_effect

        with patch("index.boto3") as mock_boto:
            mock_boto.client.return_value = ssm
            event = bedrock_event("get_ssm_inventory", params or [])
            return index.handler(event, None)

    def test_response_is_valid_bedrock_1_0_envelope(self):
        r = self._invoke()
        assert_contract(r)

    def test_httpmethod_echoed_back(self):
        # bedrock_event helper sends httpMethod=GET; handler must echo it back
        r = self._invoke()
        self.assertEqual(r["response"]["httpMethod"], "GET")

    def test_api_path_echoed_back(self):
        r = self._invoke()
        self.assertEqual(r["response"]["apiPath"], "/get_ssm_inventory")

    def test_action_group_is_monitor_actions(self):
        r = self._invoke()
        self.assertEqual(r["response"]["actionGroup"], "MonitorActions")

    def test_response_body_has_managed_instance_count(self):
        r = self._invoke()
        body = assert_contract(r)
        self.assertIn("managed_instance_count", body)

    def test_response_body_has_instances_list(self):
        r = self._invoke()
        body = assert_contract(r)
        self.assertIsInstance(body["instances"], list)

    def test_region_present_in_response(self):
        r = self._invoke()
        body = assert_contract(r)
        self.assertIn("region", body)

    def test_invalid_inventory_type_returns_400(self):
        r = self._invoke(params=[{"name": "inventory_type", "value": "BAD_TYPE"}])
        self.assertEqual(r["response"]["httpStatusCode"], 400)

    def test_invalid_region_returns_400(self):
        r = self._invoke(params=[{"name": "region", "value": "not-a-region"}])
        self.assertEqual(r["response"]["httpStatusCode"], 400)

    def test_note_field_explains_prerequisites(self):
        r = self._invoke()
        body = assert_contract(r)
        self.assertIn("SSM Agent", body["note"])

    def test_no_arn_or_account_in_response(self):
        """Security: response must not leak ARNs or account IDs."""
        r = self._invoke()
        text = json.dumps(r)
        self.assertNotRegex(text, r'arn:aws:[a-z]+:[a-z0-9-]+:\d{12}')

    def test_all_inventory_type_accepted(self):
        r = self._invoke(params=[{"name": "inventory_type", "value": "ALL"}])
        self.assertEqual(r["response"]["httpStatusCode"], 200)
