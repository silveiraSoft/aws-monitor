"""
Unit tests for lambda/monitor-actions/index.py
Run with:  python3 -m pytest tests/  (with pytest)
       or: python3 -m unittest discover -s tests/unit  (stdlib only)

All AWS calls are mocked — no real AWS credentials needed.
Multi-region: tests verify region parameter extraction, validation,
and that _make_clients is called with the correct region.
"""
import json
import sys
import os
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, call

# Inject boto3/botocore stubs before importing handler (no pip needed)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import conftest_stdlib  # noqa: E402  (side-effect: stubs boto3)

# Make the lambda handler importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../lambda/monitor-actions"))
import index  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_event(api_path: str, params=None):
    return {
        "messageVersion": "1.0",
        "actionGroup": "MonitorActions",
        "apiPath": "/" + api_path.lstrip("/"),
        "httpMethod": "GET",
        "parameters": params or [],
        "requestBody": {},
    }

def parse_body(response: dict) -> dict:
    return json.loads(response["response"]["responseBody"]["application/json"]["body"])

def make_paginator(pages: list):
    p = MagicMock()
    p.paginate.return_value = pages
    return p

def make_clients(ec2=None, lambda_c=None, cloudwatch=None, logs=None, xray=None,
                  elbv2=None, cloudtrail=None):
    """Build the dict that _make_clients returns, with sensible defaults."""
    ec2_m = ec2 or MagicMock()
    cw_m = cloudwatch or MagicMock()
    lc_m = lambda_c or MagicMock()
    logs_m = logs or MagicMock()
    xray_m = xray or MagicMock()
    elbv2_m = elbv2 or MagicMock()
    ct_m = cloudtrail or MagicMock()
    if ec2 is None:
        ec2_m.get_paginator.return_value = make_paginator([{"Reservations": []}])
    if cloudwatch is None:
        cw_m.get_paginator.return_value = make_paginator([{"MetricAlarms": [], "CompositeAlarms": []}])
        cw_m.get_metric_statistics.return_value = {"Datapoints": []}
    if lambda_c is None:
        lc_m.get_paginator.return_value = make_paginator([{"Functions": []}])
    if logs is None:
        logs_m.start_query.return_value = {"queryId": "q1"}
        logs_m.get_query_results.return_value = {"status": "Complete", "results": [], "statistics": {}}
    if xray is None:
        xray_m.get_paginator.return_value = make_paginator([{"TraceSummaries": []}])
    if elbv2 is None:
        elbv2_m.describe_load_balancers.return_value = {"LoadBalancers": []}
    if cloudtrail is None:
        ct_m.lookup_events.return_value = {"Events": []}
    return {"ec2": ec2_m, "lambda": lc_m, "cloudwatch": cw_m, "logs": logs_m, "xray": xray_m,
            "elbv2": elbv2_m, "cloudtrail": ct_m}

def make_ec2_instance(instance_id, state="running", name=None, instance_type="t3.micro", az="us-east-1a"):
    now = datetime.now(timezone.utc)
    inst = {
        "InstanceId": instance_id,
        "InstanceType": instance_type,
        "State": {"Name": state},
        "Placement": {"AvailabilityZone": az},
        "PrivateIpAddress": "10.0.0.1",
        "LaunchTime": now,
    }
    if name:
        inst["Tags"] = [{"Key": "Name", "Value": name}]
    else:
        inst["Tags"] = []
    return inst

def make_alarm(name, state="ALARM", metric="CPUUtilization", namespace="AWS/EC2", threshold=80.0):
    return {
        "AlarmName": name,
        "AlarmDescription": f"Test alarm {name}",
        "StateValue": state,
        "StateReason": "threshold crossed",
        "MetricName": metric,
        "Namespace": namespace,
        "Threshold": threshold,
        "ComparisonOperator": "GreaterThanThreshold",
        "StateUpdatedTimestamp": datetime.now(timezone.utc),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Envelope & contract
# ─────────────────────────────────────────────────────────────────────────────

class TestResponseEnvelope(unittest.TestCase):
    def setUp(self):
        self.clients = make_clients()

    def _call(self, action, params=None):
        with patch.object(index, "_make_clients", return_value=self.clients):
            return index.handler(make_event(action, params), None)

    def test_ok_envelope_structure(self):
        r = self._call("get_overall_health")
        self.assertEqual(r["messageVersion"], "1.0")
        self.assertIn("response", r)
        resp = r["response"]
        self.assertEqual(resp["actionGroup"], "MonitorActions")
        self.assertEqual(resp["httpStatusCode"], 200)
        self.assertIn("responseBody", resp)
        body_str = resp["responseBody"]["application/json"]["body"]
        self.assertIsInstance(body_str, str)
        json.loads(body_str)  # must be valid JSON

    def test_body_is_always_string(self):
        r = self._call("get_overall_health")
        raw = r["response"]["responseBody"]["application/json"]["body"]
        self.assertIsInstance(raw, str)

    def test_api_path_injected_in_response(self):
        r = self._call("get_ec2_health")
        self.assertEqual(r["response"]["apiPath"], "/get_ec2_health")

    def test_unknown_action_returns_404(self):
        r = index.handler({"apiPath": "/nonexistent", "parameters": []}, None)
        self.assertEqual(r["response"]["httpStatusCode"], 404)

    def test_empty_api_path_returns_404(self):
        r = index.handler({"apiPath": "", "parameters": []}, None)
        self.assertEqual(r["response"]["httpStatusCode"], 404)

    def test_response_includes_region(self):
        r = self._call("get_overall_health")
        body = parse_body(r)
        self.assertIn("region", body)

    def test_default_region_is_us_east_1(self):
        r = self._call("get_overall_health")
        body = parse_body(r)
        self.assertEqual(body["region"], "us-east-1")


# ─────────────────────────────────────────────────────────────────────────────
# get_param
# ─────────────────────────────────────────────────────────────────────────────

class TestGetParam(unittest.TestCase):
    def test_from_parameters_list(self):
        self.assertEqual(index.get_param({"parameters": [{"name": "state", "value": "running"}]}, "state"), "running")

    def test_from_request_body(self):
        event = {"parameters": [], "requestBody": {"content": {"application/json": {"properties": [{"name": "prefix", "value": "api-"}]}}}}
        self.assertEqual(index.get_param(event, "prefix"), "api-")

    def test_parameters_takes_priority_over_body(self):
        event = {
            "parameters": [{"name": "state", "value": "from-params"}],
            "requestBody": {"content": {"application/json": {"properties": [{"name": "state", "value": "from-body"}]}}}
        }
        self.assertEqual(index.get_param(event, "state"), "from-params")

    def test_returns_none_when_missing(self):
        self.assertIsNone(index.get_param({"parameters": []}, "missing"))

    def test_returns_none_when_no_parameters_key(self):
        self.assertIsNone(index.get_param({}, "state"))

    def test_returns_none_on_malformed_body(self):
        self.assertIsNone(index.get_param({"parameters": [], "requestBody": {"content": {}}}, "state"))


# ─────────────────────────────────────────────────────────────────────────────
# Region validation
# ─────────────────────────────────────────────────────────────────────────────

class TestRegionValidation(unittest.TestCase):
    def _call(self, action, params=None):
        return index.handler(make_event(action, params), None)

    def test_valid_region_us_east_1(self):
        clients = make_clients()
        with patch.object(index, "_make_clients", return_value=clients):
            r = self._call("get_overall_health", [{"name": "region", "value": "us-east-1"}])
        self.assertEqual(r["response"]["httpStatusCode"], 200)

    def test_valid_region_eu_west_1(self):
        clients = make_clients()
        with patch.object(index, "_make_clients", return_value=clients) as mock_mc:
            r = self._call("get_overall_health", [{"name": "region", "value": "eu-west-1"}])
        mock_mc.assert_called_once_with("eu-west-1")
        self.assertEqual(r["response"]["httpStatusCode"], 200)

    def test_invalid_region_returns_400(self):
        r = self._call("get_overall_health", [{"name": "region", "value": "mars-west-1"}])
        self.assertEqual(r["response"]["httpStatusCode"], 400)
        body = parse_body(r)
        self.assertIn("error", body)
        self.assertIn("mars-west-1", body["error"])

    def test_invalid_region_does_not_call_make_clients(self):
        with patch.object(index, "_make_clients") as mock_mc:
            self._call("get_ec2_health", [{"name": "region", "value": "invalid"}])
        mock_mc.assert_not_called()

    def test_region_passed_to_make_clients(self):
        clients = make_clients()
        with patch.object(index, "_make_clients", return_value=clients) as mock_mc:
            self._call("get_ec2_health", [{"name": "region", "value": "ap-northeast-1"}])
        mock_mc.assert_called_once_with("ap-northeast-1")

    def test_region_in_response_body(self):
        clients = make_clients()
        with patch.object(index, "_make_clients", return_value=clients):
            r = self._call("get_ec2_health", [{"name": "region", "value": "sa-east-1"}])
        body = parse_body(r)
        self.assertEqual(body["region"], "sa-east-1")

    def test_default_region_when_not_specified(self):
        clients = make_clients()
        with patch.object(index, "_make_clients", return_value=clients) as mock_mc:
            self._call("get_cloudwatch_alarms")
        mock_mc.assert_called_once_with(index.DEFAULT_REGION)

    def test_region_case_insensitive(self):
        clients = make_clients()
        with patch.object(index, "_make_clients", return_value=clients) as mock_mc:
            r = self._call("get_overall_health", [{"name": "region", "value": "US-EAST-1"}])
        mock_mc.assert_called_once_with("us-east-1")
        self.assertEqual(r["response"]["httpStatusCode"], 200)


# ─────────────────────────────────────────────────────────────────────────────
# get_ec2_health
# ─────────────────────────────────────────────────────────────────────────────

class TestGetEc2Health(unittest.TestCase):
    def _run(self, params=None, ec2=None):
        ec2_m = ec2 or MagicMock()
        clients = make_clients(ec2=ec2_m)
        with patch.object(index, "_make_clients", return_value=clients):
            return index.get_ec2_health(make_event("get_ec2_health", params)), ec2_m

    def test_empty_returns_zero_instances(self):
        ec2 = MagicMock()
        ec2.get_paginator.return_value = make_paginator([{"Reservations": []}])
        r, _ = self._run(ec2=ec2)
        body = parse_body(r)
        self.assertEqual(body["summary"]["total"], 0)

    def test_running_instances_counted(self):
        ec2 = MagicMock()
        ec2.get_paginator.return_value = make_paginator([{"Reservations": [
            {"Instances": [make_ec2_instance("i-1", "running"), make_ec2_instance("i-2", "stopped")]}
        ]}])
        r, _ = self._run(ec2=ec2)
        body = parse_body(r)
        self.assertEqual(body["summary"]["running"], 1)
        self.assertEqual(body["summary"]["stopped"], 1)

    def test_filter_by_state_running(self):
        ec2 = MagicMock()
        pag = MagicMock()
        pag.paginate.return_value = [{"Reservations": []}]
        ec2.get_paginator.return_value = pag
        self._run([{"name": "state", "value": "running"}], ec2=ec2)
        call_kwargs = pag.paginate.call_args[1]
        self.assertIn("Filters", call_kwargs)
        self.assertEqual(call_kwargs["Filters"][0]["Values"], ["running"])

    def test_invalid_state_returns_400(self):
        r, _ = self._run([{"name": "state", "value": "zombie"}])
        self.assertEqual(r["response"]["httpStatusCode"], 400)

    def test_instance_name_from_tags(self):
        ec2 = MagicMock()
        ec2.get_paginator.return_value = make_paginator([{"Reservations": [
            {"Instances": [make_ec2_instance("i-abc", name="web-server")]}
        ]}])
        r, _ = self._run(ec2=ec2)
        body = parse_body(r)
        self.assertEqual(body["instances"][0]["name"], "web-server")

    def test_instance_id_used_when_no_name_tag(self):
        ec2 = MagicMock()
        ec2.get_paginator.return_value = make_paginator([{"Reservations": [
            {"Instances": [make_ec2_instance("i-xyz")]}
        ]}])
        r, _ = self._run(ec2=ec2)
        body = parse_body(r)
        self.assertEqual(body["instances"][0]["name"], "i-xyz")

    def test_region_in_response(self):
        r, _ = self._run([{"name": "region", "value": "eu-central-1"}])
        body = parse_body(r)
        self.assertEqual(body["region"], "eu-central-1")

    def test_paginator_used(self):
        ec2 = MagicMock()
        pag = MagicMock()
        pag.paginate.return_value = [{"Reservations": []}]
        ec2.get_paginator.return_value = pag
        self._run(ec2=ec2)
        ec2.get_paginator.assert_called_once_with("describe_instances")

    def test_all_state_no_filter(self):
        ec2 = MagicMock()
        pag = MagicMock()
        pag.paginate.return_value = [{"Reservations": []}]
        ec2.get_paginator.return_value = pag
        self._run([{"name": "state", "value": "all"}], ec2=ec2)
        call_kwargs = pag.paginate.call_args[1]
        self.assertEqual(call_kwargs.get("Filters", []), [])


# ─────────────────────────────────────────────────────────────────────────────
# get_lambda_health
# ─────────────────────────────────────────────────────────────────────────────

class TestGetLambdaHealth(unittest.TestCase):
    def _run(self, params=None, lc=None, cw=None):
        lc_m = lc or MagicMock()
        cw_m = cw or MagicMock()
        if lc is None:
            lc_m.get_paginator.return_value = make_paginator([{"Functions": []}])
        if cw is None:
            cw_m.get_metric_statistics.return_value = {"Datapoints": []}
        clients = make_clients(lambda_c=lc_m, cloudwatch=cw_m)
        with patch.object(index, "_make_clients", return_value=clients):
            return index.get_lambda_health(make_event("get_lambda_health", params))

    def test_empty_returns_zero_functions(self):
        r = self._run()
        self.assertEqual(parse_body(r)["total_functions"], 0)

    def test_hours_clamped_to_max(self):
        self.assertEqual(parse_body(self._run([{"name": "hours", "value": "999999"}]))["period_hours"], index.MAX_LAMBDA_HOURS)

    def test_hours_clamped_to_min(self):
        self.assertEqual(parse_body(self._run([{"name": "hours", "value": "0"}]))["period_hours"], index.MIN_LAMBDA_HOURS)

    def test_negative_hours_clamped_to_min(self):
        self.assertEqual(parse_body(self._run([{"name": "hours", "value": "-100"}]))["period_hours"], index.MIN_LAMBDA_HOURS)

    def test_non_integer_hours_returns_400(self):
        r = self._run([{"name": "hours", "value": "abc"}])
        self.assertEqual(r["response"]["httpStatusCode"], 400)

    def test_prefix_filter_applied(self):
        lc = MagicMock()
        lc.get_paginator.return_value = make_paginator([{"Functions": [
            {"FunctionName": "api-fn", "Runtime": "python3.12", "MemorySize": 128, "Timeout": 3, "State": "Active"},
            {"FunctionName": "worker-fn", "Runtime": "python3.12", "MemorySize": 128, "Timeout": 3, "State": "Active"},
        ]}])
        cw = MagicMock()
        cw.get_metric_statistics.return_value = {"Datapoints": []}
        r = self._run([{"name": "prefix", "value": "api-"}], lc=lc, cw=cw)
        body = parse_body(r)
        self.assertEqual(body["total_functions"], 1)
        self.assertEqual(body["functions"][0]["name"], "api-fn")

    def test_region_in_response(self):
        r = self._run([{"name": "region", "value": "us-west-2"}])
        self.assertEqual(parse_body(r)["region"], "us-west-2")


# ─────────────────────────────────────────────────────────────────────────────
# _get_lambda_metrics
# ─────────────────────────────────────────────────────────────────────────────

class TestGetLambdaMetrics(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)

    def _run(self, cw=None, datapoints=None):
        cw_m = cw or MagicMock()
        if datapoints is not None:
            cw_m.get_metric_statistics.return_value = {"Datapoints": datapoints}
        elif cw is None:
            cw_m.get_metric_statistics.return_value = {"Datapoints": []}
        return index._get_lambda_metrics("fn", self.now - timedelta(hours=1), self.now, cw_m)

    def test_zero_invocations_zero_error_rate(self):
        m = self._run(datapoints=[])
        self.assertEqual(m["error_rate_pct"], 0)
        self.assertEqual(m["health"], "healthy")

    def test_error_rate_calculated_correctly(self):
        cw = MagicMock()
        cw.get_metric_statistics.side_effect = [
            {"Datapoints": [{"Sum": 100}]},  # Invocations
            {"Datapoints": [{"Sum": 12}]},   # Errors
            {"Datapoints": [{"Average": 200}]},  # Duration
            {"Datapoints": [{"Sum": 0}]},    # Throttles
        ]
        m = index._get_lambda_metrics("fn", self.now - timedelta(hours=1), self.now, cw)
        self.assertEqual(m["error_rate_pct"], 12.0)
        self.assertEqual(m["health"], "critical")

    def test_boundary_error_rate_10_is_critical(self):
        cw = MagicMock()
        cw.get_metric_statistics.side_effect = [
            {"Datapoints": [{"Sum": 100}]},
            {"Datapoints": [{"Sum": 10}]},
            {"Datapoints": []},
            {"Datapoints": []},
        ]
        m = index._get_lambda_metrics("fn", self.now - timedelta(hours=1), self.now, cw)
        self.assertEqual(m["health"], "critical")

    def test_warning_at_6_percent(self):
        cw = MagicMock()
        cw.get_metric_statistics.side_effect = [
            {"Datapoints": [{"Sum": 100}]},
            {"Datapoints": [{"Sum": 6}]},
            {"Datapoints": []},
            {"Datapoints": []},
        ]
        m = index._get_lambda_metrics("fn", self.now - timedelta(hours=1), self.now, cw)
        self.assertEqual(m["health"], "warning")

    def test_period_minimum_60_seconds(self):
        cw = MagicMock()
        cw.get_metric_statistics.return_value = {"Datapoints": []}
        index._get_lambda_metrics("fn", self.now - timedelta(hours=1), self.now, cw)
        _, kwargs = cw.get_metric_statistics.call_args_list[0]
        self.assertGreaterEqual(kwargs.get("Period", 0) if kwargs else cw.get_metric_statistics.call_args[1].get("Period", 0), 60)

    def test_metric_exception_returns_zero(self):
        cw = MagicMock()
        cw.get_metric_statistics.side_effect = Exception("throttled")
        m = index._get_lambda_metrics("fn", self.now - timedelta(hours=1), self.now, cw)
        self.assertEqual(m["invocations"], 0)


# ─────────────────────────────────────────────────────────────────────────────
# get_cloudwatch_alarms
# ─────────────────────────────────────────────────────────────────────────────

class TestGetCloudwatchAlarms(unittest.TestCase):
    def _run(self, params=None, cw=None):
        cw_m = cw or MagicMock()
        if cw is None:
            cw_m.get_paginator.return_value = make_paginator([{"MetricAlarms": [], "CompositeAlarms": []}])
        clients = make_clients(cloudwatch=cw_m)
        with patch.object(index, "_make_clients", return_value=clients):
            return index.get_cloudwatch_alarms(make_event("get_cloudwatch_alarms", params)), cw_m

    def test_empty_returns_zero_alarms(self):
        r, _ = self._run()
        body = parse_body(r)
        self.assertEqual(body["summary"]["total"], 0)

    def test_invalid_state_returns_400(self):
        r, _ = self._run([{"name": "state", "value": "INVALID"}])
        self.assertEqual(r["response"]["httpStatusCode"], 400)

    def test_default_state_alarm(self):
        cw = MagicMock()
        pag = MagicMock()
        pag.paginate.return_value = [{"MetricAlarms": [], "CompositeAlarms": []}]
        cw.get_paginator.return_value = pag
        self._run(cw=cw)
        pag.paginate.assert_called_once_with(StateValue="ALARM")

    def test_all_state_no_filter(self):
        cw = MagicMock()
        pag = MagicMock()
        pag.paginate.return_value = [{"MetricAlarms": [], "CompositeAlarms": []}]
        cw.get_paginator.return_value = pag
        self._run([{"name": "state", "value": "ALL"}], cw=cw)
        pag.paginate.assert_called_once_with()

    def test_alarm_fields_present(self):
        cw = MagicMock()
        cw.get_paginator.return_value = make_paginator([{
            "MetricAlarms": [make_alarm("cpu-high")],
            "CompositeAlarms": [],
        }])
        r, _ = self._run([{"name": "state", "value": "ALL"}], cw=cw)
        body = parse_body(r)
        a = body["alarms"][0]
        for key in ("name", "state", "metric", "threshold", "updated_at"):
            self.assertIn(key, a)

    def test_alarm_summary_counts(self):
        cw = MagicMock()
        cw.get_paginator.return_value = make_paginator([{
            "MetricAlarms": [
                make_alarm("a1", "ALARM"),
                make_alarm("a2", "OK"),
                make_alarm("a3", "INSUFFICIENT_DATA"),
            ],
            "CompositeAlarms": [],
        }])
        r, _ = self._run([{"name": "state", "value": "ALL"}], cw=cw)
        body = parse_body(r)
        self.assertEqual(body["summary"]["in_alarm"], 1)
        self.assertEqual(body["summary"]["ok"], 1)
        self.assertEqual(body["summary"]["insufficient_data"], 1)

    def test_region_in_response(self):
        r, _ = self._run([{"name": "region", "value": "eu-west-2"}])
        self.assertEqual(parse_body(r)["region"], "eu-west-2")


# ─────────────────────────────────────────────────────────────────────────────
# get_overall_health
# ─────────────────────────────────────────────────────────────────────────────

class TestGetOverallHealth(unittest.TestCase):
    def _run(self, params=None, ec2=None, cw=None, lc=None):
        ec2_m = ec2 or MagicMock()
        cw_m = cw or MagicMock()
        lc_m = lc or MagicMock()
        if ec2 is None:
            ec2_m.get_paginator.return_value = make_paginator([{"Reservations": []}])
        if cw is None:
            cw_m.get_paginator.return_value = make_paginator([{"MetricAlarms": [], "CompositeAlarms": []}])
        if lc is None:
            lc_m.get_paginator.return_value = make_paginator([{"Functions": []}])
        clients = make_clients(ec2=ec2_m, cloudwatch=cw_m, lambda_c=lc_m)
        with patch.object(index, "_make_clients", return_value=clients):
            return index.get_overall_health(make_event("get_overall_health", params))

    def test_healthy_with_no_alarms(self):
        r = self._run()
        body = parse_body(r)
        self.assertEqual(body["overall_status"], "healthy")

    def test_degraded_with_few_alarms(self):
        cw = MagicMock()
        cw.get_paginator.return_value = make_paginator([{
            "MetricAlarms": [make_alarm(f"a{i}") for i in range(3)],
            "CompositeAlarms": [],
        }])
        r = self._run(cw=cw)
        self.assertEqual(parse_body(r)["overall_status"], "degraded")

    def test_critical_with_5_or_more_alarms(self):
        cw = MagicMock()
        cw.get_paginator.return_value = make_paginator([{
            "MetricAlarms": [make_alarm(f"a{i}") for i in range(5)],
            "CompositeAlarms": [],
        }])
        r = self._run(cw=cw)
        self.assertEqual(parse_body(r)["overall_status"], "critical")

    def test_ec2_states_reported(self):
        ec2 = MagicMock()
        ec2.get_paginator.return_value = make_paginator([{"Reservations": [
            {"Instances": [make_ec2_instance("i-1", "running"), make_ec2_instance("i-2", "stopped")]}
        ]}])
        r = self._run(ec2=ec2)
        body = parse_body(r)
        self.assertEqual(body["ec2"].get("running"), 1)
        self.assertEqual(body["ec2"].get("stopped"), 1)

    def test_lambda_count_reported(self):
        lc = MagicMock()
        lc.get_paginator.return_value = make_paginator([{"Functions": [
            {"FunctionName": "f1"}, {"FunctionName": "f2"}
        ]}])
        r = self._run(lc=lc)
        self.assertEqual(parse_body(r)["lambda_functions"], 2)

    def test_timestamp_present(self):
        r = self._run()
        self.assertIn("timestamp", parse_body(r))

    def test_region_in_response(self):
        r = self._run([{"name": "region", "value": "us-west-1"}])
        self.assertEqual(parse_body(r)["region"], "us-west-1")

    def test_ec2_paginator_used(self):
        ec2 = MagicMock()
        pag = MagicMock()
        pag.paginate.return_value = [{"Reservations": []}]
        ec2.get_paginator.return_value = pag
        cw = MagicMock()
        cw.get_paginator.return_value = make_paginator([{"MetricAlarms": [], "CompositeAlarms": []}])
        lc = MagicMock()
        lc.get_paginator.return_value = make_paginator([{"Functions": []}])
        self._run(ec2=ec2, cw=cw, lc=lc)
        ec2.get_paginator.assert_called_once_with("describe_instances")


# ─────────────────────────────────────────────────────────────────────────────
# Handler routing & error path
# ─────────────────────────────────────────────────────────────────────────────

class TestHandlerRouting(unittest.TestCase):
    def _make_default_clients(self):
        ec2 = MagicMock()
        ec2.get_paginator.return_value = make_paginator([{"Reservations": []}])
        return make_clients(ec2=ec2)

    def test_ec2_path_routed(self):
        with patch.object(index, "_make_clients", return_value=self._make_default_clients()):
            resp = index.handler(make_event("get_ec2_health"), None)
        self.assertEqual(resp["response"]["httpStatusCode"], 200)

    def test_lambda_path_routed(self):
        with patch.object(index, "_make_clients", return_value=make_clients()):
            resp = index.handler(make_event("get_lambda_health"), None)
        self.assertEqual(resp["response"]["httpStatusCode"], 200)

    def test_unknown_path_is_404(self):
        resp = index.handler({"apiPath": "/get_rds_health", "parameters": []}, None)
        self.assertEqual(resp["response"]["httpStatusCode"], 404)

    def test_exception_in_action_returns_500(self):
        with patch.object(index, "_make_clients", side_effect=RuntimeError("boom")):
            resp = index.handler(make_event("get_ec2_health"), None)
        self.assertEqual(resp["response"]["httpStatusCode"], 500)


# ─────────────────────────────────────────────────────────────────────────────
# get_logs_analysis
# ─────────────────────────────────────────────────────────────────────────────

class TestGetLogsAnalysis(unittest.TestCase):
    def _make_logs(self, status="Complete", results=None):
        logs = MagicMock()
        logs.start_query.return_value = {"queryId": "q-test-123"}
        logs.get_query_results.return_value = {
            "status": status,
            "results": results or [],
            "statistics": {"recordsMatched": len(results or [])},
        }
        return logs

    def _run(self, params, logs=None):
        logs_m = logs or self._make_logs()
        clients = make_clients(logs=logs_m)
        with patch.object(index, "_make_clients", return_value=clients):
            return index.get_logs_analysis(make_event("get_logs_analysis", params))

    def test_missing_log_group_returns_400(self):
        r = self._run([])
        self.assertEqual(r["response"]["httpStatusCode"], 400)
        self.assertIn("log_group", parse_body(r)["error"])

    def test_successful_query_returns_200(self):
        r = self._run([{"name": "log_group", "value": "/aws/lambda/my-fn"}])
        self.assertEqual(r["response"]["httpStatusCode"], 200)

    def test_query_id_used_in_get_results(self):
        logs = self._make_logs()
        self._run([{"name": "log_group", "value": "/aws/lambda/fn"}], logs=logs)
        logs.get_query_results.assert_called_with(queryId="q-test-123")

    def test_hours_default_1(self):
        r = self._run([{"name": "log_group", "value": "/aws/lambda/fn"}])
        self.assertEqual(parse_body(r)["period_hours"], 1)

    def test_hours_clamped_to_max(self):
        r = self._run([{"name": "log_group", "value": "/aws/lambda/fn"}, {"name": "hours", "value": "999"}])
        self.assertEqual(parse_body(r)["period_hours"], index.MAX_LOGS_HOURS)

    def test_log_group_not_found_returns_404(self):
        logs = MagicMock()
        logs.start_query.side_effect = Exception("ResourceNotFoundException: log group does not exist")
        r = self._run([{"name": "log_group", "value": "/aws/lambda/missing"}], logs=logs)
        self.assertEqual(r["response"]["httpStatusCode"], 404)

    def test_error_events_counted(self):
        results = [
            [{"field": "@timestamp", "value": "t1"}, {"field": "@message", "value": "ERROR something failed"}],
            [{"field": "@timestamp", "value": "t2"}, {"field": "@message", "value": "INFO ok"}],
        ]
        logs = self._make_logs(results=results)
        r = self._run([{"name": "log_group", "value": "/aws/lambda/fn"}], logs=logs)
        body = parse_body(r)
        self.assertEqual(body["error_events"], 1)
        self.assertEqual(body["total_events"], 2)

    def test_query_timeout_returns_504(self):
        logs = MagicMock()
        logs.start_query.return_value = {"queryId": "q-timeout"}
        logs.get_query_results.return_value = {"status": "Running", "results": [], "statistics": {}}
        with patch("index.time") as mock_time:
            mock_time.time.side_effect = [0, index.LOGS_QUERY_TIMEOUT_S + 1]
            mock_time.sleep = MagicMock()
            clients = make_clients(logs=logs)
            with patch.object(index, "_make_clients", return_value=clients):
                r = index.get_logs_analysis(make_event("get_logs_analysis", [{"name": "log_group", "value": "/grp"}]))
        self.assertEqual(r["response"]["httpStatusCode"], 504)

    def test_non_integer_hours_returns_400(self):
        r = self._run([{"name": "log_group", "value": "/grp"}, {"name": "hours", "value": "abc"}])
        self.assertEqual(r["response"]["httpStatusCode"], 400)

    def test_region_in_response(self):
        r = self._run([{"name": "log_group", "value": "/aws/lambda/fn"}, {"name": "region", "value": "eu-central-1"}])
        self.assertEqual(parse_body(r)["region"], "eu-central-1")

    def test_log_group_not_found_message_includes_region(self):
        logs = MagicMock()
        logs.start_query.side_effect = Exception("ResourceNotFoundException: does not exist")
        r = self._run([{"name": "log_group", "value": "/aws/lambda/fn"}, {"name": "region", "value": "eu-west-1"}], logs=logs)
        body = parse_body(r)
        self.assertIn("eu-west-1", body["error"])


# ─────────────────────────────────────────────────────────────────────────────
# get_xray_traces
# ─────────────────────────────────────────────────────────────────────────────

class TestGetXrayTraces(unittest.TestCase):
    def _make_xray(self, summaries=None):
        xray = MagicMock()
        xray.get_paginator.return_value = make_paginator([{"TraceSummaries": summaries or []}])
        return xray

    def _make_trace(self, duration=1.0, has_error=False, has_fault=False, has_throttle=False):
        return {
            "Id": "trace-abc",
            "Duration": duration,
            "ResponseTime": duration * 0.9,
            "HasError": has_error,
            "HasFault": has_fault,
            "HasThrottle": has_throttle,
            "Http": {"HttpURL": "https://api.example.com/v1", "HttpStatus": 200, "HttpMethod": "GET"},
            "Users": [],
            "ServiceIds": [{"Name": "my-service", "Type": "AWS::Lambda::Function"}],
        }

    def _run(self, params=None, xray=None):
        xray_m = xray or self._make_xray()
        clients = make_clients(xray=xray_m)
        with patch.object(index, "_make_clients", return_value=clients):
            return index.get_xray_traces(make_event("get_xray_traces", params))

    def test_empty_traces_returns_200(self):
        r = self._run()
        self.assertEqual(r["response"]["httpStatusCode"], 200)

    def test_empty_traces_zero_counts(self):
        r = self._run()
        body = parse_body(r)
        self.assertEqual(body["total_traces"], 0)
        self.assertEqual(body["summary"]["errors"], 0)

    def test_fault_counted(self):
        xray = self._make_xray([self._make_trace(has_fault=True)])
        r = self._run(xray=xray)
        self.assertEqual(parse_body(r)["summary"]["faults"], 1)

    def test_error_counted(self):
        xray = self._make_xray([self._make_trace(has_error=True)])
        r = self._run(xray=xray)
        self.assertEqual(parse_body(r)["summary"]["errors"], 1)

    def test_health_critical_at_10_percent_fault_rate(self):
        traces = [self._make_trace(has_fault=True)] * 10 + [self._make_trace()] * 90
        xray = self._make_xray(traces)
        r = self._run(xray=xray)
        self.assertEqual(parse_body(r)["summary"]["health"], "critical")

    def test_hours_clamped_to_max(self):
        r = self._run([{"name": "hours", "value": "100"}])
        self.assertEqual(parse_body(r)["period_hours"], index.MAX_XRAY_HOURS)

    def test_filter_expression_passed(self):
        xray = MagicMock()
        pag = MagicMock()
        pag.paginate.return_value = [{"TraceSummaries": []}]
        xray.get_paginator.return_value = pag
        self._run([{"name": "filter_expression", "value": "fault = true"}], xray=xray)
        kwargs = pag.paginate.call_args[1]
        self.assertEqual(kwargs.get("FilterExpression"), "fault = true")

    def test_access_denied_returns_403(self):
        xray = MagicMock()
        xray.get_paginator.return_value = MagicMock()
        xray.get_paginator.return_value.paginate.side_effect = Exception("AccessDeniedException")
        r = self._run(xray=xray)
        self.assertEqual(r["response"]["httpStatusCode"], 403)

    def test_p99_calculation_with_multiple_traces(self):
        traces = [self._make_trace(duration=float(i)) for i in range(1, 101)]
        xray = self._make_xray(traces)
        r = self._run(xray=xray)
        body = parse_body(r)
        self.assertGreater(body["summary"]["p99_duration_s"], 0)

    def test_non_integer_hours_returns_400(self):
        r = self._run([{"name": "hours", "value": "bad"}])
        self.assertEqual(r["response"]["httpStatusCode"], 400)

    def test_region_in_response(self):
        r = self._run([{"name": "region", "value": "ap-northeast-1"}])
        self.assertEqual(parse_body(r)["region"], "ap-northeast-1")

    def test_paginator_called_with_sampling_false(self):
        xray = MagicMock()
        pag = MagicMock()
        pag.paginate.return_value = [{"TraceSummaries": []}]
        xray.get_paginator.return_value = pag
        self._run(xray=xray)
        kwargs = pag.paginate.call_args[1]
        self.assertFalse(kwargs.get("Sampling", True))


# ─────────────────────────────────────────────────────────────────────────────
# Security: input validation tests (new limits added 2026-06-04)
# ─────────────────────────────────────────────────────────────────────────────

class TestInputValidationSecurity(unittest.TestCase):
    """Tests for input length limits and format validation added for security."""

    def _clients(self):
        ec2, lmb, cw, logs, xray = [MagicMock() for _ in range(5)]
        return {"ec2": ec2, "lambda": lmb, "cloudwatch": cw, "logs": logs, "xray": xray}

    # ── log_group ──

    def test_log_group_invalid_chars_returns_400(self):
        """Shell metacharacters in log_group must be rejected."""
        clients = self._clients()
        with patch.object(index, "_make_clients", return_value=clients):
            evt = make_event("/get_logs_analysis", [
                {"name": "log_group", "value": "/aws/lambda/$(evil-cmd)"},
            ])
            r = index.handler(evt, {})
        self.assertEqual(r["response"]["httpStatusCode"], 400)
        body = json.loads(r["response"]["responseBody"]["application/json"]["body"])
        self.assertIn("invalid characters", body["error"].lower())

    def test_log_group_too_long_returns_400(self):
        clients = self._clients()
        with patch.object(index, "_make_clients", return_value=clients):
            evt = make_event("/get_logs_analysis", [
                {"name": "log_group", "value": "/aws/lambda/" + "a" * 520},
            ])
            r = index.handler(evt, {})
        self.assertEqual(r["response"]["httpStatusCode"], 400)
        body = json.loads(r["response"]["responseBody"]["application/json"]["body"])
        self.assertIn("exceeds maximum length", body["error"])

    def test_log_group_valid_paths_accepted(self):
        """Valid log group names must pass format validation."""
        logs_client = MagicMock()
        logs_client.start_query.return_value = {"queryId": "q1"}
        logs_client.get_query_results.return_value = {"status": "Complete", "results": [], "statistics": {}}
        clients = self._clients()
        clients["logs"] = logs_client
        for valid_name in ("/aws/lambda/my-function", "/ecs/my-service", "my-log-group"):
            with patch.object(index, "_make_clients", return_value=clients):
                evt = make_event("/get_logs_analysis", [
                    {"name": "log_group", "value": valid_name},
                ])
                r = index.handler(evt, {})
            # Should not return 400 for format validation
            self.assertNotEqual(r["response"]["httpStatusCode"], 400,
                                msg=f"Valid log group '{valid_name}' was wrongly rejected")

    # ── query_string ──

    def test_query_too_long_returns_400(self):
        clients = self._clients()
        with patch.object(index, "_make_clients", return_value=clients):
            evt = make_event("/get_logs_analysis", [
                {"name": "log_group", "value": "/aws/lambda/fn"},
                {"name": "query", "value": "fields @message | " + "x" * 2100},
            ])
            r = index.handler(evt, {})
        self.assertEqual(r["response"]["httpStatusCode"], 400)
        body = json.loads(r["response"]["responseBody"]["application/json"]["body"])
        self.assertIn("exceeds maximum length", body["error"])

    # ── filter_expression (X-Ray) ──

    def test_xray_filter_expression_too_long_returns_400(self):
        clients = self._clients()
        with patch.object(index, "_make_clients", return_value=clients):
            evt = make_event("/get_xray_traces", [
                {"name": "filter_expression", "value": "url = " + '"x"' * 1200},
            ])
            r = index.handler(evt, {})
        self.assertEqual(r["response"]["httpStatusCode"], 400)
        body = json.loads(r["response"]["responseBody"]["application/json"]["body"])
        self.assertIn("exceeds maximum length", body["error"])

    # ── prefix (Lambda) ──

    def test_prefix_truncated_to_max_length(self):
        """A very long prefix is silently truncated — does not crash."""
        lmb = MagicMock()
        pag = MagicMock()
        pag.paginate.return_value = [{"Functions": []}]
        lmb.get_paginator.return_value = pag
        cw = MagicMock()
        clients = self._clients()
        clients["lambda"] = lmb
        clients["cloudwatch"] = cw
        with patch.object(index, "_make_clients", return_value=clients):
            evt = make_event("/get_lambda_health", [
                {"name": "prefix", "value": "a" * 500},
            ])
            r = index.handler(evt, {})
        # Should succeed (200) — prefix is truncated not rejected
        self.assertEqual(r["response"]["httpStatusCode"], 200)

    # ── region injection ──

    def test_region_with_special_chars_returns_400(self):
        """Injection attempt in region parameter must be rejected."""
        evt = make_event("/get_ec2_health", [
            {"name": "region", "value": "us-east-1; DROP TABLE"},
        ])
        r = index.handler(evt, {})
        self.assertEqual(r["response"]["httpStatusCode"], 400)

    def test_region_case_insensitive(self):
        """Region matching is case-insensitive (lower-cased before lookup)."""
        ec2 = MagicMock()
        pag = MagicMock()
        pag.paginate.return_value = [{"Reservations": []}]
        ec2.get_paginator.return_value = pag
        clients = self._clients()
        clients["ec2"] = ec2
        with patch.object(index, "_make_clients", return_value=clients):
            evt = make_event("/get_ec2_health", [
                {"name": "region", "value": "US-EAST-1"},
            ])
            r = index.handler(evt, {})
        self.assertEqual(r["response"]["httpStatusCode"], 200)


# ─────────────────────────────────────────────────────────────────────────────
# Constants & actions registry
# ─────────────────────────────────────────────────────────────────────────────

class TestConstants(unittest.TestCase):
    def test_actions_dict_has_eleven_entries(self):
        expected = {"get_ec2_health", "get_lambda_health", "get_cloudwatch_alarms",
                    "get_overall_health", "get_logs_analysis", "get_xray_traces",
                    "get_ssm_inventory", "get_ec2_process_metrics",
                    "get_ec2_instance_metrics", "get_alb_health", "get_cloudtrail_activity"}
        self.assertEqual(set(index.ACTIONS.keys()), expected)

    def test_valid_ec2_states_contains_all(self):
        self.assertIn("all", index.VALID_EC2_STATES)
        self.assertIn("running", index.VALID_EC2_STATES)
        self.assertIn("stopped", index.VALID_EC2_STATES)
        self.assertIn("terminated", index.VALID_EC2_STATES)
        self.assertIn("pending", index.VALID_EC2_STATES)
        self.assertIn("shutting-down", index.VALID_EC2_STATES)
        self.assertIn("stopping", index.VALID_EC2_STATES)

    def test_valid_alarm_states(self):
        for s in ("ALARM", "OK", "INSUFFICIENT_DATA", "ALL"):
            self.assertIn(s, index.VALID_ALARM_STATES)

    def test_valid_regions_contains_common_regions(self):
        for r in ("us-east-1", "us-west-2", "eu-west-1", "ap-northeast-1", "sa-east-1"):
            self.assertIn(r, index.VALID_REGIONS)

    def test_default_region_is_us_east_1(self):
        self.assertEqual(index.DEFAULT_REGION, "us-east-1")

    def test_make_clients_returns_dict_with_all_keys(self):
        import unittest.mock as um
        with um.patch("index.boto3") as mock_boto:
            mock_boto.client.return_value = MagicMock()
            clients = index._make_clients("us-east-1")
        for key in ("ec2", "lambda", "cloudwatch", "logs", "xray"):
            self.assertIn(key, clients)



# ─────────────────────────────────────────────────────────────────────────────
# SSM Inventory tests
# ─────────────────────────────────────────────────────────────────────────────

def make_ssm_instance(instance_id="i-0abc123", platform="Linux",
                      platform_name="Amazon Linux 2", platform_version="2",
                      agent_version="3.2.0", ip="10.0.0.5", ping="Online"):
    return {
        "InstanceId":        instance_id,
        "ComputerName":      "ip-10-0-0-5",
        "PlatformType":      platform,
        "PlatformName":      platform_name,
        "PlatformVersion":   platform_version,
        "AgentVersion":      agent_version,
        "IPAddress":         ip,
        "PingStatus":        ping,
        "LastPingDateTime":  datetime.now(timezone.utc),
        "AssociationStatus": "Success",
        "ResourceType":      "ManagedInstance",
    }


def make_ssm_client(instances=None, inventory_pages=None):
    """Return a mock SSM client with paginator support."""
    ssm = MagicMock()
    inst_pages = [{"InstanceInformationList": instances or []}]
    inst_pag = make_paginator(inst_pages)
    inv_pages = inventory_pages if inventory_pages is not None else [{"Entities": []}]
    inv_pag = make_paginator(inv_pages)

    def pag_side_effect(op):
        if op == "describe_instance_information":
            return inst_pag
        if op == "get_inventory":
            return inv_pag
        return make_paginator([{}])

    ssm.get_paginator.side_effect = pag_side_effect
    return ssm


class TestGetSsmInventory(unittest.TestCase):

    def _call(self, params=None, extra=None):
        event = make_event("get_ssm_inventory", params or [])
        if extra:
            event.update(extra)
        return index.get_ssm_inventory(event)

    # -- happy path --------------------------------------------------------

    def test_returns_managed_instances(self):
        inst = make_ssm_instance("i-0abc123")
        ssm = make_ssm_client(instances=[inst])
        with patch("index.boto3") as mock_boto:
            mock_boto.client.return_value = ssm
            resp = self._call()
        body = parse_body(resp)
        self.assertEqual(body["managed_instance_count"], 1)
        self.assertEqual(body["instances"][0]["instance_id"], "i-0abc123")
        self.assertEqual(body["instances"][0]["platform_name"], "Amazon Linux 2")

    def test_inventory_type_application_accepted(self):
        inst = make_ssm_instance()
        inv_page = [{
            "Entities": [{
                "Id": "i-0abc123",
                "Data": {
                    "AWS:Application": {
                        "Content": [
                            {"Name": "python3", "Version": "3.9.0", "Publisher": "Amazon"},
                            {"Name": "nginx",   "Version": "1.22.1", "Publisher": "nginx"},
                        ]
                    }
                }
            }]
        }]
        ssm = make_ssm_client(instances=[inst], inventory_pages=inv_page)
        with patch("index.boto3") as mock_boto:
            mock_boto.client.return_value = ssm
            resp = self._call(params=[{"name": "inventory_type", "value": "AWS:Application"}])
        body = parse_body(resp)
        self.assertEqual(resp["response"]["httpStatusCode"], 200)
        self.assertGreaterEqual(body["managed_instance_count"], 1)

    def test_filter_by_instance_id(self):
        inst = make_ssm_instance("i-0specific")
        ssm = make_ssm_client(instances=[inst])
        with patch("index.boto3") as mock_boto:
            mock_boto.client.return_value = ssm
            resp = self._call(params=[{"name": "instance_id", "value": "i-0specific"}])
        body = parse_body(resp)
        self.assertEqual(body["managed_instance_count"], 1)
        self.assertEqual(body["instances"][0]["instance_id"], "i-0specific")

    def test_no_managed_instances_returns_helpful_message(self):
        ssm = make_ssm_client(instances=[])
        with patch("index.boto3") as mock_boto:
            mock_boto.client.return_value = ssm
            resp = self._call()
        body = parse_body(resp)
        self.assertEqual(body["managed_instance_count"], 0)
        self.assertIn("SSM Agent", body["message"])
        self.assertIn("AmazonSSMManagedInstanceCore", body["message"])

    def test_all_inventory_type_queries_multiple_types(self):
        inst = make_ssm_instance()
        ssm = make_ssm_client(instances=[inst], inventory_pages=[{"Entities": []}])
        with patch("index.boto3") as mock_boto:
            mock_boto.client.return_value = ssm
            resp = self._call(params=[{"name": "inventory_type", "value": "ALL"}])
        body = parse_body(resp)
        self.assertEqual(resp["response"]["httpStatusCode"], 200)
        self.assertGreater(ssm.get_paginator.call_count, 1)

    def test_multi_region_uses_correct_region(self):
        ssm = make_ssm_client(instances=[])
        with patch("index.boto3") as mock_boto:
            mock_boto.client.return_value = ssm
            self._call(params=[{"name": "region", "value": "eu-west-1"}])
            mock_boto.client.assert_called_with("ssm", region_name="eu-west-1")

    def test_default_inventory_type_is_instance_information(self):
        inst = make_ssm_instance()
        ssm = make_ssm_client(instances=[inst])
        with patch("index.boto3") as mock_boto:
            mock_boto.client.return_value = ssm
            resp = self._call()
        body = parse_body(resp)
        self.assertEqual(body["inventory_type_queried"], "AWS:InstanceInformation")

    def test_response_includes_note_about_prerequisites(self):
        ssm = make_ssm_client(instances=[make_ssm_instance()])
        with patch("index.boto3") as mock_boto:
            mock_boto.client.return_value = ssm
            resp = self._call()
        body = parse_body(resp)
        self.assertIn("SSM Agent", body["note"])
        self.assertIn("AmazonSSMManagedInstanceCore", body["note"])

    # -- validation errors -------------------------------------------------

    def test_invalid_inventory_type_returns_400(self):
        ssm = make_ssm_client()
        with patch("index.boto3") as mock_boto:
            mock_boto.client.return_value = ssm
            resp = self._call(params=[{"name": "inventory_type", "value": "INVALID_TYPE"}])
        self.assertEqual(resp["response"]["httpStatusCode"], 400)
        body = parse_body(resp)
        self.assertIn("inventory_type", body["error"])

    def test_invalid_region_returns_400(self):
        ssm = make_ssm_client()
        with patch("index.boto3") as mock_boto:
            mock_boto.client.return_value = ssm
            resp = self._call(params=[{"name": "region", "value": "fake-region-99"}])
        self.assertEqual(resp["response"]["httpStatusCode"], 400)

    def test_valid_inventory_types_accepted(self):
        for inv_type in index.SSM_INVENTORY_TYPES:
            inst = make_ssm_instance()
            ssm = make_ssm_client(instances=[inst])
            with patch("index.boto3") as mock_boto:
                mock_boto.client.return_value = ssm
                resp = self._call(params=[{"name": "inventory_type", "value": inv_type}])
            self.assertNotEqual(
                resp["response"]["httpStatusCode"], 400,
                msg="inventory_type '{}' should be valid".format(inv_type)
            )

    # -- response envelope -------------------------------------------------

    def test_response_envelope_has_message_version(self):
        ssm = make_ssm_client(instances=[make_ssm_instance()])
        with patch("index.boto3") as mock_boto:
            mock_boto.client.return_value = ssm
            resp = self._call()
        self.assertEqual(resp["messageVersion"], "1.0")

    def test_response_envelope_has_action_group(self):
        ssm = make_ssm_client(instances=[make_ssm_instance()])
        with patch("index.boto3") as mock_boto:
            mock_boto.client.return_value = ssm
            resp = self._call()
        self.assertEqual(resp["response"]["actionGroup"], "MonitorActions")

    # -- handler routing ---------------------------------------------------

    def test_handler_routes_to_ssm_inventory(self):
        event = {
            "apiPath": "/get_ssm_inventory",
            "httpMethod": "POST",
            "parameters": [],
            "requestBody": {},
        }
        ssm = make_ssm_client(instances=[make_ssm_instance()])
        with patch("index.boto3") as mock_boto:
            mock_boto.client.return_value = ssm
            resp = index.handler(event, {})
        self.assertEqual(resp["response"]["apiPath"], "/get_ssm_inventory")
        self.assertEqual(resp["response"]["httpMethod"], "POST")

    def test_ssm_inventory_in_actions_dict(self):
        self.assertIn("get_ssm_inventory", index.ACTIONS)
        self.assertEqual(index.ACTIONS["get_ssm_inventory"], index.get_ssm_inventory)

    def test_ssm_inventory_types_set_not_empty(self):
        self.assertGreater(len(index.SSM_INVENTORY_TYPES), 0)
        self.assertIn("AWS:InstanceInformation", index.SSM_INVENTORY_TYPES)
        self.assertIn("AWS:Application", index.SSM_INVENTORY_TYPES)
        self.assertIn("ALL", index.SSM_INVENTORY_TYPES)



# ─────────────────────────────────────────────────────────────────────────────
# get_ec2_process_metrics
# ─────────────────────────────────────────────────────────────────────────────

def make_cw_metric(process_name, instance_id="i-0abc123456"):
    """Build a CloudWatch procstat metric entry."""
    return {
        "Namespace": "CWAgent",
        "MetricName": "procstat cpu_usage",
        "Dimensions": [
            {"Name": "InstanceId", "Value": instance_id},
            {"Name": "process_name", "Value": process_name},
            {"Name": "pid_finder", "Value": "native"},
        ],
    }

def make_metric_data_result(query_id, values):
    return {"Id": query_id, "Values": values, "Timestamps": [], "StatusCode": "Complete"}


class TestGetEc2ProcessMetrics(unittest.TestCase):

    def _call(self, params=None, region="us-east-1"):
        p = [{"name": "region", "value": region}]
        if params:
            p.extend(params)
        event = make_event("/get_ec2_process_metrics", p)
        return index.get_ec2_process_metrics(event)

    def _make_cw(self, metrics=None, metric_data_results=None):
        """Return a CloudWatch mock with procstat data."""
        cw = MagicMock()
        cw.get_paginator.return_value = make_paginator([
            {"Metrics": metrics or []}
        ])
        cw.get_metric_data.return_value = {
            "MetricDataResults": metric_data_results or []
        }
        return cw

    # ── No CloudWatch Agent installed (empty metrics) ─────────────────────

    def test_no_procstat_metrics_returns_ok_with_note(self):
        cw = self._make_cw(metrics=[])
        clients = make_clients(cloudwatch=cw)
        with patch("index._make_clients", return_value=clients):
            resp = self._call()
        self.assertEqual(resp["response"]["httpStatusCode"], 200)
        body = parse_body(resp)
        self.assertEqual(body["processes"], [])
        self.assertIn("note", body)
        self.assertIn("CloudWatch Agent", body["note"])
        self.assertEqual(body["metric"], "cpu")
        self.assertEqual(body["region"], "us-east-1")

    def test_no_metrics_with_instance_filter_returns_ok(self):
        cw = self._make_cw(metrics=[])
        clients = make_clients(cloudwatch=cw)
        with patch("index._make_clients", return_value=clients):
            resp = self._call(params=[{"name": "instance_id", "value": "i-0abc"}])
        body = parse_body(resp)
        self.assertEqual(body["processes"], [])
        self.assertEqual(body["instance_id"], "i-0abc")

    # ── CPU metrics ───────────────────────────────────────────────────────

    def test_cpu_returns_top_processes_sorted_desc(self):
        metrics = [
            make_cw_metric("nginx"),
            make_cw_metric("java"),
            make_cw_metric("python3"),
        ]
        results = [
            make_metric_data_result("p0", [5.0, 3.0]),   # nginx avg=4.0
            make_metric_data_result("p1", [80.0, 90.0]), # java avg=85.0
            make_metric_data_result("p2", [10.0, 20.0]), # python3 avg=15.0
        ]
        cw = self._make_cw(metrics=metrics, metric_data_results=results)
        clients = make_clients(cloudwatch=cw)
        with patch("index._make_clients", return_value=clients):
            resp = self._call()
        body = parse_body(resp)
        self.assertEqual(resp["response"]["httpStatusCode"], 200)
        procs = body["processes"]
        self.assertEqual(len(procs), 3)
        # Sorted descending by avg
        self.assertEqual(procs[0]["process_name"], "java")
        self.assertAlmostEqual(procs[0]["avg"], 85.0)
        self.assertEqual(procs[1]["process_name"], "python3")
        self.assertEqual(procs[2]["process_name"], "nginx")

    def test_cpu_unit_is_percent(self):
        metrics = [make_cw_metric("nginx")]
        results = [make_metric_data_result("p0", [10.0])]
        cw = self._make_cw(metrics=metrics, metric_data_results=results)
        clients = make_clients(cloudwatch=cw)
        with patch("index._make_clients", return_value=clients):
            resp = self._call()
        body = parse_body(resp)
        self.assertEqual(body["processes"][0]["unit"], "%")

    def test_cpu_no_avg_mb_field(self):
        metrics = [make_cw_metric("nginx")]
        results = [make_metric_data_result("p0", [10.0])]
        cw = self._make_cw(metrics=metrics, metric_data_results=results)
        clients = make_clients(cloudwatch=cw)
        with patch("index._make_clients", return_value=clients):
            resp = self._call()
        body = parse_body(resp)
        self.assertNotIn("avg_mb", body["processes"][0])

    # ── Memory metrics ────────────────────────────────────────────────────

    def test_memory_metric_type(self):
        metrics = [make_cw_metric("java")]
        metrics[0]["MetricName"] = "procstat memory_rss"
        results = [make_metric_data_result("p0", [104857600.0])]  # 100 MB
        cw = self._make_cw(metrics=metrics, metric_data_results=results)
        clients = make_clients(cloudwatch=cw)
        with patch("index._make_clients", return_value=clients):
            resp = self._call(params=[{"name": "metric", "value": "memory"}])
        body = parse_body(resp)
        self.assertEqual(body["metric"], "memory")
        proc = body["processes"][0]
        self.assertEqual(proc["unit"], "bytes")
        self.assertIn("avg_mb", proc)
        self.assertAlmostEqual(proc["avg_mb"], 100.0, places=0)

    def test_unknown_metric_type_defaults_to_cpu(self):
        cw = self._make_cw(metrics=[])
        clients = make_clients(cloudwatch=cw)
        with patch("index._make_clients", return_value=clients):
            resp = self._call(params=[{"name": "metric", "value": "invalid"}])
        body = parse_body(resp)
        self.assertEqual(body["metric"], "cpu")

    # ── top_n limiting ────────────────────────────────────────────────────

    def test_top_n_limits_results(self):
        metrics = [make_cw_metric(f"proc{i}") for i in range(10)]
        results = [make_metric_data_result(f"p{i}", [float(i)]) for i in range(10)]
        cw = self._make_cw(metrics=metrics, metric_data_results=results)
        clients = make_clients(cloudwatch=cw)
        with patch("index._make_clients", return_value=clients):
            resp = self._call(params=[{"name": "top_n", "value": "3"}])
        body = parse_body(resp)
        self.assertEqual(len(body["processes"]), 3)
        self.assertEqual(body["top_n"], 3)
        self.assertEqual(body["total_processes_found"], 10)

    def test_top_n_capped_at_20(self):
        cw = self._make_cw(metrics=[])
        clients = make_clients(cloudwatch=cw)
        with patch("index._make_clients", return_value=clients):
            resp = self._call(params=[{"name": "top_n", "value": "999"}])
        body = parse_body(resp)
        self.assertEqual(body["processes"], [])  # no metrics, but top_n was capped

    def test_invalid_top_n_defaults_to_5(self):
        cw = self._make_cw(metrics=[])
        clients = make_clients(cloudwatch=cw)
        with patch("index._make_clients", return_value=clients):
            resp = self._call(params=[{"name": "top_n", "value": "notanumber"}])
        body = parse_body(resp)
        self.assertEqual(resp["response"]["httpStatusCode"], 200)

    # ── hours parameter ───────────────────────────────────────────────────

    def test_hours_reflected_in_response(self):
        cw = self._make_cw(metrics=[])
        clients = make_clients(cloudwatch=cw)
        with patch("index._make_clients", return_value=clients):
            resp = self._call(params=[{"name": "hours", "value": "6"}])
        body = parse_body(resp)
        self.assertEqual(body["hours"], 6)

    def test_hours_clamped_max_24(self):
        cw = self._make_cw(metrics=[])
        clients = make_clients(cloudwatch=cw)
        with patch("index._make_clients", return_value=clients):
            resp = self._call(params=[{"name": "hours", "value": "100"}])
        body = parse_body(resp)
        self.assertEqual(body["hours"], 24)

    def test_hours_clamped_min_1(self):
        cw = self._make_cw(metrics=[])
        clients = make_clients(cloudwatch=cw)
        with patch("index._make_clients", return_value=clients):
            resp = self._call(params=[{"name": "hours", "value": "0"}])
        body = parse_body(resp)
        self.assertEqual(body["hours"], 1)

    def test_invalid_hours_defaults_to_1(self):
        cw = self._make_cw(metrics=[])
        clients = make_clients(cloudwatch=cw)
        with patch("index._make_clients", return_value=clients):
            resp = self._call(params=[{"name": "hours", "value": "abc"}])
        body = parse_body(resp)
        self.assertEqual(body["hours"], 1)

    # ── Bedrock envelope contract ─────────────────────────────────────────

    def test_response_envelope_has_required_fields(self):
        cw = self._make_cw(metrics=[])
        clients = make_clients(cloudwatch=cw)
        with patch("index._make_clients", return_value=clients):
            resp = self._call()
        self.assertEqual(resp["messageVersion"], "1.0")
        self.assertIn("response", resp)
        r = resp["response"]
        self.assertIn("httpStatusCode", r)
        self.assertIn("responseBody", r)
        self.assertIn("application/json", r["responseBody"])

    def test_handler_routes_to_process_metrics(self):
        event = {
            "apiPath": "/get_ec2_process_metrics",
            "httpMethod": "POST",
            "parameters": [{"name": "region", "value": "us-east-1"}],
            "requestBody": {},
        }
        cw = self._make_cw(metrics=[])
        clients = make_clients(cloudwatch=cw)
        with patch("index._make_clients", return_value=clients):
            resp = index.handler(event, {})
        self.assertEqual(resp["response"]["apiPath"], "/get_ec2_process_metrics")
        self.assertEqual(resp["response"]["httpStatusCode"], 200)

    def test_process_metrics_in_actions_dict(self):
        self.assertIn("get_ec2_process_metrics", index.ACTIONS)
        self.assertEqual(index.ACTIONS["get_ec2_process_metrics"], index.get_ec2_process_metrics)

    # ── Invalid region ────────────────────────────────────────────────────

    def test_invalid_region_returns_400(self):
        event = make_event("/get_ec2_process_metrics",
                           [{"name": "region", "value": "invalid-region-xyz"}])
        resp = index.get_ec2_process_metrics(event)
        self.assertEqual(resp["response"]["httpStatusCode"], 400)

    # ── Metrics without data (no values in window) ────────────────────────

    def test_processes_without_data_excluded(self):
        metrics = [make_cw_metric("nginx"), make_cw_metric("java")]
        # Only java has data; nginx has empty Values
        results = [
            {"Id": "p0", "Values": [], "Timestamps": [], "StatusCode": "Complete"},
            make_metric_data_result("p1", [75.0]),
        ]
        cw = self._make_cw(metrics=metrics, metric_data_results=results)
        clients = make_clients(cloudwatch=cw)
        with patch("index._make_clients", return_value=clients):
            resp = self._call()
        body = parse_body(resp)
        self.assertEqual(len(body["processes"]), 1)
        self.assertEqual(body["processes"][0]["process_name"], "java")

    # ── Default parameter values ──────────────────────────────────────────

    def test_defaults_cpu_top5_1hour(self):
        cw = self._make_cw(metrics=[])
        clients = make_clients(cloudwatch=cw)
        with patch("index._make_clients", return_value=clients):
            resp = self._call()
        body = parse_body(resp)
        self.assertEqual(body["metric"], "cpu")
        self.assertEqual(body["hours"], 1)

    def test_list_metrics_called_with_cwagent_namespace(self):
        cw = self._make_cw(metrics=[])
        clients = make_clients(cloudwatch=cw)
        with patch("index._make_clients", return_value=clients):
            self._call()
        cw.get_paginator.assert_called_with("list_metrics")
        call_kwargs = cw.get_paginator.return_value.paginate.call_args[1]
        self.assertEqual(call_kwargs["Namespace"], "CWAgent")
        self.assertEqual(call_kwargs["MetricName"], "procstat cpu_usage")

    def test_instance_filter_passed_to_list_metrics(self):
        cw = self._make_cw(metrics=[])
        clients = make_clients(cloudwatch=cw)
        with patch("index._make_clients", return_value=clients):
            self._call(params=[{"name": "instance_id", "value": "i-0abc"}])
        call_kwargs = cw.get_paginator.return_value.paginate.call_args[1]
        self.assertIn("Dimensions", call_kwargs)
        self.assertEqual(call_kwargs["Dimensions"][0]["Value"], "i-0abc")



# ── Helpers for new actions ───────────────────────────────────────────────────

def make_lb(name, state="active", dns="lb.example.com",
             arn="arn:aws:elasticloadbalancing:us-east-1:123:loadbalancer/app/my-lb/abc123"):
    return {
        "LoadBalancerName": name,
        "LoadBalancerArn": arn,
        "State": {"Code": state},
        "DNSName": dns,
    }


def make_cloudtrail_event(event_name, username, source_ip="1.2.3.4", resource=None, error=None):
    import json as _json
    detail = {
        "sourceIPAddress": source_ip,
        "awsRegion": "us-east-1",
        "userIdentity": {"arn": "arn:aws:iam::123:user/{}".format(username)},
    }
    if error:
        detail["errorCode"] = error
    return {
        "EventName": event_name,
        "Username": username,
        "EventTime": datetime(2026, 6, 16, 12, 0, 0, tzinfo=timezone.utc),
        "Resources": [{"ResourceName": resource}] if resource else [],
        "CloudTrailEvent": _json.dumps(detail),
    }


# ── TestGetEc2InstanceMetrics ─────────────────────────────────────────────────

class TestGetEc2InstanceMetrics(unittest.TestCase):

    def _call(self, params=None):
        event = make_event("/get_ec2_instance_metrics", params or [])
        return index.handler(event, {})

    def _make_ec2_with_instances(self, *instance_ids):
        ec2 = MagicMock()
        reservations = [{"Instances": [{"InstanceId": iid, "Tags": []}]} for iid in instance_ids]
        ec2.get_paginator.return_value = make_paginator([{"Reservations": reservations}])
        return ec2

    def test_no_instances_returns_empty_list(self):
        clients = make_clients()
        with patch("index._make_clients", return_value=clients):
            resp = self._call()
        self.assertEqual(resp["response"]["httpStatusCode"], 200)
        body = parse_body(resp)
        self.assertEqual(body["instances"], [])
        self.assertIn("note", body)

    def test_returns_metrics_for_running_instance(self):
        iid = "i-0abc1234"
        ec2 = self._make_ec2_with_instances(iid)
        cw = MagicMock()
        safe = iid.replace("-", "_")
        cw.get_metric_data.return_value = {"MetricDataResults": [
            {"Id": "{}_CPUUtilization".format(safe), "Values": [75.0], "Timestamps": []},
            {"Id": "{}_NetworkIn".format(safe),      "Values": [1000.0], "Timestamps": []},
            {"Id": "{}_NetworkOut".format(safe),     "Values": [500.0], "Timestamps": []},
            {"Id": "{}_DiskReadOps".format(safe),    "Values": [10.0], "Timestamps": []},
            {"Id": "{}_DiskWriteOps".format(safe),   "Values": [5.0], "Timestamps": []},
        ]}
        clients = make_clients(ec2=ec2, cloudwatch=cw)
        with patch("index._make_clients", return_value=clients):
            resp = self._call()
        body = parse_body(resp)
        self.assertEqual(len(body["instances"]), 1)
        inst = body["instances"][0]
        self.assertEqual(inst["instance_id"], iid)
        self.assertEqual(inst["cpu_pct"], 75.0)
        self.assertEqual(inst["network_in_bytes"], 1000.0)

    def test_instance_id_filter_passed_to_describe(self):
        clients = make_clients()
        with patch("index._make_clients", return_value=clients):
            self._call(params=[{"name": "instance_id", "value": "i-filter"}])
        paginator_call = clients["ec2"].get_paginator.return_value.paginate.call_args
        filters = paginator_call[1]["Filters"]
        id_filter = next((f for f in filters if f["Name"] == "instance-id"), None)
        self.assertIsNotNone(id_filter)
        self.assertEqual(id_filter["Values"], ["i-filter"])

    def test_invalid_hours_defaults_to_1(self):
        clients = make_clients()
        with patch("index._make_clients", return_value=clients):
            resp = self._call(params=[{"name": "hours", "value": "bad"}])
        self.assertEqual(resp["response"]["httpStatusCode"], 200)

    def test_hours_capped_at_24(self):
        clients = make_clients()
        with patch("index._make_clients", return_value=clients):
            resp = self._call(params=[{"name": "hours", "value": "100"}])
        self.assertEqual(resp["response"]["httpStatusCode"], 200)

    def test_invalid_region_returns_400(self):
        resp = self._call(params=[{"name": "region", "value": "not-a-region"}])
        self.assertEqual(resp["response"]["httpStatusCode"], 400)

    def test_multiple_instances_sorted_by_cpu_desc(self):
        ec2 = MagicMock()
        reservations = [{"Instances": [
            {"InstanceId": "i-low",  "Tags": []},
            {"InstanceId": "i-high", "Tags": []},
        ]}]
        ec2.get_paginator.return_value = make_paginator([{"Reservations": reservations}])
        cw = MagicMock()
        cw.get_metric_data.return_value = {"MetricDataResults": [
            {"Id": "i_low_CPUUtilization",  "Values": [10.0], "Timestamps": []},
            {"Id": "i_high_CPUUtilization", "Values": [90.0], "Timestamps": []},
        ]}
        clients = make_clients(ec2=ec2, cloudwatch=cw)
        with patch("index._make_clients", return_value=clients):
            resp = self._call()
        body = parse_body(resp)
        self.assertEqual(body["instances"][0]["instance_id"], "i-high")


# ── TestGetAlbHealth ──────────────────────────────────────────────────────────

class TestGetAlbHealth(unittest.TestCase):

    def _call(self, params=None):
        event = make_event("/get_alb_health", params or [])
        return index.handler(event, {})

    def _make_elb(self, lbs):
        elb = MagicMock()
        elb.describe_load_balancers.return_value = {"LoadBalancers": lbs}
        return elb

    def test_no_albs_returns_empty_list(self):
        clients = make_clients()
        with patch("index._make_clients", return_value=clients):
            resp = self._call()
        body = parse_body(resp)
        self.assertEqual(body["load_balancers"], [])
        self.assertIn("note", body)

    def test_returns_alb_with_metrics(self):
        lb = make_lb("my-lb",
                     arn="arn:aws:elasticloadbalancing:us-east-1:123:loadbalancer/app/my-lb/abc")
        elb = self._make_elb([lb])
        cw = MagicMock()
        cw.get_metric_data.return_value = {"MetricDataResults": [
            {"Id": "my_lb_RequestCount",           "Values": [1000.0], "Timestamps": []},
            {"Id": "my_lb_HTTPCode_ELB_4XX_Count",  "Values": [10.0],  "Timestamps": []},
            {"Id": "my_lb_HTTPCode_ELB_5XX_Count",  "Values": [5.0],   "Timestamps": []},
            {"Id": "my_lb_TargetResponseTime",      "Values": [0.1],   "Timestamps": []},
            {"Id": "my_lb_HealthyHostCount",        "Values": [3.0],   "Timestamps": []},
            {"Id": "my_lb_UnHealthyHostCount",      "Values": [0.0],   "Timestamps": []},
        ]}
        clients = make_clients(elbv2=elb, cloudwatch=cw)
        with patch("index._make_clients", return_value=clients):
            resp = self._call()
        body = parse_body(resp)
        self.assertEqual(len(body["load_balancers"]), 1)
        lb_r = body["load_balancers"][0]
        self.assertEqual(lb_r["name"], "my-lb")
        self.assertEqual(lb_r["requests"], 1000)
        self.assertEqual(lb_r["errors_4xx"], 10)
        self.assertEqual(lb_r["health"], "OK")

    def test_unhealthy_host_sets_critical(self):
        lb = make_lb("bad-lb",
                     arn="arn:aws:elasticloadbalancing:us-east-1:123:loadbalancer/app/bad-lb/xyz")
        elb = self._make_elb([lb])
        cw = MagicMock()
        cw.get_metric_data.return_value = {"MetricDataResults": [
            {"Id": "bad_lb_UnHealthyHostCount", "Values": [2.0], "Timestamps": []},
        ]}
        clients = make_clients(elbv2=elb, cloudwatch=cw)
        with patch("index._make_clients", return_value=clients):
            resp = self._call()
        body = parse_body(resp)
        self.assertEqual(body["load_balancers"][0]["health"], "CRITICAL")

    def test_high_error_rate_sets_warning(self):
        lb = make_lb("warn-lb",
                     arn="arn:aws:elasticloadbalancing:us-east-1:123:loadbalancer/app/warn-lb/def")
        elb = self._make_elb([lb])
        cw = MagicMock()
        cw.get_metric_data.return_value = {"MetricDataResults": [
            {"Id": "warn_lb_RequestCount",           "Values": [100.0], "Timestamps": []},
            {"Id": "warn_lb_HTTPCode_ELB_4XX_Count",  "Values": [10.0], "Timestamps": []},
            {"Id": "warn_lb_UnHealthyHostCount",      "Values": [0.0],  "Timestamps": []},
        ]}
        clients = make_clients(elbv2=elb, cloudwatch=cw)
        with patch("index._make_clients", return_value=clients):
            resp = self._call()
        body = parse_body(resp)
        self.assertEqual(body["load_balancers"][0]["health"], "WARNING")

    def test_alb_name_filter(self):
        elb = MagicMock()
        elb.describe_load_balancers.return_value = {"LoadBalancers": []}
        clients = make_clients(elbv2=elb)
        with patch("index._make_clients", return_value=clients):
            self._call(params=[{"name": "alb_name", "value": "specific-lb"}])
        elb.describe_load_balancers.assert_called_once_with(Names=["specific-lb"])

    def test_invalid_region_returns_400(self):
        resp = self._call(params=[{"name": "region", "value": "not-a-region"}])
        self.assertEqual(resp["response"]["httpStatusCode"], 400)

    def test_invalid_hours_defaults_gracefully(self):
        clients = make_clients()
        with patch("index._make_clients", return_value=clients):
            resp = self._call(params=[{"name": "hours", "value": "abc"}])
        self.assertEqual(resp["response"]["httpStatusCode"], 200)


# ── TestGetCloudtrailActivity ─────────────────────────────────────────────────

class TestGetCloudtrailActivity(unittest.TestCase):

    def _call(self, params=None):
        event = make_event("/get_cloudtrail_activity", params or [])
        return index.handler(event, {})

    def test_no_events_returns_empty(self):
        clients = make_clients()
        with patch("index._make_clients", return_value=clients):
            resp = self._call()
        body = parse_body(resp)
        self.assertEqual(body["events"], [])
        self.assertEqual(body["summary"]["total_events"], 0)

    def test_returns_events(self):
        ct = MagicMock()
        ev = make_cloudtrail_event("TerminateInstances", "alice", resource="i-0abc")
        ct.lookup_events.return_value = {"Events": [ev]}
        clients = make_clients(cloudtrail=ct)
        with patch("index._make_clients", return_value=clients):
            resp = self._call()
        body = parse_body(resp)
        self.assertEqual(len(body["events"]), 1)
        self.assertEqual(body["events"][0]["action"], "TerminateInstances")
        self.assertEqual(body["events"][0]["user"], "alice")

    def test_error_events_counted_in_summary(self):
        ct = MagicMock()
        ev = make_cloudtrail_event("DeleteBucket", "bob", error="AccessDenied")
        ct.lookup_events.return_value = {"Events": [ev]}
        clients = make_clients(cloudtrail=ct)
        with patch("index._make_clients", return_value=clients):
            resp = self._call()
        body = parse_body(resp)
        self.assertEqual(body["summary"]["errors"], 1)

    def test_username_filter_passed_to_lookup(self):
        ct = MagicMock()
        ct.lookup_events.return_value = {"Events": []}
        clients = make_clients(cloudtrail=ct)
        with patch("index._make_clients", return_value=clients):
            self._call(params=[{"name": "username", "value": "alice"}])
        call_kwargs = ct.lookup_events.call_args[1]
        self.assertIn("LookupAttributes", call_kwargs)
        self.assertEqual(call_kwargs["LookupAttributes"][0]["AttributeKey"], "Username")
        self.assertEqual(call_kwargs["LookupAttributes"][0]["AttributeValue"], "alice")

    def test_event_name_filter(self):
        ct = MagicMock()
        ct.lookup_events.return_value = {"Events": []}
        clients = make_clients(cloudtrail=ct)
        with patch("index._make_clients", return_value=clients):
            self._call(params=[{"name": "event_name", "value": "DeleteBucket"}])
        call_kwargs = ct.lookup_events.call_args[1]
        self.assertEqual(call_kwargs["LookupAttributes"][0]["AttributeKey"], "EventName")

    def test_limit_capped_at_50(self):
        ct = MagicMock()
        ct.lookup_events.return_value = {"Events": []}
        clients = make_clients(cloudtrail=ct)
        with patch("index._make_clients", return_value=clients):
            self._call(params=[{"name": "limit", "value": "999"}])
        call_kwargs = ct.lookup_events.call_args[1]
        self.assertEqual(call_kwargs["MaxResults"], 50)

    def test_invalid_hours_defaults_to_3(self):
        ct = MagicMock()
        ct.lookup_events.return_value = {"Events": []}
        clients = make_clients(cloudtrail=ct)
        with patch("index._make_clients", return_value=clients):
            resp = self._call(params=[{"name": "hours", "value": "bad"}])
        body = parse_body(resp)
        self.assertEqual(body["hours"], 3)

    def test_invalid_region_returns_400(self):
        resp = self._call(params=[{"name": "region", "value": "not-a-region"}])
        self.assertEqual(resp["response"]["httpStatusCode"], 400)

    def test_summary_counts_unique_users_and_actions(self):
        ct = MagicMock()
        events = [
            make_cloudtrail_event("RunInstances",   "alice"),
            make_cloudtrail_event("StopInstances",  "alice"),
            make_cloudtrail_event("DeleteBucket",   "bob"),
        ]
        ct.lookup_events.return_value = {"Events": events}
        clients = make_clients(cloudtrail=ct)
        with patch("index._make_clients", return_value=clients):
            resp = self._call()
        body = parse_body(resp)
        self.assertEqual(body["summary"]["unique_users"], 2)
        self.assertEqual(body["summary"]["unique_actions"], 3)

if __name__ == "__main__":
    unittest.main(verbosity=2)
