"""
Verifies that all CDK TypeScript files compile without errors.
Runs `npx tsc --noEmit` which type-checks the whole project without producing output files.
"""
import subprocess
import sys
import os
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


class TestTypeScriptCompilation(unittest.TestCase):

    def test_cdk_stack_compiles_without_errors(self):
        """tsc --noEmit must exit 0 — catches truncated files and type errors in CDK stacks."""
        result = subprocess.run(
            "npx tsc --noEmit",
            cwd=ROOT,
            capture_output=True,
            text=True,
            shell=True,
        )
        self.assertEqual(
            result.returncode, 0,
            f"TypeScript compilation failed:\n{result.stdout}\n{result.stderr}",
        )
