"""Follow-ups of the 2026-10-06 review sweep: no silent layout fallback, solar kept by default, explicit plan offsets."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


class Followups(unittest.TestCase):
    def test_unreadable_ms_layout_is_needs_input_not_a_default_list(self):
        import mds_tou
        with mock.patch.object(mds_tou, "api", side_effect=mds_tou.SetupError("read failed")):
            with self.assertRaises(mds_tou.SetupError) as cm:
                mds_tou.dcx_ms_sections("11111111-1111-4111-8111-111111111111")
        self.assertIn("NEEDS_INPUT", str(cm.exception))
        self.assertFalse(hasattr(mds_tou, "MS_FALLBACK"))

    def test_onboarding_solar_default_keeps_the_requested_solar_home(self):
        import mds_tou
        src = Path(mds_tou.__file__).read_text()
        self.assertIn('"--onboarding-solar", choices=("decouple", "keep"), default="keep"', src)

    def test_manual_send_snapshot_is_the_event_time_state(self):
        """Review 2 R5: a decoupled manual send ran with solarUser=false; diagnosis must use that, not the transition."""
        import types
        import mds_tou
        run = types.SimpleNamespace(m={"homeAtTransition": {"at": 1000.0, "solarUser": True},
                                       "transitionApplied": {"at": 1000.0}}, save=lambda: None)
        mds_tou.snapshot_before_send(run, mds_tou.ONB, 5000.0, False)
        self.assertIs(mds_tou.solar_at_event(run, mds_tou.ONB), False)
        self.assertEqual(mds_tou.event_window(run, mds_tou.ONB)[0], 4940.0)
        run.m["homeAtMsPass"] = {"at": 100.0, "solarUser": True}
        mds_tou.snapshot_before_send(run, mds_tou.MS, 7000.0, True)
        self.assertIs(mds_tou.solar_at_event(run, mds_tou.MS), True)
        self.assertEqual(mds_tou.event_window(run, mds_tou.MS)[0], 6940.0)
        empty = types.SimpleNamespace(m={}, save=lambda: None)
        self.assertIsNone(mds_tou.solar_at_event(empty, mds_tou.ONB))                # no snapshot -> unknown

    def test_nve_plan_user_needs_an_explicit_offset(self):
        import io
        import create_mds_nve_plan_user as m
        from target_fixture import make_profile
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, mock.patch("sys.stderr", err), \
                mock.patch.object(m.mc, "get_token", side_effect=AssertionError("must refuse before any token/network use")):
            m.main(["--profile", str(make_profile()), "--nve-plan", "198"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("--plan-offset is required", err.getvalue())


if __name__ == "__main__":
    unittest.main()
