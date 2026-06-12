"""
AWS Monitor Agent - Bedrock Action Group Handler
Handles: EC2 health, Lambda health, CloudWatch alarms,
         CloudWatch Logs Insights queries, X-Ray trace analysis
Supports: multi-region queries via optional 'region' parameter
"""
import json
import os
import time
import boto3
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DEFAULT_REGION = os.environ.get("REGION", "us-east-1")

VALID_EC2_STATES = {"pending", "running", "shutting-down", "terminated", "stopping", "stopped", "all"}
VALID_ALARM_STATES = {"ALARM", "OK", "INSUFFICIENT_DATA", "ALL"}
MAX_LAMBDA_HOURS = 168
MIN_LAMBDA_HOURS = 1
MAX_LOGS_HOURS = 24
MIN_LOGS_HOURS = 1
MAX_XRAY_HOURS = 6
MIN_XRAY_HOURS = 1
LOGS_QUERY_TIMEOUT_S = 25
DEFAULT_LOGS_QUERY = "fields @timestamp, @message | filter @message like /ERROR|Exception|WARN|error/ | sort @timestamp desc | limit 20"

# Input validation limits (security)
MAX_PREFIX_LEN = 128
MAX_LOG_GROUP_LEN = 512
MAX_QUERY_LEN = 2048
MAX_FILTER_EXPRESSION_LEN = 2048
LOG_GROUP_PATTERN = __import__('re').compile(r'^[a-zA-Z0-9_./#-]{1,512}$')

# All standard AWS regions (opt-in regions included; access depends on account settings)
VALID_REGIONS = {
    "us-east-1", "us-east-2", "us-west-1", "us-west-2",
    "eu-west-1", "eu-west-2", "eu-west-3", "eu-central-1", "eu-central-2",
    "eu-north-1", "eu-south-1", "eu-south-2",
    "ap-northeast-1", "ap-northeast-2", "ap-northeast-3",
    "ap-southeast-1", "ap-southeast-2", "ap-southeast-3", "ap-southeast-4",
    "ap-south-1", "ap-south-2",
    "ap-east-1",
    "sa-east-1",
    "ca-central-1", "ca-west-1",
    "me-south-1", "me-central-1",
    "af-south-1",
    "il-central-1",
}


def _make_clients(region):
    """Create boto3 clients for the given region. Called per-request to support multi-region."""
    return {
        "ec2": boto3.client("ec2", region_name=region),
        "lambda": boto3.client("lambda", region_name=region),
        "cloudwatch": boto3.client("cloudwatch", region_name=region),
        "logs": boto3.client("logs", region_name=region),
        "xray": boto3.client("xray", region_name=region),
    }


def ok(body, api_path="", http_method="POST"):
    return {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": "MonitorActions",
            "apiPath": api_path,
            "httpMethod": http_method,
            "httpStatusCode": 200,
            "responseBody": {
                "application/json": {
                    "body": json.dumps(body)
                }
            }
        }
    }


def err(message, code=500, api_path="", http_method="POST"):
    return {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": "MonitorActions",
            "apiPath": api_path,
            "httpMethod": http_method,
            "httpStatusCode": code,
            "responseBody": {
                "application/json": {
                    "body": json.dumps({"error": message})
                }
            }
        }
    }


def get_param(event, name):
    for p in event.get("parameters", []):
        if p.get("name") == name:
            return p.get("value")
    for p in (
        event.get("requestBody", {})
             .get("content", {})
             .get("application/json", {})
             .get("properties", [])
    ):
        if p.get("name") == name:
            return p.get("value")
    return None


def _resolve_region(event):
    """Extract and validate the region parameter. Returns (region_str, error_response_or_None)."""
    region = (get_param(event, "region") or DEFAULT_REGION).strip().lower()
    if region not in VALID_REGIONS:
        return None, err(
            "Invalid region '{}'. Examples of valid regions: us-east-1, us-west-2, "
            "eu-west-1, ap-northeast-1, sa-east-1. Default is {}.".format(region, DEFAULT_REGION),
            code=400,
        )
    return region, None


def _handle_region_error(e, region, api_path=""):
    """Convert boto3 ClientError from a region call into a user-friendly error response."""
    error_msg = str(e)
    code = getattr(getattr(e, "response", {}).get("Error", {}), "get", lambda k, d=None: d)("Code", "")
    if not isinstance(code, str):
        code = e.response.get("Error", {}).get("Code", "") if hasattr(e, "response") else ""
    if code in ("AuthFailure", "InvalidClientTokenId", "OptInRequired"):
        return err(
            "Region '{}' is not enabled for this AWS account. "
            "To enable it go to AWS Console -> Account -> Regions.".format(region),
            code=403, api_path=api_path,
        )
    if code == "UnauthorizedOperation":
        return err(
            "Access denied in region '{}'. Check IAM permissions.".format(region),
            code=403, api_path=api_path,
        )
    return err("AWS error in region '{}': {}".format(region, error_msg), api_path=api_path)


def get_ec2_health(event):
    region, region_err = _resolve_region(event)
    if region_err:
        return region_err
    state_filter = (get_param(event, "state") or "all").lower()
    if state_filter not in VALID_EC2_STATES:
        return err(
            "Invalid state '{}'. Valid values: {}".format(
                state_filter, ", ".join(sorted(VALID_EC2_STATES))
            ),
            code=400,
        )
    clients = _make_clients(region)
    filters = []
    if state_filter != "all":
        filters.append({"Name": "instance-state-name", "Values": [state_filter]})
    try:
        paginator = clients["ec2"].get_paginator("describe_instances")
        instances = []
        for page in paginator.paginate(Filters=filters):
            for reservation in page["Reservations"]:
                for inst in reservation["Instances"]:
                    name = next(
                        (t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Name"),
                        inst["InstanceId"],
                    )
                    instances.append({
                        "id": inst["InstanceId"],
                        "name": name,
                        "type": inst["InstanceType"],
                        "state": inst["State"]["Name"],
                        "az": inst["Placement"]["AvailabilityZone"],
                        "private_ip": inst.get("PrivateIpAddress", "N/A"),
                        "public_ip": inst.get("PublicIpAddress", "N/A"),
                        "launch_time": inst["LaunchTime"].isoformat() if "LaunchTime" in inst else "N/A",
                        "platform": inst.get("Platform", "linux"),
                    })
    except Exception as e:
        return _handle_region_error(e, region)
    summary = {
        "total": len(instances),
        "running": sum(1 for i in instances if i["state"] == "running"),
        "stopped": sum(1 for i in instances if i["state"] == "stopped"),
        "other": sum(1 for i in instances if i["state"] not in ("running", "stopped")),
    }
    return ok({"region": region, "summary": summary, "instances": instances})


def get_lambda_health(event):
    region, region_err = _resolve_region(event)
    if region_err:
        return region_err
    prefix = (get_param(event, "prefix") or "")[:MAX_PREFIX_LEN]
    raw_hours = get_param(event, "hours") or "24"
    try:
        hours = int(raw_hours)
    except ValueError:
        return err("Parameter 'hours' must be an integer, got: '{}'".format(raw_hours), code=400)
    hours = max(MIN_LAMBDA_HOURS, min(MAX_LAMBDA_HOURS, hours))
    clients = _make_clients(region)
    try:
        paginator = clients["lambda"].get_paginator("list_functions")
        functions = []
        for page in paginator.paginate():
            for fn in page["Functions"]:
                if prefix and not fn["FunctionName"].startswith(prefix):
                    continue
                functions.append(fn)
    except Exception as e:
        return _handle_region_error(e, region)
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(hours=hours)
    result = []
    for fn in functions[:50]:
        name = fn["FunctionName"]
        metrics = _get_lambda_metrics(name, start_time, end_time, clients["cloudwatch"])
        result.append({
            "name": name,
            "runtime": fn.get("Runtime", "N/A"),
            "memory_mb": fn.get("MemorySize", 128),
            "timeout_s": fn.get("Timeout", 3),
            "last_modified": fn.get("LastModified", "N/A"),
            "state": fn.get("State", "Active"),
            "metrics": metrics,
        })
    return ok({
        "region": region,
        "period_hours": hours,
        "total_functions": len(result),
        "functions": result,
    })


def _get_lambda_metrics(fn_name, start, end, cw_client):
    """Fetch Lambda CloudWatch metrics. Accepts explicit cloudwatch client for multi-region support."""
    raw_seconds = int((end - start).total_seconds())
    period = max(60, raw_seconds - (raw_seconds % 60))

    def stat(metric_name, stat_type):
        try:
            resp = cw_client.get_metric_statistics(
                Namespace="AWS/Lambda",
                MetricName=metric_name,
                Dimensions=[{"Name": "FunctionName", "Value": fn_name}],
                StartTime=start,
                EndTime=end,
                Period=period,
                Statistics=[stat_type],
            )
            dp = resp.get("Datapoints", [])
            return round(dp[0].get(stat_type, 0), 2) if dp else 0
        except Exception:
            return 0

    invocations = stat("Invocations", "Sum")
    errors = stat("Errors", "Sum")
    avg_duration = stat("Duration", "Average")
    throttles = stat("Throttles", "Sum")
    error_rate = round(errors / invocations * 100, 1) if invocations > 0 else 0
    return {
        "invocations": invocations,
        "errors": errors,
        "error_rate_pct": error_rate,
        "avg_duration_ms": avg_duration,
        "throttles": throttles,
        "health": "critical" if error_rate >= 10 else "warning" if error_rate > 5 else "healthy",
    }


def get_cloudwatch_alarms(event):
    region, region_err = _resolve_region(event)
    if region_err:
        return region_err
    state = (get_param(event, "state") or "ALARM").upper()
    if state not in VALID_ALARM_STATES:
        return err(
            "Invalid state '{}'. Valid values: {}".format(
                state, ", ".join(sorted(VALID_ALARM_STATES))
            ),
            code=400,
        )
    clients = _make_clients(region)
    kwargs = {}
    if state != "ALL":
        kwargs["StateValue"] = state
    try:
        paginator = clients["cloudwatch"].get_paginator("describe_alarms")
        alarms = []
        for page in paginator.paginate(**kwargs):
            for alarm in page.get("MetricAlarms", []):
                alarms.append({
                    "name": alarm["AlarmName"],
                    "description": alarm.get("AlarmDescription", ""),
                    "state": alarm["StateValue"],
                    "state_reason": alarm.get("StateReason", ""),
                    "metric": alarm.get("MetricName", "N/A"),
                    "namespace": alarm.get("Namespace", "N/A"),
                    "threshold": alarm.get("Threshold"),
                    "comparison": alarm.get("ComparisonOperator", ""),
                    "updated_at": (
                        alarm["StateUpdatedTimestamp"].isoformat()
                        if "StateUpdatedTimestamp" in alarm
                        else "N/A"
                    ),
                })
            for alarm in page.get("CompositeAlarms", []):
                alarms.append({
                    "name": alarm["AlarmName"],
                    "description": alarm.get("AlarmDescription", ""),
                    "state": alarm["StateValue"],
                    "state_reason": alarm.get("StateReason", ""),
                    "type": "composite",
                })
    except Exception as e:
        return _handle_region_error(e, region)
    summary = {
        "total": len(alarms),
        "in_alarm": sum(1 for a in alarms if a["state"] == "ALARM"),
        "ok": sum(1 for a in alarms if a["state"] == "OK"),
        "insufficient_data": sum(1 for a in alarms if a["state"] == "INSUFFICIENT_DATA"),
    }
    return ok({"region": region, "summary": summary, "alarms": alarms})


def get_overall_health(event):
    region, region_err = _resolve_region(event)
    if region_err:
        return region_err
    clients = _make_clients(region)
    try:
        ec2_paginator = clients["ec2"].get_paginator("describe_instances")
        ec2_states = {}
        for page in ec2_paginator.paginate():
            for r in page["Reservations"]:
                for i in r["Instances"]:
                    s = i["State"]["Name"]
                    ec2_states[s] = ec2_states.get(s, 0) + 1
        alarm_paginator = clients["cloudwatch"].get_paginator("describe_alarms")
        alarm_count = 0
        for page in alarm_paginator.paginate(StateValue="ALARM"):
            alarm_count += (
                len(page.get("MetricAlarms", [])) +
                len(page.get("CompositeAlarms", []))
            )
        lambda_paginator = clients["lambda"].get_paginator("list_functions")
        lambda_count = 0
        for page in lambda_paginator.paginate():
            lambda_count += len(page.get("Functions", []))
    except Exception as e:
        return _handle_region_error(e, region)
    health_status = "healthy"
    if alarm_count > 0:
        health_status = "degraded" if alarm_count < 5 else "critical"
    return ok({
        "region": region,
        "overall_status": health_status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ec2": ec2_states,
        "active_alarms": alarm_count,
        "lambda_functions": lambda_count,
        "recommendation": (
            "{} alarms are firing - investigate CloudWatch alarms.".format(alarm_count)
            if alarm_count > 0
            else "All monitored resources appear healthy."
        ),
    })


def get_logs_analysis(event):
    region, region_err = _resolve_region(event)
    if region_err:
        return region_err
    log_group = get_param(event, "log_group")
    if not log_group:
        return err("Parameter 'log_group' is required (e.g. /aws/lambda/my-function)", code=400)
    log_group = log_group.strip()
    if len(log_group) > MAX_LOG_GROUP_LEN:
        return err("Parameter 'log_group' exceeds maximum length of {}.".format(MAX_LOG_GROUP_LEN), code=400)
    if not __import__('re').match(r'^[a-zA-Z0-9_./#-][a-zA-Z0-9_./#\-]*$', log_group):
        return err("Parameter 'log_group' contains invalid characters. Use only: letters, digits, /, _, ., #, -", code=400)
    raw_hours = get_param(event, "hours") or "1"
    try:
        hours = int(raw_hours)
    except ValueError:
        return err("Parameter 'hours' must be an integer, got: '{}'".format(raw_hours), code=400)
    hours = max(MIN_LOGS_HOURS, min(MAX_LOGS_HOURS, hours))
    raw_query = get_param(event, "query")
    if raw_query and len(raw_query) > MAX_QUERY_LEN:
        return err("Parameter 'query' exceeds maximum length of {}.".format(MAX_QUERY_LEN), code=400)
    query_string = raw_query or DEFAULT_LOGS_QUERY
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(hours=hours)
    clients = _make_clients(region)
    try:
        start_resp = clients["logs"].start_query(
            logGroupName=log_group,
            startTime=int(start_time.timestamp()),
            endTime=int(end_time.timestamp()),
            queryString=query_string,
            limit=50,
        )
    except Exception as e:
        error_msg = str(e)
        if "ResourceNotFoundException" in error_msg or "does not exist" in error_msg.lower():
            return err("Log group '{}' not found in region '{}'.".format(log_group, region), code=404)
        return _handle_region_error(e, region)
    query_id = start_resp["queryId"]
    deadline = time.time() + LOGS_QUERY_TIMEOUT_S
    status = "Running"
    results = []
    stats = {}
    while time.time() < deadline:
        try:
            resp = clients["logs"].get_query_results(queryId=query_id)
        except Exception as e:
            return err("Failed to get query results: {}".format(str(e)))
        status = resp.get("status", "Running")
        if status in ("Complete", "Failed", "Cancelled", "Timeout"):
            results = resp.get("results", [])
            stats = resp.get("statistics", {})
            break
        time.sleep(1)
    if status != "Complete":
        try:
            clients["logs"].stop_query(queryId=query_id)
        except Exception:
            pass
        return err(
            "Query did not complete in time (status: {}). Try a smaller time range.".format(status),
            code=504,
        )
    events = []
    for row in results:
        raw = {field["field"]: field["value"] for field in row}
        events.append({
            "timestamp": raw.get("@timestamp", ""),
            "message": raw.get("@message", ""),
        })
    messages = [e.get("message", "") for e in events]
    error_count = sum(1 for m in messages if any(kw in m for kw in ("ERROR", "Error", "error", "Exception", "WARN")))
    return ok({
        "region": region,
        "log_group": log_group,
        "period_hours": hours,
        "query": query_string,
        "status": status,
        "total_events": len(events),
        "error_events": error_count,
        "statistics": stats,
        "events": events,
    })


def get_xray_traces(event):
    region, region_err = _resolve_region(event)
    if region_err:
        return region_err
    raw_hours = get_param(event, "hours") or "1"
    try:
        hours = int(raw_hours)
    except ValueError:
        return err("Parameter 'hours' must be an integer, got: '{}'".format(raw_hours), code=400)
    hours = max(MIN_XRAY_HOURS, min(MAX_XRAY_HOURS, hours))
    raw_filter = get_param(event, "filter_expression") or ""
    if len(raw_filter) > MAX_FILTER_EXPRESSION_LEN:
        return err("Parameter 'filter_expression' exceeds maximum length of {}.".format(MAX_FILTER_EXPRESSION_LEN), code=400)
    filter_expression = raw_filter
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(hours=hours)
    clients = _make_clients(region)
    kwargs = {
        "StartTime": start_time,
        "EndTime": end_time,
        "Sampling": False,
    }
    if filter_expression:
        kwargs["FilterExpression"] = filter_expression
    summaries = []
    try:
        paginator = clients["xray"].get_paginator("get_trace_summaries")
        for page in paginator.paginate(**kwargs):
            for trace in page.get("TraceSummaries", []):
                entry = {
                    "trace_id": trace.get("Id", ""),
                    "duration_s": round(trace.get("Duration", 0), 3),
                    "response_time_s": round(trace.get("ResponseTime", 0), 3),
                    "has_error": trace.get("HasError", False),
                    "has_fault": trace.get("HasFault", False),
                    "has_throttle": trace.get("HasThrottle", False),
                    "http": {
                        "url": trace.get("Http", {}).get("HttpURL", ""),
                        "status": trace.get("Http", {}).get("HttpStatus"),
                        "method": trace.get("Http", {}).get("HttpMethod", ""),
                    },
                    "users": [u.get("UserName", "") for u in trace.get("Users", [])],
                    "service_ids": [
                        {"Name": s.get("Name", ""), "Type": s.get("Type", "")}
                        for s in trace.get("ServiceIds", [])
                    ],
                }
                summaries.append(entry)
    except Exception as e:
        error_msg = str(e)
        if "AccessDeniedException" in error_msg:
            return err(
                "X-Ray access denied in region '{}'. "
                "Ensure xray:GetTraceSummaries permission is granted.".format(region),
                code=403,
            )
        return _handle_region_error(e, region)
    errors = sum(1 for t in summaries if t["has_error"])
    faults = sum(1 for t in summaries if t["has_fault"])
    throttles = sum(1 for t in summaries if t["has_throttle"])
    avg_duration = (
        round(sum(t["duration_s"] for t in summaries) / len(summaries), 3)
        if summaries else 0
    )
    p99_duration = (
        round(sorted(t["duration_s"] for t in summaries)[int(len(summaries) * 0.99) - 1], 3)
        if len(summaries) >= 2 else (summaries[0]["duration_s"] if summaries else 0)
    )
    health = "healthy"
    if faults > 0 or errors > 0:
        fault_rate = (faults + errors) / len(summaries) * 100 if summaries else 0
        health = "critical" if fault_rate >= 10 else "warning"
    return ok({
        "region": region,
        "period_hours": hours,
        "filter_expression": filter_expression or "(none)",
        "total_traces": len(summaries),
        "summary": {
            "errors": errors,
            "faults": faults,
            "throttles": throttles,
            "avg_duration_s": avg_duration,
            "p99_duration_s": p99_duration,
            "health": health,
        },
        "traces": summaries[:100],
    })


SSM_INVENTORY_TYPES = {
    "AWS:InstanceInformation",
    "AWS:Application",
    "AWS:AWSComponent",
    "AWS:Network",
    "AWS:WindowsUpdate",
    "AWS:PatchSummary",
    "AWS:PatchCompliance",
    "AWS:ComplianceSummary",
    "ALL",
}

# Human-readable labels for inventory types
SSM_TYPE_LABELS = {
    "AWS:InstanceInformation": "OS & instance info",
    "AWS:Application":         "installed applications",
    "AWS:AWSComponent":        "AWS components",
    "AWS:Network":             "network configuration",
    "AWS:WindowsUpdate":       "Windows updates",
    "AWS:PatchSummary":        "patch summary",
    "AWS:PatchCompliance":     "patch compliance",
    "AWS:ComplianceSummary":   "compliance summary",
    "ALL":                     "all inventory types",
}


def get_ssm_inventory(event):
    """
    Query AWS Systems Manager Inventory for managed EC2 instances.
    Returns OS info, installed applications, AWS components, network config, etc.
    Requires SSM Agent installed + AmazonSSMManagedInstanceCore role on each EC2 instance.
    """
    region, region_err = _resolve_region(event)
    if region_err:
        return region_err

    instance_id = get_param(event, "instance_id") or ""
    inventory_type = (get_param(event, "inventory_type") or "AWS:InstanceInformation").strip()

    # Validate inventory_type
    if inventory_type not in SSM_INVENTORY_TYPES:
        return err(
            "Invalid inventory_type '{}'. Valid values: {}".format(
                inventory_type, ", ".join(sorted(SSM_INVENTORY_TYPES))
            ),
            code=400,
        )

    ssm = boto3.client("ssm", region_name=region)

    try:
        # ── 1. Get managed instances list ────────────────────────────────────
        paginator = ssm.get_paginator("describe_instance_information")
        filters = []
        if instance_id:
            filters.append({"Key": "InstanceIds", "Values": [instance_id]})

        managed_instances = []
        try:
            for page in paginator.paginate(
                Filters=filters if filters else [],
                PaginationConfig={"MaxItems": 100},
            ):
                for inst in page.get("InstanceInformationList", []):
                    managed_instances.append({
                        "instance_id":        inst.get("InstanceId", ""),
                        "computer_name":      inst.get("ComputerName", ""),
                        "platform_type":      inst.get("PlatformType", ""),
                        "platform_name":      inst.get("PlatformName", ""),
                        "platform_version":   inst.get("PlatformVersion", ""),
                        "agent_version":      inst.get("AgentVersion", ""),
                        "ip_address":         inst.get("IPAddress", ""),
                        "ping_status":        inst.get("PingStatus", ""),
                        "last_ping":          inst.get("LastPingDateTime", "").isoformat() if hasattr(inst.get("LastPingDateTime", ""), "isoformat") else str(inst.get("LastPingDateTime", "")),
                        "association_status": inst.get("AssociationStatus", ""),
                        "resource_type":      inst.get("ResourceType", ""),
                    })
        except Exception as e:
            return err(
                "Could not list SSM managed instances in region '{}'. "
                "Ensure SSM Agent is installed and instances have the AmazonSSMManagedInstanceCore IAM role. "
                "Error: {}".format(region, str(e)),
                code=500,
            )

        if not managed_instances:
            msg = (
                "No SSM-managed instances found in region '{}'.".format(region)
                + (" Instance '{}' is not managed by SSM.".format(instance_id) if instance_id else "")
                + " Ensure: (1) SSM Agent is running on the instance, "
                  "(2) instance has AmazonSSMManagedInstanceCore IAM role, "
                  "(3) instance can reach SSM endpoints."
            )
            return ok({
                "region": region,
                "managed_instance_count": 0,
                "instances": [],
                "inventory": [],
                "inventory_type": inventory_type,
                "message": msg,
            })

        # ── 2. Fetch inventory data ───────────────────────────────────────────
        inventory_results = []

        if inventory_type == "ALL":
            types_to_query = [
                "AWS:InstanceInformation",
                "AWS:Application",
                "AWS:AWSComponent",
                "AWS:Network",
            ]
        else:
            types_to_query = [inventory_type]

        for inv_type in types_to_query:
            filters_inv = [{"Key": "TypeName", "Values": [inv_type]}]
            if instance_id:
                filters_inv.append({"Key": "AWS:InstanceInformation.InstanceId", "Values": [instance_id]})

            try:
                inv_paginator = ssm.get_paginator("get_inventory")
                for page in inv_paginator.paginate(
                    Filters=filters_inv,
                    ResultAttributes=[{"TypeName": inv_type}],
                    PaginationConfig={"MaxItems": 200},
                ):
                    for entity in page.get("Entities", []):
                        entity_id = entity.get("Id", "")
                        data = entity.get("Data", {})
                        type_data = data.get(inv_type, {})
                        entries = type_data.get("Content", [])
                        if entries:
                            inventory_results.append({
                                "instance_id":    entity_id,
                                "inventory_type": inv_type,
                                "type_label":     SSM_TYPE_LABELS.get(inv_type, inv_type),
                                "count":          len(entries),
                                "entries":        entries[:50],  # cap at 50 per type per instance
                            })
            except Exception:
                # Non-fatal: some types may not be collected on all instances
                pass

        return ok({
            "region":                  region,
            "managed_instance_count":  len(managed_instances),
            "inventory_type_queried":  inventory_type,
            "instances":               managed_instances,
            "inventory":               inventory_results,
            "note": (
                "SSM Inventory requires: (1) SSM Agent running on each EC2 instance, "
                "(2) AmazonSSMManagedInstanceCore IAM role on the instance, "
                "(3) Inventory collection configured in SSM console or via Quick Setup."
            ),
        })

    except Exception as e:
        return _handle_region_error(e, region)


ACTIONS = {
    "get_ec2_health":     get_ec2_health,
    "get_lambda_health":  get_lambda_health,
    "get_cloudwatch_alarms": get_cloudwatch_alarms,
    "get_overall_health": get_overall_health,
    "get_logs_analysis":  get_logs_analysis,
    "get_xray_traces":    get_xray_traces,
    "get_ssm_inventory":  get_ssm_inventory,
}


def handler(event, context):
    logger.info("Event: %s", json.dumps(event, default=str))
    api_path = event.get("apiPath", "")
    http_method = event.get("httpMethod", "POST")
    action = api_path.lstrip("/")
    fn = ACTIONS.get(action)
    if not fn:
        return err("Unknown action: {}".format(action), 404, api_path=api_path, http_method=http_method)
    try:
        result = fn(event)
        # Inject apiPath and httpMethod into the response envelope (required by Bedrock Agents contract)
        result["response"]["apiPath"] = api_path
        result["response"]["httpMethod"] = http_method
        return result
    except Exception as e:
        logger.exception("Action %s failed", action)
        return err(str(e), api_path=api_path, http_method=http_method)
