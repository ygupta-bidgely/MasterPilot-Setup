"""mds_common.kit_env: the debug-kit profile bridge. No fallback profile; the SAME resolved profile reaches the kit."""
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mds_common as mc  # noqa: E402
import mds_target  # noqa: E402
import target_fixture  # noqa: E402

EXPORT = """P_PROFILE=/x/p.json
P_ENV=envA
P_DEPLOY=alpha
P_DB_CREDS_FILE='/home/q/db file'
P_SERVICES='emailer|emailersandsms|/var/log/a.log|
notifications|notifications|/var/log/b.log|'
P_PROBLEMS=''
"""


class KitEnv(unittest.TestCase):
    def setUp(self):
        mc._KIT_ENV = None

    def tearDown(self):
        mc._KIT_ENV = None

    def test_no_target_is_profile_required(self):
        saved, mds_target.VIEW = mds_target.VIEW, None
        try:
            with self.assertRaises(SystemExit) as cm:
                mc.kit_env()
            self.assertEqual(cm.exception.code, 3)
        finally:
            mds_target.VIEW = saved

    def test_multiline_export_and_the_resolved_profile(self):
        prof = target_fixture.use(pilot=88002)
        fake = subprocess.CompletedProcess([], 0, stdout=EXPORT, stderr="")
        with mock.patch("subprocess.run", return_value=fake) as run:
            env = mc.kit_env()
        self.assertEqual(env["BIDGELY_PROFILE"], str(prof))
        self.assertEqual(run.call_args.args[0][-1], str(prof))          # the kit loads the same profile
        self.assertEqual((env["ENV"], env["DEPLOY"], env["DB_CREDS_FILE"]), ("envA", "alpha", "/home/q/db file"))

    def test_unusable_profile_is_fatal(self):
        target_fixture.use(pilot=88002)
        fake = subprocess.CompletedProcess([], 3, stdout="", stderr="NEEDS_PROFILE: not found")
        with mock.patch("subprocess.run", return_value=fake):
            with self.assertRaises(mc.SetupError):
                mc.kit_env()


class Bootstrap(unittest.TestCase):
    def test_no_profile_anywhere(self):
        saved, mds_target.VIEW = mds_target.VIEW, None
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BIDGELY_PROFILE", None)
            try:
                with self.assertRaises(SystemExit) as cm:
                    mds_target.bootstrap([])
                self.assertEqual(cm.exception.code, 3)
            finally:
                mds_target.VIEW = saved

    def test_profile_applied_to_every_module_and_read_back(self):
        import mds_ba  # noqa: F401
        import mds_bb  # noqa: F401
        import mds_her
        import mds_tou
        target_fixture.use(pilot=66001, env="envr", dep="stackb", region="yy-test-2", aws_profile="qa-b", account="222222222222")
        d = mds_target.describe()
        self.assertNotIn("_inconsistent", d)
        self.assertEqual((d["pilot"], d["aws_region"], d["aws_profile"], d["aws_account"]), (66001, "yy-test-2", "qa-b", "222222222222"))
        self.assertEqual(d["queues"]["aggregation"], "Agg-envr-stackb")
        self.assertEqual((mds_tou.TOU_PLAN, mds_her.NTYPE), ("TOU_STACKB", 66001))

    def test_contract_mismatch_stops(self):
        f = target_fixture.make_profile(pilot=88002)
        saved, mds_target.VIEW = mds_target.VIEW, None
        with mock.patch.dict(os.environ, {"BIDGELY_CONTRACT_SHA256": "0" * 64}):
            try:
                with self.assertRaises(SystemExit) as cm:
                    mds_target.bootstrap(["--profile", str(f)])
                self.assertEqual(cm.exception.code, 3)
            finally:
                mds_target.VIEW = saved


if __name__ == "__main__":
    unittest.main()
