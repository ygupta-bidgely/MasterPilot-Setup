"""Regression tests for the hard-coding audit (2026-10-06): operational values that were literals in executable code
(image bucket/host, default rate plans, feed-in rate, Chrome path, token path, comparison map, TOU plan number,
ETL host commands, brand lists, a forbidden Launchpad config write, pilot-derived TOU layouts) and an automatic
solarUser decouple. Every test runs offline: fixture profiles, no socket other than 127.0.0.1."""
from __future__ import annotations

import argparse
import ast
import contextlib
import io
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import target_fixture as TF  # noqa: E402
import mds_common as mc  # noqa: E402
import mds_target  # noqa: E402

_REAL_CONNECT = socket.socket.connect


def _guarded_connect(self, addr):          # only the local stub/loopback may be reached
    host = addr[0] if isinstance(addr, tuple) else addr
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise AssertionError(f"network call to {addr} in an offline test")
    return _REAL_CONNECT(self, addr)


def setUpModule():
    socket.socket.connect = _guarded_connect


def tearDownModule():
    socket.socket.connect = _REAL_CONNECT


def _boom(what):
    def f(*a, **k):
        raise AssertionError(f"{what} reached: {a[:2]}")
    return f


def _constants(path: Path) -> list:
    """Every constant of the EXECUTABLE code (module/function/class docstrings excluded)."""
    tree = ast.parse(path.read_text())
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant):
                docs.add(id(first.value))
    return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and id(n) not in docs]


def _profile(**over) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="hc-prof-"))
    return TF.make_profile(pilot=66001, env="envq", dep="stacka", tmp=tmp, **over)


def _use(**over) -> Path:
    f = _profile(**over)
    mds_target.VIEW = None
    mds_target.bootstrap(["--profile", str(f)])
    return f


def _run(tmp: Path | None = None, **m) -> mc.Run:
    d = Path(tmp or tempfile.mkdtemp(prefix="hc-run-"))
    return mc.Run(d, dict({"uuid": "aaaaaaaa-0000-4000-8000-000000000001"}, **m))


# ---------------------------------------------------------------------------------------------------------------------
# 1. create_mds_nve_tou_rate_plan.py / copy_nve_rates_to_mds.py
# ---------------------------------------------------------------------------------------------------------------------
class RatePlanScript(unittest.TestCase):
    def setUp(self):
        import create_mds_nve_tou_rate_plan as NP
        self.NP = NP

    def test_no_operational_literals_in_code(self):
        bad = [c for c in _constants(ROOT / "create_mds_nve_tou_rate_plan.py")
               if (isinstance(c, str) and any(x in c for x in ("bidgely-email-images", "cloudfront.net", "Google Chrome",
                                                              "/Applications/", "MDS_TOU_EVRR")))
               or (isinstance(c, (int, float)) and not isinstance(c, bool) and c in (907, 0.03))]
        self.assertEqual(bad, [])

    def test_image_settings_come_from_the_target_after_it_is_applied(self):
        _use(**{"email.provisioner.target.image_bucket": "img-bucket-q",
                "email.provisioner.target.image_host": "https://img.envq.invalid"})
        a = argparse.Namespace(image_bucket=None, image_host=None)
        bucket, host, prefix = self.NP.image_settings(a)
        self.assertEqual((bucket, host), ("img-bucket-q", "https://img.envq.invalid"))
        self.assertEqual(prefix, "rate/mds66001")            # never rate/mdsNone (computed after the target)
        a2 = argparse.Namespace(image_bucket="explicit-b", image_host="https://x.invalid/")
        self.assertEqual(self.NP.image_settings(a2), ("explicit-b", "https://x.invalid", "rate/mds66001"))

    def _main(self, *extra, **over):
        f = _profile(**over)
        src = _profile()
        err = io.StringIO()
        with mock.patch.object(mc, "api", _boom("api")), mock.patch.object(self.NP, "api", _boom("api")), \
                mock.patch.object(mc, "aws", _boom("aws")), mock.patch.object(self.NP, "nve_get", _boom("source GET")), \
                mock.patch.object(self.NP, "fetch_source", _boom("fetch_source")), \
                mock.patch("urllib.request.urlopen", _boom("urlopen")), contextlib.redirect_stderr(err):
            mds_target.VIEW = None
            try:
                rc = self.NP.main(["--profile", str(f), "--source-profile", str(src), "--source-plan", "1",
                                   "--output-dir", tempfile.mkdtemp(prefix="hc-out-"), *extra])
            except SystemExit as e:
                rc = e.code
        return rc, err.getvalue()

    def test_plan_number_name_feed_in_are_required(self):
        rc, err = self._main("--phases", "build")
        self.assertEqual(rc, 2)
        for flag in ("--plan-number", "--plan-name", "--feed-in-rate"):
            self.assertIn(flag, err)

    def test_image_phase_blocked_without_image_settings_before_any_network(self):
        rc, err = self._main("--phases", "images", "--plan-number", "1234", "--plan-name", "P_TOU", "--feed-in-rate", "0.05",
                             "--chrome", "/nonexistent/chrome")
        self.assertEqual(rc, 2)
        self.assertIn("BLOCKED", err)
        self.assertIn("image_bucket", err)

    def test_chrome_is_explicit(self):
        a = argparse.Namespace(chrome=None)
        with self.assertRaises(mc.SetupError) as cm:
            self.NP.render_png("<html/>", Path(tempfile.mkdtemp()) / "x.png", 100, a.chrome)
        self.assertIn("--chrome", str(cm.exception))


class CopyRatesScript(unittest.TestCase):
    def test_offset_and_prefix_are_required(self):
        import copy_nve_rates_to_mds as CP
        self.assertFalse(hasattr(CP, "PLAN_OFFSET") and CP.PLAN_OFFSET is not None)
        self.assertNotIn("NVE_", [c for c in _constants(ROOT / "copy_nve_rates_to_mds.py") if isinstance(c, str)])
        f, src, err = _profile(), _profile(), io.StringIO()
        with mock.patch.object(mc, "api", _boom("api")), mock.patch.object(CP, "fetch_source", _boom("fetch_source")), \
                contextlib.redirect_stderr(err):
            mds_target.VIEW = None
            with self.assertRaises(SystemExit) as cm:
                CP.main(["--profile", str(f), "--source-profile", str(src), "--output-dir", tempfile.mkdtemp()])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("--plan-offset", err.getvalue())
        self.assertIn("--name-prefix", err.getvalue())


# ---------------------------------------------------------------------------------------------------------------------
# 2./3. rate comparison scripts
# ---------------------------------------------------------------------------------------------------------------------
class PilotRateComparison(unittest.TestCase):
    def setUp(self):
        import set_mds_pilot_rate_comparison as S
        self.S = S

    def test_no_token_path_or_default_map_literal(self):
        strs = [c for c in _constants(ROOT / "set_mds_pilot_rate_comparison.py") if isinstance(c, str)]
        self.assertFalse([s for s in strs if "api_token_" in s or ".config/bidgely" in s], strs)
        self.assertFalse(hasattr(self.S, "DEFAULT_MAP"))

    def _main(self, *args, get_token=None):
        f, err = _profile(), io.StringIO()
        with mock.patch.object(mc, "api", _boom("api")), mock.patch.object(self.S, "api", _boom("api")), \
                mock.patch.object(mc, "get_token", get_token or _boom("get_token")), contextlib.redirect_stderr(err):
            mds_target.VIEW = None
            try:
                rc = self.S.main(["--profile", str(f), *args])
            except SystemExit as e:
                rc = e.code
        return rc, err.getvalue()

    def test_map_is_required(self):
        rc, err = self._main()
        self.assertEqual(rc, 2)
        self.assertIn("--map", err)

    def test_token_comes_from_the_profile(self):
        seen = []

        def tok(tf):
            seen.append(tf)
            raise mc.SetupError("stop here")
        rc, _ = self._main("--map", "1:2", get_token=tok)
        self.assertEqual(rc, 2)
        self.assertEqual(seen, [None])          # None -> mc.get_token reads BIDGELY_TOKEN or the profile's api.token_file


class EnableRateComparison(unittest.TestCase):
    def setUp(self):
        import enable_mds_rate_comparison as E
        self.E = E

    def test_no_tou_plan_literal(self):
        self.assertNotIn(907, [c for c in _constants(ROOT / "enable_mds_rate_comparison.py") if isinstance(c, int)])

    def test_scope_defaults_to_user_and_pilot_needs_explicit_scope(self):
        f, err = _profile(), io.StringIO()
        with mock.patch.object(self.E, "load_run", _boom("load_run")), mock.patch.object(mc, "api", _boom("api")), \
                contextlib.redirect_stderr(err):
            mds_target.VIEW = None
            with self.assertRaises(SystemExit) as cm:
                self.E.main(["--profile", str(f), "--phases", "config"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("--scope pilot", err.getvalue())

    def _build(self, live, **kw):
        a = argparse.Namespace(**dict(dict(uuid="u-1", map=[], auto_candidates=0, min_saving=10.0, max_saving_pct=None,
                                           computation_method=None, tou_plan_number=None), **kw))
        with mock.patch.object(self.E, "user_plan", lambda u: 12), mock.patch.object(mc, "read_config", lambda *x: {}):
            return self.E.build_config(a, live)

    def test_tou_target_resolved_from_the_profile_plan_name(self):
        _use()                                                    # email.rate_plans.tou = TOU_STACKA
        live = {12: {"planName": "FLAT_STACKA"}, 34: {"planName": "TOU_STACKA"}}
        _, info = self._build(live)
        self.assertEqual(info["extra"], {"12": [34]})
        _, info = self._build({**live, 56: {"planName": "OTHER"}}, tou_plan_number=56)
        self.assertEqual(info["extra"], {"12": [56]})

    def test_tou_target_unresolvable_is_an_error(self):
        _use()
        with self.assertRaises(mc.SetupError):
            self._build({12: {"planName": "FLAT_STACKA"}})


# ---------------------------------------------------------------------------------------------------------------------
# 4. mds_her.py / mds_paper_her.py
# ---------------------------------------------------------------------------------------------------------------------
class HerShcUnavailable(unittest.TestCase):
    def setUp(self):
        import mds_her as mh
        self.mh = mh

    def test_no_etl_host_literals_in_code(self):
        bad = [c for c in _constants(ROOT / "mds_her.py") if isinstance(c, str)
               and any(x in c for x in ("/usr/bin/java", "sudo -u", "bprod", "/opt/bidgely", "/tmp", "OneJar-"))]
        self.assertEqual(bad, [])
        self.assertFalse([c for c in _constants(ROOT / "mds_her.py") if c == "etl"])

    def test_bootstrap_is_unavailable_before_any_s3_call(self):
        calls = []

        class S3:
            def __getattr__(self, name):
                def f(*a, **k):
                    calls.append(name)
                return f

        class Sess:
            def client(self, *a, **k):
                return S3()
        _use(**{"email.provisioner.target.dw_bucket": None})
        a = argparse.Namespace(aws_profile=None, nid=None, zip="00000", skip_ncrunner=True)
        with mock.patch.object(mc, "aws", lambda *x: Sess()), mock.patch.object(self.mh, "envlogs", _boom("envlogs")):
            with self.assertRaises(mc.SetupError) as cm:
                self.mh.shc_bootstrap(_run(), a, ["u"])
        self.assertIn("UNAVAILABLE", str(cm.exception))
        self.assertEqual(calls, [])                                # no put_object on a None bucket (or any bucket)

    def test_publish_is_unavailable_without_envlogs(self):
        _use()
        with mock.patch.object(self.mh, "envlogs", _boom("envlogs")):
            with self.assertRaises(mc.SetupError) as cm:
                self.mh.publish_nmetrics(_run(cycles=[]), argparse.Namespace(aws_profile=None))
        self.assertIn("UNAVAILABLE", str(cm.exception))

    def test_cli_flags_refused_before_any_network(self):
        f, err = _profile(), io.StringIO()
        for flag in ("--shc-bootstrap", "--shc-publish"):
            with mock.patch.object(mc, "api", _boom("api")), mock.patch.object(self.mh, "api", _boom("api")), \
                    mock.patch.object(mc, "main", _boom("user creation")), mock.patch.object(mc, "aws", _boom("aws")), \
                    mock.patch.object(mc, "get_token", _boom("token")), contextlib.redirect_stderr(err):
                mds_target.VIEW = None
                rc = self.mh.main("doc", ("ELECTRIC",), tempfile.mkdtemp(), ["--profile", str(f), flag])
            self.assertEqual(rc, 2, flag)
        self.assertIn("UNAVAILABLE", err.getvalue())

    def test_brand_lists_come_from_the_profile(self):
        import mds_paper_her as mp
        for path in (ROOT / "mds_her.py", ROOT / "mds_paper_her.py"):
            strs = [c.lower() for c in _constants(path) if isinstance(c, str)]
            self.assertFalse([s for s in strs if "nv energy" in s or "wattsmart" in s or "pacificorp" in s], path.name)
        _use()
        self.assertEqual((self.mh.brand_blocklist(), mp.brand_blocklist()), ((), ()))
        _use(**{"email.provisioner.target.brand_blocklist": ["Acme Power"]})
        self.assertEqual((self.mh.brand_blocklist(), mp.brand_blocklist()), (("acme power",), ("acme power",)))
        self.assertIn("brand text 'acme power'", self.mh.branding_issues("", "Hello from ACME Power"))


# ---------------------------------------------------------------------------------------------------------------------
# 5. mds_bb.py
# ---------------------------------------------------------------------------------------------------------------------
class BudgetBillingNeverWritesLaunchpadConfig(unittest.TestCase):
    def setUp(self):
        import mds_bb
        self.bb = mds_bb

    def test_program_part_launchpad_is_never_written(self):
        _use()
        posts = []

        def api(method, path, body=None, **k):
            if method != "GET":
                posts.append((path, (body or {}).get("configType")))
            return {}
        with mock.patch.object(self.bb, "api", api), mock.patch.object(mc, "read_config", lambda *x: {}):
            self.bb.ensure_pilot_bb(None)
        self.assertNotIn("program_part_launchpad", [ct for _, ct in posts])
        self.assertNotIn("program_part_launchpad", self.bb.PILOT_BB_CONFIGS)

    def test_missing_launchpad_config_blocks_user_creation(self):
        f, err = _profile(), io.StringIO()
        with mock.patch.object(mc, "read_config", lambda *x: {}), mock.patch.object(mc, "main", _boom("user creation")), \
                mock.patch.object(self.bb, "api", _boom("api write")), contextlib.redirect_stderr(err):
            mds_target.VIEW = None
            rc = self.bb.main("doc", "user", ["--profile", str(f), "--no-pilot-config", "--output-dir", tempfile.mkdtemp()])
        self.assertEqual(rc, 2)
        self.assertIn("program_part_launchpad", err.getvalue())


# ---------------------------------------------------------------------------------------------------------------------
# 6. mds_tou.py
# ---------------------------------------------------------------------------------------------------------------------
class TouKeepsTheNonSolarAccount(unittest.TestCase):
    def setUp(self):
        import mds_tou
        self.t = mds_tou

    def test_default_keeps_solar_user_and_auto_is_gone(self):
        a, _ = self.t.parse("doc", "user", [])
        self.assertEqual(a.solar_tou_chart, "flag")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.t.parse("doc", "user", ["--solar-tou-chart", "auto"])
        for flow in ("all", "coaching", "onboarding"):          # non-solar: the solar TOU branch is never opened
            self.assertFalse(self.t.solar_tou_chart_flag(argparse.Namespace(manual_trigger=False, solar_tou_chart="flag",
                                                                            flow=flow)), flow)

    def test_solar_template_failure_is_reported_not_retried_with_decouple(self):
        _use()
        a, _ = self.t.parse("doc", "coaching", ["--manual-trigger", "--email-wait", "1", "--poll", "1"])
        flips, sends = [], []
        run = _run(touConfigured={"at": 1})
        with mock.patch.object(self.t, "tou_pricing", lambda r, add: True), \
                mock.patch.object(self.t, "set_solar_user", lambda r, a, v: flips.append(v)), \
                mock.patch.object(self.t, "set_home_solar", lambda r, v: flips.append(v)), \
                mock.patch.object(mc, "raise_user_attributes", lambda *x, **k: None), \
                mock.patch.object(mc, "apply_user_configs", lambda *x, **k: None), \
                mock.patch.object(mc, "rewind_ms_state", lambda *x, **k: None), \
                mock.patch.object(self.t, "settle", lambda *x: None), mock.patch.object(mc, "aws", lambda *x: mock.Mock()), \
                mock.patch.object(mc, "sqs_send", lambda *x, **k: sends.append(x)), \
                mock.patch.object(self.t, "wait_rows", lambda *x, **k: []), \
                mock.patch.object(self.t, "envlogs_pass", lambda *x: "Missing required templates tou_usage_solar"), \
                mock.patch.object(self.t, "diagnose", lambda *x, **k: []), \
                mock.patch.object(self.t, "home", lambda *x, **k: {"solarUser": True}):     # the pre-send state snapshot read
            checks = self.t.coaching(run, a)
        self.assertEqual(flips, [])                         # home.solarUser never touched
        self.assertEqual(len(sends), 1)                     # one send, no automatic decouple retry
        self.assertIs(run.m["sendSnapshot"]["MONTHLY_SUMMARY"]["solarUser"], True)   # event-time state recorded at the send
        self.assertTrue(any(c.status == "FAIL" for c in checks))

    def test_onboarding_layout_comes_from_the_target_or_input(self):
        _use()
        self.assertFalse(hasattr(self.t, "ONBOARDING_SECTIONS"))
        lst = [{"emailTemplateSection": "HEADER_V9"}, {"emailTemplateSection": "TOU_RATE_STRUCTURE", "mandatory": True}]
        body = {"email_template_sections_config": json.dumps(
            {"kvs": [{"key": "TOU_ONBOARDING.OPT_IN", "val": json.dumps(lst)}, {"key": "TOU_ONBOARDING.OPT_OUT", "val": "[]"}]})}
        with mock.patch.object(self.t, "api", lambda *x, **k: body):
            self.assertEqual(self.t.onboarding_sections("u", None), lst)
        with mock.patch.object(self.t, "api", lambda *x, **k: {}):
            with self.assertRaises(mc.SetupError) as cm:
                self.t.onboarding_sections("u", None)
            self.assertIn("--onboarding-sections", str(cm.exception))
            got = self.t.onboarding_sections("u", "HEADER_V3,TOU_ONBOARDING_WELCOME,TOU_RATE_STRUCTURE")
        self.assertEqual([s["emailTemplateSection"] for s in got], ["HEADER_V3", "TOU_ONBOARDING_WELCOME", "TOU_RATE_STRUCTURE"])
        self.assertTrue(all(s["mandatory"] for s in got if s["emailTemplateSection"] != "HEADER_V3"))

    def test_pilot_tou_values_are_not_copied_from_another_pilot(self):
        _use()
        posts = []

        def api(method, path, body=None, **k):
            if method != "GET":
                posts.append(body)
            return {}
        with mock.patch.object(self.t, "api", api), mock.patch.object(mc, "read_config", lambda *x: {}):
            with self.assertRaises(mc.SetupError) as cm:
                self.t.ensure_pilot_tou(None)
            self.assertIn("NEEDS_INPUT", str(cm.exception))
            self.assertIn("color301", str(cm.exception))
            self.assertEqual(posts, [])                      # nothing written when any value is missing
            vals = {"tou_usage_section_configs.min_peak_usage_for_rendering_in_kwh_solar": "2",
                    **{f"tou_usage_section_configs_palette.color30{i}": f"#00000{i}" for i in range(1, 5)}}
            written = self.t.ensure_pilot_tou(None, vals)
        self.assertEqual(written["tou_usage_section_configs_palette"]["color301"], "#000001")
        self.assertEqual(written["meta_data"], {"enable_tou_chart_for_solar": "true"})


if __name__ == "__main__":
    unittest.main()
