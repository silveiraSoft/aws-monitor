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
        "elbv2": boto3.client("elbv2", region_name=region),
        "cloudtrail": boto3.client("cloudtrail", region_name=region),
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



def get_ec2_process_metrics(event):
    """
    Returns top N processes by CPU or memory usage via CloudWatch Agent procstat metrics.
    Requires CloudWatch Agent with procstat plugin on EC2 instances.
    """
    region, region_err = _resolve_region(event)
    if region_err:
        return region_err

    metric_type = (get_param(event, "metric") or "cpu").lower()
    instance_id = (get_param(event, "instance_id") or "").strip()[:64]

    try:
        top_n = min(int(get_param(event, "top_n") or 5), 20)
    except (ValueError, TypeError):
        top_n = 5

    try:
        hours = max(1, min(int(get_param(event, "hours") or 1), 24))
    except (ValueError, TypeError):
        hours = 1

    if metric_type == "memory":
        metric_name = "procstat memory_rss"
        unit_label = "bytes"
    else:
        metric_type = "cpu"
        metric_name = "procstat cpu_usage"
        unit_label = "%"

    try:
        clients = _make_clients(region)
        cw = clients["cloudwatch"]

        # List available procstat metrics
        list_kwargs = {"Namespace": "CWAgent", "MetricName": metric_name}
        if instance_id:
            list_kwargs["Dimensions"] = [{"Name": "InstanceId", "Value": instance_id}]

        raw_metrics = []
        paginator = cw.get_paginator("list_metrics")
        for page in paginator.paginate(**list_kwargs):
            raw_metrics.extend(page["Metrics"])

        if not raw_metrics:
            return ok({
                "metric": metric_type,
                "instance_id": instance_id or "all",
                "hours": hours,
                "processes": [],
                "note": (
                    "No procstat metrics found in CloudWatch (namespace CWAgent). "
                    "To enable process monitoring: (1) Install CloudWatch Agent on each EC2 instance "
                    "via SSM Run Command or manually, (2) Configure the procstat plugin to collect "
                    "cpu_usage and memory_rss, (3) Start the agent. "
                    "Metrics appear within 1-2 minutes of agent startup."
                ),
                "region": region,
            })

        # Build GetMetricData queries (max 500 per call)
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(hours=hours)
        period = max(60, (hours * 3600) // 60)

        queries = []
        for i, m in enumerate(raw_metrics[:500]):
            queries.append({
                "Id": "p{}".format(i),
                "MetricStat": {
                    "Metric": m,
                    "Period": period,
                    "Stat": "Average",
                },
                "ReturnData": True,
            })

        # Fetch metric data
        data_by_id = {}
        for batch_start in range(0, len(queries), 500):
            batch = queries[batch_start:batch_start + 500]
            resp = cw.get_metric_data(
                MetricDataQueries=batch,
                StartTime=start_time,
                EndTime=end_time,
            )
            for r in resp.get("MetricDataResults", []):
                if r["Values"]:
                    data_by_id[r["Id"]] = {
                        "avg": sum(r["Values"]) / len(r["Values"]),
                        "max": max(r["Values"]),
                    }

        # Build and sort process list
        processes = []
        for i, m in enumerate(raw_metrics[:500]):
            qid = "p{}".format(i)
            if qid not in data_by_id:
                continue
            dims = {d["Name"]: d["Value"] for d in m["Dimensions"]}
            val = data_by_id[qid]
            entry = {
                "process_name": dims.get("process_name", "unknown"),
                "instance_id": dims.get("InstanceId", "unknown"),
                "avg": round(val["avg"], 2),
                "max": round(val["max"], 2),
                "unit": unit_label,
            }
            if metric_type == "memory":
                entry["avg_mb"] = round(val["avg"] / 1048576, 1)
                entry["max_mb"] = round(val["max"] / 1048576, 1)
            processes.append(entry)

        processes.sort(key=lambda x: x["avg"], reverse=True)

        return ok({
            "metric": metric_type,
            "instance_id": instance_id or "all",
            "hours": hours,
            "top_n": top_n,
            "total_processes_found": len(processes),
            "processes": processes[:top_n],
            "region": region,
        })

    except Exception as e:
        return _handle_region_error(e, region)



def get_ec2_instance_metrics(event):
    """
    Returns CPU, network and disk metrics per EC2 instance from CloudWatch basic monitoring.
    No CloudWatch Agent required — metrics available for all instances by default.
    """
    region, region_err = _resolve_region(event)
    if region_err:
        return region_err

    instance_id = (get_param(event, "instance_id") or "").strip()[:64]
    try:
        hours = max(1, min(int(get_param(event, "hours") or 1), 24))
    except (ValueError, TypeError):
        hours = 1

    try:
        clients = _make_clients(region)
        ec2 = clients["ec2"]
        cw  = clients["cloudwatch"]

        # Get running instances
        instances = []
        paginator = ec2.get_paginator("describe_instances")
        filters = [{"Name": "instance-state-name", "Values": ["running"]}]
        if instance_id:
            filters.append({"Name": "instance-id", "Values": [instance_id]})
        for page in paginator.paginate(Filters=filters):
            for r in page["Reservations"]:
                for i in r["Instances"]:
                    name = next((t["Value"] for t in i.get("Tags", []) if t["Key"] == "Name"), "")
                    instances.append({"id": i["InstanceId"], "name": name})

        if not instances:
            return ok({"instances": [], "region": region,
                       "note": "No running instances found" + (f" matching {instance_id}" if instance_id else ".")})

        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(hours=hours)
        period = max(60, (hours * 3600) // 60)

        METRIC_NAMES = ["CPUUtilization", "NetworkIn", "NetworkOut", "DiskReadOps", "DiskWriteOps"]
        queries = []
        for inst in instances:
            iid = inst["id"]
            for mname in METRIC_NAMES:
                qid = "{}_{}".format(iid.replace("-", "_"), mname)
                queries.append({
                    "Id": qid,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "AWS/EC2",
                            "MetricName": mname,
                            "Dimensions": [{"Name": "InstanceId", "Value": iid}],
                        },
                        "Period": period,
                        "Stat": "Average",
                    },
                    "ReturnData": True,
                })

        data = {}
        for i in range(0, len(queries), 500):
            resp = cw.get_metric_data(MetricDataQueries=queries[i:i+500],
                                      StartTime=start_time, EndTime=end_time)
            for r in resp.get("MetricDataResults", []):
                if r["Values"]:
                    data[r["Id"]] = round(sum(r["Values"]) / len(r["Values"]), 2)

        result = []
        for inst in instances:
            iid = inst["id"]
            pfx = iid.replace("-", "_")
            result.append({
                "instance_id": iid,
                "name": inst["name"],
                "cpu_pct": data.get("{}_CPUUtilization".format(pfx)),
                "network_in_bytes": data.get("{}_NetworkIn".format(pfx)),
                "network_out_bytes": data.get("{}_NetworkOut".format(pfx)),
                "disk_read_ops": data.get("{}_DiskReadOps".format(pfx)),
                "disk_write_ops": data.get("{}_DiskWriteOps".format(pfx)),
                "hours": hours,
            })

        result.sort(key=lambda x: (x["cpu_pct"] or 0), reverse=True)
        return ok({"instances": result, "count": len(result), "region": region})

    except Exception as e:
        return _handle_region_error(e, region)


def get_alb_health(event):
    """
    Returns health and traffic metrics for Application Load Balancers (ALB).
    Includes request count, error rates (4xx/5xx), latency and host health.
    """
    region, region_err = _resolve_region(event)
    if region_err:
        return region_err

    alb_name = (get_param(event, "alb_name") or "").strip()[:128]
    try:
        hours = max(1, min(int(get_param(event, "hours") or 1), 24))
    except (ValueError, TypeError):
        hours = 1

    try:
        clients = _make_clients(region)
        elb = clients["elbv2"]
        cw  = clients["cloudwatch"]

        kwargs = {"LoadBalancerArns": []} if not alb_name else {}
        resp = elb.describe_load_balancers(**({"Names": [alb_name]} if alb_name else {}))
        lbs = resp.get("LoadBalancers", [])

        if not lbs:
            return ok({"load_balancers": [], "region": region,
                       "note": "No Application Load Balancers found" + (f" named '{alb_name}'" if alb_name else ".")})

        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(hours=hours)
        period = max(60, (hours * 3600) // 60)

        ALB_METRICS = ["RequestCount", "HTTPCode_ELB_4XX_Count",
                       "HTTPCode_ELB_5XX_Count", "TargetResponseTime",
                       "HealthyHostCount", "UnHealthyHostCount"]

        queries = []
        lb_keys = {}
        for lb in lbs:
            # ALB dimension uses the suffix after "app/"
            arn_suffix = "/".join(lb["LoadBalancerArn"].split(":")[-1].split("/")[1:])
            lb_keys[lb["LoadBalancerArn"]] = arn_suffix
            for mname in ALB_METRICS:
                stat = "Sum" if mname in ("RequestCount", "HTTPCode_ELB_4XX_Count",
                                           "HTTPCode_ELB_5XX_Count") else "Average"
                safe = lb["LoadBalancerName"].replace("-", "_")
                qid = "{}_{}".format(safe, mname)
                queries.append({
                    "Id": qid,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "AWS/ApplicationELB",
                            "MetricName": mname,
                            "Dimensions": [{"Name": "LoadBalancer", "Value": arn_suffix}],
                        },
                        "Period": period,
                        "Stat": stat,
                    },
                    "ReturnData": True,
                })

        data = {}
        for i in range(0, len(queries), 500):
            r = cw.get_metric_data(MetricDataQueries=queries[i:i+500],
                                   StartTime=start_time, EndTime=end_time)
            for result in r.get("MetricDataResults", []):
                if result["Values"]:
                    data[result["Id"]] = round(sum(result["Values"]), 2)                         if any(m in result["Id"] for m in ["RequestCount", "4XX", "5XX"])                         else round(sum(result["Values"]) / len(result["Values"]), 4)

        lb_results = []
        for lb in lbs:
            safe = lb["LoadBalancerName"].replace("-", "_")
            req = data.get("{}_RequestCount".format(safe), 0) or 0
            err4 = data.get("{}_HTTPCode_ELB_4XX_Count".format(safe), 0) or 0
            err5 = data.get("{}_HTTPCode_ELB_5XX_Count".format(safe), 0) or 0
            error_rate = round((err4 + err5) / req * 100, 2) if req > 0 else 0
            lb_results.append({
                "name": lb["LoadBalancerName"],
                "state": lb["State"]["Code"],
                "dns": lb["DNSName"],
                "requests": int(req),
                "errors_4xx": int(err4),
                "errors_5xx": int(err5),
                "error_rate_pct": error_rate,
                "avg_latency_ms": round((data.get("{}_TargetResponseTime".format(safe)) or 0) * 1000, 1),
                "healthy_hosts": int(data.get("{}_HealthyHostCount".format(safe)) or 0),
                "unhealthy_hosts": int(data.get("{}_UnHealthyHostCount".format(safe)) or 0),
                "health": "CRITICAL" if (data.get("{}_UnHealthyHostCount".format(safe)) or 0) > 0
                          else "WARNING" if error_rate > 5 else "OK",
                "hours": hours,
            })

        return ok({"load_balancers": lb_results, "count": len(lb_results), "region": region})

    except Exception as e:
        return _handle_region_error(e, region)


def get_cloudtrail_activity(event):
    """
    Returns recent AWS API activity from CloudTrail.
    Useful for auditing who changed what and detecting unauthorized actions.
    """
    region, region_err = _resolve_region(event)
    if region_err:
        return region_err

    try:
        hours = max(1, min(int(get_param(event, "hours") or 3), 24))
    except (ValueError, TypeError):
        hours = 3

    username   = (get_param(event, "username") or "").strip()[:128]
    event_name = (get_param(event, "event_name") or "").strip()[:128]
    try:
        limit = min(int(get_param(event, "limit") or 20), 50)
    except (ValueError, TypeError):
        limit = 20

    try:
        clients = _make_clients(region)
        ct = clients["cloudtrail"]

        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(hours=hours)

        lookup_attrs = []
        if username:
            lookup_attrs.append({"AttributeKey": "Username", "AttributeValue": username})
        elif event_name:
            lookup_attrs.append({"AttributeKey": "EventName", "AttributeValue": event_name})

        kwargs = {
            "StartTime": start_time,
            "EndTime": end_time,
            "MaxResults": limit,
        }
        if lookup_attrs:
            kwargs["LookupAttributes"] = lookup_attrs

        resp = ct.lookup_events(**kwargs)
        raw_events = resp.get("Events", [])

        events = []
        for e in raw_events:
            detail = {}
            try:
                detail = json.loads(e.get("CloudTrailEvent", "{}"))
            except Exception:
                pass
            events.append({
                "time": e["EventTime"].isoformat() if hasattr(e.get("EventTime"), "isoformat") else str(e.get("EventTime")),
                "action": e.get("EventName"),
                "user": e.get("Username", detail.get("userIdentity", {}).get("arn", "unknown")),
                "source_ip": detail.get("sourceIPAddress"),
                "resource": e.get("Resources", [{}])[0].get("ResourceName") if e.get("Resources") else None,
                "error": detail.get("errorCode"),
                "region": detail.get("awsRegion", region),
            })

        summary = {
            "total_events": len(events),
            "errors": sum(1 for ev in events if ev.get("error")),
            "unique_users": len({ev["user"] for ev in events}),
            "unique_actions": len({ev["action"] for ev in events}),
        }

        return ok({
            "hours": hours,
            "filter_user": username or None,
            "filter_action": event_name or None,
            "summary": summary,
            "events": events,
            "region": region,
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
    "get_ec2_process_metrics": get_ec2_process_metrics,
    "get_ec2_instance_metrics": get_ec2_instance_metrics,
    "get_alb_health": get_alb_health,
    "get_cloudtrail_activity": get_cloudtrail_activity,
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
