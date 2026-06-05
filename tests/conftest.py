"""
Shared fixtures for AWS Monitor Agent test suite.
"""
import json
import os
import pytest


# ── Environment defaults ──────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    """Ensure boto3 always targets the mock region and never hits real AWS."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-key-id")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "test-token")
    monkeypatch.setenv("REGION", "us-east-1")


# ── Bedrock event builders ────────────────────────────────────────────────────

def make_bedrock_event(api_path: str, parameters: list | None = None) -> dict:
    """
    Build a synthetic Bedrock Agent action-group event.

    Bedrock sends events of the form:
        {
          "apiPath": "/get_ec2_health",
          "parameters": [{"name": "state", "value": "running"}],
          "actionGroup": "MonitorActions",
          "messageVersion": "1.0"
        }
    """
    return {
        "messageVersion": "1.0",
        "actionGroup": "MonitorActions",
        "apiPath": f"/{api_path.lstrip('/')}",
        "httpMethod": "GET",
        "parameters": parameters or [],
        "requestBody": {},
        "sessionAttributes": {},
        "promptSessionAttributes": {},
    }


def parse_response(response: dict) -> dict:
    """Unwrap the Bedrock response envelope and return the inner dict."""
    body_str = (
        response["response"]["responseBody"]["application/json"]["body"]
    )
    return json.loads(body_str)


@pytest.fixture
def bedrock_event():
    return make_bedrock_event


@pytest.fixture
def parse():
    return parse_response
