#!/usr/bin/env python3
"""
Test runner for aws-monitor.
Injects boto3/botocore stubs, then discovers and runs all tests.

Usage:
    python3 run_tests.py              # all tests
    python3 run_tests.py unit         # unit tests only
    python3 run_tests.py integration  # integration tests only
    python3 run_tests.py e2e          # e2e tests only
"""
import sys
import os
import unittest

# Redirect pyc cache so stale .pyc files on Windows mounts don't shadow updated sources
os.environ.setdefault("PYTHONPYCACHEPREFIX", "/tmp/aws-monitor-pycache")

# ── 1. Bootstrap stubs BEFORE any import of index.py or validate_aws_access ──
ROOT = os.path.dirname(__file__)

# Add all relevant paths
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, os.path.join(ROOT, "lambda", "monitor-actions"))
sys.path.insert(0, ROOT)

# Inject boto3/botocore stubs if not installed
if "boto3" not in sys.modules:
    import types
    from unittest.mock import MagicMock

    boto3_mod = types.ModuleType("boto3")

    class _Session:
        def __init__(self, **kwargs): pass
        def client(self, svc, **kw): return MagicMock()

    boto3_mod.Session = _Session
    boto3_mod.client = lambda svc, **kw: MagicMock()
    boto3_mod.resource = lambda svc, **kw: MagicMock()
    sys.modules["boto3"] = boto3_mod
    print("[runner] boto3 stub injected")

if "botocore" not in sys.modules:
    import types

    botocore_mod = types.ModuleType("botocore")
    botocore_exc = types.ModuleType("botocore.exceptions")

    class ClientError(Exception):
        def __init__(self, error_response, operation_name=""):
            self.response = error_response
            self.operation_name = operation_name
            super().__init__(str(error_response))

    botocore_exc.ClientError = ClientError
    botocore_mod.exceptions = botocore_exc
    sys.modules["botocore"] = botocore_mod
    sys.modules["botocore.exceptions"] = botocore_exc
    print("[runner] botocore stub injected")

# ── 2. Discover and run ───────────────────────────────────────────────────────
suite_map = {
    "unit":        os.path.join(ROOT, "tests", "unit"),
    "integration": os.path.join(ROOT, "tests", "integration"),
    "e2e":         os.path.join(ROOT, "tests", "e2e"),
}

target = sys.argv[1] if len(sys.argv) > 1 else "all"

loader = unittest.TestLoader()

if target == "all":
    suites = []
    for name, path in suite_map.items():
        s = loader.discover(start_dir=path, pattern="test_*.py", top_level_dir=ROOT)
        suites.append(s)
    combined = unittest.TestSuite(suites)
else:
    path = suite_map.get(target)
    if not path:
        print(f"Unknown target '{target}'. Valid: unit, integration, e2e, all")
        sys.exit(1)
    combined = loader.discover(start_dir=path, pattern="test_*.py", top_level_dir=ROOT)

runner = unittest.TextTestRunner(verbosity=2)
result = runner.run(combined)
sys.exit(0 if result.wasSuccessful() else 1)
