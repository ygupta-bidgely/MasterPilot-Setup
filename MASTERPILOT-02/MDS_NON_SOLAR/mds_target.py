"""The ONE effective target of a provisioning run (debug-kit execution contract). No module carries an environment,
deployment, pilot, endpoint, AWS identity, bucket, queue, schema or rate plan of its own any more.

Resolution: `--profile <name|path>` on the command line, else BIDGELY_PROFILE. Neither -> PROFILE_REQUIRED (exit 3)
before any network call or write. The profile is loaded and validated by the debug kit (emailkit.target), and the
provisioner's values come from emailkit.contract.provisioner_view -- the same mapping the kit compares against.

    bootstrap(argv) -> argv without --profile/--print-target; applies the target to every loaded module
    --print-target  -> prints the values the modules ACTUALLY hold after bootstrap (read back from their globals) and
                       the code hash, then exits 0: the kit refuses to provision unless they equal its own view.
When the kit launches a run it also sets BIDGELY_CONTRACT_SHA256; a mismatch with the profile loaded here stops.
Writes: only under a kit authorization this module validates itself (BIDGELY_AUTHORIZATION + BIDGELY_PLAN +
BIDGELY_RUN_TOKEN, see "write policy" below); otherwise the run is a read-only preview. BIDGELY_WRITE_POLICY is ignored.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCOPE_PATH = HERE / "project_scope.json"      # the pilots THIS project directory may target (see check_project_scope)
SKILL_DIR = next((d for d in (Path.home() / ".claude/skills/config-fail-testcase-debugger",) if d.is_dir()),
                 Path.home() / ".claude/skills/config-fail-testcase-debugger")
MODULES = ("mds_common", "mds_tou", "mds_ba", "mds_bb", "mds_her", "mds_paper_her")
VIEW: dict | None = None          # the applied view (None until bootstrap)
PROFILE_PATH: str | None = None
_EXPORTED: str | None = None      # the contract digest THIS process exported for its children (not a kit authorization)
_TARGET = None                    # the kit Target loaded by resolve() (None until a profile is loaded)


class ProfileRequired(SystemExit):
    def __init__(self, msg: str):
        print(f"PROFILE_REQUIRED: {msg}", file=sys.stderr)
        super().__init__(3)


class TargetError(SystemExit):
    def __init__(self, msg: str):
        print(f"TARGET_ERROR: {msg}", file=sys.stderr)
        super().__init__(3)


def kit_scripts() -> str:
    """The ONE place the kit is found (BIDGELY_KIT_SCRIPTS for a kit checkout, else the installed skill)."""
    return os.environ.get("BIDGELY_KIT_SCRIPTS") or str(SKILL_DIR / "scripts")


def _kit():
    p = kit_scripts()
    if p not in sys.path:
        sys.path.insert(0, p)
    from emailkit import contract, target       # noqa: E402  (the kit owns profile loading and the mapping)
    return contract, target


def resolve(argv: list[str]) -> tuple[dict, list[str]]:
    argv = list(argv)
    prof = None
    if "--profile" in argv:
        i = argv.index("--profile")
        if i + 1 >= len(argv):
            raise ProfileRequired("--profile needs a value")
        prof = argv[i + 1]
        del argv[i:i + 2]
    prof = prof or os.environ.get("BIDGELY_PROFILE")
    if not prof:
        raise ProfileRequired("pass --profile <name|path> or export BIDGELY_PROFILE (no target is assumed)")
    contract, target = _kit()
    try:
        t = target.load_target(prof, {}, environ={k: v for k, v in os.environ.items() if k.startswith("IDBG_")})
        target.require_resolved(t)
        view = contract.provisioner_view(t)
        want = os.environ.get("BIDGELY_CONTRACT_SHA256")
        if want and want == _EXPORTED:
            want = None                # our own export (a later explicit --profile in the same process), not the kit's
        if want and want != contract.digest(contract.build(t)):
            raise TargetError("the profile loaded here does not match the execution contract the kit authorized "
                              f"({want[:12]}): profile or provisioner code changed since authorization")
    except SystemExit:
        raise
    except Exception as e:           # noqa: BLE001 - any profile problem stops before network calls
        raise TargetError(f"profile {prof}: {getattr(e, 'message', e)}")
    global PROFILE_PATH, _TARGET
    PROFILE_PATH = t.settings.profile_path
    _TARGET = t                        # the authorization is validated against THIS target (never a variable)
    return view, argv


def apply(view: dict) -> None:
    """Hand the view to every loaded provisioning module; each sets its own globals in `_retarget`."""
    global VIEW
    VIEW = view
    for name in MODULES:
        mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, "_retarget"):
            mod._retarget(view)


def require() -> dict:
    if VIEW is None:
        raise ProfileRequired("this operation needs a target: call through a create_/send_ script with a profile")
    return VIEW


def code_sha() -> str:
    """Hash of the modules actually running (the kit's rule, applied to this directory)."""
    contract, _ = _kit()
    return contract.code_sha_of_dir(HERE)


def describe() -> dict:
    """Read back the values the modules actually hold (not the requested ones)."""
    mc = sys.modules["mds_common"]
    out = {
        "environment": mc.TARGET_ENVIRONMENT, "deployment": mc.TARGET_DEPLOYMENT,
        "api_url": mc.BASE_URL, "dashboard_url": mc.DASHBOARD_URL, "pilot": mc.PILOT_ID,
        "aws_profile": mc.AWS_PROFILE, "aws_region": mc.AWS_REGION, "aws_account": mc.AWS_ACCOUNT,
        "launchpad_bucket": mc.DEFAULT_BUCKET, "marker_bucket": mc.MARKER_BUCKET, "marker_prefix": mc.MARKER_PREFIX,
        "model_bucket": mc.MODEL_BUCKET, "model_keys": mc.MODEL_KEYS,
        "queues": {"aggregation": mc.AGG_QUEUE, "disagg": mc.DISAGG_QUEUE, "notifications": mc.NOTIF_QUEUE,
                   "user_attributes": mc.ATTR_QUEUE},
        "token_file": mc.TOKEN_FILE, "db_schema": mc.DB_SCHEMA,
        "rate_plans": {"flat": mc.DEFAULT_RATE_PLAN, "gas": mc.DEFAULT_GAS_RATE_PLAN, "tou": mc.TOU_RATE_PLAN},
        "bill_cycle_code": mc.DEFAULT_BILL_CYCLE_CODE, "time_zone": mc.DEFAULT_TZ, "utility_name": mc.UTILITY_NAME,
        "keyspace": mc.KEYSPACE, "dw_bucket": mc.DW_BUCKET, "shc_template_pilot": mc.SHC_TEMPLATE_PILOT,
        "profile_bucket": mc.PROFILE_BUCKET, "pdf_bucket": mc.PDF_BUCKET, "etl_java_env": mc.ETL_JAVA_ENV,
        "etl_queue_suffix": mc.ETL_QUEUE_SUFFIX, "link_hosts": list(mc.LINK_HOSTS), "default_recipient": mc.DEFAULT_RECIPIENT,
    }
    for name in MODULES[1:]:        # every loaded engine must hold the same values as mds_common
        mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, "_target_view"):
            mine = mod._target_view()
            for k, v in mine.items():
                if out.get(k) != v:
                    out.setdefault("_inconsistent", []).append(f"{name}.{k}={v!r} != mds_common {out.get(k)!r}")
    out["code_sha256"] = code_sha()
    return out


def check_project_scope(view: dict) -> None:
    """Refuse a profile whose pilot this project directory is not allowed to act on.

    One project directory serves one pilot family. MASTERPILOT-02 must never run against
    masterpilot01 (88001), which has its own separate automation, so passing the wrong
    --profile has to fail here -- after the profile resolves, before apply() commits it
    and before anything touches a target.
    """
    # The offline suite drives synthetic pilots (66001, 77001, ...) and asserts cross-pilot behaviour, so it opts
    # out explicitly. Only the test runner sets this; nothing on a real run does.
    if os.environ.get("MDS_SCOPE_BYPASS_FOR_TESTS") == "1":
        return
    if not SCOPE_PATH.exists():
        raise TargetError(f"{SCOPE_PATH.name} is missing: it declares which pilots this project may target")
    try:
        scope = json.loads(SCOPE_PATH.read_text())
    except json.JSONDecodeError as exc:
        raise TargetError(f"{SCOPE_PATH.name} is not valid JSON: {exc}") from exc
    allowed = scope.get("allowed_pilots")
    if not allowed:
        raise TargetError(f"{SCOPE_PATH.name} lists no allowed_pilots")
    pilot = int(view["pilot"])
    if pilot not in {int(p) for p in allowed}:
        raise TargetError(
            f"pilot {pilot} is out of scope for {scope.get('project', SCOPE_PATH.parent.name)}: "
            f"this project may only target {sorted(int(p) for p in allowed)}. "
            f"The profile that resolved it is {PROFILE_PATH}. "
            f"Each pilot family has its own project directory and automation -- use that one instead of widening "
            f"{SCOPE_PATH.name}."
        )


def bootstrap(argv: list[str] | None) -> list[str]:
    argv = list(sys.argv[1:] if argv is None else argv)
    if VIEW is not None and "--profile" not in argv:      # nested engine call in the same run: same target, never a new one
        apply(VIEW)
        return [x for x in argv if x != "--print-target"]
    view, argv = resolve(argv)
    check_project_scope(view)                             # out-of-scope pilot stops here, before the target is applied
    apply(view)
    # child processes (kit tools, nested scripts) get the SAME resolved profile and contract
    os.environ["BIDGELY_PROFILE"] = PROFILE_PATH or ""
    contract, target = _kit()
    global _EXPORTED
    if not os.environ.get("BIDGELY_CONTRACT_SHA256") or os.environ.get("BIDGELY_CONTRACT_SHA256") == _EXPORTED:
        _EXPORTED = contract.digest(contract.build(target.load_target(PROFILE_PATH, {}, environ={})))
        os.environ["BIDGELY_CONTRACT_SHA256"] = _EXPORTED
    if "--print-target" in argv:
        print(json.dumps(describe(), sort_keys=True))
        raise SystemExit(0)
    return argv


# ---- write policy: the scope a kit-launched run may write, enforced INSIDE the provisioner -------------------------
# The provisioner never trusts a policy handed to it as a variable. It COMPUTES its write scope from a concrete batch
# authorization that it validates itself with the kit's own code:
#   BIDGELY_AUTHORIZATION  path to the run's authorization.json  (written by `emailctl run --execute` into the run dir)
#   BIDGELY_PLAN           path to the run's plan.json           (same run dir)
#   BIDGELY_RUN_TOKEN      the kit's per-invocation token (also stamped into the manifest as kitRunToken)
# emailkit.auth.Authorization(doc, plan, <the target loaded here>) must be valid (plan_sha256 of THIS plan, contract
# digest of THIS effective target + provisioner code, unexpired, every required field filled); the policy is then
# emailkit.provisioning.build_write_policy(doc, provisioner_view(target), email.forbidden_config).
# BIDGELY_WRITE_POLICY is IGNORED: whatever it holds, it neither grants nor narrows anything.
# Anything missing or invalid -> READ-ONLY PREVIEW: every API mutation, S3/SQS write and SQL write is refused
# (WRITE_REFUSED: PREVIEW_MODE ...). BIDGELY_WRITE_JOURNAL (JSONL path) records every attempt, allowed or refused.
import hashlib as _hashlib
import re as _re
import time as _time
from datetime import datetime as _dt, timezone as _tz

UUID_RX = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_PINNED: set = set()
_AUTH_CACHE: dict = {}            # sha of (authorization, plan, profile) -> (policy | None, why)


class WriteRefused(RuntimeError):
    pass


def _expired(doc: dict) -> bool:
    try:
        exp = _dt.fromisoformat(str(doc.get("expires")).replace("Z", "+00:00"))
        return exp.tzinfo is None or exp <= _dt.now(_tz.utc)
    except ValueError:
        return True


def policy_status() -> tuple[dict | None, str]:
    """-> (the write policy computed from a valid kit authorization, "") or (None, why this run is a preview)."""
    ignored = " (BIDGELY_WRITE_POLICY is ignored: a policy is never taken from a variable)" \
        if os.environ.get("BIDGELY_WRITE_POLICY") else ""
    ap, pp = os.environ.get("BIDGELY_AUTHORIZATION"), os.environ.get("BIDGELY_PLAN")
    if not (ap and pp):
        return None, "no kit authorization (BIDGELY_AUTHORIZATION/BIDGELY_PLAN not set: standalone run)" + ignored
    if not os.environ.get("BIDGELY_RUN_TOKEN"):
        return None, "no kit run token (BIDGELY_RUN_TOKEN not set)" + ignored
    if _TARGET is None:
        return None, "no target loaded (the authorization is validated against the --profile target)" + ignored
    try:
        raw_a, raw_p = Path(ap).read_bytes(), Path(pp).read_bytes()
        doc, plan = json.loads(raw_a), json.loads(raw_p)
    except (OSError, ValueError) as e:
        return None, f"authorization/plan unreadable ({type(e).__name__})" + ignored
    if not (isinstance(doc, dict) and isinstance(plan, dict)):
        return None, "authorization/plan is not a JSON object" + ignored
    key = _hashlib.sha256(raw_a + b"\0" + raw_p + b"\0" + str(PROFILE_PATH).encode()).hexdigest()
    if key not in _AUTH_CACHE:
        try:
            contract, _ = _kit()
            from emailkit.auth import Authorization                 # noqa: E402  (the kit's own validation)
            from emailkit.provisioning import build_write_policy      # noqa: E402  (the kit's own policy builder)
            a = Authorization(doc, plan, _TARGET)
            if a.problems:
                _AUTH_CACHE[key] = (None, "authorization invalid for this plan/target: " + "; ".join(a.problems))
            else:
                _AUTH_CACHE[key] = (build_write_policy(doc, contract.provisioner_view(_TARGET),
                                                       _TARGET.section("email.forbidden_config")), "")
        except Exception as e:      # noqa: BLE001 - any validation problem is a preview, never a write
            _AUTH_CACHE[key] = (None, f"authorization not validated ({type(e).__name__}: {getattr(e, 'message', e)})")
    pol, why = _AUTH_CACHE[key]
    if pol is not None and _expired(doc):                # an authorization that expires mid-run stops granting writes
        return None, f"authorization expired ({doc.get('expires')})" + ignored
    return pol, why + (ignored if pol is None else "")


def policy() -> dict | None:
    return policy_status()[0]


def pin_user(uuid: str) -> None:
    """The user this run created/owns: user-scope writes must name it (no other user's entity)."""
    _PINNED.add(uuid.lower())


_LAST_JOURNAL_REF: dict | None = None


def _journal(rec: dict) -> None:
    """Append one attempt to BIDGELY_WRITE_JOURNAL and remember where it went (last_journal_ref): the manifest's upload
    records point at their journal entry by path + 1-based line + sha256 of that line, so the kit can cross-check them."""
    global _LAST_JOURNAL_REF
    _LAST_JOURNAL_REF = None
    path = os.environ.get("BIDGELY_WRITE_JOURNAL")
    if path:
        line = json.dumps(dict(rec, t=round(_time.time(), 3)), default=str)
        try:
            with open(path, "rb") as f:
                n = sum(1 for _ in f)
        except FileNotFoundError:
            n = 0
        with open(path, "a") as f:
            f.write(line + "\n")
        _LAST_JOURNAL_REF = {"path": str(Path(path).resolve()), "line": n + 1,
                             "sha256": _hashlib.sha256(line.encode()).hexdigest()}


def last_journal_ref() -> dict | None:
    """The journal entry of the most recent check_write in this process (None when no journal is configured)."""
    return dict(_LAST_JOURNAL_REF) if _LAST_JOURNAL_REF else None


# operations no authorization can grant (the kit's policy has no vocabulary for them): deletes, ACL/tagging, bucket and
# queue administration, receive/visibility changes, the boto3 resource API and any other unclassified AWS call
_NEVER = {"aws_admin": lambda d: f"{d.get('service')}.{d.get('op')} is never authorized (only object uploads to the "
                                 "authorized launchpad bucket and sends to authorized queues are)"}


def _decide(pol: dict, kind: str, d: dict) -> tuple[bool, str]:
    if kind == "api":
        path, method, body = d["path"].split("?")[0].lstrip("/"), d["method"], d.get("body")
        ctype = (body or {}).get("configType") if isinstance(body, dict) else None
        if ctype and any(_re.fullmatch(p.replace("*", ".*"), ctype) for p in pol.get("forbidden_ctypes") or []):
            return False, f"configType {ctype} is ingestion/Launchpad configuration (never written)"
        m = _re.match(rf"(?:entities|meta/users|2\.1/stringResources|notification/notificationStatus)/({UUID_RX})", path)
        if m and _PINNED and m.group(1).lower() not in _PINNED:
            return False, f"user {m.group(1)} is not the user this run created"
        for r in pol.get("api") or []:
            if r["method"] == method and _re.fullmatch(r["path_regex"], path):
                if r.get("ctypes") is not None and ctype not in r["ctypes"]:
                    continue
                if r.get("body_keys") is not None and isinstance(body, dict) and not set(body) <= set(r["body_keys"]):
                    continue
                return True, r.get("why", "")
        return False, f"{method} {path} is outside the authorized write scope"
    if kind == "s3_put":
        s3 = pol.get("s3_put") or {}
        if d["bucket"] not in (s3.get("buckets") or []):
            return False, f"s3://{d['bucket']} is not an authorized bucket"
        if s3.get("shared_key_regex") and _re.match(s3["shared_key_regex"], d["key"] or "") and not s3.get("allow_shared"):
            return False, f"{d['key']} changes shared pilot data and is not authorized"
        return True, ""
    if kind == "sqs":
        return (d["queue"] in ((pol.get("sqs") or {}).get("queues") or []), f"queue {d['queue']}")
    if kind == "sql":                 # direct DB writes: only when the policy names the operation (the kit's never does)
        ok = d.get("op") in ((pol.get("sql") or {}).get("ops") or [])
        return ok, ("" if ok else f"SQL write {d.get('op')} is not in the authorized write scope (no kit authorization grants SQL)")
    return False, f"unknown write kind {kind}"


def check_write(kind: str, **d) -> None:
    if kind in _NEVER:
        ok, why = False, _NEVER[kind](d)
    else:
        pol, pwhy = policy_status()
        ok, why = ((False, f"PREVIEW_MODE: {pwhy}; writes run only through `emailctl run --execute --authorization <file>`")
                   if pol is None else _decide(pol, kind, d))
    _journal({"kind": kind, "allowed": ok, "why": why, **{k: v for k, v in d.items() if k != "body"},
              "body_keys": sorted(d["body"]) if isinstance(d.get("body"), dict) else None,
              "config_type": (d.get("body") or {}).get("configType") if isinstance(d.get("body"), dict) else None})
    if not ok:
        raise WriteRefused(f"WRITE_REFUSED: {why}")


class _Guarded:
    """boto3 client proxy, deny by default. Reads pass through; S3 object uploads (single and multipart) and SQS sends
    pass check_write; every other operation (deletes, ACL/tagging, bucket/queue administration, receive/visibility
    changes, private/low-level calls, any other service's mutations) is refused and journalled."""
    _READ_PREFIXES = ("get_", "head_", "list_", "describe_", "download_", "generate_presigned_")
    _READ_NAMES = {"can_paginate", "close"}
    _S3_PUTS = {"put_object", "copy_object", "copy", "upload_file", "upload_fileobj", "create_multipart_upload",
                "upload_part", "upload_part_copy", "complete_multipart_upload", "abort_multipart_upload"}
    _SQS_SENDS = {"send_message", "send_message_batch"}

    def __init__(self, client, service: str, queues_by_url: dict):
        self._c, self._svc, self._urls = client, service, queues_by_url

    def _refuse(self, name):
        def refused(*a, **k):
            check_write("aws_admin", service=self._svc, op=name)
        return refused

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        if name.startswith("_"):                      # _make_api_call & co would bypass every check
            return self._refuse(name)
        fn = getattr(self._c, name)
        if not callable(fn):                          # meta, exceptions
            return fn
        if self._svc == "sqs" and name == "get_queue_url":
            def gq(*a, **k):
                out = fn(*a, **k)
                self._urls[out["QueueUrl"]] = k.get("QueueName")
                return out
            return gq
        if name in self._READ_NAMES or name.startswith(self._READ_PREFIXES):
            return fn
        if self._svc == "s3" and name in self._S3_PUTS:
            def wrapped(*a, **k):
                # upload_file(Filename, Bucket, Key) / upload_fileobj(Fileobj, Bucket, Key) / copy(CopySource, Bucket, Key)
                bucket = k.get("Bucket") or (a[1] if len(a) > 1 else None)
                key = k.get("Key") or (a[2] if len(a) > 2 else "")
                body = k.get("Body")
                digest = {}
                if isinstance(body, (bytes, bytearray)):      # the journal names the exact bytes this put carries
                    digest = {"sha256": _hashlib.sha256(body).hexdigest(), "size": len(body)}
                check_write("s3_put", bucket=bucket, key=key, op=name, **digest)
                return fn(*a, **k)
            return wrapped
        if self._svc == "sqs" and name in self._SQS_SENDS:
            def wrapped(*a, **k):
                check_write("sqs", queue=self._urls.get(k.get("QueueUrl"), k.get("QueueUrl")), op=name)
                return fn(*a, **k)
            return wrapped
        return self._refuse(name)


class GuardedSession:
    """boto3 Session proxy: every client is guarded; the resource API (which would bypass the client guard) is refused."""

    def __init__(self, sess):
        self._s, self._urls = sess, {}

    def client(self, service, *a, **k):
        return _Guarded(self._s.client(service, *a, **k), service, self._urls)

    def resource(self, service=None, *a, **k):
        check_write("aws_admin", service=service or k.get("service_name"), op="resource")

    def __getattr__(self, name):
        return getattr(self._s, name)


def target_section(dotted: str, default=None):
    """A value of the loaded profile (None until a profile is loaded): optional target-specific check settings."""
    return _TARGET.section(dotted, default) if _TARGET is not None else default


def chart_image_host_regex() -> str | None:
    """Host pattern of the target's html-to-image chart service (email.provisioner.target.chart_image_host_regex).
    Unset -> chart image checks report 'not checked' (no host is assumed)."""
    v = target_section("email.provisioner.target.chart_image_host_regex")
    return str(v) if v else None

