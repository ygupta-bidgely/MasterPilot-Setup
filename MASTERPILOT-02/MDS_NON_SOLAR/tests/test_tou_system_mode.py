"""Offline tests for the system-generated TOU flow: the pilot handle_meter_enrollment window must always close."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mds_common as mc  # noqa: E402


class FakeRun:
    def __init__(self):
        self.m = {}
        self.saved = 0
        self.out = Path("/tmp/fake_run")

    def save(self):
        self.saved += 1

    def mutate(self, *a, **k):
        pass

    def dir(self, name):
        return Path("/nonexistent")


class Args:
    flag_settle = 0
    flag_transition_wait = 1
    poll = 1


class FlagWindowTest(unittest.TestCase):
    """The pilot handle_meter_enrollment window: opened only from a clean 'true', closed only on a matching readback."""

    def setUp(self):
        self.flag = {"val": "true"}
        self.writes = []
        self.sticky = False                       # True = the API accepts writes but the value does not change

        def post_config(run, eid, ctype, values, scope):
            self.writes.append(values[mc.FLAG_KEY])
            if not self.sticky:
                self.flag["val"] = values[mc.FLAG_KEY]

        patches = [
            mock.patch.object(mc, "read_config", lambda *a: {mc.FLAG_KEY: dict(self.flag)}),
            mock.patch.object(mc, "post_config", post_config),
            mock.patch.object(mc, "backup", lambda *a: Path("/tmp/backup.json")),
            mock.patch.object(mc, "log", lambda *a, **k: None),
            mock.patch.object(mc.time, "sleep", lambda s: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.uploads = []
        p = mock.patch.object(mc, "upload", lambda run, s3, bucket, f: self.uploads.append(f.name))
        p.start()
        self.addCleanup(p.stop)

    def run_window(self, transition_results, run=None, sticky_after_open=False):
        results = iter(transition_results)

        def poll(label, fn, wait, every):
            if "readback" in label:
                if sticky_after_open:
                    self.sticky = True
                return fn()[1]
            if not next(results):
                raise mc.WaitTimeout(label)
            return "ok"
        run = run or FakeRun()
        with mock.patch.object(mc, "poll", poll):
            try:
                mc.userenroll_flag_window(run, Args(), None, "b", Path("USERENROLL_T.csv"), None, {"plan": "P"})
            except (mc.WaitTimeout, mc.SetupError) as exc:
                run.error = exc
        return run

    def test_applied_first_upload_flag_restored(self):
        run = self.run_window([True])
        self.assertEqual(self.writes, ["false", "true"])
        self.assertEqual(self.uploads, ["USERENROLL_T.csv"])
        self.assertEqual(run.m["pilotFlagWindow"]["restoredValue"], "true")
        self.assertIn("closedAt", run.m["pilotFlagWindow"])

    def test_cached_flag_reuploads_once(self):
        self.run_window([False, True])
        self.assertEqual(self.uploads, ["USERENROLL_T.csv", "USERENROLL_T.csv"])
        self.assertEqual(self.flag["val"], "true")

    def test_never_applied_flag_still_restored(self):
        run = self.run_window([False, False])
        self.assertEqual(self.flag["val"], "true")
        self.assertIn("closedAt", run.m["pilotFlagWindow"])

    def test_restore_is_idempotent(self):
        run = self.run_window([True])
        mc.restore_pilot_flag(run)
        self.assertEqual(self.writes, ["false", "true"])

    def test_window_refused_unless_flag_reads_true(self):
        for state in ("false", ""):
            self.flag["val"], self.writes = state, []
            run = self.run_window([True])
            self.assertIsInstance(run.error, mc.SetupError)
            self.assertEqual(self.writes, [], f"nothing may be written when the flag reads {state!r}")
            self.assertNotIn("pilotFlagWindow", run.m)

    def test_mismatched_readback_stays_unresolved_and_fails(self):
        run = self.run_window([True], sticky_after_open=True)
        w = run.m["pilotFlagWindow"]
        self.assertIsInstance(run.error, mc.SetupError)
        self.assertIn("NOT restored", str(run.error))
        self.assertIn("--restore-pilot-flag", str(run.error))
        self.assertNotIn("closedAt", w)
        self.assertTrue(w["unresolved"])
        self.assertEqual(len(w["restoreAttempts"]), 3)
        # a later call retries instead of returning early, and closes once the readback matches
        self.sticky = False
        mc.restore_pilot_flag(run)
        self.assertIn("closedAt", w)
        self.assertNotIn("unresolved", w)

    def test_unresolved_window_blocks_a_new_one(self):
        run = FakeRun()
        run.m["pilotFlagWindow"] = {"key": mc.FLAG_KEY, "original": "true", "openedAt": 1, "unresolved": True}
        self.writes = []
        run = self.run_window([True], run=run)
        self.assertIsInstance(run.error, mc.SetupError)
        self.assertEqual(self.writes, [])


class IngestionOnlyTransitionTest(unittest.TestCase):
    """apply_transition(home_api_fallback=False): an ingestion timeout fails with zero schedule mutation."""

    def setUp(self):
        self.home = {"plannumber": "905", "ratePlanId": "FLAT", "solarUser": "true", "ratesSchedule": json.dumps([
            {"rateType": "SQL", "startTime": 0, "endTime": 2147483647, "metaData": {"planNumber": "905"},
             "measurementType": "ELECTRIC"},
            {"rateType": "SQL", "startTime": 0, "endTime": 2147483647, "metaData": {"planNumber": "905"},
             "measurementType": "GAS"}])}
        self.posts = []

        def api(method, path, body=None, **kw):
            if method == "POST":
                self.posts.append((path, body))
                self.home.update(body or {})
            return {"payload": dict(self.home)}
        for p in (mock.patch.object(mc, "api", api), mock.patch.object(mc, "payload", lambda r: (r or {}).get("payload")),
                  mock.patch.object(mc, "log", lambda *a, **k: None), mock.patch.object(mc, "upload", lambda *a: None),
                  mock.patch.object(mc, "raise_user_attributes", lambda *a, **k: None),
                  mock.patch.object(mc, "poll", self.poll)):
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def poll(label, fn, wait, every):
        if "home API" in label:
            return fn()[1]
        raise mc.WaitTimeout(f"{label}: timed out")

    def run_(self, fallback):
        run = FakeRun()
        run.m.update({"uuid": "u", "files": {"USERENROLL_T": "/x/T.csv"}, "transition": {
            "plan": "TOU", "planNumber": 907, "fromPlanNumber": 905, "fromTs": 1785571200}})
        a = Args()
        a.transition_via, a.transition_wait = "userenroll", 1
        before = self.home["ratesSchedule"]
        err = None
        try:
            mc.apply_transition(run, a, None, "b", home_api_fallback=fallback)
        except mc.SetupError as exc:
            err = exc
        return run, before, err

    def gas(self):
        return [e for e in json.loads(self.home["ratesSchedule"]) if e["measurementType"] == "GAS"]

    def test_timeout_without_fallback_fails_with_zero_mutation(self):
        run, before, err = self.run_(False)
        self.assertIsInstance(err, mc.SetupError)
        self.assertIn("NO home API", str(err))
        self.assertEqual(self.posts, [])
        self.assertEqual(self.home["ratesSchedule"], before)
        self.assertFalse(run.m["transitionFailed"]["homeApiWrite"])
        self.assertNotIn("transitionApplied", run.m)

    def test_explicit_fallback_keeps_the_gas_schedule(self):
        gas_before = self.gas()
        run, _, err = self.run_(True)
        self.assertIsNone(err)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.gas(), gas_before)
        elec = [e["metaData"]["planNumber"] for e in json.loads(self.home["ratesSchedule"])
                if e["measurementType"] == "ELECTRIC"]
        self.assertEqual(elec, ["905", "907"])


class KnownIssueTest(unittest.TestCase):
    """Signatures copied from the masterpilot 88002 logs of 2026-10-05."""
    NPE = ('java.lang.NullPointerException: Cannot invoke "api.response.ItemizationWithCostAndHighBill.'
           'getItemizationWithCost()" because the return value of "java.util.Map.get(Object)" is null |  at '
           'notifications.EmailDataProvider.populateSolarEnergyFlowBlock(EmailDataProvider.java:980)')
    TEMPLATE = ('emailers.Emailer:process:206 - pool-12-thread-1 - ERROR : Received exception for user x | '
                'api.exceptions.BidgelyRuntimeException: Could not load template for user: x templateType: Section '
                'templateName: tou_usage_solar_placeholder.vm')

    def classify(self, txt, event="MONTHLY_SUMMARY"):
        import mds_tou
        import re
        return [(what, kind) for rx, what, kind, _, ev in mds_tou.KNOWN_ISSUES if re.search(rx, txt) and event in ev]

    def test_onboarding_npe_is_a_symptom_with_a_check(self):
        """Review 2026-10-06 R3: the NPE names the failing frame, not why its input was empty."""
        hits = self.classify(self.NPE, "TOU_ONBOARDING")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][1], "HYPOTHESIS")
        self.assertIn("candidates", hits[0][0])

    def test_template_load_failure_is_a_symptom_not_a_ticket(self):
        hits = self.classify(self.TEMPLATE)
        self.assertEqual([k for _, k in hits], ["HYPOTHESIS"])
        self.assertIn("wrong entity/locale scope", hits[0][0])

    def test_onboarding_signature_not_blamed_for_the_monthly_summary(self):
        line = "TouOnboardingEmailEventProcessor:processGenericEventData:193 | tou rate structure data not available"
        self.assertEqual(self.classify(line, "MONTHLY_SUMMARY"), [])

    def test_api_outage_is_env(self):
        line = ("RestRetryFilter:handle:54 - ERROR : Rest call attempt 3 failed. URI: https://api-server-masterpilot-"
                "productqa.bidgely.com/v2.0/pii/users/u StatusCode: 503")
        self.assertEqual([k for _, k in self.classify(line)], ["ENV"])

    def test_unrelated_line_matches_nothing(self):
        self.assertEqual(self.classify("INFO : Delivery modes for user u = deliveryModes: [Email]"), [])



class CoachingSectionsTest(unittest.TestCase):
    def sections(self, dcx):
        import mds_tou
        with mock.patch.object(mds_tou, "dcx_ms_sections", lambda u: [mds_tou.sec(x) for x in dcx]):
            return [x["emailTemplateSection"] for x in mds_tou.coaching_sections("u")]

    def test_non_solar_list_gets_no_solar_flow(self):
        got = self.sections(["TEASER", "MONTHLY_SUMMARY_V2", "ITEMIZATION_V2", "TOU_RATE_PROMOTION", "FOOTER_V4"])
        self.assertEqual(got, ["TEASER", "MONTHLY_SUMMARY_V2", "ITEMIZATION_V2", "TOU_USAGE",
                               "TOU_BILL_SAVING_RECO", "FOOTER_V4"])
        got = self.sections(["TEASER", "SOLAR_ENERGY_FLOW", "ITEMIZATION_V2", "FOOTER_V4"])     # a DCX list carrying it
        self.assertNotIn("SOLAR_ENERGY_FLOW", got)
        self.assertEqual(got[got.index("ITEMIZATION_V2") + 1:got.index("ITEMIZATION_V2") + 3], ["TOU_USAGE", "TOU_BILL_SAVING_RECO"])

    def test_idempotent_over_an_edited_list(self):
        once = self.sections(["TEASER", "ITEMIZATION_V2", "FOOTER_V4"])
        self.assertEqual(self.sections(once), once)



class SolarTouChartFlagTest(unittest.TestCase):
    def flag(self, **kw):
        import argparse
        import mds_tou
        base = {"manual_trigger": False, "solar_tou_chart": "decouple", "flow": "all"}
        base.update(kw)
        return mds_tou.solar_tou_chart_flag(argparse.Namespace(**base))

    def test_non_solar_never_opens_the_solar_branch(self):
        for flow in ("onboarding", "all", "coaching"):
            for chart in ("flag", "decouple"):
                self.assertFalse(self.flag(flow=flow, solar_tou_chart=chart), (flow, chart))

    def test_coaching_flow_keeps_it_off_and_decouples_instead(self):
        self.assertFalse(self.flag(flow="coaching"))

    def test_manual_trigger_also_keeps_it_off(self):
        self.assertFalse(self.flag(manual_trigger=True, solar_tou_chart="decouple"))
        self.assertFalse(self.flag(manual_trigger=True, solar_tou_chart="flag"))



class RecoveryTransitionRowTest(unittest.TestCase):
    GAS = "|".join(["c", "a", "p"] + [""] * 24 + ["853743998", "GAS", "2025-09-01", "", "", "2025-09-01", "4", "2025-09-01", "", "AMI", ""])
    ELEC = "|".join(["c", "a", "p"] + [""] * 24 + ["604891179", "ELECTRIC", "2025-09-01", "", "", "2025-09-01", "4", "2025-09-01", "True", "AMI", ""])

    def row(self, text):
        import datetime
        import transition_mds_user_to_tou as tr
        return tr.electric_transition_row(text, "MDS_TOU_EVRR_D_R", datetime.date(2026, 8, 1)).split("|")

    def test_dual_fuel_sends_only_the_electric_row_with_plan_and_date(self):
        r = self.row(self.GAS + "\n" + self.ELEC + "\n")
        self.assertEqual((r[27], r[28], r[31], r[32], r[35]), ("604891179", "ELECTRIC", "MDS_TOU_EVRR_D_R", "2026-08-01", "True"))
        self.assertEqual(len(r), len(self.ELEC.split("|")))

    def test_no_or_ambiguous_electric_row_is_refused(self):
        import mds_common as mc
        with self.assertRaises(mc.SetupError):
            self.row(self.GAS)
        with self.assertRaises(mc.SetupError):
            self.row(self.ELEC + "\n" + self.ELEC)



class RecoveryPreflightTest(unittest.TestCase):
    """transition_mds_user_to_tou.preflight_failures over live-shaped facts (no network), on a fixture target pilot 88002."""

    @classmethod
    def setUpClass(cls):
        import target_fixture
        cls.profile = target_fixture.use(pilot=88002)

    @staticmethod
    def good():
        meter = {"present": True, "active": True, "contractEnd": 0}
        return {
            "manualTrigger": False, "uuid": "7195dc95", "manifestPilotId": 88002, "pilotId": 88002,
            "resolvedUuid": "7195dc95", "uuidMatches": True, "fuels": ["ELECTRIC", "GAS"],
            "meters": {"ELECTRIC:DELIVERED": dict(meter, token="d", solar="false"),
                       "ELECTRIC:RECEIVED": dict(meter, token="r", solar="false"),
                       "GAS:DELIVERED": dict(meter, token="g", solar="false")},
            "homeSolarUser": "false", "electricSchedule": [(0, 2147483647, "905")], "electricCurrentPlan": "905",
            "electricCurrentPlanIsTou": False, "electricLastScheduleStart": 0, "gasSchedule": [(0, 2147483647, "905")],
            "targetPlan": "MDS_TOU_EVRR_D_R", "targetPlanNumber": "907",
            "targetTouPeriods": ["Mid-Peak", "Off-Peak", "On-Peak"], "effectiveDate": "2026-08-01",
            "effectiveTs": 1785571200, "todayTs": 1791144000, "cycleStarts": [1782892800, 1785571200, 1788249600],
            "onboardingSentCount": 0, "recoveryAlreadyApplied": None, "pilotFlag": "true", "unresolvedWindow": False}

    def failures(self, **change):
        import transition_mds_user_to_tou as tr
        f = self.good()
        for k, v in change.items():
            f[k] = v
        return tr.preflight_failures(f)

    def assertBlocked(self, needle, **change):
        bad = self.failures(**change)
        self.assertTrue(any(needle in b for b in bad), f"{needle!r} not in {bad}")

    def test_good_dual_fuel_non_solar_account_passes(self):
        self.assertEqual(self.failures(), [])

    def test_identity(self):
        self.assertBlocked("is not 88002", pilotId=88001)
        self.assertBlocked("not the manifest uuid", uuidMatches=False, resolvedUuid="other")

    def test_both_fuels_and_meters(self):
        self.assertBlocked("dual-fuel", fuels=["ELECTRIC"])
        m = self.good()["meters"]
        m["GAS:DELIVERED"]["active"] = False
        self.assertBlocked("not active", meters=m)
        m2 = self.good()["meters"]
        del m2["GAS:DELIVERED"]
        self.assertBlocked("no GAS:DELIVERED meter", meters=m2)

    def test_non_solar_electric_required(self):
        self.assertBlocked("a non-solar ELECTRIC home is required", homeSolarUser="true")
        m = self.good()["meters"]
        m["ELECTRIC:RECEIVED"]["solar"] = "true"
        self.assertBlocked("ELECTRIC:RECEIVED meter is flagged solar", meters=m)

    def test_current_plan_must_be_non_tou_and_target_tou(self):
        self.assertBlocked("is TOU (or unknown)", electricCurrentPlanIsTou=True)
        self.assertBlocked("is TOU (or unknown)", electricCurrentPlanIsTou=None)
        self.assertBlocked("no On-Peak/Off-Peak", targetTouPeriods=[])
        self.assertBlocked("not found on pilot", targetPlanNumber=None)

    def test_gas_baseline_required(self):
        self.assertBlocked("GAS rate schedule", gasSchedule=[])

    def test_effective_date(self):
        self.assertBlocked("in the future", effectiveTs=1791144000 + 86400)
        self.assertBlocked("not a billing cycle start", effectiveTs=1785571200 + 86400)
        self.assertBlocked("not after the current ELECTRIC schedule start", electricLastScheduleStart=1785571200)

    def test_already_completed_or_sent_is_rejected(self):
        self.assertBlocked("already in the ELECTRIC schedule",
                           electricSchedule=[(0, 1785571200, "905"), (1785571200, 2147483647, "907")])
        self.assertBlocked("already sent", onboardingSentCount=1)
        self.assertBlocked("already applied", recoveryAlreadyApplied={"at": 1})

    def test_manual_trigger_and_open_windows_are_rejected(self):
        self.assertBlocked("--manual-trigger is not allowed", manualTrigger=True)
        self.assertBlocked("another window open", pilotFlag="false")
        self.assertBlocked("unresolved pilot-flag window", unresolvedWindow=True)

    def test_cli_rejects_manual_trigger_before_anything(self):
        import transition_mds_user_to_tou as tr
        self.assertEqual(tr.main(["--profile", str(self.profile), "--manifest", "/nonexistent/manifest.json", "--manual-trigger"]), 2)

    def test_cli_without_profile_is_profile_required(self):
        import mds_target
        import transition_mds_user_to_tou as tr
        saved, mds_target.VIEW = mds_target.VIEW, None
        env = __import__("os").environ.pop("BIDGELY_PROFILE", None)
        try:
            with self.assertRaises(SystemExit) as cm:
                tr.main(["--manifest", "/nonexistent/manifest.json"])
            self.assertEqual(cm.exception.code, 3)
        finally:
            mds_target.VIEW = saved
            if env:
                __import__("os").environ["BIDGELY_PROFILE"] = env


if __name__ == "__main__":
    unittest.main()
