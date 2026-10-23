"""Fixture profiles for the provisioner tests (fictional targets; the debug kit validates them exactly like real ones)."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import mds_target  # noqa: E402

KIT_SCRIPTS = mds_target.SKILL_DIR / "scripts"


def make_profile(pilot: int = 88002, env: str = "envq", dep: str = "stacka", api_url: str = "http://127.0.0.1:9",
                 account: str = "111111111111", region: str = "xx-test-1", aws_profile: str = "qa-a", tmp: Path | None = None,
                 checkout: str | None = None, **over) -> Path:
    tmp = Path(tmp or tempfile.mkdtemp(prefix="mds-target-"))
    tok = tmp / "token"
    tok.write_text("synthetic-token")
    tok.chmod(0o600)
    prof = {
        "name": f"fixture-{env}-{dep}", "role": "lower", "environment": env, "deployment": dep, "pilot": pilot,
        "api": {"url": api_url, "token_file": str(tok)},
        "db": {"schema": f"db_{dep}"},
        "email": {
            "adapters": "real",
            "queues": {"notifications_processor": f"Notif-{env}-{dep}", "aggregation": f"Agg-{env}-{dep}",
                       "user_attributes": f"Attr-{env}-{dep}", "disagg": f"Disagg-{env}-{dep}"},
            "aws": {"profile_ref": aws_profile, "region": region, "account_id": account},
            "s3": {"launchpad_bucket": f"lp-{dep}-{env}", "marker_bucket": f"mk-{dep}-{env}", "marker_prefix": "Error/"},
            "cassandra": {"keyspace": f"ks_{dep}", "service": "etl"},
            "provisioner": {"kind": "mds_user_automation", "checkout": checkout or str(HERE.parent),
                            "target": {"dashboard_url": f"http://dash.{dep}.{env}.invalid", "model_bucket": f"models-{env}",
                                       "model_keys": {"ELECTRIC": f"v2/{dep}/US_{pilot}_AMI_electric.xlsx",
                                                      "GAS": f"{dep}/US_{pilot}_AMI_gas.xlsx"},
                                       "bill_cycle_code": "4", "time_zone": "UTC", "utility_name": f"{dep}-{pilot}",
                                       "dw_bucket": f"dw-{env}", "shc_template_pilot": 70000, "profile_bucket": f"prof-{env}",
                                       "pdf_bucket": f"pdf-{env}", "etl_java_env": env, "etl_queue_suffix": dep,
                                       "link_hosts": [f"settings.{env}.invalid"],
                                       "default_recipient": "qa+{df}{customer}@example.invalid"}},
            "rate_plans": {"flat": f"FLAT_{dep.upper()}", "tou": f"TOU_{dep.upper()}"},
        },
    }
    for k, v in over.items():
        cur = prof
        parts = k.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        if v is None:
            cur.pop(parts[-1], None)
        else:
            cur[parts[-1]] = v
    f = tmp / f"{prof['name']}.json"
    f.write_text(json.dumps(prof, indent=1))
    f.chmod(0o600)
    return f


def use(pilot: int = 88002, **kw) -> Path:
    """Bootstrap the provisioning modules on a fixture target (through the kit, like a real run)."""
    f = make_profile(pilot=pilot, **kw)
    mds_target.VIEW = None
    mds_target.bootstrap(["--profile", str(f)])
    return f
