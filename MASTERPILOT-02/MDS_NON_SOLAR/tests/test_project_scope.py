"""The project-scope gate: this directory's automation may only target its own pilot family.

MASTERPILOT-02 and masterpilot01 (88001) are kept deliberately separate -- separate automation, separate
buckets, no shared engine. The gate exists so that passing the wrong --profile fails loudly instead of
quietly provisioning the other pilot. These tests are the regression guard for that gate.
"""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import mds_target  # noqa: E402


# These tests assert the gate itself, so they must run with the suite-wide bypass OFF even when the
# rest of the suite sets it to drive synthetic pilots.
@mock.patch.dict(os.environ, {"MDS_SCOPE_BYPASS_FOR_TESTS": "0"})
class TestProjectScope(unittest.TestCase):
    def test_scope_file_exists_and_is_valid(self):
        self.assertTrue(mds_target.SCOPE_PATH.exists(), f"{mds_target.SCOPE_PATH} must exist")
        scope = json.loads(mds_target.SCOPE_PATH.read_text())
        self.assertTrue(scope.get("allowed_pilots"), "allowed_pilots must be non-empty")

    def test_in_scope_pilots_pass(self):
        for pilot in json.loads(mds_target.SCOPE_PATH.read_text())["allowed_pilots"]:
            with self.subTest(pilot=pilot):
                mds_target.check_project_scope({"pilot": pilot})      # must not raise

    def test_masterpilot01_is_refused(self):
        """88001 has its own automation; this project must never act on it."""
        with self.assertRaises(SystemExit):
            mds_target.check_project_scope({"pilot": 88001})

    def test_unrelated_pilot_is_refused(self):
        for pilot in (10130, 10037, 73969, 99999):
            with self.subTest(pilot=pilot):
                with self.assertRaises(SystemExit):
                    mds_target.check_project_scope({"pilot": pilot})

    def test_88001_never_listed_as_allowed(self):
        """A direct guard against someone widening the scope file to re-couple the two pilots."""
        allowed = {int(p) for p in json.loads(mds_target.SCOPE_PATH.read_text())["allowed_pilots"]}
        self.assertNotIn(88001, allowed, "88001 must never be in MASTERPILOT-02's scope")

    def test_string_pilot_is_handled(self):
        """A profile may carry the pilot as a string; the gate must still compare numerically."""
        mds_target.check_project_scope({"pilot": "88011"})
        with self.assertRaises(SystemExit):
            mds_target.check_project_scope({"pilot": "88001"})


if __name__ == "__main__":
    unittest.main()
