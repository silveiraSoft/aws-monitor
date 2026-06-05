"""
Bootstrap: inject boto3/botocore stubs before any test module imports them.
Import this at the top of each test file BEFORE importing index or validate_aws_access.
"""
import sys
import os
from unittest.mock import MagicMock

# ── Inject boto3 stub if not installed ────────────────────────────────────────
if "boto3" not in sys.modules:
    import types

    boto3_mod = types.ModuleType("boto3")

    class _Session:
        def __init__(self, **kwargs): pass
        def client(self, svc, **kw): return MagicMock()

    boto3_mod.Session = _Session
    boto3_mod.client = lambda svc, **kw: MagicMock()
    boto3_mod.resource = lambda svc, **kw: MagicMock()
    sys.modules["boto3"] = boto3_mod

# ── Inject botocore.exceptions stub if not installed ─────────────────────────
if "botocore" not in sys.modules:
    import types

    botocore_mod = types.ModuleType("botocore")
    botocore_exc = types.ModuleType("botocore.exceptions")

    class ClientError(Exception):
        def __init__(self, error_response, operation_name):
            self.response = error_response
            self.operation_name = operation_name
            super().__init__(str(error_response))

    botocore_exc.ClientError = ClientError
    botocore_mod.exceptions = botocore_exc
    sys.modules["botocore"] = botocore_mod
    sys.modules["botocore.exceptions"] = botocore_exc

# Re-export for convenience
import botocore.exceptions  # noqa: E402
