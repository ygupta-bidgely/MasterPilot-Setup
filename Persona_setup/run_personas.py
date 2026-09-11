#!/usr/bin/env python3
"""Interactive runner for the Persona_setup scripts.

Reads the shared `config.json`, shows what is configured, and lets you pick
which scripts to run. Everything pilot-specific is discovered from the pilot,
so setting up a brand-new pilot is: point `BASE_URL`/`PILOT_ID` at it, list the
users, and run this.

  uv run python run_personas.py                 # interactive menu
  uv run python run_personas.py --list          # show scripts and configured users
  uv run python run_personas.py --all           # run every script that has users
  uv run python run_personas.py WelcomeEmail HER
  uv run python run_personas.py --dry-run --all # show what would run

Each script is a normal standalone script in its own folder and can still be
run directly; this only sequences them.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import SetupError, load  # noqa: E402

PERSONA_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Script:
    """One runnable setup script."""

    name: str
    folder: str
    entry: str
    what: str

    @property
    def path(self) -> Path:
        return PERSONA_DIR / self.folder / self.entry

    @property
    def exists(self) -> bool:
        return self.path.exists()


# Ordered so persona configuration runs before the emails that depend on it.
# PostCdgAggregation comes first: freshly ported users have no billing cycle,
# and the cycle-derived emails have nothing to generate from until it has run.
SCRIPTS: tuple[Script, ...] = (
    Script("PostCdgAggregation", "PostCdgAggregation", "run_post_cdg_aggregation.py",
           "Post-CDG: mark users modified + run aggregations (builds billing cycles)"),
    Script("Standard", "Standard", "setup_standard_persona.py",
           "Standard (flat/tier) persona: rate plan + MS email variant"),
    Script("TOU", "TOU", "setup_tou_persona.py",
           "TOU-family persona (TOU, Promotion, Coaching)"),
    Script("EV", "EV", "setup_ev_persona.py",
           "EV-family persona (EV, TOU + EV)"),
    Script("Solar", "Solar", "setup_solar_persona.py",
           "Solar-family persona (Solar, + TOU, + EV, + EV + TOU)"),
    Script("SolarTrueUp", "SolarTrueUp", "setup_solar_trueup_persona.py",
           "Solar true-up persona (kWh vs $, x plain/TOU/EV/TOU+EV)"),
    Script("PoolPump", "PoolPump", "setup_pool_pump_persona.py",
           "Pool Pump persona"),
    Script("BudgetBilling", "BudgetBilling", "setup_budget_billing_persona.py",
           "Budget Billing-family persona (BB, + TOU, + TOU + EV)"),
    Script("WelcomeEmail", "WelcomeEmail", "setup_welcome_email.py",
           "Trigger the User Welcome email"),
    Script("MonthlySummary", "MonthlySummary", "setup_monthly_summary_email.py",
           "Trigger the Monthly Summary email (VARIANT: standard/solar/TOU/BB/demand)"),
    Script("BillProjection", "BillProjection", "setup_bill_projection_email.py",
           "Trigger the Bill Projection email (VARIANT: standard/TOU promotion/coaching)"),
    Script("BudgetAlert", "BudgetAlert", "setup_budget_alert_email.py",
           "Set a crossed budget and trigger the Budget Alert email (75/100%)"),
    Script("BestRateEmail", "BestRateEmail", "setup_best_rate_email.py",
           "Prepare + trigger the Best Rate (RATE_COMPARISON) email"),
    Script("TOUOnboarding", "TOUOnboarding", "setup_tou_onboarding_email.py",
           "Prepare + trigger the TOU Rate Onboarding email"),
    Script("HER", "HER", "setup_her.py",
           "HER persona: config, DB rows, interactions, SHC mock pipeline"),
    Script("HBA", "HBA", "create_hba_s3_user.py",
           "High Bill Alert: creates its own synthetic user (CLI-flag driven)"),
)

BY_NAME = {script.name: script for script in SCRIPTS}


def describe(config) -> list[tuple[Script, list[dict]]]:
    """Pair each script with the users configured for it."""
    rows = []
    for script in SCRIPTS:
        scoped = config.for_script(script.name)
        try:
            users = scoped.users_for(script.name)
        except SetupError:
            users = []
        rows.append((script, users))
    return rows


def print_listing(config) -> None:
    print(f"Pilot   : {config.pilot_id}")
    print(f"Base URL: {config.base_url}")
    print(f"Region  : {config.region}")
    print()
    print(f"{'#':<4}{'Script':<16}{'Users':<7}What")
    print("-" * 78)
    for index, (script, users) in enumerate(describe(config), start=1):
        marker = "" if script.exists else "  (missing)"
        print(f"{index:<4}{script.name:<16}{len(users):<7}{script.what}{marker}")
        for user in users:
            label = user.get("label") or ""
            print(f"{'':<27}- {user['UUID']}  {label}")


def choose(config) -> list[Script]:
    """Ask which scripts to run."""
    rows = describe(config)
    print_listing(config)
    print()
    print("Enter numbers (e.g. 1,3,8), a range (1-4), 'all', or blank to cancel.")
    try:
        answer = input("Run which scripts? ").strip()
    except EOFError:
        return []
    if not answer:
        return []
    if answer.lower() == "all":
        return [script for script, users in rows if users and script.exists]

    chosen: list[Script] = []
    for token in answer.replace(" ", "").split(","):
        if not token:
            continue
        if "-" in token:
            start, _, end = token.partition("-")
            try:
                span = range(int(start), int(end) + 1)
            except ValueError:
                print(f"Skipping unrecognised range {token!r}")
                continue
            indexes = list(span)
        else:
            try:
                indexes = [int(token)]
            except ValueError:
                if token in BY_NAME:
                    chosen.append(BY_NAME[token])
                else:
                    print(f"Skipping unrecognised entry {token!r}")
                continue
        for index in indexes:
            if 1 <= index <= len(rows):
                chosen.append(rows[index - 1][0])
            else:
                print(f"Skipping out-of-range number {index}")
    # Preserve SCRIPTS order and drop duplicates.
    return [s for s in SCRIPTS if s in chosen]


def run(script: Script, dry_run: bool) -> int:
    print()
    print("=" * 78)
    print(f"{script.name}: {script.what}")
    print("=" * 78)
    if not script.exists:
        print(f"SKIPPED: {script.path} does not exist")
        return 0
    if dry_run:
        print(f"would run: {script.path}")
        return 0
    completed = subprocess.run(
        [sys.executable, str(script.path)], cwd=script.path.parent, check=False
    )
    return completed.returncode


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the Persona_setup scripts against the configured pilot."
    )
    parser.add_argument("names", nargs="*", help="Script names to run.")
    parser.add_argument("--list", action="store_true", help="Show scripts and exit.")
    parser.add_argument(
        "--all", action="store_true", help="Run every script that has users."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would run, without running."
    )
    args = parser.parse_args()

    try:
        config = load()
    except SetupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.list:
        print_listing(config)
        return 0

    if args.names:
        unknown = [n for n in args.names if n not in BY_NAME]
        if unknown:
            print(f"ERROR: unknown script(s): {', '.join(unknown)}", file=sys.stderr)
            print(f"Known: {', '.join(BY_NAME)}", file=sys.stderr)
            return 1
        selected = [s for s in SCRIPTS if s.name in set(args.names)]
    elif args.all:
        selected = [s for s, users in describe(config) if users and s.exists]
    else:
        selected = choose(config)

    if not selected:
        print("Nothing selected.")
        return 0

    results: list[tuple[str, int]] = []
    for script in selected:
        try:
            code = run(script, args.dry_run)
        except KeyboardInterrupt:
            print("\nInterrupted", file=sys.stderr)
            return 130
        results.append((script.name, code))

    print()
    print("=" * 78)
    print("SUMMARY")
    print("=" * 78)
    for name, code in results:
        print(f"  {'OK  ' if code == 0 else 'FAIL'}  {name}")
    failed = [name for name, code in results if code != 0]
    if failed:
        print(f"\n{len(failed)} failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
