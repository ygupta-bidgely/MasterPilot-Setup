# MDS NON-SOLAR user automation

Same structure, scripts, CLI, profile, write policy, phases, waits, run layout and offline tests as
`../MDS_AUTOMATION`, for **MDS non-solar** users. Every file here is a copy of the solar suite with the non-solar
behaviour written into the code (the shape the Hawk masterpilot-02 MDSNonSolar suite proved on pilot 88002):

| Piece | Solar suite (`../MDS_AUTOMATION`) | This folder |
|---|---|---|
| Enrollment `SOLAR_USER` (USERENROLL 35, METERENROLL 11, transition rows) | `True` | `FALSE` |
| RAW / invoice | rooftop solar (`--solar-kw 2.5`), RECEIVED <= 0, FEED_IN credit | `--solar-kw 0`: RAW_R all `0.000`, FEED_IN `0.000 kWh / $0.00`; the RECEIVED stream still exists |
| User-scope configs | solar disagg + solar display keys | none of the solar keys (hybrid disagg, $ itemization, gas unit kept) |
| Monthly Summary prerequisite | `supported_solar_communication_events` (pilot write with `--ensure-ms-config`) | `supported_communication_events` is read only (WARN if MONTHLY_SUMMARY is missing; never written) |
| Checks | E02 solarUser true, E04 GLOBAL.SOLAR true, E14/E15 solar row 16, E21 solar energy flow | E02 solarUser **not** true, E04 GLOBAL.SOLAR **not** true, **no** solar row, **no** SOLAR_ENERGY_FLOW |
| Combined dual-fuel MS | fallback list with SOLAR_ENERGY_FLOW | without it |
| TOU (`mds_tou.py`) | `enable_tou_chart_for_solar=true`, SOLAR_ENERGY_FLOW in the coaching list, solarUser decouple/restore | flag always false, no SOLAR_ENERGY_FLOW, `home.solarUser` is never set true |
| Paper HER (`mds_paper_her.py`) | layouts for the resolved suffixes + DEFAULT | also `.AMI` (a non-solar AMI home resolves `HER_*.<fuel>.AMI`) |
| Budget Billing B14 | solar cost-breakdown flag set | home is NOT solar |
| `transition_mds_user_to_tou.py` | requires a solar home + solar D/R meters | requires them NOT solar |

Prerequisites from the Hawk non-solar runs: the TOU plan needs `rateStructureImageUrl` on its weekday energy rows
(TOU Coaching / Onboarding), rate plans are cached by the JVMs until restart, High Bill Alert is blocked for every MDS
user (DA-2480), SHC needs the pilot's neighbourhood metrics. `BUGS_budget_billing_mds.md` and
`DEPLOY_TOU_ONBOARDING_FIX.md` are the solar suite's documents, kept for reference.

Tests: `MDS_SCOPE_BYPASS_FOR_TESTS=1 python3 -m unittest discover -s tests` (147 tests; the solar-specific ones assert
the non-solar behaviour; `test_print_target_equals_the_kit_view` fails the same way in `../MDS_AUTOMATION`).

---

The solar suite's README follows (paths updated to this folder).


Each run creates a **brand-new** MDS user from launchpad files generated at run time. It then waits for every
stage (ingestion, disagg, aggregation, Monthly Summary email) and checks the result through the APIs.

## Target selection (read this first)

No module carries an environment, deployment, pilot, bucket, queue or rate plan of its own. Every run names its
target with a **debug-kit profile**; without one the script exits `PROFILE_REQUIRED` (3) before any network call.

```bash
python3 create_mds_single_fuel_user.py --profile <name|path>
BIDGELY_PROFILE=<name|path> python3 create_mds_single_fuel_user.py
python3 create_mds_single_fuel_user.py --profile <name> --print-target   # what the modules actually hold
```

Profiles live with the `config-fail-testcase-debugger` skill
(`~/.claude/skills/config-fail-testcase-debugger/profiles/`), with a template under `templates/` and
`scripts/profile_init.py` to create one. `copy_nve_rates_to_mds.py` and `create_mds_nve_tou_rate_plan.py` copy
*between* environments, so they additionally require `--source-profile`.

### Project scope: this directory serves ONE pilot family

`project_scope.json` lists the only pilots this directory may act on. `mds_target.bootstrap()` checks the
resolved profile against it and exits `TARGET_ERROR` before the target is applied, so passing the wrong
`--profile` fails loudly instead of quietly provisioning another pilot.

```
allowed_pilots : 88002, 88011        (MASTERPILOT-02)
refused        : 88001 and everything else
```

**masterpilot01 (88001) is deliberately out of scope.** It uses a different ingestion bucket
(`...resi-fl-...` vs `...resi-id-/...resi-az-...`), has no NVE lineage, and keeps its own separate automation —
so the NVE/TOU/rate-comparison half of this suite does not apply to it. Never widen `project_scope.json` to
include it, and never reference one pilot's resources from the other's code: duplicate instead of sharing.
`tests/test_project_scope.py` is the regression guard, including an explicit assertion that 88001 is never listed.

```
MDS_NON_SOLAR/
├── README.md
├── mds_common.py                      shared engine (generation, upload, waits, disagg/agg, MS email, checks)
├── create_mds_single_fuel_user.py     ELECTRIC solar MDS (DELIVERED + RECEIVED)        -> 21 checks
├── create_mds_dual_fuel_user.py       ELECTRIC solar MDS + GAS AMI                       -> 32 checks
├── create_mds_single_fuel_her_user.py HER email (eHER) + paper HER PDF, single fuel
├── create_mds_dual_fuel_her_user.py   HER email (combined DF eHER) + paper DFHER PDF, dual fuel
├── mds_her.py / mds_paper_her.py      HER email engine / paper HER (PDF) engine
├── create_mds_nve_tou_rate_plan.py    NVE UAT TOU plan -> the target pilot plan 907 (rates APIs + rate-structure images)
├── copy_nve_rates_to_mds.py           ALL NVE UAT <source pilot> plans -> the target pilot (only the missing ones), rates APIs + verify
├── create_mds_nve_plan_user.py        new user on a copied NVE plan (preflight, then the single fuel / TOU flow)
├── enable_mds_rate_comparison.py      NVE rate_comparison config (user or pilot scope) + RC API + promotion gates
├── create_mds_tou_rate_promotion_user.py  NEW user end to end: user + rate comparison + promotion config + MS email
├── send_mds_tou_rate_promotion_email.py  TOU Rate Promotion = TOU_RATE_PROMOTION section in the Monthly Summary
├── create_mds_tou_user.py             TOU user: files + ingestion + rate transition + both TOU emails
├── create_mds_tou_onboarding_user.py / create_mds_tou_coaching_user.py  dedicated TOU user flows
├── transition_mds_user_to_tou.py      move an existing user onto the TOU plan
├── send_mds_tou_onboarding_email.py   TOU Onboarding email for an existing TOU user
├── send_mds_tou_coaching_email.py     TOU Coaching (Monthly Summary, MS_TOU_COACHING) for an existing TOU user
├── mds_tou.py / tou_evidence.py       TOU email engine / TOU evidence capture
├── mds_bb.py                          budget billing engine
├── create_mds_budget_billing_user.py / send_mds_budget_billing_email.py  budget billing flow
├── mds_ba.py                          budget alert engine
├── create_mds_budget_alert_user.py / generate_mds_budget_alerts.py / send_mds_budget_alert_email.py
├── mds_target.py                      the ONE target of a run: --profile resolution, applied to every module
├── tests/                             offline suite (11 files; needs pytest, not installed in this repo's venv)
└── results/
    ├── single_fuel/run_<YYYYmmdd_HHMMSS>_<id>/
    ├── dual_fuel/run_<YYYYmmdd_HHMMSS>_<id>/
    ├── tou/run_<YYYYmmdd_HHMMSS>_<id>/
    └── rate_plans/<planName>_<ts>/
```

## Run

```bash
cd MASTERPILOT-02/MDS_NON_SOLAR
export BIDGELY_TOKEN=<target bearer token>     # or --token-file ~/.mp_token (chmod 600)

python3 create_mds_single_fuel_user.py    # ~8 min
python3 create_mds_dual_fuel_user.py      # ~20 min

# optional
python3 create_mds_single_fuel_user.py --dry-run          # generate files only, offline
python3 create_mds_single_fuel_user.py --phases preflight # pilot readiness only
python3 create_mds_single_fuel_user.py 2>&1 | tee run.out # keep console copy
python3 create_mds_dual_fuel_user.py --phases notify,validate \
        --manifest results/dual_fuel/run_<id>/manifest.json # re-drive MS email + checks
```

### Run: HER (email + paper PDF), single fuel and dual fuel

```bash
cd MASTERPILOT-02/MDS_NON_SOLAR
export BIDGELY_TOKEN=<target bearer token>     # or --token-file ~/.mp_token (chmod 600)

# NEW user end to end: create -> SHC -> configs -> interactions -> HER email -> validate -> paper HER PDF
python3 create_mds_single_fuel_her_user.py
python3 create_mds_dual_fuel_her_user.py

# EXISTING user (skips user creation; runs every other phase)
python3 create_mds_single_fuel_her_user.py --uuid <uuid>
python3 create_mds_dual_fuel_her_user.py   --uuid <uuid>

# HER email only (resend + validate)
python3 create_mds_single_fuel_her_user.py --uuid <uuid> --phases trigger,validate
python3 create_mds_dual_fuel_her_user.py   --uuid <uuid> --phases trigger,validate

# paper HER PDF only (HER_MONTHLY_REPORT / DFHER_MONTHLY_REPORT)
python3 create_mds_single_fuel_her_user.py --uuid <uuid> --phases paper
python3 create_mds_dual_fuel_her_user.py   --uuid <uuid> --phases paper

# resume a run that stopped after user creation
python3 create_mds_single_fuel_her_user.py --manifest <run>/manifest.json \
        --phases shc,configure,interactions,trigger,validate,paper

# SHC data missing (nmetrics API empty): republish into the reader keyspace; needs the jumphost password file
export BOPS_PW_FILE=$TMPDIR/bops_pw_$USER
python3 create_mds_dual_fuel_her_user.py --uuid <uuid> --phases shc,trigger,validate --shc-publish
```

- **HER phases**, in order: `user shc configure interactions trigger validate paper`. Details are in the HER email
  and Paper HER sections below.
- **Output:** `results/<single_fuel|dual_fuel>/run_*/`:
  - `email/HER_email_*.html` and `reports/her_validation_report.md` are the HER email and its checks.
  - `paper/*.pdf`, `paper/PDF_LINK.txt` and `reports/paper_her_validation_report.md` are the paper HER, its links and
    its checks.

### Run: TOU rate plan, TOU Onboarding and TOU Coaching (NVE TOU plan)

```bash
cd MASTERPILOT-02/MDS_NON_SOLAR
export BIDGELY_TOKEN=<target bearer token>     # or --token-file ~/.mp_token (chmod 600)
export BOPS_PW_FILE=$TMPDIR/bops_pw_$USER                     # optional: log diagnosis when an email is not generated

# 1) rate plan: NVE UAT <source pilot> plan 198 OD-1-TOU_EVRR -> the target pilot plan 907 MDS_TOU_EVRR_D_R (idempotent; replaces 907's rates)
python3 create_mds_nve_tou_rate_plan.py
python3 create_mds_nve_tou_rate_plan.py --phases verify          # re-check only
NVE_UAT_TOKEN=<NVE UAT token> python3 create_mds_nve_tou_rate_plan.py --refresh-source

# 2) NEW TOU user, QA style: upload files, let the platform send the emails (no NotificationsProcessorEvent)
python3 create_mds_tou_user.py
python3 create_mds_tou_user.py --manifest <run>/manifest.json --restore-pilot-flag  # repair

# 3) re-check / diagnose the system-generated emails of an EXISTING TOU user (sends nothing)
python3 send_mds_tou_onboarding_email.py --uuid <uuid>
python3 send_mds_tou_coaching_email.py   --uuid <uuid>

# 4) legacy: trigger each email by hand (SQS NotificationsProcessorEvent)
python3 create_mds_tou_user.py --manual-trigger
python3 send_mds_tou_coaching_email.py   --uuid <uuid> --manual-trigger --phases reaggregate,coaching
```

- **QA flow** (default): history first, then the plan switch, as for a real customer.
  1. User + USER-scope email configs, then BC_SCHEDULE / USERENROLL / METERENROLL (flat 905) / RAW D+R / INVOICE,
     each confirmed through the APIs, then disagg.
  2. The customer moves to TOU: `USERENROLL_*_T.csv` (plan 907 + `RATE_PLAN_EFFECTIVE_DATE`). The ingester re-prices
     from that date and publishes `TOU_ONBOARDING` itself.
  3. The notifications-enabled aggregation runs the system Monthly Summary pass: `MS_TOU_COACHING`.
  4. Checks T01-T03, O01-O10, C01-C08, then the E01-E21 data validation.
  The only SQS messages are the data-pipeline DISAGG and AGG (`manifest.json` -> `sqs`).
- **Pilot flag window.** Only `LaunchpadUserCreator` publishes TOU_ONBOARDING, and only while
  `launchpad_ingestion_configs.handle_meter_enrollment=false`; the target pilot keeps it `true` (METERENROLL owns rate plans and
  `LaunchpadMeterCreator` publishes nothing). The flag is set false for the one transition file (~150 s, backed up,
  always restored, recorded as `pilotFlagWindow`). Code fix: `TouOnboardingEmailTrigger` in `LaunchpadMeterCreator`.
- **Solar MDS home: TOU Onboarding cannot render today (ticket).** The event IS published and processed, then dropped:
  `populateTouRateStructure` / `TouRateStructureSectionContentProvider` only accept `touUser && !home.solarUser` (no
  solar branch, unlike TOU_USAGE), and ingestion re-derives `home.solarUser=true` from the solar meter right before it
  publishes. The earlier `populateSolarEnergyFlowBlock:980` NPE is avoided by config:
  `<uuid>.TOU_ONBOARDING.ELECTRIC email_itemization_section_configs.use_previous_billing_cycle=true` (set by the
  script) + the transition after disagg; code fix `EmailDataProvider.getElectricItemizationWithCost`.
- **TOU Coaching on a solar home.** The deployed `tou_usage_solar_*.vm` carry a U+200E filename and kill the MS
  (ticket/deploy; rename staged on this branch). Workaround `--solar-tou-chart decouple` (default): `home.solarUser`
  is false for the one aggregation pass (`GLOBAL.SOLAR` untouched, so solar sections stay) and restored after.
  `SOLAR_ENERGY_FLOW` is added to the user's MS section list (the target pilot's DCX list omits it).
- **Diagnosis.** A missing email pulls the notification(-priority) and emailer passes and classifies every known
  signature as CONFIG / DATA / SQL (fixable here) or TICKET (`mds_tou.KNOWN_ISSUES`; checks `OX*` / `CX*`).
- **TOU Coaching needs T03.** The TOU cycles must be priced on plan 907 and have a TOU aggregate. A plan created after
  the `aggregation` / `aggregation-priority` JVMs started is priced at `defaultRate` 0.1 with no TOU aggregate: the
  `SQLRatesDao` cache is never invalidated. Restart those services, then re-aggregate.
- **Output:** `results/tou/run_*/`: `input_files/` (incl. `METERENROLL_*_T.csv`, the transition row),
  `email/TOU_ONBOARDING_*.html`, `email/TOU_COACHING_MS_*.html`, `reports/tou_onboarding_validation_report.md`,
  `reports/tou_coaching_validation_report.md`, `logs/` (plus the notification/emailer passes when a send fails).
- Rate plan output: `results/rate_plans/<plan>_<ts>/` (`payload.json`, `images/`, `rate_plan_report.md`).

### Run: copy every NVE rate plan to the target pilot, and create users on them

```bash
export BIDGELY_TOKEN=<target token>      # or --token-file / --client-file (mint)
export NVE_UAT_TOKEN=<NVE UAT token>                    # or --nve-token-file / --nve-client-file; omit = snapshot
python3 copy_nve_rates_to_mds.py --dry-run          # diff NVE UAT vs the target pilot + payloads, no writes
python3 copy_nve_rates_to_mds.py                    # create the missing plans + verify
python3 copy_nve_rates_to_mds.py --plans 104,205    # only these NVE plans
python3 copy_nve_rates_to_mds.py --repair           # finish interrupted (PARTIAL) copies
python3 copy_nve_rates_to_mds.py --phases source,diff,verify   # re-check, no writes
python3 copy_nve_rates_to_mds.py --feed-in-rate 0.03   # MDS-proper copies (adds a RECEIVED band)
python3 copy_nve_rates_to_mds.py --phases remove       # delete copies that are not MDS-proper

python3 create_mds_nve_plan_user.py --nve-plan 205              # user on NVE_Y01 (demand)
python3 create_mds_nve_plan_user.py --nve-plan 104 --tou        # TOU user + TOU emails
python3 create_mds_nve_plan_user.py --nve-plan 104 --preflight-only
```

- **MDS-only (2026-09-29):** a plan is created only when it prices BOTH MDS streams (DELIVERED + RECEIVED) and passes
  the MDS band rule. NVE plans carry DELIVERED rows only, so they are `not-mds` unless `--feed-in-rate <$/kWh>` adds a
  flat export band. The first import (143 DELIVERED-only copies) was removed with `--phases remove` (rates via API,
  plan rows + rate_resources via `host/eutility_plans_delete.py`; backups in `pingpong/backups/` and the run folder).
  the target pilot now has 905, 906, 907 (D&R) plus the older plans 1-21. `rates/the target pilot_nve_plans.csv` has the per-plan outcome.
- **Mapping:** NVE UAT <source pilot> plan N -> the target pilot plan N+10000, name `NVE_<NVE name>`, rows verbatim + `data_stream`.
- **Only what is missing:** skips plans already copied, explicit ports (907 = NVE 198) and plans with no rows; never
  overwrites a plan number that holds another plan. Identical rates under a different name are still created.
- **API only:** configuration POST (plan row), DELETE + `POST /v3.0/rates` (rows), rate-resources. Out-of-range
  months (NVE 104's fixed row `month 0-12`) are rejected by both APIs and clamped to 1-12; the report lists them.
- **Verify:** every created plan compared row by row with NVE, rate-resources, and eRates counts (groupName,
  data_stream, chargeSubType) when the dbq tunnel is available. Output `results/nve_rate_copy/<ts>/`.
- **Credentials:** never hard-coded. Client id/secret pairs come from Jenkins self-service
  (`https://jenkins.bidgely.com:4567`, VPN, "Get Client"); `--client-file` / `--nve-client-file` take a mode-600
  JSON `{"client_id": ..., "client_secret": ...}` and mint a token with `oauth/token?grant_type=client_credentials`.
- **Users:** the preflight stops when `/structure` does not serve the plan (JVM rate cache predates it: restart
  data-server, ingestion, aggregation, aggregation-priority). TOU Onboarding also needs `rateStructureImageUrl`,
  which only plan 907 has (no API writes it).

### Run: rate comparison + TOU Rate Promotion (Monthly Summary section, like NVE)

```bash
# rate comparison config (NVE UAT <source pilot> rate_comparison, map translated to the target pilot plans) + RC API + gate checks
python3 enable_mds_rate_comparison.py --uuid <uuid>                   # USER scope (default)
python3 enable_mds_rate_comparison.py --scope pilot --phases config   # whole pilot (backed up)
# TOU Rate Promotion END TO END: new user (flat 905, automatic MS held) -> rate comparison -> config + text -> MS email
python3 create_mds_tou_rate_promotion_user.py
# same for an EXISTING user: rate comparison + user-scope MS section list + text + MS resend + validation
python3 send_mds_tou_rate_promotion_email.py --uuid <flat-plan MDS user>
python3 send_mds_tou_rate_promotion_email.py --uuid <uuid> --phases configure,email
```

- **Where it renders:** section `TOU_RATE_PROMOTION` of the Monthly Summary, variant `MS_TOU_PROMOTION`. Everything is
  user scope (`rate_comparison`, `email_template_sections_config`, `tou_rate_promotion_section_configs`).
- **RC API:** `POST v3.0/users/<uuid>/homes/1/rate-comparison/candidate-rates?measurement-type=ELECTRIC` computes and
  stores every candidate (~1-5 min). The script waits for it and polls the `GET .../rate-comparison` read
  (`--rc-wait 600`). `rate_comparison_enabled` only gates the automatic triggers, not this API.
- **Candidates:** only plans that price every stream of the user's plan (D&R) and are in the data-server rate cache;
  the RC API skips any other. The current plan gets the 3 such TOU plans estimated cheapest for the user's own RAW
  (`--auto-candidates`, `--min-saving 10`), or `--map CUR:CAND,...`. NVE map pairs are kept only when both plans are
  MDS-proper (none today: the NVE copies were removed).
- **Section text:** the configure phase writes user-scope header/bullets/paragraph computed from the RC result and the
  plans: the off-peak ratio (the platform prints "0X" for a flat current plan), off-peak/peak hours and prices, both
  annual costs. `--plan-display-name` (default "Time-of-Use Saver") replaces an internal rate_resources plan name.
  907 is not a good default: on this load profile it costs more than the flat 905 ($935 vs $763 a year).
- **Computation method:** user-scope `BILLING_CYCLE_ACTUAL` + `check_computation_type_based_on_user_data=false`.
  NVE's data-duration rule picks `BILLING_CYCLE_PROJECTED` for >= 350 days, and that stored the current plan's
  RECEIVED stream only (no DELIVERED) for 905. The current cost is then negative, and the savings-% cap drops
  the whole result (the GET answers 400). `--computation-method nve` keeps NVE's values.
- **Gates checked before sending (RC01-RC08):** current plan in the map, result stored, best plan (cheapest incl.
  current) is TOU, savings >= `minimum_saving_amount`, savings % <= `max_saving_threshold_in_percentage`,
  rate-resources planName, current plan's `computedDurationBillEnd` = the MS cycle end.

### Options (Monthly Summary scripts)

- **Options:** `--zip/--city/--state` (default 11691, Far Rockaway, NY, a ZIP with weather data), `--cycles 13`,
  `--email`, `--rate-plan`, `--output-dir`.
- **Phases**, in order: `preflight generate upload configure aggregate notify validate dashboard`.
- **Exit codes:** `0` all PASS/WARN, `1` a check failed, `2` a setup or wait error.

## Budget Billing user: MS email + web dashboard (one command)

```bash
export BIDGELY_TOKEN=<target token>            # or --token-file (0600)
python3 create_mds_budget_billing_user.py                      # new BB user, MS email, web data, 41 checks
python3 create_mds_budget_billing_user.py --dry-run-files      # offline: only the files
python3 send_mds_budget_billing_email.py --uuid <uuid>         # resend + validate for an existing BB user
python3 send_mds_budget_billing_email.py --uuid <uuid> --phases verify,email --no-send   # re-check only
```

The script creates its own prerequisites, in this order: **pilot** keys that are missing (`program_part_launchpad`,
`budget_billing_configs`, the BB section config and palette; additive, backed up to `pingpong/backups/`), then the **user** files
(INVOICE carries a positive `BB_AMOUNT` row on each BB cycle; a `USERPROGRAM` file enrols the user in `BB_PROGRAM_1` with a
positive `BB_TrueUp_Period`), then **user-scope** config (section list with `BB_CUMULATIVE_BALANCE`, strings if missing), then
disagg/aggregation and the Monthly Summary (variant `MS_BB`). The web side needs nothing more: `usage-widget-data` returns
`budgetBillingInfo` as soon as the programme row and the BB invoices exist; the run prints a passwordless dashboard link.
`reports/BB_SUMMARY.md` has the uuid, link, email file and verdict. BillProjection is not generated on MDS (known) and is not checked.
Design notes and the platform defects found while building it: `mds_bb.py` docstring and `BUGS_budget_billing_mds.md`.

## Budget Alert email (one command)

```
python3 generate_mds_budget_alerts.py --days-left 6 --days-over 1   # choose days left / days over
python3 create_mds_budget_alert_user.py            # same, defaults (8 days left)
python3 send_mds_budget_alert_email.py --uuid <u>  # existing MDS user
```
Creates the user with data ending mid-cycle (`--mid-cycle-days 8`, so the email says "8 days left"), sets USER-scope config
(Email subscription, normal-user subject, home budget derived from live cost), decouples `home.solarUser` for the send
(restored after; `EmailDataProvider:980` NPE) and triggers the aggregation-driven pass, which is the only path that runs
Budget Alert. 36 checks (prerequisites, subject variant, bar image, budget/spend values, days left, links, state).
Output: `results/budget_alert/run_*/email/BA_*.html`, `reports/BA_SUMMARY.md`. Findings: `BUGS_budget_billing_mds.md` (BA1-BA8).

## Dashboard login link

Each run generates a passwordless dashboard URL for its new user. This happens right after USERENROLL creates
the user, and again at the end of the run, because the hash carries a timestamp.

```
GET  /v2.0/user-auth/cipher?user-id=<uuid>&pilot-id=the target pilot    -> payload = user-hash
GET  /v2.0/user-auth/<user-hash>?pilot-id=the target pilot              -> must decipher back to <uuid>
URL  https://<deployment>-<environment>.bidgely.com/dashboard?user-hash=<url-encoded hash>&locale=en_US
```

- The URL is logged in `logs/run.log`, and appears in `SUMMARY.txt` as "dashboard url" with its
  verification, expiry and generation time. It is also saved in `manifest.json` under `dashboard`.
- The link logs in as that test user, so share it like a password. The OAuth token in the decipher
  response is never logged or saved.
- To get a fresh link for an earlier run:
  `python3 create_mds_single_fuel_user.py --phases dashboard --manifest <run>/manifest.json`
  (use the dual-fuel script for a dual-fuel run).

## Result layout (one folder per run)

| Path | Contents |
|---|---|
| `SUMMARY.txt` | **User details** (uuid, zipcode, customer/account/premise, meter ids, tokens, plan, MS cycle, invoice totals), the uploaded files, the MS email subject, and the check table |
| `manifest.json` | everything the run generated and learned; `--manifest` resumes from it |
| `input_files/` | the launchpad files uploaded to S3 (BC_SCHEDULE, USERENROLL, METERENROLL, RAW_D/R[/G], INVOICE) |
| `logs/run.log` | full timestamped log; every line is tagged `[uuid=...]` once the user exists |
| `logs/log_grep_hints.txt` | host and grep command for each backend service |
| `reports/validation_report.md` and `.json` | every check with its API and details |
| `email/MS_email_<n>.html` | the Monthly Summary email exactly as sent |
| `api_evidence/*.json` | the raw API responses behind every check |
| `sqs_messages/` | every disagg, aggregation and MS message that was sent |
| `pilot_config_backups/` | pilot configs, saved before any `--ensure-*` pilot write |

## What the data is

- **Ids and names:** customer, account, premise and meter ids, and every file name, are unique per run.
- **Cycles:** 13 completed monthly cycles, plus RAW data up to now for the open month. The **last completed month
  is the MS cycle**; run in September, that is August.
- **RAW:** in the pilot's unit (electric Wh, gas ccf), labelled at the interval end. D is ≥ 0, R is ≤ 0.
- **Invoices:** built from the RAW data (TOTAL, CONSUMPTION_BASED, FEED_IN and a fixed charge; gas has its own
  lines). End dates are exclusive.

## Checks

Every check asserts content, not just HTTP 200. The pure check functions are unit-tested offline
(`python3 -m pytest tests -q`, 47 tests on the captured responses of user `7f41ebec`).

- **E01–E05:** user, home and plan, D and `-R` meters, attributes, RAW sample day stored exactly.
- **E06 coverage:** per stream over the whole window: expected intervals, gaps, duplicates, bounds, rows per
  cycle. **Every stored reading** (`gb.json`, 14-day chunks, whole window) is compared with the file, with no
  missing, extra or duplicate intervals. **WARN by design:** the first fall-back hour (4
  intervals) can't be written in the pilot's local `yyyyMMddHHmmss` format (see "Known limits").
- **E07–E12 billing:**
  - E07: billing cycles.
  - E08: every persisted invoice line equals the generated INVOICE file, on the exact period.
  - E09: the INVOICE file equals the RAW files (parsed independently) and the stream aggregates, for every
    cycle.
  - E10–E12: energy, cost and net-billable.
- **E13:** hybrid status, plus **triggered-job completion**: each DISAGG/AGG needs a
  `hybridSucessRunTimeStamp` past **both** the pre-trigger baseline and the trigger, in a later second (the timestamp
  has 1 s resolution). Same-second or unreadable timestamps are reported UNVERIFIED (WARN).
- **E14/E15 itemization (presentation, not model accuracy):** requests use `totalFromInvoice=true`.
  - Required rows: 16 solar and 17 total.
  - Appliances == total == invoice energy, to the cent (±$0.005 float slack).
  - Total usage == net + solar estimate.
- **E16–E18 dashboard:**
  - E16 usage chart: every in-window cycle (trailing year + ongoing), D/R kWh and cost, energy, weather.
  - E17 bill summary.
  - E18 monthly-summary widget: ID-matched to the itemization API.
- **E19 projection:** both endpoints need status OK, the exact ongoing period, numeric amounts, and must
  agree with each other. Zero or negative projected cost is allowed.
- **E20 bill analysis:** the MS cycle against its **weather-normal** self (`useNormalCompareBillCycle=true`),
  with invoice, energy, net consumption, days and itemization reconciled.
- **E21 / G10 MS email:** re-fetched live every time. The claim level is stated explicitly:
  - **generated:** the notification record, for this event, fuel, recipient and run;
  - **application-sent:** `notificationStatus` holds `billStartTs` = MS cycle, `sentCount >= 1`, and a
    `sent` time from this run;
  - **inbox delivery:** never claimed; `deliveryStatus` is always `{}`.

  The content check covers the cycle, the total, and every appliance line against the itemization. It also
  parses the `SOLAR_ENERGY_FLOW` block (solar = the value between the generation and home icons, grid = the
  value between the home and grid icons, net = the signed import/export headline), so the check can't be
  satisfied by a matching number elsewhere in the email.
- **G01–G09 (gas):** line-level invoice and RAW reconciliation, as for electric; V1 itemization; widgets.
  **DF01:** identical invoice periods.

## Coverage matrix (what a green run does NOT prove)

| Area | Covered | Not covered |
|---|---|---|
| Rates | plan 905: **flat** D $0.15 / R $0.05 all day (logged as FLAT) | TOU peak/off-peak, tiers, holidays. `RateBook` refuses these instead of pricing them at $0 |
| Streams | ELECTRIC DELIVERED + RECEIVED; GAS DELIVERED (dual) | CONTROLLED_LOAD_1/2 from `supported_streams` |
| Bills | net-import bills | net-export-negative bills, zero-export users |
| Data | one clean upload | meter replacement, replay or idempotency, malformed-input rejection, alternate units |
| Geography | ZIP 11691 (for weather); timestamps parsed in the pilot's `parser_time_zone` America/Anchorage | a realistic address/zone pairing: the NY address + Anchorage time zone is synthetic |
| Disagg | presentation reconciliation to the invoice | model accuracy: this needs independent ground truth |
| Email | generated + application sent-state | inbox delivery |

## Known limits

- **DST fall-back:** `LaunchpadRawDataParser` parses the local label with `SimpleDateFormat`, and Java
  maps the repeated hour to *standard* time (checked with the JDK: `20251102010000` → 01:00 AKST).
  - A first-hour label would store its reading one hour late, so the generator skips those 4 intervals
    and records them in `manifest.raw.unrepresentable`.
  - Runs made before this fix wrote first-hour values that landed in the second hour.
  - A complete year of data needs `raw_data_date_type` with an offset or epoch, which is a pilot change.
- **E19** currently FAILs on the target pilot: `bill-projection-graph-data` reports status 256 `NOT_ENOUGH_DATA`
  with $0, while `billprojections` projects $63.19. The root cause is a backend question, kept separate
  from the validator.

## Pilot the target pilot prerequisites (set 2026-09-28; backups in `pingpong/backups/`)

- **Hybrid:** V2 for electric and V1 for gas, via `disagg_preference` / `hybrid_moderation`. The model files
  `US_the target pilot_*` are in `s3://hybrid-models-<environment>`.
- **`meta_data.supported_solar_communication_events`:** needed for the solar MS email. It is only read at
  pilot level.
- **`meta_data.gas_unit = com.bidgely.cloud.unitType.ccf`:** when this is empty, gas itemization returns 400
  "kWh and kW".
- **`launchpad_ingestion_configs.supported_streams`:** needs a GAS entry for dual fuel. The dual-fuel script
  adds it only when run with `--ensure-gas-stream`.
- **TOU (set 2026-09-29, `mds_tou.ensure_pilot_tou`, additive, mirrors pilot <pilot>):**
  - `meta_data.enable_tou_chart_for_solar=true`: the solar branch of every TOU section gate.
  - `tou_usage_section_configs.min_peak_usage_for_rendering_in_kwh_solar=1`.
  - `tou_usage_section_configs_palette` `color301`–`color304`: the solar TOU chart colours.

  Backups are in `backups/pilotthe target pilot_*_before_tou_*.json`. Rate plan 907 `MDS_TOU_EVRR_D_R` holds NVE UAT plan 198.

## Dual fuel: ONE combined Monthly Summary email (default)

The separate GAS email is blocked by a platform bug (see below), so `create_mds_dual_fuel_user.py` sends
**one combined dual-fuel email** (`--ms-mode combined`). All of its settings are **user scope only**; nothing
changes at pilot level. They are applied as soon as the meters exist, before any data triggers a notification.

| Setting (user scope) | Why it is needed (code) |
|---|---|
| `email_moderation.enable_combined_dual_fuel_email=true`, `eligible_fuels_for_dual_fuel_email=["ELECTRIC","GAS"]` | `EventProcessorUtils.isCombinedDfEmail:1117` routes MS to `sendEmailForCombinedDF` |
| `email_moderation.consumption_unit_type_map={"ELECTRIC":"kWh","GAS":"CCF"}` | without it only the first fuel gets a unit; `DF_ITEMIZATION` NPEs on the gas unit |
| `event_subscriptions.MONTHLY_SUMMARY.delivery_modes=["Email"]` | the combined pass asks for delivery modes with no fuel; the GLOBAL default is `[]` |
| `email_template_sections_config.MONTHLY_SUMMARY.DF_EMAILS.OPT_IN/OPT_OUT` | the user's MS list with `DF_ITEMIZATION` instead of `ITEMIZATION_V2`. If `ITEMIZATION_V2` is present it wins and renders one fuel |
| strings at `<uuid>.MONTHLY_SUMMARY.ELECTRIC/GAS` | per-fuel block headings "Your Electricity Use By Appliance" / "Your Natural Gas Use By Appliance", and "Your Electricity Summary" |

Resending: `--phases notify,validate --ms-resend --manifest <run>/manifest.json`.
- It rewinds `billStartTs` to the previous cycle and sets `sentCount` 0 at the no-fuel state path.
- Zeroing `sentCount` alone does nothing.

## Monthly Summary in $ (both scripts, user scope)

Following NVE (PEREQ-3146, PEREQ-3312), every new user gets:

| Setting (user scope) | Section it turns to $ |
|---|---|
| `email_monthly_summary_section_configs.show_cost_break_down_for_solar_users=true` | bill summary (Total, Energy Usage Charges, Fixed Charges) and the subject; otherwise solar is kWh (`MonthlySummarySectionContentProvider:179`) |
| `email_itemization_section_configs.solar_display_value_identifier=cost`, `display_value_identifier=cost` | appliance itemization (electric and gas) |
| `email_usage_chart_section_configs.solar_display_value_identifier=cost` | monthly usage history chart (PR #29288) |

- **Single fuel:** the whole email is in $. The solar energy-flow block stays in kWh because it shows generation and grid flow, the same as NVE.
- **Combined dual fuel:** the itemization of both fuels is in $. The subject and bill summary stay in kWh
  (`EmailDataProvider:397` forces CONSUMPTION for a combined email on a solar home), so the summary is labelled
  "Your Estimated Usage", as NVE did. Switching them to $ needs the code change described below.

Product limits that config cannot change:
- **kWh only on solar homes:** a combined email for a solar home is always in kWh
  (`EmailDataProvider:397`: `isDualFuelCombinedEmail && isSolarElecEmail -> CONSUMPTION`).
- **Electric-only summary:** the subject and the summary section cover electricity only.
- **No gas cost:** the gas block shows usage without cost. G10 reports this as a WARN.

## Known platform bug

The **GAS Monthly Summary email** can't be generated for a solar dual-fuel home on a DCX pilot, so check G10
fails.

- `EmailDataProvider:392` sets `isSolarElecEmail` from `GLOBAL_SOLAR` on the GAS pass too.
- `populateSolarEnergyFlowBlock:980` then NPEs on `itemization.get(ELECTRIC)`.
- Fix: require `measurementType == ELECTRIC` for `isSolarElecEmail`, and null-guard that lookup. The electric MS
  email is unaffected.

## HER email (eHER): `create_mds_single_fuel_her_user.py` / `create_mds_dual_fuel_her_user.py`

**What gets built**
- The paper HER (`EventNames.HER`) only produces a PDF: `HEREventProcessor:128` stops without Paper.
- The HER that is emailed is the **NEIGHBOURHOOD_COMPARISON (similar-home) email with an itemization block**.
  `EmailDataProvider:2141` flags it `isEHER`. This is what NVE RESI <source pilot> sends in production; see
  `db/277-Drop2/.../NVE-resi-missing-configs.sh`.
- Both scripts use NVE's section lists, all written at USER scope, plus the GenAI NBI reco block:

| | single fuel (`NEIGHBOURHOOD_COMPARISON.OPT_*`) | dual fuel, ONE combined email (`.DF_EMAILS.OPT_*`) |
|---|---|---|
| SHC | SHC_SUMMARY, SHC_BARGRAPH_COMPARISON*, SHC_COMPARISON_WIDGET* | DF_SHC_SUMMARY, DF_SHC_BARGRAPH_COMPARISON*, SHC_COMPARISON_WIDGET_V2* |
| itemization | ITEMIZATION, ITEMIZATION_EXPLAINER | DF_ITEMIZATION |
| tips | GEN_AI_NBI_RECO_ELEMENT_SECTION, ENERGY_SAVINGS_TIPS | GEN_AI_NBI_RECO_ELEMENT_SECTION, SPONSORED_RECO_V2, ENERGY_SAVINGS_TIPS_V2, CTA_WITH_DES_SECTION_V2 |
| frame | TEASER, HEADER, GREETING, NEW_SERVICE, FEEDBACK, FOOTER | TEASER, HEADER_V4, GREETING_V2, NEW_SERVICE_V2, FEEDBACK_V4, FOOTER_V4 |

`*` marks mandatory sections. Amounts are in $ (`displayValueIdentifier=COST`, itemization `cost`).

**Phases:** `user` → `shc` → `configure` → `interactions` → `trigger` → `validate`
- With no `--uuid`, the script creates a brand-new user (`user` = mds_common preflight, generate, upload, configure
  and aggregate, about 30-40 minutes), then runs every HER phase on it. The run is handed over by the one run
  directory that user creation made; the script never picks "the newest manifest". If user creation fails, the HER
  phases don't run. The error message prints the exact resume command.
- `--uuid <existing MDS user>` skips `user`.
- `--phases trigger,validate` resends. The trigger re-posts the mock interactions with fresh ids first.

```
export BIDGELY_TOKEN=...                       # or --token-file (0600)
export BOPS_PW_FILE=$TMPDIR/bops_pw_$USER      # jumphost sudo password, only for --shc-bootstrap / --shc-publish
# new user -> SHC -> configs -> interactions -> HER email -> validation
python3 create_mds_single_fuel_her_user.py
python3 create_mds_dual_fuel_her_user.py
# resume a run that stopped after user creation
python3 create_mds_single_fuel_her_user.py --manifest <run>/manifest.json \
    --phases shc,configure,interactions,trigger,validate
# the cluster exists but /nhs/the target pilot/ids/<nid>/nmetrics is empty
python3 create_mds_dual_fuel_her_user.py --uuid <uuid> --phases shc,trigger,validate --shc-publish
```

**Gates found live on 2026-09-29.** Each one dropped the email silently, and every fix is USER scope:

| gate | symptom | fix in `mds_her.py` |
|---|---|---|
| NMetrics keyspace | ncRunner/publish succeed and the etl DAO shows rows, but the nmetrics API is `[]` for every cluster | the etl host's datacubejobs context writes Cassandra `bidgely`; <deployment> notifications and the API read `bidgely_<deployment>`. `publish_nmetrics` (`host/NcPublish<deployment>.java`) republishes ntype the target pilot into the reader keyspace and refuses any other keyspace. It runs after ncRunner, or alone with `--shc-publish` |
| solar SHC_SUMMARY | emailer NPE `EmailSummaryStringsProvider.getSolarTemplateDataMap` | seeds `…greeting.summary.web.url.ms.text.solar` (production value `""`) |
| ITEMIZATION (AMI solar) | emailer NPE `ItemizationSectionContentProvider.populateEnergySectionTitle` | seeds the AMI title and the survey/feedback nudge strings (`HER_REQUIRED_STRINGS`) |
| V1 FEEDBACK (single) | "Unable to render template: $feedbackTitle has not been set" | `email_feedback_section_configs.title_text_content` |
| GenAI reco section | emailer NPE `GENAINBIRecoElementSectionContentProvider` | `gen_ai_nbi_reco_element_section_configs` + `_palette` (production values) |
| combined subscription (dual) | "already processed all delivery modes … skipping" | `event_subscriptions.NEIGHBOURHOOD_COMPARISON.delivery_modes=["Email"]` |
| interactions not shown | reco block empty, no error | mock insights are `COMMUNITY` (the combined path keeps only those); ids are fresh on every send (already-sent ids are dropped, including by attempts that failed later) |

**Branding: NVE's layout, generic everything else.** The header and footer on the target pilot are already the generic
"Energy Co." ones: `EnergyCo.png` and "Energy Co. - 128 Saratoga Ave - River Valley". <deployment> ships
placeholder targets for two links: "Complete home profile" went to `https://www.google.com` and the GenAI tips CTA
to `dashboard.bidgely.com`. `GENERIC_LINK_STRINGS` always sets both to the environment dashboard. Check H10 fails
on any real-utility name (NV Energy, …) or on a link or image outside the generic hosts.

**SHC data is the hard prerequisite**
- `SHCDataProvider.getNeighbourhoodEvent` reads NMetrics (C / GAS_C) for the home's cluster from Cassandra. With
  none, the fuel is dropped with CANNOT_COMPUTE_NEIGHBOURHOOD and no email goes out.
- Nothing produces NMetrics on <deployment>:
  - the target pilot had no SHC clusters.
  - The ncRunner cron is commented out on `datacubeandetls-<environment>-<deployment>`.
  - The legacy Redshift `ncRunner` hangs connecting to Redshift. Use `ncRunnerStarburst`.
- `--shc-bootstrap` builds the fixture using the Confluence runbook "SHC 2.0 Pipeline - Manual Run With Mock Data":
  1. It writes the mock cluster files to `s3://bidgely-data-warehouse-<environment>/shc-2/algo-outputs/outputs/{diff,total,cluster_info}/pilot_id=the target pilot/ntype=the target pilot/batch_id=<ts>/`.
  2. It runs `NeighbourhoodPostProcessingRunner` on the etl host, which writes `/nhs/the target pilot/ids` and `defNhoodId`.
  3. It runs `ncRunnerStarburst`, which writes the NMetrics.
  - This needs the jumphost password file (`BOPS_PW_FILE`, from `envlogs setpw`).
- A DUAL_FUEL cluster (12 MDS users) was created this way on 2026-09-29.
- `data_point_threshold=0` is set at user scope, because the mock cluster is small.

**Other user-scope settings**
- `genai_nbi_config`: without it, `getGenAINBIConfigs` returns null.
- NC delivery modes.
- Dual fuel only:
  - the combined-DF flags;
  - `use_secondary_fuels_for_shc_email`;
  - the three DF summary strings that are missing on <deployment>. Without them, the render hits an NPE at
    `EmailSHCSummarySectionContentProvider:267`.

**Mock interactions**
- 3 per fuel, sent to `POST v1.0/nbi/interactions/userUpdate` (Cassandra `genai_nbi_interaction`) for the
  processed cycle.
- They are read back through `GET v1.0/nbi/interactions/user/<uuid>`.

**Checks**

| id | check |
|---|---|
| H01 | email generated |
| H02E / H02G | SHC you / average / efficient match the SHC API per fuel, as $ or as the bar graph's labelled usage (the combined solar bar graph shows kWh/CCF) |
| H03 | SHC wording |
| H04 | itemization |
| H05 | dual fuel: both fuels plus "Total Spent on Electricity and Gas" |
| H06 | mock interaction titles rendered |
| H07 | no unrendered `${...}` and no null links |
| H08 | amounts shown in $ |
| H09 | sent-state updated by THIS send: the per-fuel path for single fuel, the no-fuel path for combined |
| H10 | generic branding: no utility names; links and images only on generic hosts |

H01 and H09 only accept evidence newer than the moment the trigger reset the sent-state. `utility_notifications`
timestamps are in milliseconds; comparing them to seconds used to let a previous send's email pass.

## Paper HER (PDF): the `paper` phase of both HER scripts

A paper HER (`EventNames.HER`) is a PDF and a report URL. It is never an email. `mds_paper_her.py` runs it after the
eHER email phases:

```
python3 create_mds_single_fuel_her_user.py --uuid <uuid> --phases paper   # HER_MONTHLY_REPORT
python3 create_mds_dual_fuel_her_user.py   --uuid <uuid> --phases paper   # DFHER_MONTHLY_REPORT
```

The output lands in the run directory:
- `paper/<ReportType>_the target pilot_<account>_<ts>_0_1_V2.pdf` is the PDF.
- `paper/PDF_LINK.txt` holds the report URL from the notification record and a 7-day presigned download link.
- `reports/paper_her_validation_report.md` is the validation report.

| step | what / why |
|---|---|
| schedule (**PILOT**) | HERScheduleManager reads PILOT rows only. the target pilot had the HER_MONTHLY_REPORT and seasonal rows per fuel, and `DFHER_MONTHLY_REPORT \| ELECTRIC,GAS` was added on 2026-09-29. The step only adds missing rows. |
| welcome letter (**PILOT**) | The GLOBAL `HER.WLC.PARA_3` / `HER.WLC.V2.WELCOME_MSG.PARA_2` name a real utility programme site. They are overridden at pilot scope only when a brand is found. The PDF then drops that paragraph, because the front end does not read the pilot override. |
| layouts (USER) | DCX gives `HER_MONTHLY_REPORT.<fuel>.SOLAR` orders that all need EE_NBI or HER ITEMIZATION. <deployment> has **no PAPER_NBI assets**, so these fail with "NBI is null or not have any properties". The DCX orders are kept first and NBI-free orders are appended. `ITEMIZATION_TOP_APPLIANCES` is excluded and stripped: it validates, but the homeEnergyReport page never loads with it ("Received null PDF"). |
| her_moderation (USER) | `report_type_nbi_run_mapping` gets the report type added with `\|HER`. DF gets `max_dfher_cycle_gap_in_seconds`. |
| trigger | Backs up and clears `HER/<fuel>/Paper` state, then sends SQS `EventName=HER`. |

| id | check (the PDF against the pdf-report APIs the report is built from) |
|---|---|
| P01 | PDF in `s3://bidgely-pdf-reports-<environment>/the target pilot/...` newer than this run, plus the Paper notification row |
| P02 | the report type asked for (a DF report that fell back to single fuel is a WARN) |
| P03 | header: name, address, account number, zip |
| P04E/G | similar-homes you / average / efficient and "more than average" match `shc-bargraph-and-widget`. The DF comparison shows one fuel, so the other fuel is a WARN. |
| P05 | the 13 month labels of the `bar-chart` series |
| P06 | itemization rows. WARN on <deployment>, because no renderable layout carries itemization. |
| P07 | no real-utility names |
| P08 | solar energy flow: generation and from-grid match `energy-flow-data` |
