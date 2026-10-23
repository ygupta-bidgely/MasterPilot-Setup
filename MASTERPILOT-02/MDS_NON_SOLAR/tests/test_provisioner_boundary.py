"""Provisioner boundary tests: the REAL provisioning CLI (create_mds_*_user.py) as a child process, with the data-server
API replaced by a recording local stub, boto3 replaced by a recording fake, and every other socket blocked.

They assert what the provisioner ACTUALLY does, not what it says: every API request (method, path, Host), every AWS
session (profile, region), the STS account check, every S3 key/bucket and queue, the rate plan written into
METERENROLL, solar/fuel semantics, the kit write policy + journal, the ingestion configuration never being written, and
manifest ownership (run token, contract digest, pilot). Two different profiles and several mismatched targets are used.

The kit is imported from BIDGELY_KIT_SCRIPTS (default: the installed skill).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import stub_world as SW  # noqa: E402
import target_fixture as TF  # noqa: E402
import mds_target  # noqa: E402

KIT = Path(os.environ.get("BIDGELY_KIT_SCRIPTS") or mds_target.kit_scripts())
sys.path.insert(0, str(KIT))
from emailkit import contract as K  # noqa: E402
from emailkit.auth import template  # noqa: E402
from emailkit.provisioning import build_write_policy, read_journal, verify_manifest  # noqa: E402
from emailkit.target import load_target  # noqa: E402

A = dict(pilot=77001, env="envq", dep="stacka", account="111111111111", region="xx-test-1", aws_profile="qa-a")
B = dict(pilot=77002, env="envr", dep="stackb", account="222222222222", region="yy-test-2", aws_profile="qa-b")
ACCOUNTS = {"qa-a": A["account"], "qa-b": B["account"]}
FAST = ["--poll", "1", "--bc-wait", "20", "--user-wait", "20", "--meter-wait", "20", "--raw-wait", "20", "--invoice-wait", "20"]
PHASES = "preflight,generate,upload,configure"
AUTH_WRITES = {"user_scope": True, "user_strings": True, "home_fields": [], "pilot": [], "pilot_data": ["BC_SCHEDULE"]}
NOTHING = {"user_scope": False, "user_strings": False, "home_fields": [], "pilot": [], "pilot_data": []}
PERMISSIVE = json.dumps({"api": [{"method": m, "path_regex": ".*"} for m in ("POST", "PUT", "DELETE")],
                         "s3_put": {"buckets": ["lp-stacka-envq", "lp-stackb-envr"], "allow_shared": True},
                         "sqs": {"queues": ["Agg-envq-stacka", "Disagg-envq-stacka"]}, "forbidden_ctypes": [],
                         "sql": {"ops": ["erates_image_update"]}})


def batch_plan(batch_id: str = "PB-boundary") -> dict:
    return {"batch_id": batch_id, "scenarios": [{"id": "S1", "variant": "ms.standard", "account": "mds_electric_gas",
                                                 "recipient": "qa+boundary@example.invalid"}]}


def filled_authorization(plan: dict, target, writes: dict | None = None, **over) -> dict:
    """The kit's own template for (plan, target), filled the way a human authorizer does."""
    d = template(plan, target)
    d["recipients"]["allow_regex"] = r"qa\+[a-z0-9._-]+@example\.invalid"
    d["expires"] = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    d["authorized_by"] = "synthetic-boundary-test"
    d["budgets"]["per_batch"] = 10
    d["triggers"]["max_triggers"] = 10
    d["triggers"]["kinds"] = ["queue", "api", "aggregate"]
    d["writes"] = dict(d["writes"], **(AUTH_WRITES if writes is None else writes))
    d.update(over)
    return d


class World:
    """One target: a profile, its stub API, the child's environment and every recording."""

    def __init__(self, spec: dict, stub_pilot: int | None = None, stub_bucket: str | None = None, gas: bool = True, **profile_over):
        self.tmp = Path(tempfile.mkdtemp(prefix="prov-boundary-"))
        self.spec = spec
        dep, env = spec["dep"], spec["env"]
        self.bucket = f"lp-{dep}-{env}"
        self.stub = SW.StubApi(pilot=stub_pilot or spec["pilot"], flat_plan=f"FLAT_{dep.upper()}", tou_plan=f"TOU_{dep.upper()}",
                               bucket=stub_bucket or self.bucket, gas=gas)
        self.profile = TF.make_profile(pilot=spec["pilot"], env=env, dep=dep, api_url=self.stub.url, account=spec["account"],
                                       region=spec["region"], aws_profile=spec["aws_profile"], tmp=self.tmp, **profile_over)
        self.site = SW.write_sitecustomize(self.tmp / "site")
        self.aws_rec, self.journal, self.out = self.tmp / "aws.jsonl", self.tmp / "journal.jsonl", self.tmp / "out"

    def close(self):
        self.stub.close()

    # what the kit computes for this profile (its own code, not a copy); never the caller's BIDGELY_PROFILE
    def target(self):
        return load_target(str(self.profile), {}, environ={})

    def contract(self) -> str:
        return K.digest(K.build(self.target()))

    def policy(self, writes: dict | None = None) -> dict:
        return build_write_policy({"writes": writes or AUTH_WRITES}, K.provisioner_view(self.target()))

    def authorize(self, writes: dict | None = None, plan: dict | None = None, auth_target=None, **over) -> tuple[Path, Path]:
        """Write a real, filled kit authorization + its plan into this World's run dir (what `emailctl run --execute`
        persists) and return their paths. auth_target: bind the authorization to another target (negative tests)."""
        plan = plan or batch_plan()
        run_dir = self.tmp / "run"
        run_dir.mkdir(exist_ok=True)
        doc = filled_authorization(plan, auth_target or self.target(), writes, **over)
        (run_dir / "plan.json").write_text(json.dumps(plan))
        (run_dir / "authorization.json").write_text(json.dumps(doc))
        return run_dir / "authorization.json", run_dir / "plan.json"

    def run(self, *args, script="create_mds_single_fuel_user.py", profile_arg=True, env_over: dict | None = None,
            writes: dict | None = None, authorize: bool = True, contract: str | None = None,
            token="tok-" + "a" * 8) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("BIDGELY_", "STUB_", "AWS_"))}
        env.update(PYTHONPATH=str(self.site), STUB_ALLOW=f"127.0.0.1:{self.stub.port}", STUB_RECORD=str(self.aws_rec),
                   STUB_ACCOUNTS=json.dumps(ACCOUNTS), BIDGELY_KIT_SCRIPTS=str(KIT), BIDGELY_WRITE_JOURNAL=str(self.journal),
                   BIDGELY_RUN_TOKEN=token, BIDGELY_CONTRACT_SHA256=contract or self.contract())
        if authorize:
            ap, pp = self.authorize(writes)
            env.update(BIDGELY_AUTHORIZATION=str(ap), BIDGELY_PLAN=str(pp))
        env.update(env_over or {})
        env = {k: v for k, v in env.items() if v is not None}
        argv = [sys.executable, str(ROOT / script)] + (["--profile", str(self.profile)] if profile_arg else [])
        argv += ["--email", "qa+boundary@example.invalid", "--output-dir", str(self.out), *args]
        return subprocess.run(argv, capture_output=True, text=True, env=env, timeout=600, cwd=str(ROOT.parent))

    @property
    def aws(self) -> list[dict]:
        return [json.loads(x) for x in self.aws_rec.read_text().splitlines()] if self.aws_rec.exists() else []

    @property
    def api(self) -> list[dict]:
        return list(self.stub.records)

    @property
    def writes(self) -> list[dict]:
        return read_journal(self.journal)

    def puts(self) -> list[dict]:
        return [r for r in self.aws if r.get("op") == "put_object"]

    def tail(self, p) -> str:
        return (p.stdout + p.stderr)[-1500:]


class TwoProfiles(unittest.TestCase):
    """The same CLI, two different profiles: every observable destination follows the profile."""

    @classmethod
    def setUpClass(cls):
        cls.w = {}
        for name, spec in (("A", A), ("B", B)):
            w = World(spec)
            w.proc = w.run("--phases", PHASES, *FAST)
            cls.w[name] = w

    @classmethod
    def tearDownClass(cls):
        for w in cls.w.values():
            w.close()

    def each(self):
        return list(self.w.values())        # assertion messages name the pilot (profiles differ in every value)

    def test_runs_complete(self):
        for w in self.each():
            self.assertEqual(w.proc.returncode, 0, w.tail(w.proc))

    def test_api_requests_go_to_the_profile_target_only(self):
        for w in self.each():
            self.assertTrue(w.api)
            for r in w.api:
                self.assertEqual(r["host"], f"127.0.0.1:{w.stub.port}")
                for pilot in re.findall(r"(?:pilot/|utilityId/|utilities/|utility/)(\d+)", r["path"]):
                    self.assertEqual(int(pilot), w.spec["pilot"], r["path"])
                if r["query"].get("pilotId"):
                    self.assertEqual(int(r["query"]["pilotId"]), w.spec["pilot"])

    def test_aws_identity_profile_region(self):
        for w in self.each():
            sessions = [r for r in w.aws if r.get("svc") == "session"]
            self.assertTrue(sessions)
            self.assertEqual({(r["profile"], r["region"]) for r in sessions}, {(w.spec["aws_profile"], w.spec["region"])})
            self.assertTrue([r for r in w.aws if r.get("op") == "get_caller_identity"])
            self.assertFalse([r for r in w.aws if "blocked_connect" in r], "the provisioner tried to reach another host")

    def test_s3_destinations(self):
        for w in self.each():
            puts = w.puts()
            self.assertTrue(puts)
            self.assertEqual({p["Bucket"] for p in puts}, {w.bucket})
            kinds = {p["Key"].split("_")[0] for p in puts}
            self.assertEqual(kinds, {"BC", "USERENROLL", "METERENROLL", "RAW", "INVOICE"})
            for p in puts:
                self.assertIn(str(w.spec["pilot"]), p["Key"])
                self.assertEqual((p.get("Metadata") or {}).get("utility_file_name"), p["Key"])   # launchpad ingestion contract

    def test_rate_plan_mapping_comes_from_the_profile(self):
        for w in self.each():
            self.assertEqual(w.stub.home_plan, f"FLAT_{w.spec['dep'].upper()}")
            self.assertIn(f"v3.0/rates/utilities/{w.spec['pilot']}/plans/9101/structure", [r["path"] for r in w.api])

    def test_mds_streams(self):
        """An MDS non-solar premise still has DELIVERED + RECEIVED electric streams and both RAW files."""
        for w in self.each():
            streams = {m["streamType"] for m in w.stub.meters.values() if m["fuel"] == "ELECTRIC"}
            self.assertEqual(streams, {"DELIVERED", "RECEIVED"})
            raw = [p["Key"] for p in w.puts() if p["Key"].startswith("RAW_")]
            self.assertTrue(any("_D_" in k for k in raw) and any("_R_" in k for k in raw), raw)

    def test_non_solar_semantics(self):
        """What was uploaded is non-solar: SOLAR_USER FALSE (USERENROLL 35, METERENROLL 11), RAW_R all zero,
        FEED_IN 0 kWh / $0; the uploaded bytes are the run's files; no solar user-scope key is ever written."""
        import hashlib
        solar_keys = ("enable_solar", "trigger_ami_disagg_for_solar_user", "disagg_solar_estimation_data",
                      "show_cost_break_down_for_solar_users", "solar_display_value_identifier")
        for w in self.each():
            run = sorted(w.out.glob("run_*"))[-1] / "input_files"
            files = {f.name: f for f in run.iterdir()}
            for p in w.puts():
                self.assertEqual(hashlib.sha256(files[p["Key"]].read_bytes()).hexdigest(), p["sha256"], p["Key"])
            ue = next(v for k, v in files.items() if k.startswith("USERENROLL"))
            me = next(v for k, v in files.items() if k.startswith("METERENROLL"))
            self.assertEqual({ln.split("|")[35] for ln in ue.read_text().splitlines() if ln.strip()}, {"FALSE"})
            self.assertEqual({ln.split("|")[11] for ln in me.read_text().splitlines() if ln.strip()}, {"FALSE"})
            rr = next(v for k, v in files.items() if k.startswith("RAW_") and "_R_" in k)
            self.assertTrue(all(float(ln.split("|")[5]) == 0 for ln in rr.read_text().splitlines() if ln.strip()))
            feed = [ln for ln in next(v for k, v in files.items() if k.startswith("INVOICE")).read_text().splitlines()
                    if "|FEED_IN|" in ln]
            self.assertTrue(feed and all(ln.split("|")[11:13] == ["0.000", "0.00"] for ln in feed), feed[:2])
            for r in w.api:
                if r["method"] != "GET":
                    blob = json.dumps(r.get("body"))
                    self.assertFalse([k for k in solar_keys if k in blob], r["path"])

    def test_every_write_journalled_and_allowed(self):
        for w in self.each():
            j = w.writes
            self.assertTrue(j)
            self.assertEqual([x for x in j if not x["allowed"]], [])
            mutating = [r for r in w.api if r["method"] != "GET"]
            self.assertEqual(len([x for x in j if x["kind"] == "api"]), len(mutating))     # nothing bypassed the policy
            self.assertEqual(len([x for x in j if x["kind"] == "s3_put"]), len(w.puts()))

    def test_ingestion_configuration_is_preserved(self):
        for w in self.each():
            for r in w.api:
                if r["method"] != "GET":
                    self.assertNotIn(f"entities/{w.spec['pilot']}/configs", r["path"], "pilot-level write")
                    self.assertNotEqual((r["body"] or {}).get("configType") if isinstance(r["body"], dict) else None,
                                        "launchpad_ingestion_configs")

    def test_user_writes_pinned_to_the_created_user(self):
        for w in self.each():
            uuids = {m for r in w.api if r["method"] != "GET"
                     for m in re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", r["path"])}
            self.assertLessEqual(len(uuids), 1, uuids)

    def test_manifest_ownership(self):
        for w in self.each():
            _, m = verify_manifest(w.out, "tok-" + "a" * 8, w.contract(), w.spec["pilot"])
            self.assertEqual(m["target"]["pilot"], w.spec["pilot"])
            self.assertEqual(m["uuid"], next(iter(w.stub.users.values())))

    def test_print_target_equals_the_kit_view(self):
        for w in self.each():
            p = w.run("--print-target")
            self.assertEqual(p.returncode, 0, w.tail(p))
            printed = json.loads(p.stdout.strip().splitlines()[-1])
            self.assertEqual(K.compare_view(K.provisioner_view(w.target()), printed), [])
            self.assertEqual(printed["code_sha256"], K.build(w.target())["provisioner_code_sha256"])
            self.assertFalse(printed.get("_inconsistent"))

    def test_profiles_really_differ(self):
        a, b = self.w["A"], self.w["B"]
        self.assertNotEqual(a.contract(), b.contract())
        self.assertNotEqual({p["Bucket"] for p in a.puts()}, {p["Bucket"] for p in b.puts()})


class FailClosed(unittest.TestCase):
    """Mismatched / incomplete targets stop before any write; nothing is assumed."""

    def setUp(self):
        self.w = None

    def tearDown(self):
        if self.w:
            self.w.close()

    def nothing_written(self, w):
        self.assertEqual(w.puts(), [])
        self.assertEqual([r for r in w.api if r["method"] != "GET"], [])
        self.assertFalse([r for r in w.aws if r.get("op") == "send_message"])

    def test_no_profile_is_profile_required(self):
        self.w = w = World(A)
        p = w.run("--phases", PHASES, *FAST, profile_arg=False, env_over={"BIDGELY_PROFILE": None})
        self.assertEqual(p.returncode, 3, w.tail(p))
        self.assertIn("PROFILE_REQUIRED", p.stdout + p.stderr)
        self.assertEqual(w.api, [])
        self.assertEqual(w.aws, [])

    def test_bidgely_profile_env_is_honoured(self):
        self.w = w = World(A)
        p = w.run("--print-target", profile_arg=False, env_over={"BIDGELY_PROFILE": str(w.profile)})
        self.assertEqual(p.returncode, 0, w.tail(p))
        self.assertEqual(json.loads(p.stdout.strip().splitlines()[-1])["pilot"], A["pilot"])

    def test_contract_mismatch_stops_before_network(self):
        self.w = w = World(A)
        p = w.run("--phases", PHASES, *FAST, contract="0" * 64)
        self.assertEqual(p.returncode, 3, w.tail(p))
        self.assertEqual(w.api, [])
        self.assertEqual([r for r in w.aws if r.get("svc") != "session"], [])

    def test_other_profiles_contract_is_a_mismatch(self):
        self.w = w = World(A)
        other = World(B)
        try:
            p = w.run("--phases", PHASES, *FAST, contract=other.contract())
        finally:
            other.close()
        self.assertEqual(p.returncode, 3)
        self.assertEqual(w.api, [])

    def test_wrong_aws_account_stops_before_writes(self):
        self.w = w = World(dict(A, account="999999999999"))       # profile declares 999..., qa-a resolves to 111...
        p = w.run("--phases", PHASES, *FAST)
        self.assertNotEqual(p.returncode, 0)
        self.assertRegex(p.stdout + p.stderr, r"999999999999|account")
        self.nothing_written(w)

    def test_incomplete_profile_is_blocked(self):
        self.w = w = World(A, **{"email.rate_plans": None})
        p = w.run("--phases", PHASES, *FAST, contract="x", authorize=False)
        self.assertEqual(p.returncode, 3, w.tail(p))
        self.assertIn("rate_plans", p.stdout + p.stderr)
        self.assertEqual(w.api, [])

    def test_server_of_another_pilot(self):
        """Profile says pilot 77001 but the API answers as pilot 77002: preflight finds no config and stops."""
        self.w = w = World(A, stub_pilot=B["pilot"])
        p = w.run("--phases", PHASES, *FAST)
        self.assertNotEqual(p.returncode, 0)
        self.nothing_written(w)

    def test_pilot_bucket_disagreement(self):
        self.w = w = World(A, stub_bucket="lp-somewhere-else")
        p = w.run("--phases", PHASES, *FAST)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("lp-somewhere-else", p.stdout + p.stderr)
        self.nothing_written(w)

    def test_bucket_override_cannot_redirect(self):
        self.w = w = World(A)
        p = w.run("--phases", PHASES, *FAST, "--bucket", "lp-attacker")
        self.assertNotEqual(p.returncode, 0)
        self.nothing_written(w)

    def test_bc_schedule_refused_without_shared_data_authorization(self):
        self.w = w = World(A)
        p = w.run("--phases", PHASES, *FAST, writes=dict(AUTH_WRITES, pilot_data=[]))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("WRITE_REFUSED", p.stdout + p.stderr)
        self.assertFalse([x for x in w.puts() if x["Key"].startswith("BC_SCHEDULE")])
        refused = [x for x in w.writes if not x["allowed"]]
        self.assertEqual(len(refused), 1)
        self.assertTrue(refused[0]["key"].startswith("BC_SCHEDULE"))

    def test_pilot_ingestion_write_refused(self):
        """--ensure-gas-stream would rewrite launchpad_ingestion_configs.supported_streams: the policy refuses it."""
        self.w = w = World(A, gas=False)
        p = w.run("--phases", PHASES, *FAST, "--ensure-gas-stream", script="create_mds_dual_fuel_user.py")
        self.assertNotEqual(p.returncode, 0)
        self.assertFalse([r for r in w.api if r["method"] != "GET" and isinstance(r["body"], dict)
                          and r["body"].get("configType") == "launchpad_ingestion_configs"])
        self.assertTrue([x for x in w.writes if not x["allowed"]])

    def test_write_outside_policy_refused(self):
        """An authorization that grants no write scope: API mutations and shared pilot data are refused; nothing reaches
        the stub or S3."""
        self.w = w = World(A)
        p = w.run("--phases", PHASES, *FAST, writes=NOTHING)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("WRITE_REFUSED", p.stdout + p.stderr)
        self.nothing_written(w)
        self.assertTrue([x for x in w.writes if not x["allowed"]])

    def test_standalone_run_without_kit_policy_is_a_read_only_preview(self):
        """A colleague running the script directly (no kit authorization -> no policy): reads only, every write refused."""
        self.w = w = World(A)
        p = w.run("--phases", PHASES, *FAST, authorize=False, env_over={"BIDGELY_CONTRACT_SHA256": None,
                                                                         "BIDGELY_RUN_TOKEN": None})
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("PREVIEW_MODE", p.stdout + p.stderr)
        self.nothing_written(w)
        self.assertTrue(all(not x["allowed"] for x in w.writes))

    def test_positional_upload_file_key_is_checked(self):
        import mds_target as MT
        calls = []

        class C:
            def upload_file(self, *a, **k):
                calls.append(a)
        pol = {"s3_put": {"buckets": ["b1"], "shared_key_regex": "^shared/", "allow_shared": False}}
        with mock.patch.object(MT, "policy_status", return_value=(pol, "")):
            g = MT._Guarded(C(), "s3", {})
            g.upload_file("/tmp/f", "b1", "users/x.csv")
            with self.assertRaises(MT.WriteRefused):
                g.upload_file("/tmp/f", "b1", "shared/BC_SCHEDULE.txt")
        self.assertEqual(len(calls), 1)


class AuthorizationBinding(unittest.TestCase):
    """H2: a policy is never taken from a variable; the provisioner validates the kit authorization itself and computes
    its policy. Every failure is a read-only preview with zero writes."""

    def setUp(self):
        self.w = World(A)
        self.addCleanup(self.w.close)

    def preview(self, p, needle: str | None = None):
        w, out = self.w, p.stdout + p.stderr
        self.assertNotEqual(p.returncode, 0, w.tail(p))
        self.assertIn("WRITE_REFUSED", out)
        self.assertIn("PREVIEW_MODE", out)
        if needle:
            self.assertIn(needle, out)
        self.assertEqual(w.puts(), [])
        self.assertEqual([r for r in w.api if r["method"] != "GET"], [])
        self.assertFalse([r for r in w.aws if r.get("op") == "send_message"])
        self.assertTrue(w.writes, "the refused write must be journalled")
        self.assertEqual([x for x in w.writes if x["allowed"]], [])

    def test_a_permissive_write_policy_variable_alone_grants_nothing(self):
        p = self.w.run("--phases", PHASES, *FAST, authorize=False, env_over={"BIDGELY_WRITE_POLICY": PERMISSIVE})
        self.preview(p, "BIDGELY_WRITE_POLICY is ignored")

    def test_a_write_policy_variable_cannot_widen_a_valid_authorization(self):
        """Valid authorization without shared pilot data + a permissive variable: BC_SCHEDULE is still refused."""
        w = self.w
        p = w.run("--phases", PHASES, *FAST, writes=dict(AUTH_WRITES, pilot_data=[]),
                  env_over={"BIDGELY_WRITE_POLICY": PERMISSIVE})
        self.assertNotEqual(p.returncode, 0)
        self.assertFalse([x for x in w.puts() if x["Key"].startswith("BC_SCHEDULE")])
        refused = [x for x in w.writes if not x["allowed"]]
        self.assertEqual(len(refused), 1)
        self.assertIn("shared pilot data", refused[0]["why"])

    def test_b_expired_authorization(self):
        ap, pp = self.w.authorize(expires="2020-01-01T00:00:00Z")
        p = self.w.run("--phases", PHASES, *FAST, authorize=False, env_over={"BIDGELY_AUTHORIZATION": str(ap),
                                                                              "BIDGELY_PLAN": str(pp)})
        self.preview(p, "expired")

    def test_c_authorization_for_another_plan(self):
        ap, pp = self.w.authorize()
        pp.write_text(json.dumps(batch_plan("PB-another-batch")))         # the run's plan is not the authorized one
        p = self.w.run("--phases", PHASES, *FAST, authorize=False, env_over={"BIDGELY_AUTHORIZATION": str(ap),
                                                                              "BIDGELY_PLAN": str(pp)})
        self.preview(p, "plan_sha256")

    def test_c_authorization_for_another_contract(self):
        other = World(B)
        try:
            ap, pp = self.w.authorize(auth_target=other.target())          # authorized for profile B, run on profile A
        finally:
            other.close()
        p = self.w.run("--phases", PHASES, *FAST, authorize=False, env_over={"BIDGELY_AUTHORIZATION": str(ap),
                                                                              "BIDGELY_PLAN": str(pp)})
        self.preview(p, "contract_sha256")

    def test_unfilled_template_is_not_an_authorization(self):
        ap, pp = self.w.authorize()
        doc = json.loads(ap.read_text())
        doc["authorized_by"] = "<REQUIRED: the human authorizing this batch>"
        ap.write_text(json.dumps(doc))
        p = self.w.run("--phases", PHASES, *FAST, authorize=False, env_over={"BIDGELY_AUTHORIZATION": str(ap),
                                                                              "BIDGELY_PLAN": str(pp)})
        self.preview(p, "placeholders")

    def test_valid_authorization_without_the_kit_run_token(self):
        p = self.w.run("--phases", PHASES, *FAST, env_over={"BIDGELY_RUN_TOKEN": None})
        self.preview(p, "BIDGELY_RUN_TOKEN")

    def test_missing_authorization_file(self):
        p = self.w.run("--phases", PHASES, *FAST, authorize=False,
                       env_over={"BIDGELY_AUTHORIZATION": str(self.w.tmp / "nope.json"),
                                 "BIDGELY_PLAN": str(self.w.tmp / "nope-plan.json")})
        self.preview(p, "unreadable")


class _Rec:
    """A boto3-like client that records every call that reaches it."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def f(*a, **k):
            self.calls.append(name)
            return {"QueueUrl": f"https://q.invalid/{k.get('QueueName')}"} if name == "get_queue_url" else {}
        return f


S3_ADMIN = ["put_object_acl", "put_object_tagging", "delete_object_tagging", "put_bucket_policy", "put_bucket_acl",
            "put_bucket_lifecycle_configuration", "put_bucket_notification_configuration", "put_bucket_versioning",
            "delete_bucket", "create_bucket", "delete_object", "delete_objects", "restore_object", "_make_api_call"]
S3_UPLOADS = ["create_multipart_upload", "upload_part", "upload_part_copy", "complete_multipart_upload", "copy_object"]
SQS_ADMIN = ["change_message_visibility", "change_message_visibility_batch", "set_queue_attributes", "create_queue",
             "delete_queue", "tag_queue", "untag_queue", "purge_queue", "delete_message", "delete_message_batch",
             "receive_message", "add_permission"]


class GuardCoverage(unittest.TestCase):
    """M3: every S3/SQS mutation boto3 offers is guarded (deny by default); resource() is refused."""

    def setUp(self):
        import mds_target as MT
        self.MT = MT
        self.journal = Path(tempfile.mkdtemp()) / "j.jsonl"
        env = {k: v for k, v in os.environ.items() if k not in ("BIDGELY_AUTHORIZATION", "BIDGELY_PLAN", "BIDGELY_WRITE_POLICY")}
        env["BIDGELY_WRITE_JOURNAL"] = str(self.journal)
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def kit_policy(self):
        w = World(A)
        self.addCleanup(w.close)
        return w.policy()                                   # the kit's build_write_policy for a full authorization

    def journal_rows(self):
        return read_journal(self.journal)

    def test_d_new_ops_refused_without_policy(self):
        for svc, ops in (("s3", S3_ADMIN + S3_UPLOADS), ("sqs", SQS_ADMIN + ["send_message", "send_message_batch"])):
            for op in ops:
                with self.subTest(svc=svc, op=op):
                    rec = _Rec()
                    g = self.MT._Guarded(rec, svc, {})
                    with self.assertRaises(self.MT.WriteRefused):
                        getattr(g, op)(Bucket="lp-stacka-envq", Key="USERENROLL_x.csv", QueueUrl="https://q.invalid/Agg-envq-stacka")
                    self.assertEqual(rec.calls, [])
        rows = self.journal_rows()
        self.assertEqual(len(rows), len(S3_ADMIN + S3_UPLOADS + SQS_ADMIN) + 2)
        self.assertFalse([r for r in rows if r["allowed"]])

    def test_d_admin_ops_refused_even_under_a_kit_policy(self):
        pol = self.kit_policy()
        bucket, queue = pol["s3_put"]["buckets"][0], pol["sqs"]["queues"][0]
        with mock.patch.object(self.MT, "policy_status", return_value=(pol, "")):
            for svc, ops in (("s3", S3_ADMIN), ("sqs", SQS_ADMIN)):
                for op in ops:
                    with self.subTest(svc=svc, op=op):
                        rec = _Rec()
                        urls = {}
                        g = self.MT._Guarded(rec, svc, urls)
                        qurl = g.get_queue_url(QueueName=queue)["QueueUrl"] if svc == "sqs" else None
                        with self.assertRaises(self.MT.WriteRefused) as cm:
                            getattr(g, op)(Bucket=bucket, Key="USERENROLL_x.csv", QueueUrl=qurl)
                        self.assertIn("never authorized", str(cm.exception))
                        self.assertNotIn(op, rec.calls)
            # multipart/copy uploads follow the same bucket + shared-key rules as put_object
            rec = _Rec()
            g = self.MT._Guarded(rec, "s3", {})
            for op in S3_UPLOADS:
                getattr(g, op)(Bucket=bucket, Key="USERENROLL_x.csv")
                with self.assertRaises(self.MT.WriteRefused):
                    getattr(g, op)(Bucket="another-bucket", Key="USERENROLL_x.csv")
            self.assertEqual(rec.calls, S3_UPLOADS)
            # sends: only to an authorized queue resolved through get_queue_url
            rec = _Rec()
            g = self.MT._Guarded(rec, "sqs", {})
            g.send_message(QueueUrl=g.get_queue_url(QueueName=queue)["QueueUrl"], MessageBody="{}")
            with self.assertRaises(self.MT.WriteRefused):
                g.send_message(QueueUrl=g.get_queue_url(QueueName="Some-other-queue")["QueueUrl"], MessageBody="{}")
            self.assertEqual(rec.calls.count("send_message"), 1)

    def test_reads_pass_through(self):
        rec = _Rec()
        g = self.MT._Guarded(rec, "s3", {})
        for op in ("head_object", "get_object", "list_objects_v2", "generate_presigned_url", "download_file"):
            getattr(g, op)(Bucket="b", Key="k")
        self.assertEqual(len(rec.calls), 5)
        self.assertEqual(self.journal_rows(), [])

    def test_e_resource_api_refused(self):
        class Sess:
            def __init__(self):
                self.calls = []

            def resource(self, *a, **k):
                self.calls.append("resource")

            def client(self, *a, **k):
                self.calls.append("client")
                return _Rec()
        for pol in (None, self.kit_policy()):
            with self.subTest(policy=bool(pol)), mock.patch.object(self.MT, "policy_status", return_value=(pol, "test")):
                sess = Sess()
                gs = self.MT.GuardedSession(sess)
                for svc in ("s3", "sqs", "dynamodb"):
                    with self.assertRaises(self.MT.WriteRefused):
                        gs.resource(svc)
                self.assertEqual(sess.calls, [])
                self.assertIsInstance(gs.client("sts"), self.MT._Guarded)        # every client is guarded

    def test_f_erates_sql_update_refused(self):
        import argparse
        import create_mds_nve_tou_rate_plan as NP
        import mds_common as mc
        a = argparse.Namespace(no_image_sql=False, plan_number=12345)
        body = {"components": [{"rates": [{"groupName": "Summer", "rateStructureImageUrl": "https://img.invalid/s.png"}]}]}
        ok = subprocess.CompletedProcess([], 0, "1 row", "")
        for pol, needle in ((None, "PREVIEW_MODE"), (self.kit_policy(), "not in the authorized write scope")):
            with self.subTest(policy=bool(pol)), mock.patch.object(self.MT, "policy_status", return_value=(pol, "test")), \
                    mock.patch.object(NP, "DBQ_PY", Path(sys.executable)), mock.patch.object(NP.subprocess, "run", return_value=ok) as run:
                with self.assertRaises(mc.SetupError) as cm:
                    NP.set_images_sql(a, body)
                self.assertIn("WRITE_REFUSED", str(cm.exception))
                self.assertIn(needle, str(cm.exception))
                run.assert_not_called()
        sql = [r for r in self.journal_rows() if r["kind"] == "sql"]
        self.assertEqual(len(sql), 2)
        self.assertFalse([r for r in sql if r["allowed"]])
        # the only way through: a policy that names the operation explicitly (no kit authorization produces one)
        with mock.patch.object(self.MT, "policy_status", return_value=({"sql": {"ops": ["erates_image_update"]}}, "")), \
                mock.patch.object(NP, "DBQ_PY", Path(sys.executable)), mock.patch.object(NP.subprocess, "run", return_value=ok) as run:
            NP.set_images_sql(a, body)
            run.assert_called_once()


class NveSourceProfile(unittest.TestCase):
    """M3: the NVE port scripts have no built-in source host/pilot/plan/token: without --source-profile they stop before
    any network call (no API request, no AWS session)."""

    def test_g_nve_scripts_refuse_without_source_profile(self):
        for script in ("create_mds_nve_tou_rate_plan.py", "copy_nve_rates_to_mds.py"):
            with self.subTest(script=script):
                w = World(A)
                try:
                    p = w.run("--phases", "source", "--refresh-source", script=script, authorize=False,
                              env_over={"NVE_UAT_TOKEN": "synthetic-not-used"})
                    self.assertEqual(p.returncode, 2, w.tail(p))
                    self.assertIn("BLOCKED: source target must come from a --source-profile", p.stderr)
                    self.assertEqual(w.api, [])
                    self.assertEqual(w.aws, [])
                    self.assertEqual(w.writes, [])
                finally:
                    w.close()

    def test_source_comes_from_the_source_profile(self):
        import create_mds_nve_tou_rate_plan as NP
        w = World(B)
        try:
            src = NP.load_source(str(w.profile))
            self.assertEqual(src["base"], w.stub.url)
            self.assertEqual(src["pilot"], B["pilot"])
            self.assertEqual(src["token_file"], str(w.tmp / "token"))
            argv, prof = NP.take_source_profile(["--x", "1", "--source-profile", str(w.profile)])
            self.assertEqual((argv, prof), (["--x", "1"], str(w.profile)))
            self.assertEqual(NP.take_source_profile(["--source-plan", "3"]), (["--source-plan", "3"], None))
            with mock.patch.dict(os.environ, {"NVE_UAT_TOKEN": "synthetic-env-token"}), mock.patch.object(NP, "SOURCE_TOKEN_FILE", None):
                self.assertEqual(NP.source_token(None), "")                  # an env variable is never a source token
        finally:
            w.close()


class DualFuel(unittest.TestCase):
    def test_dual_fuel_creates_gas_and_non_solar_electric(self):
        w = World(A)
        try:
            p = w.run("--phases", PHASES, *FAST, script="create_mds_dual_fuel_user.py")
            self.assertEqual(p.returncode, 0, w.tail(p))
            fuels = {m["fuel"] for m in w.stub.meters.values()}
            self.assertEqual(fuels, {"ELECTRIC", "GAS"})
            self.assertEqual({m["streamType"] for m in w.stub.meters.values() if m["fuel"] == "GAS"}, {"DELIVERED"})
            self.assertEqual({m["streamType"] for m in w.stub.meters.values() if m["fuel"] == "ELECTRIC"}, {"DELIVERED", "RECEIVED"})
            self.assertEqual({p_["Bucket"] for p_ in w.puts()}, {w.bucket})
            self.assertEqual([x for x in w.writes if not x["allowed"]], [])
        finally:
            w.close()


if __name__ == "__main__":
    unittest.main()
