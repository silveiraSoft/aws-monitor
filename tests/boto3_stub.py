"""
Minimal boto3 stub for running tests without the real boto3 installed.
Injected into sys.modules before importing index.py or validate_aws_access.py.
All actual clients are replaced by MagicMock in each test anyway.
"""
from unittest.mock import MagicMock

# Minimal Session class
class Session:
    def __init__(self, **kwargs):
        self._kwargs = kwargs
    def client(self, service, **kw):
        return MagicMock()

def client(service, **kw):
    return MagicMock()

def resource(service, **kw):
    return MagicMock()
