#!/usr/bin/env python3
"""Create and ingest a synthetic MasterPilot user designed to trigger HBA email.

The script performs the complete ProductQA workflow:

1. Generate unique external user/account/premise/meter identifiers.
2. Generate a USERENROLL file, completed-cycle INVOICE file, and two RAW files.
3. Upload them to the MasterPilot residential S3 ingestion bucket.
4. Resolve the UUID created by ingestion and configure HBA at USER level.
5. Seed and verify every string resource the HBA render dereferences without a
   null check, BEFORE any aggregation is queued (see below - this is the step
   that most often decides whether an email ever arrives).
6. Upload only current-cycle raw data; completed historical bills remain INVOICE data.
7. Calibrate the abnormal usage, upload it, then reseed the expected bill against
   it and confirm the resulting projection lands inside HBA's accept band -
   correcting the multiplier once if it does not.
8. Close the seeding window so later passes evaluate instead of reseeding.
9. Send tightly scoped MONTH-only AggregateUploadEvent XML messages to the
   MasterPilot ProductQA aggregation queue.
10. Confirm HIGH_BILL_ALERT notification state.

Why steps 7 and 8 are ordered this way
--------------------------------------
``isHBAEligibleForMTDTrigger`` accepts a projection only in
``[expected * threshold, expected * threshold * 4)`` and drops it silently at
BOTH ends - the upper one logs "Projection crossed max threshold". Neither end
writes notification state, so an overshoot, an undershoot and a render crash all
look identical from the API. ``expected`` is not the invoice fixture: it is
whatever ``calculateExpectedBill`` stored in ``hba_expected_bill``, readable via
``/2.1/users/{uuid}/homes/1/hbaExpectedCost``.

That row is written only while ``today <= billCycleStart +
high_bill_alert_trigger_period_in_days`` days, and for the whole of that window
``isEligibleForHBATrigger`` returns false - so the window seeds but never
evaluates. The default comparison mode, SAME_TIME_LAST_YEAR_AND_CURRENT_YEAR,
averages the CURRENT cycle in, which means an expected bill stored before the
high-usage upload is built from a nearly empty cycle. The projection then
overshoots the ``* 4`` ceiling and is dropped as implausible. So the window has
to stay open across the high-usage upload, close only once the stored value
reflects the data the evaluation passes will judge, and the band check has to
run against that stored value rather than against the invoice mean used to pick
the multiplier.

Why step 5 exists
-----------------
Every content provider on the HBA path does ``stringMap.get(<id>).getText()``
with no null check, so a single unseeded string resource id throws out of
``GenericEventDataEmailContentBuilder.getHtmlContentData`` and aborts the entire
email - not just its own section. Notification state is written only on a
successful send, so from the API the result is indistinguishable from a
trigger-gate rejection: an empty
``notificationStatus/<uuid>/1/HIGH_BILL_ALERT/ELECTRIC/Email``. Only Emailer.log
tells the two apart. Several of the required ids are compiled-in
``*_STRING_RESOURCE_DEFAULT`` constants that no deployment seeds, so a fresh
pilot fails this way by default. ``HBA_RESOURCE_NEEDS`` lists each id with the
exact frame that dereferences it; ids carrying copy this script can reasonably
supply are seeded at USER scope, and the rest are verified so an absent one
fails immediately with a name and a stack location.

Dates are derived from the execution date and ``--cycle-day``.  For example,
running on 2026-08-27 with the default cycle day 5 creates the active cycle
2026-08-05 through 2026-09-04 and completed historical cycles before it.

Prerequisites:

* AWS credentials that can PutObject in the destination bucket.
* AWS credentials that can SendMessage to
  AggregationReadyUsers-productqa-masterpilot.
* A ProductQA API token in BIDGELY_TOKEN, or enter it at the secure prompt.
* Pilot 88001 must already include HIGH_BILL_ALERT in
  meta_data.supported_communication_events (this script verifies but does not
  silently modify the pilot-wide gate).

Usage (from this folder, after `uv sync` at the repo root):

    uv run python create_hba_s3_user.py
    uv run python create_hba_s3_user.py --dry-run
    uv run python create_hba_s3_user.py --anchor-date 2026-08-27 --cycle-day 5
    uv run python create_hba_s3_user.py --drop-mtd-section
    uv run python create_hba_s3_user.py --restore-from hba-s3-user-output/<run>/manifest.json

Cleanup
-------
Config keys this run changed are restored exactly, from a snapshot on disk, so an
interrupted run can be finished with ``--restore-from``. String resources cannot
be: ``2.1/stringResources`` exposes POST and PUT but no DELETE, so any id this
run seeds stays as a USER-scope row. Those are recorded in the manifest under
``resourceSnapshot`` and reported as RESOURCE LEFTOVER warnings at the end of the
run - harmless for a throwaway QA user, worth cleaning up in SQL if the user is
reused for anything that should show the pilot's real copy.

This is ProductQA test-data automation.  It intentionally produces abnormally
high current-cycle consumption after establishing a normal expected bill.
"""

from __future__ import annotations

import argparse
import calendar
import getpass
import hashlib
import json
import math
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid as uuid_lib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:  # pragma: no cover - reported cleanly by main
    boto3 = None
    BotoCoreError = ClientError = Exception


DEFAULT_BUCKET = "bidgely-masterpilot-resi-fl-productqa"
DEFAULT_REGION = "us-west-2"
DEFAULT_API = "https://api-server-masterpilot-productqa.bidgely.com"
DEFAULT_SQS_QUEUE = "AggregationReadyUsers-productqa-masterpilot"
DEFAULT_SQS_QUEUE_URL = (
    "https://sqs.us-west-2.amazonaws.com/189675173661/"
    "AggregationReadyUsers-productqa-masterpilot"
)
DEFAULT_PILOT = 88001
DEFAULT_EMAIL_PREFIX = "bidgelyqa+AUT_MP01_"
DEFAULT_EMAIL_DOMAIN = "bidgely.com"
DEFAULT_TIMEZONE = "America/Los_Angeles"
DEFAULT_CYCLE_DAY = 5
# Match the supplied ProductQA fixture shape: two full years of completed bills.
DEFAULT_HISTORY_CYCLES = 24
DEFAULT_OUTPUT = Path("hba-s3-user-output")
API_503_MAX_ATTEMPTS = 3
API_503_RETRY_SECONDS = 5 * 60
SQS_AGGREGATE_TYPES = ("ENERGY_CONSUMPTION", "TOU", "BILLING_COST", "TIER")
# The two interfaces use different representations of the same mode:
# AggregateUploadEvent JAXB expects the enum name, while the billing-data REST
# endpoint parses the lowercase wire value via AggMode.fromString("month").
SQS_AGG_MODE = "MONTH"
API_AGG_MODE = "month"
SQS_STREAM_TYPE = "DELIVERED_AND_RECEIVED"
SQS_SENT_TIMESTAMP_ATTRIBUTE = "MessageSentTimestamp"

# How long a MONTH aggregation must have been in flight before an unchanged
# output fingerprint may be accepted as an idempotent rewrite rather than an
# unprocessed message. Long enough for aggregationMessageProcessor to pick the
# message up, short enough not to stall a run that had nothing to change.
AGGREGATION_NOOP_GRACE_SECONDS = 120

# HighBillAlertUserProcessor accepts a projection in
# [expected * threshold, expected * threshold * 4). Aim for the lower-middle of
# that band so ordinary variation cannot push the run out either side.
HBA_PROJECTION_THRESHOLD = 1.1
HBA_TARGET_RATIO = 2.2
# How many reseed passes may run before the run gives up on pulling the
# projection back inside the accept band. One corrective pass converges:
# raising the multiplier lifts the projection far more than it lifts the
# expected bill, which is a mean across several cycles.
CALIBRATION_ATTEMPTS = 2
# Aim a correction this far above the floor rather than exactly at it, so a
# projection that lands short does not need a third pass.
HBA_BAND_SAFETY_MARGIN = 1.15

HID = 1
MEASUREMENT_TYPE = "ELECTRIC"
ANY_REGEX = "(?s).*"
NUM_REGEX = r"^\d+(\.\d+)?$"
RESOURCE_LOCALE = "en_US"

# Resolving a string resource id that the render dereferences without a null check is the
# single most common reason an HBA email silently never arrives. Every provider on the HBA
# path does `stringMap.get(<id>).getText()` bare, so ONE absent id throws out of
# GenericEventDataEmailContentBuilder.getHtmlContentData and aborts the whole email - not
# just its section. Notification state is only written on a successful send, so from the
# API side the result is byte-for-byte identical to a trigger-gate rejection: an empty
# `notificationStatus/.../HIGH_BILL_ALERT/ELECTRIC/Email`. The only way to tell them apart
# is Emailer.log. Hence: check and seed every one of these BEFORE spending an hour on
# aggregation passes.
HBA_TEASER_RESOURCE_ID = "com.bidgely.cloud.email.teaser.highBillAlert"
HBA_FOOTER_FAQ_RESOURCE_ID = "com.bidgely.cloud.email.text.footer.faq.hba.url"


@dataclass(frozen=True)
class ResourceNeed:
    """One string resource the HBA render dereferences without a null check.

    ``config_type``/``config_key`` name the config that chooses the id, when the id is
    configurable. When that config is unset the platform falls back to ``resource_id``,
    a constant compiled into the section config class - which is exactly the case that
    bites, because nothing seeds those defaults.

    ``text`` is the fallback this script writes at USER scope when the id resolves
    nowhere. ``None`` means "verify only": the id needs content this script has no
    business inventing (an image URL, say), so an absent one is a hard failure naming
    the deref site instead of a placeholder that renders as a broken email.

    ``format_args`` is how many ``%s`` the consuming code passes through
    ``String.format``. A resource whose arity disagrees crashes on the format call
    rather than the lookup, which is a different stack for the same root cause.
    """

    resource_id: str
    deref: str
    text: str | None = None
    format_args: int = 0
    config_type: str | None = None
    config_key: str | None = None


HBA_RESOURCE_NEEDS: tuple[ResourceNeed, ...] = (
    # TeaserTextEnum maps HIGH_BILL_ALERT here (EmailStrings.java:314) and
    # EmailStringsListProvider:1526 requests it, but the HBA feature shipped
    # `com.bidgely.cloud.high.bill.alert.teaser` instead - which nothing reads. The id
    # below exists in no deployment, so every pilot enabling HBA with the V1 TEASER
    # section hits this.
    ResourceNeed(
        resource_id=HBA_TEASER_RESOURCE_ID,
        deref="EmailTeaserSectionContentProvider.getTeaserText:73",
        text=(
            "Your electricity bill is projected to be higher than usual this billing "
            "cycle. See what is driving it."
        ),
    ),
    # BillProjectionSectionConfig.PROJECTED_COST_TEXT_STRING_RESOURCE_DEFAULT. Seeded
    # nowhere, and `projected_cost_text` is unset on a fresh pilot, so the compiled-in
    # default is what gets requested. The %s is the billing-cycle end date, supplied by
    # i18nUtils.getFormattedDuration.
    ResourceNeed(
        resource_id="com.bidgely.cloud.email.bill.projection.projected.cost.text",
        deref="BillProjectionSectionContentProvider.populateBpV2Version:320",
        text="Projected cost by %s",
        format_args=1,
        config_type="bill_projection_section_configs",
        config_key="projected_cost_text",
    ),
    # ADJUSTED_PROJECTED_COST_TEXT_STRING_RESOURCE_DEFAULT, same story. Reached only when
    # showAdjustedProjectedCost holds: TOU user, show_bp_consumption off, non-zero
    # adjustedProjectedPrice. That is precisely the masterpilot TOU test user, so treat it
    # as always on the path rather than an edge case.
    ResourceNeed(
        resource_id="com.bidgely.cloud.email.bill.projection.adjusted.projected.cost.text",
        deref="BillProjectionSectionContentProvider.populateBpV2Version:336",
        text="Adjusted projected cost",
        config_type="bill_projection_section_configs",
        config_key="adjusted_projected_cost_text",
    ),
    # EmailStrings.LONG_TRANSITION_BC_DISCLAIMER_TEXT, dereferenced unguarded whenever
    # billProjectionEventData.transitionBillCycle is true. Seeded here so a transition
    # cycle does not become a fresh mystery on some later run.
    ResourceNeed(
        resource_id="com.bidgely.cloud.email.text.longBillCycleDisclaimer",
        deref="BillProjectionSectionContentProvider.populateBpV2Version:365",
        text="This billing cycle is longer than usual.",
    ),
    # EmailFooterSectionContentProvider:239 and :436 both deref the id named by
    # `high_bill_alert_faq_page_url` with no null check. configure_hba_user repoints that
    # config at the id below, which every other event already uses; the platform default,
    # `com.bidgely.cloud.high.bill.alert.faq.url`, is undefined everywhere.
    ResourceNeed(
        resource_id=HBA_FOOTER_FAQ_RESOURCE_ID,
        deref="EmailFooterSectionContentProvider:239,436",
        text="https://www.bidgely.com/faq",
        config_type="email_template",
        config_key="high_bill_alert_faq_page_url",
    ),
    # Verify-only from here down. These are requested by
    # EmailStringsListProvider.HIGH_BILL_ALERT and dereferenced unguarded, but they hold
    # content (icons, disclaimer copy, headlines) that belongs to whoever owns the pilot's
    # string resources - inventing it would produce a plausible-looking but wrong email.
    # They were all present on 88001; the check exists so a pilot that lacks one gets an
    # explicit id and stack location instead of an empty notification state.
    ResourceNeed(
        resource_id="com.bidgely.cloud.high.bill.alert.icon",
        deref="EmailGreetingSectionContentProvider.populateHighBillAlertContent:192",
    ),
    ResourceNeed(
        resource_id="com.bidgely.cloud.email.mtd.breakdown.section.title.text",
        deref="MTDBreakDownContentProvider.getMTDBreakDownSectionDetail",
    ),
    ResourceNeed(
        resource_id="com.bidgely.cloud.email.hba.recommenddationHeadline",  # typo is real
        deref="EmailEnergySavingsDataMapProvider.HIGH_BILL_ALERT:305",
    ),
    ResourceNeed(resource_id="com.bidgely.cloud.email.hba.disclaimer1", deref="HBA disclaimer"),
    ResourceNeed(resource_id="com.bidgely.cloud.email.hba.disclaimer2", deref="HBA disclaimer"),
)

HBA_SECTIONS = [
    "TEASER",
    "HEADER_V4",
    "GREETING_V2",
    "BILL_PROJECTION_V2",
    "MTD_BREAK_DOWN_SECTION",
    "ENERGY_SAVINGS_TIPS_V2",
    "CTA_WITH_DES_SECTION_V2",
    "FEEDBACK_V4",
    "FOOTER_V4",
]


class SetupError(RuntimeError):
    """Actionable setup failure."""


@dataclass(frozen=True)
class Ids:
    external_user: str
    account: str
    premise: str
    meter: str
    phone: str


@dataclass(frozen=True)
class Cycle:
    start: date
    end_exclusive: date

    @property
    def end_inclusive(self) -> date:
        return self.end_exclusive - timedelta(days=1)

    @property
    def days(self) -> int:
        return (self.end_exclusive - self.start).days


@dataclass
class Generated:
    ids: Ids
    active_cycle: Cycle
    user_file: Path
    invoice_file: Path
    raw_seed_file: Path
    raw_high_file: Path
    seed_end: datetime
    high_start: datetime
    high_end: datetime
    object_keys: dict[str, str]
    manifest_file: Path
    log_file: Path


LOG_FILE: Path | None = None
STEP_NUMBER = 0


def set_log_file(path: Path) -> None:
    global LOG_FILE
    LOG_FILE = path
    path.write_text("", encoding="utf-8")


def log(message: str, level: str = "INFO") -> None:
    line = f"{datetime.now().astimezone().isoformat(timespec='seconds')} | {level:<5} | {message}"
    print(line, flush=True)
    if LOG_FILE is not None:
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def step(name: str, **context: Any) -> None:
    global STEP_NUMBER
    STEP_NUMBER += 1
    fields = " ".join(f"{key}={value}" for key, value in context.items() if value is not None)
    log(f"STEP {STEP_NUMBER:02d} START | {name}" + (f" | {fields}" if fields else ""))


def add_months(value: date, months: int, day: int) -> date:
    absolute = value.year * 12 + value.month - 1 + months
    year, month0 = divmod(absolute, 12)
    month = month0 + 1
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def active_cycle_for(anchor: date, cycle_day: int) -> Cycle:
    this_start = date(anchor.year, anchor.month, min(cycle_day, calendar.monthrange(anchor.year, anchor.month)[1]))
    if anchor < this_start:
        this_start = add_months(this_start, -1, cycle_day)
    return Cycle(this_start, add_months(this_start, 1, cycle_day))


def completed_cycles(active: Cycle, count: int, cycle_day: int) -> list[Cycle]:
    starts = [add_months(active.start, -offset, cycle_day) for offset in range(count, 0, -1)]
    return [Cycle(start, add_months(start, 1, cycle_day)) for start in starts]


def random_digits(length: int, *, first_nonzero: bool = True) -> str:
    first = str(secrets.randbelow(9) + 1) if first_nonzero else str(secrets.randbelow(10))
    return first + "".join(str(secrets.randbelow(10)) for _ in range(length - 1))


def generate_ids() -> Ids:
    values: set[str] = set()

    def unique(length: int) -> str:
        while True:
            candidate = random_digits(length)
            if candidate not in values:
                values.add(candidate)
                return candidate

    return Ids(unique(10), unique(10), unique(10), unique(9), unique(10))


def dynamic_qa_email(ids: Ids) -> str:
    """Build a unique, traceable Bidgely QA plus-address for this run."""
    return f"{DEFAULT_EMAIL_PREFIX}{ids.meter}@{DEFAULT_EMAIL_DOMAIN}"


def interval_value(ts: datetime, *, multiplier: float = 1.0) -> float:
    """Deterministic residential kWh for one 15-minute interval."""
    hour = ts.hour + ts.minute / 60
    base = 0.28
    morning = 0.30 * math.exp(-((hour - 7.5) / 2.0) ** 2)
    evening = 0.55 * math.exp(-((hour - 19.0) / 2.7) ** 2)
    seasonal = 1.0 + 0.12 * math.sin((ts.timetuple().tm_yday / 365.25) * 2 * math.pi)
    weekday = 1.05 if ts.weekday() >= 5 else 1.0
    return round((base + morning + evening) * seasonal * weekday * multiplier, 3)


def stamp_offset(stamp: str, seconds: int) -> str:
    """Derive a distinct but deterministic batch stamp from the run stamp."""
    return (datetime.strptime(stamp, "%Y%m%d%H%M%S") + timedelta(seconds=seconds)).strftime("%Y%m%d%H%M%S")


def raw_line(ids: Ids, ts: datetime, value: float) -> str:
    return (
        f"{ids.external_user}|{ids.account}|{ids.premise}|{ids.meter}|"
        f"{ts:%Y%m%d%H%M%S}|{value:.3f}|0|AMI"
    )


def iter_intervals(start: datetime, end_inclusive: datetime) -> Iterable[datetime]:
    current = start
    while current <= end_inclusive:
        yield current
        current += timedelta(minutes=15)


def write_lines(path: Path, lines: Iterable[str]) -> int:
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for line in lines:
            handle.write(line)
            handle.write("\n")
            count += 1
    return count


def user_detail_line(ids: Ids, args: argparse.Namespace, active: Cycle) -> str:
    service_start = add_months(active.start, -(args.history_cycles + 1), args.cycle_day)
    fields = [
        ids.external_user,
        ids.account,
        ids.premise,
        "RES",
        args.email,
        args.first_name.upper(),
        args.last_name.upper(),
        args.address,
        "",
        "",
        "",
        args.city,
        args.state,
        args.zipcode,
        "",
        args.address,
        "",
        "",
        "",
        args.city,
        args.state,
        args.zipcode,
        ids.phone,
        "",
        "EN",
        "",
        "",
        ids.meter,
        "ELECTRIC",
        service_start.isoformat(),
        "",
        args.rate_plan,
        service_start.isoformat(),
        args.billing_cycle,
        "",
        "FALSE",
        "AMI",
        "",
    ]
    if len(fields) != 38:
        raise AssertionError(f"USERENROLL schema drift: expected 38 fields, got {len(fields)}")
    return "|".join(fields)


def cycle_usage(cycle: Cycle) -> tuple[float, float]:
    off_peak = 0.0
    peak = 0.0
    cursor = datetime.combine(cycle.start, dt_time(0, 15))
    final = datetime.combine(cycle.end_exclusive, dt_time(0, 0))
    while cursor <= final:
        value = interval_value(cursor)
        if 16 <= cursor.hour < 21:
            peak += value
        else:
            off_peak += value
        cursor += timedelta(minutes=15)
    return round(off_peak, 3), round(peak, 3)


def invoice_lines(ids: Ids, cycles: list[Cycle], args: argparse.Namespace) -> Iterable[str]:
    for cycle in cycles:
        off_peak, peak = cycle_usage(cycle)
        fixed_cost = 13.50
        off_cost = off_peak * 0.19
        peak_cost = peak * 0.34
        total_usage = off_peak + peak
        total_cost = off_cost + peak_cost + fixed_cost
        prefix = (
            f"{ids.external_user}|{ids.account}|{ids.premise}|{ids.meter}|ELECTRIC|"
            f"{args.billing_cycle}|{cycle.start.isoformat()}|{cycle.end_inclusive.isoformat()}|{cycle.days}"
        )
        rows = [
            ("TOTAL", "TOTAL", total_usage, total_cost),
            ("TOU_OP", "CONSUMPTION_BASED", off_peak, off_cost),
            ("TOU_MP", "CONSUMPTION_BASED", 0.0, 0.0),
            ("TOU_PK", "CONSUMPTION_BASED", peak, peak_cost),
            ("FIXED", "FIXED", 0.0, fixed_cost),
        ]
        for band, charge_type, usage, cost in rows:
            yield f"{prefix}|{band}|{charge_type}|{usage:.3f}|{cost:.6f}|AMI|"


def build_files(args: argparse.Namespace) -> Generated:
    tz = ZoneInfo(args.timezone)
    wall_date = datetime.now(tz).date()
    anchor = date.fromisoformat(args.anchor_date) if args.anchor_date else wall_date
    active = active_cycle_for(anchor, args.cycle_day)
    fraction = (anchor - active.start).days / active.days
    if not (0.50 <= fraction <= 0.95):
        if args.anchor_date:
            raise SetupError(
                f"explicit anchor {anchor} is {fraction:.1%} through cycle "
                f"{active.start}..{active.end_inclusive}; choose a date in the 50%-95% HBA window"
            )
        # Make the no-argument command useful every day. If the current cycle is
        # still before midpoint, replay the previous cycle; otherwise replay the
        # current cycle at 70%. HBA uses the user's maximum logical data timestamp.
        if fraction < 0.50:
            prior_start = add_months(active.start, -1, args.cycle_day)
            active = Cycle(prior_start, active.start)
        anchor = active.start + timedelta(days=round(active.days * 0.70))
        fraction = (anchor - active.start).days / active.days
        replay_reason = (
            f"wall date {wall_date} was outside HBA evaluation; using logical replay date {anchor} "
            f"({fraction:.1%} through {active.start}..{active.end_inclusive})"
        )
    else:
        replay_reason = None

    ids = generate_ids()
    args.email = args.email or dynamic_qa_email(ids)
    # Bucket-generated examples use UTC in the filename timestamp.
    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    run_dir = args.output_dir / f"hba-{ids.external_user}-{run_stamp}"
    run_dir.mkdir(parents=True, exist_ok=False)
    log_file = run_dir / "run.log"
    set_log_file(log_file)
    step(
        "generate dynamic HBA files",
        external_user=ids.external_user,
        meter=ids.meter,
        email=args.email,
    )
    if replay_reason:
        log(f"LOGICAL DATE SELECTED | {replay_reason}", "WARN")

    suffix = random_digits(20, first_nonzero=True)
    user_key = f"USERENROLL_D_{run_stamp}_{suffix}.txt"
    invoice_key = f"INVOICE_01_{run_stamp}_T{secrets.randbelow(5) + 1}.txt"
    # Each RAW file needs its OWN batch stamp. Two files that share the
    # 14-digit stamp and differ only in the trailing sequence (…001 vs …002)
    # collide: masterpilot ingests the first and silently discards the second,
    # with no error anywhere. Verified 2026-08-27 — eight consecutive runs
    # ingested only the seed day, and re-uploading the identical high-usage
    # payload under a fresh stamp ingested in 18 seconds. Keep sequence 001 for
    # both files; that is the only sequence observed to ingest.
    raw_seed_key = f"RAW_D_900_S_{run_stamp}001-000_01.txt"
    raw_high_key = f"RAW_D_900_S_{stamp_offset(run_stamp, 1)}001-000_01.txt"

    user_file = run_dir / user_key
    invoice_file = run_dir / invoice_key
    raw_seed_file = run_dir / raw_seed_key
    raw_high_file = run_dir / raw_high_key
    user_file.write_text(user_detail_line(ids, args, active) + "\n", encoding="utf-8")

    history = completed_cycles(active, args.history_cycles, args.cycle_day)
    invoice_count = write_lines(invoice_file, invoice_lines(ids, history, args))

    # Historical completed bills are supplied by INVOICE. Keep RAW ingestion
    # strictly inside the active cycle so ingestion cannot launch a multi-year
    # historical raw-data aggregation.
    seed_start = datetime.combine(active.start, dt_time(0, 0))
    seed_end = datetime.combine(active.start, dt_time(23, 45))
    seed_count = write_lines(
        raw_seed_file,
        (raw_line(ids, ts, interval_value(ts)) for ts in iter_intervals(seed_start, seed_end)),
    )

    if args.anchor_date or anchor != wall_date:
        high_end = datetime.combine(anchor, dt_time(23, 45))
    else:
        now_local = datetime.now(tz).replace(tzinfo=None, second=0, microsecond=0)
        minutes = (now_local.minute // 15) * 15
        high_end = now_local.replace(minute=minutes)
    high_start = datetime.combine(active.start + timedelta(days=1), dt_time(0, 0))
    high_count = write_lines(
        raw_high_file,
        (
            raw_line(ids, ts, interval_value(ts, multiplier=args.high_usage_multiplier or 6.0))
            for ts in iter_intervals(high_start, high_end)
        ),
    )

    manifest_file = run_dir / "manifest.json"
    manifest = {
        "createdAt": datetime.now().astimezone().isoformat(),
        "pilotId": args.pilot_id,
        "bucket": args.bucket,
        "region": args.region,
        "email": args.email,
        "timezone": args.timezone,
        "wallDate": wall_date.isoformat(),
        "logicalAnchorDate": anchor.isoformat(),
        "logicalReplay": replay_reason is not None,
        "logicalReplayReason": replay_reason,
        "ids": ids.__dict__,
        "activeCycle": {
            "start": active.start.isoformat(),
            "endInclusive": active.end_inclusive.isoformat(),
            "fractionAtAnchor": fraction,
        },
        "aggregation": {
            "transport": "SQS_XML",
            "queueName": DEFAULT_SQS_QUEUE,
            "mode": SQS_AGG_MODE,
            "startTimestamp": f"{seed_start:%Y%m%d%H%M%S}",
            "endTimestamp": f"{high_end:%Y%m%d%H%M%S}",
            "consumptionTypes": list(SQS_AGGREGATE_TYPES),
            "streamTypes": [SQS_STREAM_TYPE],
            "messages": [],
        },
        "files": {
            "user": {"key": user_key, "rows": 1},
            "invoice": {"key": invoice_key, "rows": invoice_count},
            "rawSeed": {"key": raw_seed_key, "rows": seed_count, "lastTimestamp": f"{seed_end:%Y%m%d%H%M%S}"},
            "rawHigh": {"key": raw_high_key, "rows": high_count, "lastTimestamp": f"{high_end:%Y%m%d%H%M%S}"},
        },
        "uuid": None,
        "notification": None,
        "runLog": str(log_file),
    }
    manifest_file.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    log(f"Generated {run_dir}")
    log(f"  active cycle: {active.start} -> {active.end_inclusive} ({fraction:.1%} elapsed)")
    log(f"  unique IDs: external={ids.external_user}, account={ids.account}, premise={ids.premise}, meter={ids.meter}")
    log(f"  recipient email: {args.email}")
    log(f"  rows: USER=1, INVOICE={invoice_count}, RAW-SEED={seed_count}, RAW-HIGH={high_count}")
    log(f"FILE READY | type=USERENROLL file={user_file} rows=1")
    log(f"FILE READY | type=INVOICE file={invoice_file} rows={invoice_count}")
    log(
        f"FILE READY | type=RAW_SEED file={raw_seed_file} rows={seed_count} "
        f"scope=current_cycle_only first={seed_start:%Y%m%d%H%M%S} last={seed_end:%Y%m%d%H%M%S}"
    )
    log(f"FILE READY | type=RAW_HIGH file={raw_high_file} rows={high_count} last={high_end:%Y%m%d%H%M%S}")
    return Generated(
        ids,
        active,
        user_file,
        invoice_file,
        raw_seed_file,
        raw_high_file,
        seed_end,
        high_start,
        high_end,
        {"user": user_key, "invoice": invoice_key, "rawSeed": raw_seed_key, "rawHigh": raw_high_key},
        manifest_file,
        log_file,
    )


class ApiClient:
    def __init__(self, base: str, token: str) -> None:
        self.base = base.rstrip("/")
        self.token = token

    def request(self, method: str, path: str, body: Any = None, timeout: int = 180) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(1, API_503_MAX_ATTEMPTS + 1):
            req = urllib.request.Request(self.base + path, data=data, method=method)
            req.add_header("Authorization", f"Bearer {self.token}")
            req.add_header("Accept", "application/json")
            if data is not None:
                req.add_header("Content-Type", "application/json")
            started = time.monotonic()
            log(
                f"HTTP REQUEST | method={method} path={path} "
                f"attempt={attempt}/{API_503_MAX_ATTEMPTS}"
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    raw = response.read().decode("utf-8", "replace")
                    log(
                        f"HTTP RESPONSE | method={method} path={path} status={response.status} "
                        f"attempt={attempt}/{API_503_MAX_ATTEMPTS} seconds={time.monotonic() - started:.2f}"
                    )
                    if not raw.strip():
                        return None
                    try:
                        return json.loads(raw)
                    except json.JSONDecodeError:
                        return raw
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", "replace")
                if exc.code == 503 and attempt < API_503_MAX_ATTEMPTS:
                    log(
                        f"HTTP 503 RETRY | method={method} path={path} "
                        f"attempt={attempt}/{API_503_MAX_ATTEMPTS} "
                        f"next_attempt={attempt + 1} wait_seconds={API_503_RETRY_SECONDS} "
                        f"body={raw[:300]!r}",
                        "WARN",
                    )
                    time.sleep(API_503_RETRY_SECONDS)
                    continue
                log(
                    f"HTTP ERROR | method={method} path={path} status={exc.code} "
                    f"attempt={attempt}/{API_503_MAX_ATTEMPTS} body={raw[:300]!r}",
                    "ERROR",
                )
                raise SetupError(f"{method} {path} -> HTTP {exc.code}: {raw[:700]}") from exc
            except urllib.error.URLError as exc:
                raise SetupError(f"{method} {path} transport failure: {exc.reason}") from exc

        raise AssertionError("unreachable API retry state")


def payload_of(value: Any) -> Any:
    if isinstance(value, dict) and "payload" in value:
        return value["payload"]
    return value


def extract_rows(response: Any) -> list[dict[str, Any]]:
    value = payload_of(response)
    if isinstance(value, dict) and isinstance(value.get("data"), list):
        return value["data"]
    return value if isinstance(value, list) else []


def resolve_uuid_once(api: ApiClient, pilot_id: int, external_user: str) -> str | None:
    params = urllib.parse.urlencode(
        {"pilotId": pilot_id, "externalUserId": f"{pilot_id}_{external_user}", "limit": 10, "offset": 0}
    )
    rows = extract_rows(api.request("GET", f"/v2.0/pii/users?{params}"))
    matches = [str(row.get("uuid")) for row in rows if row.get("uuid")]
    if len(matches) > 1:
        raise SetupError(f"external ID unexpectedly resolved to multiple UUIDs: {matches}")
    return matches[0] if matches else None


def wait_for_uuid(api: ApiClient, args: argparse.Namespace, external_user: str) -> str:
    deadline = time.monotonic() + args.wait_minutes * 60
    while time.monotonic() < deadline:
        uuid = resolve_uuid_once(api, args.pilot_id, external_user)
        if uuid:
            log(f"USER RESOLVED | external_user={external_user} uuid={uuid}")
            return uuid
        log(f"WAIT USER | external_user={external_user} state=uuid_not_created")
        time.sleep(args.poll_seconds)
    raise SetupError(f"UUID was not created within {args.wait_minutes} minutes")


def config_kvs(value: Any, config_type: str) -> list[dict[str, Any]]:
    raw = value.get(config_type) if isinstance(value, dict) else None
    if isinstance(raw, str):
        raw = json.loads(raw)
    return raw.get("kvs", []) if isinstance(raw, dict) else []


def verify_pilot_gate(api: ApiClient, pilot_id: int) -> None:
    cfg = api.request("GET", f"/entities/pilot/{pilot_id}/configs")
    events = next(
        (row.get("val", "") for row in config_kvs(cfg, "meta_data") if row.get("key") == "supported_communication_events"),
        "",
    )
    if "HIGH_BILL_ALERT" not in {item.strip() for item in events.split(",")}:
        raise SetupError(
            f"pilot {pilot_id} does not support HIGH_BILL_ALERT. Enable the pilot gate first; "
            "user-level configuration cannot bypass the automatic processor gate."
        )
    log(f"Verified pilot {pilot_id} supported_communication_events contains HIGH_BILL_ALERT")


def post_config(
    api: ApiClient,
    entity_id: str,
    config_type: str,
    values: dict[str, tuple[str, str, str, str]],
) -> None:
    body = {
        "configType": config_type,
        "configKVs": [
            {
                "configKey": key,
                "configVal": val,
                "configDataType": data_type,
                "configRegex": regex,
                "configDocumentation": documentation,
            }
            for key, (val, data_type, regex, documentation) in values.items()
        ],
    }
    api.request("POST", f"/entities/{entity_id}/configs", body)
    log(f"CONFIG OK | entity={entity_id} config_type={config_type} keys={','.join(values)}")


def read_user_config(api: ApiClient, uuid: str, config_type: str) -> dict[str, tuple[str, str]]:
    """Return {key: (value, source)} for one config type as this user resolves it.

    ``configSource`` distinguishes a value the user already owns (USER) from one
    inherited from the pilot (PILOT), which is what makes it possible to put
    every key back exactly as it was.
    """
    quoted = urllib.parse.quote(config_type, safe="")
    try:
        rows = payload_of(api.request("GET", f"/v2.0/configs/{quoted}/user/{uuid}"))
    except SetupError as exc:
        log(f"CONFIG READ FAILED | uuid={uuid} config_type={config_type} error={exc}", "WARN")
        return {}
    resolved: dict[str, tuple[str, str]] = {}
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get("key") is not None:
                resolved[str(row["key"])] = (
                    "" if row.get("val") is None else str(row["val"]),
                    str(row.get("configSource") or "UNKNOWN"),
                )
    return resolved


def same_config_value(current: str, desired: str) -> bool:
    """Compare two config values the way the platform would read them."""
    if current == desired:
        return True
    for parse in (json.loads, float):
        try:
            return parse(current) == parse(desired)
        except (ValueError, TypeError):
            continue
    return False


def ensure_configs(
    api: ApiClient,
    uuid: str,
    config_type: str,
    values: dict[str, tuple[str, str, str, str]],
    snapshot: dict[str, dict[str, Any]],
) -> None:
    """Write only the keys whose effective value differs, recording what was there.

    Skipping equal values keeps the run from taking ownership of settings the
    user or pilot already had right, and the snapshot lets the finally-block put
    back exactly what this run changed - and nothing else.
    """
    current = read_user_config(api, uuid, config_type)
    pending: dict[str, tuple[str, str, str, str]] = {}
    for key, spec in values.items():
        desired, data_type, regex, _documentation = spec
        existing = current.get(key)
        if existing is not None and same_config_value(existing[0], desired):
            log(
                f"CONFIG KEEP | uuid={uuid} config_type={config_type} key={key} "
                f"value={existing[0][:60]!r} source={existing[1]} (already satisfies HBA)"
            )
            continue
        recorded = snapshot.setdefault(config_type, {})
        if key in recorded:
            # Second write of the same key in one run (the seed window is set
            # wide, then narrowed). Keep the ORIGINAL value, not the one this
            # run just wrote, or restore would put back our own scratch value.
            recorded[key]["appliedValue"] = desired
        else:
            recorded[key] = {
                "previousValue": existing[0] if existing else None,
                "previousSource": existing[1] if existing else None,
                "appliedValue": desired,
                "dataType": data_type,
                "regex": regex,
            }
        pending[key] = spec
    if pending:
        post_config(api, uuid, config_type, pending)


def restore_configs(api: ApiClient, uuid: str, snapshot: dict[str, dict[str, Any]]) -> None:
    """Put back exactly the keys this run changed. Never guess a 'normal' value."""
    for config_type, keys in snapshot.items():
        for key, meta in keys.items():
            previous = meta.get("previousValue")
            source = meta.get("previousSource")
            try:
                if previous is None:
                    log(
                        f"CONFIG LEFTOVER | uuid={uuid} config_type={config_type} key={key} "
                        "had no value before this run, so a user-level override remains. "
                        f"Remove it with DELETE /v2.0/configs/{config_type}/user/{uuid} "
                        "if this user will be reused.",
                        "WARN",
                    )
                    continue
                if source and source != "USER":
                    log(
                        f"CONFIG INHERITED | uuid={uuid} config_type={config_type} key={key} "
                        f"came from {source}; writing the inherited value {previous[:60]!r} back "
                        "as an explicit user override rather than leaving this run's value.",
                        "WARN",
                    )
                post_config(
                    api,
                    uuid,
                    config_type,
                    {key: (previous, meta["dataType"], meta["regex"], "restored by create_hba_s3_user")},
                )
                log(
                    f"CONFIG RESTORED | uuid={uuid} config_type={config_type} key={key} "
                    f"value={previous[:60]!r} source={source}"
                )
            except Exception as exc:  # never let one key abort the rest of cleanup
                log(
                    f"CONFIG RESTORE FAILED | uuid={uuid} config_type={config_type} key={key} "
                    f"error={exc}. Re-run with --restore-from <manifest> to finish cleanup.",
                    "ERROR",
                )


def report_resource_leftovers(
    api: ApiClient,
    uuid: str,
    resource_snapshot: dict[str, dict[str, Any]],
) -> None:
    """Report the user-level string resources this run created.

    ``2.1/stringResources`` exposes POST and PUT but no DELETE, so these rows are
    permanent - restore cannot undo them the way it undoes configs. Reporting them is
    the whole remedy: the next person to reuse this user needs to know the render is
    leaning on QA placeholder copy rather than the pilot's real strings.

    The owned-resource list is read back from the ``unresolved`` endpoint, which reports
    only what the entity itself defines (no hierarchy walk), so it confirms the writes
    actually landed at USER scope rather than resolving from somewhere upstream.
    """
    owned: set[str] = set()
    try:
        payload = payload_of(
            api.request("GET", f"/2.1/stringResources/USER/{uuid}/unresolved?locale={RESOURCE_LOCALE}")
        )
        if isinstance(payload, dict):
            owned = set(payload)
    except SetupError as exc:
        log(f"RESOURCE LIST FAILED | uuid={uuid} error={exc}", "WARN")

    for resource_id, meta in sorted(resource_snapshot.items()):
        confirmed = "" if not owned else (
            " (confirmed at USER scope)" if resource_id in owned else " (NOT found at USER scope)"
        )
        log(
            f"RESOURCE LEFTOVER | uuid={uuid} id={resource_id}{confirmed} "
            f"text={str(meta.get('text'))[:60]!r} needed_by={meta.get('deref')}. "
            "The string resource API has no DELETE, so this user-level row stays. That is "
            "harmless for a throwaway QA user; seed the id at pilot level and drop this row "
            "in SQL if the pilot needs the real copy.",
            "WARN",
        )


def configure_hba_user(
    api: ApiClient,
    uuid: str,
    seed_period_days: int,
    snapshot: dict[str, dict[str, Any]],
    resource_snapshot: dict[str, dict[str, Any]],
    wanted_sections: frozenset[str],
) -> None:
    user = payload_of(api.request("GET", f"/v2.0/users/{uuid}"))
    notification_type = "OPT_OUT"
    if isinstance(user, dict):
        notification_type = str(user.get("notificationUserType") or notification_type)

    ensure_configs(
        api,
        uuid,
        "event_subscriptions.HIGH_BILL_ALERT.ELECTRIC",
        {"delivery_modes": ('["Email"]', "JSON", ANY_REGEX, "HBA ELECTRIC delivery channels")},
        snapshot,
    )
    ensure_configs(
        api,
        uuid,
        f"event_subscriptions.HIGH_BILL_ALERT.ELECTRIC.{notification_type}",
        {"delivery_modes": ('["Email"]', "JSON", ANY_REGEX, f"HBA delivery channels for {notification_type}")},
        snapshot,
    )
    section_names = [name for name in HBA_SECTIONS if name in wanted_sections]
    if len(section_names) != len(HBA_SECTIONS):
        log(
            "HBA SECTIONS | omitting "
            f"{sorted(set(HBA_SECTIONS) - set(section_names))} from this user's section list"
        )
    sections = json.dumps([{"emailTemplateSection": name, "mandatory": False} for name in section_names])
    ensure_configs(
        api,
        uuid,
        "email_template_sections_config",
        {
            f"HIGH_BILL_ALERT.{notification_type}": (
                sections,
                "JSON",
                ANY_REGEX,
                f"HBA email sections for {notification_type}",
            )
        },
        snapshot,
    )
    ensure_configs(
        api,
        uuid,
        "email_template",
        {
            # EmailFooterSectionContentProvider:239 and :436 both do
            # stringMap.get(<this id>).getText() with no null check, so the id must
            # resolve to a real resource. `com.bidgely.cloud.high.bill.alert.faq.url`
            # is undefined everywhere; the conventional footer id below is seeded at
            # GLOBAL and is what every other event uses.
            "high_bill_alert_faq_page_url": (
                HBA_FOOTER_FAQ_RESOURCE_ID,
                "STRING_RESOURCE",
                ANY_REGEX,
                "HBA FAQ string resource id that actually resolves",
            )
        },
        snapshot,
    )
    ensure_hba_resources(api, uuid, resource_snapshot)

    # Only the seed window is forced. The three threshold keys are left alone
    # whenever the value already in place works, so a pilot that has tuned them
    # keeps its tuning instead of silently inheriting this script's numbers.
    ensure_configs(
        api,
        uuid,
        "meta_data",
        {
            "high_bill_projection_threshold": ("1.1", "DOUBLE", NUM_REGEX, "HBA projected to expected ratio"),
            "high_bill_alert_minimum_projection_difference_threshold": (
                "1",
                "INTEGER",
                NUM_REGEX,
                "HBA minimum projected minus expected amount",
            ),
            "high_bill_alert_trigger_period_in_days": (
                str(seed_period_days),
                "INTEGER",
                NUM_REGEX,
                "Temporary QA seed window covering the current logical cycle day",
            ),
            "high_bill_alert_bill_cycle_upper_bound": (
                "0.95",
                "DOUBLE",
                NUM_REGEX,
                "HBA latest eligible bill-cycle fraction",
            ),
        },
        snapshot,
    )


def resolve_resource_id(
    api: ApiClient,
    uuid: str,
    need: ResourceNeed,
    config_cache: dict[str, dict[str, tuple[str, str]]],
) -> str:
    """Return the id the render will actually request for this need.

    A configurable need uses whatever the config says; only when that config is unset
    does the platform fall back to the compiled-in default carried on ``need``. Reading
    it rather than assuming means a pilot that points a key at its own custom id gets
    that id checked, instead of this script verifying a default nobody uses.

    ``config_cache`` is shared across needs because ``email_template`` alone is ~344 rows
    and several needs read the same config type.
    """
    if not (need.config_type and need.config_key):
        return need.resource_id
    if need.config_type not in config_cache:
        config_cache[need.config_type] = read_user_config(api, uuid, need.config_type)
    configured = config_cache[need.config_type].get(need.config_key)
    if configured and configured[0].strip():
        return configured[0].strip()
    return need.resource_id


def probe_resource(api: ApiClient, uuid: str, resource_id: str) -> str | None:
    """Return the text this user resolves for an id, or None when nothing resolves.

    Uses the USER-scope per-key form, which walks the whole
    GLOBAL -> COUNTRY -> PILOT -> USER_SEGMENT -> CLUSTER -> USER hierarchy, so an id
    already seeded at pilot level reads back here and is left alone. Note the
    superficially similar `/2.1/stringResources/pilot/<id>/resource/<key>` form returns
    an empty payload even for ids that DO exist - useless as a probe, do not switch to it.
    """
    path = f"/2.1/stringResources/user/{uuid}/resource/{resource_id}?locale={RESOURCE_LOCALE}"
    try:
        payload = payload_of(api.request("GET", path)) or {}
    except SetupError as exc:
        log(f"RESOURCE READ FAILED | uuid={uuid} id={resource_id} error={exc}", "WARN")
        return None
    if isinstance(payload, dict) and payload.get(resource_id) is not None:
        return str(payload[resource_id])
    return None


def check_format_arity(resource_id: str, text: str, expected: int) -> None:
    """Warn when a resolved resource carries the wrong number of %s placeholders.

    Right id, wrong arity fails inside String.format instead of at the lookup, so it
    presents as a completely different stack for the same underlying cause. Warn rather
    than fail: the count is a heuristic (%% escapes, %1$s positional forms) and a false
    alarm must not block a run.
    """
    found = len(re.findall(r"(?<!%)%[sd]", text))
    if found != expected:
        log(
            f"RESOURCE ARITY | id={resource_id} expects {expected} format arg(s) but its text "
            f"has {found}; String.format will throw at the deref site if this is wrong. "
            f"text={text[:80]!r}",
            "WARN",
        )


def ensure_hba_resources(
    api: ApiClient,
    uuid: str,
    resource_snapshot: dict[str, dict[str, Any]],
) -> None:
    """Seed and verify every string resource the HBA render dereferences unguarded.

    Runs before any aggregation is queued, so a missing id costs one API call instead of
    an hour of passes that end in an empty notification state. Writes are USER-scope so a
    shared pilot is never touched, and each write is read back rather than trusted: the
    write endpoint reports ``{"status": true}`` whether or not the row landed.

    Every id created here is recorded in ``resource_snapshot`` because the platform has no
    DELETE for string resources - cleanup can only report them, so it has to know them.
    """
    missing_verify_only: list[ResourceNeed] = []
    config_cache: dict[str, dict[str, tuple[str, str]]] = {}
    for need in HBA_RESOURCE_NEEDS:
        resource_id = resolve_resource_id(api, uuid, need, config_cache)
        if resource_id != need.resource_id:
            log(
                f"RESOURCE CONFIGURED | uuid={uuid} {need.config_type}.{need.config_key} points at "
                f"{resource_id} instead of the platform default {need.resource_id}; checking that."
            )
        existing = probe_resource(api, uuid, resource_id)
        if existing is not None:
            log(f"RESOURCE KEEP | uuid={uuid} id={resource_id} value={existing[:60]!r}")
            if need.format_args:
                check_format_arity(resource_id, existing, need.format_args)
            continue
        if need.text is None:
            missing_verify_only.append(need)
            continue
        api.request(
            "POST",
            f"/2.1/stringResources/{uuid}/resource/{resource_id}",
            [
                {
                    "id": resource_id,
                    "entityId": uuid,
                    "locale": RESOURCE_LOCALE,
                    "text": need.text,
                }
            ],
        )
        resolved = probe_resource(api, uuid, resource_id)
        if resolved is None:
            raise SetupError(
                f"wrote {resource_id} for {uuid} but it does not resolve. {need.deref} "
                "dereferences it without a null check, so the HBA render will abort and the "
                "notification state will stay empty."
            )
        resource_snapshot[resource_id] = {
            "locale": RESOURCE_LOCALE,
            "text": need.text,
            "deref": need.deref,
        }
        log(f"RESOURCE SEEDED | uuid={uuid} id={resource_id} text={need.text!r}")
        if need.format_args:
            check_format_arity(resource_id, need.text, need.format_args)

    if missing_verify_only:
        detail = "; ".join(f"{n.resource_id} (dereferenced at {n.deref})" for n in missing_verify_only)
        raise SetupError(
            "these string resource ids resolve nowhere for this user and this script will not "
            f"invent their content: {detail}. Each is dereferenced as stringMap.get(id).getText() "
            "with no null check, so any one of them aborts the whole HIGH_BILL_ALERT render and "
            "leaves the notification state empty. Seed them at pilot level, then re-run."
        )


def set_hba_trigger_period(
    api: ApiClient,
    uuid: str,
    days: int,
    documentation: str,
    snapshot: dict[str, dict[str, Any]],
) -> None:
    ensure_configs(
        api,
        uuid,
        "meta_data",
        {
            "high_bill_alert_trigger_period_in_days": (
                str(days),
                "INTEGER",
                NUM_REGEX,
                documentation,
            )
        },
        snapshot,
    )
    log(f"HBA TRIGGER PERIOD | uuid={uuid} days={days}")


def upload_file(s3: Any, bucket: str, path: Path, key: str) -> None:
    log(f"S3 UPLOAD START | file={path} s3=s3://{bucket}/{key}")
    try:
        s3.upload_file(
            str(path),
            bucket,
            key,
            ExtraArgs={
                "ContentType": "text/plain; charset=UTF-8",
                "ServerSideEncryption": "AES256",
                "Metadata": {"utility_file_name": key},
            },
        )
        head = s3.head_object(Bucket=bucket, Key=key)
    except (BotoCoreError, ClientError) as exc:
        raise SetupError(f"S3 upload failed for {key}: {exc}") from exc
    if head.get("Metadata", {}).get("utility_file_name") != key:
        raise SetupError(f"S3 metadata verification failed for {key}")
    log(
        f"S3 UPLOAD OK | file={path.name} s3=s3://{bucket}/{key} "
        f"bytes={head['ContentLength']} etag={head.get('ETag')} metadata_verified=true"
    )


def settle(args: argparse.Namespace, label: str) -> None:
    if args.settle_seconds <= 0:
        return
    log(f"Waiting {args.settle_seconds}s for {label} ingestion ...")
    time.sleep(args.settle_seconds)


def notification_state(api: ApiClient, uuid: str) -> Any:
    return payload_of(
        api.request(
            "GET",
            f"/notification/notificationStatus/{uuid}/{HID}/HIGH_BILL_ALERT/{MEASUREMENT_TYPE}/Email",
        )
    )


def was_sent(state: Any) -> bool:
    """True once the notification state records a delivered HBA email.

    HIGH_BILL_ALERT stores a ``HighBillAlertState``, so the shape is
    ``{"monthStartTs", "lastSentTs", "sentCount"}`` - NOT the ``{"billStartTs", "sent"}``
    shape most other events return. Both are accepted so this keeps working if the state
    class is ever swapped for the generic one.
    """
    if not isinstance(state, dict):
        return False
    if int(state.get("sentCount") or 0) > 0:
        return True
    for key in ("lastSentTs", "sent", "sentAt"):
        if int(state.get(key) or 0) > 0:
            return True
    return False


def raw_timestamp_epoch_seconds(raw_value: datetime) -> int:
    """Interpret a RAW ``YYYYMMDDHHMMSS`` value as UTC, matching the Java event helper."""
    return int(raw_value.replace(tzinfo=timezone.utc).timestamp())


def build_monthly_aggregation_xml(
    *,
    uuid: str,
    pilot_id: int,
    start_ts: int,
    end_ts: int,
    trigger_id: str,
) -> str:
    """Serialize the JAXB AggregateUploadEvent contract consumed by SQSXMLQueue."""
    if start_ts >= end_ts:
        raise SetupError(f"invalid monthly aggregation range: startTs={start_ts}, endTs={end_ts}")
    root = ET.Element("aggregateUploadEvent")
    scalar_fields: tuple[tuple[str, str], ...] = (
        ("uuid", uuid),
        ("hid", str(HID)),
        ("startTs", str(start_ts)),
        ("endTs", str(end_ts)),
        ("sendNotifications", "true"),
        ("refreshRate", "false"),
        ("measurementType", MEASUREMENT_TYPE),
        ("pilotId", str(pilot_id)),
        ("triggerName", "MANUAL_TEST_TRIGGER"),
        ("triggerId", trigger_id),
        ("raiseNSMEvent", "false"),
    )
    for name, value in scalar_fields:
        ET.SubElement(root, name).text = value

    consumption_types = ET.SubElement(root, "consumptionTypes")
    for aggregate_type in SQS_AGGREGATE_TYPES:
        ET.SubElement(consumption_types, "consumptionType").text = aggregate_type

    # AggregateUploadEvent intentionally declares the JAXB item name as
    # "addMode" (not "aggMode"). Keep this spelling aligned with the consumer.
    modes = ET.SubElement(root, "aggModes")
    ET.SubElement(modes, "addMode").text = SQS_AGG_MODE
    streams = ET.SubElement(root, "streamTypes")
    ET.SubElement(streams, "streamType").text = SQS_STREAM_TYPE

    trailing_fields: tuple[tuple[str, str], ...] = (
        ("deleteBeforeRun", "false"),
        ("runHybridV2", "false"),
        ("storeTimebandInCassandra", "false"),
        ("eventHBATriggered", "false"),
    )
    for name, value in trailing_fields:
        ET.SubElement(root, name).text = value
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def validate_monthly_aggregation_xml(body: str, expected_uuid: str, expected_trigger_id: str) -> None:
    """Fail closed before SendMessage if the XML expands scope or changes contract."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise SetupError(f"generated AggregateUploadEvent XML is invalid: {exc}") from exc
    if root.tag != "aggregateUploadEvent":
        raise SetupError(f"unexpected SQS XML root: {root.tag}")
    try:
        uuid_lib.UUID(expected_uuid)
        uuid_lib.UUID(expected_trigger_id)
    except ValueError as exc:
        raise SetupError(f"invalid UUID in aggregation event: {exc}") from exc

    expected_scalars = {
        "uuid": expected_uuid,
        "hid": str(HID),
        "sendNotifications": "true",
        "refreshRate": "false",
        "measurementType": MEASUREMENT_TYPE,
        "triggerName": "MANUAL_TEST_TRIGGER",
        "triggerId": expected_trigger_id,
        "raiseNSMEvent": "false",
        "deleteBeforeRun": "false",
        "runHybridV2": "false",
        "storeTimebandInCassandra": "false",
        "eventHBATriggered": "false",
    }
    for name, expected in expected_scalars.items():
        actual = root.findtext(name)
        if actual != expected:
            raise SetupError(f"unsafe SQS XML field {name}: expected={expected!r}, actual={actual!r}")
    for name in ("pilotId", "startTs", "endTs"):
        value = root.findtext(name)
        if value is None or not value.isdigit():
            raise SetupError(f"invalid numeric SQS XML field {name}: {value!r}")
    start_ts = int(root.findtext("startTs", "0"))
    end_ts = int(root.findtext("endTs", "0"))
    if start_ts >= end_ts:
        raise SetupError(f"invalid SQS XML aggregation range: startTs={start_ts}, endTs={end_ts}")

    modes = [node.text for node in root.findall("./aggModes/addMode")]
    aggregate_types = [node.text for node in root.findall("./consumptionTypes/consumptionType")]
    stream_types = [node.text for node in root.findall("./streamTypes/streamType")]
    if modes != [SQS_AGG_MODE]:
        raise SetupError(f"refusing non-monthly aggregation modes: {modes}")
    if aggregate_types != list(SQS_AGGREGATE_TYPES):
        raise SetupError(f"unexpected aggregation consumption types: {aggregate_types}")
    if stream_types != [SQS_STREAM_TYPE]:
        raise SetupError(f"unexpected aggregation stream types: {stream_types}")


def resolve_masterpilot_productqa_queue(sqs: Any) -> str:
    """Resolve and validate the exact non-priority MasterPilot ProductQA queue."""
    try:
        queue_url = sqs.get_queue_url(QueueName=DEFAULT_SQS_QUEUE)["QueueUrl"]
    except (BotoCoreError, ClientError, KeyError) as exc:
        raise SetupError(f"cannot resolve SQS queue {DEFAULT_SQS_QUEUE}: {exc}") from exc
    if queue_url.rstrip("/") != DEFAULT_SQS_QUEUE_URL:
        raise SetupError(
            f"refusing unexpected SQS queue URL for {DEFAULT_SQS_QUEUE}: "
            f"expected={DEFAULT_SQS_QUEUE_URL}, actual={queue_url}"
        )
    log(f"SQS QUEUE VERIFIED | queue={DEFAULT_SQS_QUEUE} url={queue_url}")
    return queue_url


def send_monthly_aggregation(
    sqs: Any,
    queue_url: str,
    args: argparse.Namespace,
    generated: Generated,
    uuid: str,
    label: str,
) -> dict[str, Any]:
    start_ts = raw_timestamp_epoch_seconds(datetime.combine(generated.active_cycle.start, dt_time(0, 0)))
    end_ts = raw_timestamp_epoch_seconds(generated.high_end)
    trigger_id = str(uuid_lib.uuid4())
    body = build_monthly_aggregation_xml(
        uuid=uuid,
        pilot_id=args.pilot_id,
        start_ts=start_ts,
        end_ts=end_ts,
        trigger_id=trigger_id,
    )
    validate_monthly_aggregation_xml(body, uuid, trigger_id)
    audit_file = generated.manifest_file.parent / f"SQS_AGGREGATION_{trigger_id}.xml"
    audit_file.write_text(body, encoding="utf-8")
    expected_md5 = hashlib.md5(body.encode("utf-8")).hexdigest()
    log(
        f"SQS SEND START | uuid={uuid} label={label!r} queue={DEFAULT_SQS_QUEUE} "
        f"trigger_id={trigger_id} start_ts={start_ts} end_ts={end_ts} "
        f"agg_modes={SQS_AGG_MODE} consumption_types={','.join(SQS_AGGREGATE_TYPES)} "
        f"stream_types={SQS_STREAM_TYPE} historical=false"
    )
    try:
        sent_timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        response = sqs.send_message(
            QueueUrl=queue_url,
            MessageBody=body,
            MessageAttributes={
                SQS_SENT_TIMESTAMP_ATTRIBUTE: {
                    "DataType": "String",
                    "StringValue": sent_timestamp,
                }
            },
        )
    except (BotoCoreError, ClientError) as exc:
        raise SetupError(f"SQS SendMessage failed for {DEFAULT_SQS_QUEUE}: {exc}") from exc
    actual_md5 = str(response.get("MD5OfMessageBody") or "")
    message_id = str(response.get("MessageId") or "")
    if not message_id:
        raise SetupError(f"SQS response for {DEFAULT_SQS_QUEUE} did not contain MessageId")
    if actual_md5.lower() != expected_md5:
        raise SetupError(
            f"SQS body MD5 mismatch for message {response.get('MessageId')}: "
            f"expected={expected_md5}, actual={actual_md5 or '<missing>'}"
        )
    details = {
        "label": label,
        "queueName": DEFAULT_SQS_QUEUE,
        "messageId": message_id,
        "triggerId": trigger_id,
        "xmlAuditFile": str(audit_file),
        "messageSentTimestamp": sent_timestamp,
        "md5": actual_md5,
        "startTs": start_ts,
        "endTs": end_ts,
        "aggModes": [SQS_AGG_MODE],
        "consumptionTypes": list(SQS_AGGREGATE_TYPES),
        "streamTypes": [SQS_STREAM_TYPE],
    }
    log(
        f"SQS SEND OK | uuid={uuid} label={label!r} queue={DEFAULT_SQS_QUEUE} "
        f"message_id={details['messageId']} trigger_id={trigger_id} md5_verified=true"
    )
    return details


def monthly_output_fingerprint(
    api: ApiClient,
    args: argparse.Namespace,
    generated: Generated,
    uuid: str,
) -> tuple[str, dict[str, int], dict[str, Any]]:
    """Fingerprint the persisted MONTH outputs required by HBA.

    MONTH-only aggregation events do not reliably advance the generic
    processing-status marker. Reading the stored ENERGY_CONSUMPTION and
    BILLING_COST outputs verifies the result HBA consumes and is independent
    of which aggregationMessageProcessor instance receives the SQS message.
    """
    start_ts = raw_timestamp_epoch_seconds(datetime.combine(generated.active_cycle.start, dt_time(0, 0)))
    end_ts = raw_timestamp_epoch_seconds(generated.high_end)
    outputs: dict[str, Any] = {}
    counts: dict[str, int] = {}
    for aggregate_type in ("ENERGY_CONSUMPTION", "BILLING_COST"):
        query = urllib.parse.urlencode(
            {
                "t0": start_ts,
                "t1": end_ts,
                "mode": API_AGG_MODE,
                "tz": args.timezone,
                "skipInvoice": "true",
                "measurementType": MEASUREMENT_TYPE,
                "convertConsumptionUnit": "false",
            }
        )
        path = (
            f"/billingdata/users/{uuid}/homes/{HID}/consumption/ctypes/{aggregate_type}/"
            f"stream-types/{SQS_STREAM_TYPE}?{query}"
        )
        value = payload_of(api.request("GET", path))
        outputs[aggregate_type] = value
        counts[aggregate_type] = len(value) if isinstance(value, (dict, list)) else 0
    canonical = json.dumps(outputs, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest(), counts, outputs


def current_cycle_output_ready(outputs: dict[str, Any]) -> tuple[bool, str]:
    """Whether the latest MONTH bucket already holds priced, non-empty output.

    A MONTH aggregation over data that ingestion has already aggregated rewrites
    identical rows, so a fingerprint comparison cannot prove the message ran.
    What the rest of the run actually depends on is the *content*: the active
    cycle must have a priced MONTH bucket, because calibration reads its cost
    and HighBillAlertUserProcessor compares its projection against the expected
    bill. This checks that directly, using the same latest-bucket convention as
    cycle_cost_to_date.
    """
    energy = outputs.get("ENERGY_CONSUMPTION")
    billing = outputs.get("BILLING_COST")
    if not isinstance(energy, dict) or not energy:
        return False, "no MONTH ENERGY_CONSUMPTION buckets"
    if not isinstance(billing, dict) or not billing:
        return False, "no MONTH BILLING_COST buckets"
    try:
        latest = max(energy, key=lambda key: int(key))
    except (TypeError, ValueError):
        return False, "MONTH ENERGY_CONSUMPTION keys are not epoch seconds"
    if latest not in billing:
        return False, f"MONTH BILLING_COST has no bucket {latest}"
    value = float((energy.get(latest) or {}).get("value") or 0.0)
    bucket = billing.get(latest) or {}
    cost = float(bucket.get("costWithFixedCharges") or bucket.get("cost") or 0.0)
    if value <= 0:
        return False, f"MONTH bucket {latest} has no consumption"
    if cost <= 0:
        return False, f"MONTH bucket {latest} is unpriced"
    return True, f"bucket={latest} consumption={value} cost={cost}"


def queue_and_wait(
    sqs: Any,
    queue_url: str,
    api: ApiClient,
    args: argparse.Namespace,
    generated: Generated,
    uuid: str,
    label: str,
    verification_baseline: tuple[str, dict[str, int], str] | None = None,
    required: bool = True,
) -> dict[str, Any]:
    at_send_fingerprint, at_send_counts, _ = monthly_output_fingerprint(api, args, generated, uuid)
    if verification_baseline is None:
        baseline_fingerprint = at_send_fingerprint
        baseline_counts = at_send_counts
        baseline_label = "pre-SQS output"
    else:
        baseline_fingerprint, baseline_counts, baseline_label = verification_baseline
        if not baseline_fingerprint or not baseline_counts or not baseline_label:
            raise SetupError(f"invalid MONTH output verification baseline for {label!r}")

    details = send_monthly_aggregation(sqs, queue_url, args, generated, uuid, label)
    append_sqs_message(generated, details)
    log(
        f"AGGREGATION QUEUED | uuid={uuid} label={label!r} transport=SQS "
        f"message_id={details['messageId']} trigger_id={details['triggerId']} "
        f"verification_baseline={baseline_label!r} "
        f"monthly_output_counts_baseline={json.dumps(baseline_counts, separators=(',', ':'))} "
        f"monthly_output_counts_at_send={json.dumps(at_send_counts, separators=(',', ':'))} "
        f"output_changed_before_send={str(at_send_fingerprint != baseline_fingerprint).lower()}"
    )
    after_fingerprint, after_counts = at_send_fingerprint, at_send_counts
    started = time.monotonic()
    deadline = started + args.wait_minutes * 60
    ready_reason = "not evaluated"
    while time.monotonic() < deadline:
        time.sleep(args.poll_seconds)
        after_fingerprint, after_counts, after_outputs = monthly_output_fingerprint(api, args, generated, uuid)
        required_outputs_present = all(
            after_counts.get(name, 0) > 0 for name in ("ENERGY_CONSUMPTION", "BILLING_COST")
        )
        changed = after_fingerprint != baseline_fingerprint
        ready, ready_reason = current_cycle_output_ready(after_outputs)
        # An unchanged fingerprint is not a failure. Ingestion aggregates the
        # file it just loaded, so by the time this message is processed the
        # MONTH rows it writes are frequently identical to the ones already
        # stored, and no observable output can change. The message is still
        # worth sending - it is what drives
        # BillingDataAggregateProcessor.sendNotifications - but only its content
        # can be verified. Give the message time to be picked up before
        # accepting that weaker signal, so a genuinely unprocessed message is
        # not mistaken for an idempotent one.
        settled = time.monotonic() - started >= AGGREGATION_NOOP_GRACE_SECONDS
        if (changed and required_outputs_present) or (ready and settled):
            mode = "output-changed" if changed else "already-current"
            log(
                f"AGGREGATION OUTPUT VERIFIED | uuid={uuid} label={label!r} "
                f"message_id={details['messageId']} trigger_id={details['triggerId']} "
                f"verification_baseline={baseline_label!r} mode={mode} "
                f"current_cycle={ready_reason} "
                f"monthly_output_counts_after={json.dumps(after_counts, separators=(',', ':'))}"
            )
            # Let notification processors immediately following aggregation finish.
            time.sleep(min(args.poll_seconds, 30))
            details["monthlyOutputVerificationBaseline"] = baseline_label
            details["monthlyOutputVerified"] = True
            details["monthlyOutputVerificationMode"] = mode
            details["monthlyOutputCurrentCycle"] = ready_reason
            details["monthlyOutputFingerprintBefore"] = baseline_fingerprint
            details["monthlyOutputFingerprintAfter"] = after_fingerprint
            details["monthlyOutputCountsBefore"] = baseline_counts
            details["monthlyOutputCountsAtSend"] = at_send_counts
            details["monthlyOutputCountsAfter"] = after_counts
            return details
        log(
            f"WAIT AGGREGATION OUTPUT | uuid={uuid} label={label!r} "
            f"message_id={details['messageId']} trigger_id={details['triggerId']} "
            f"verification_baseline={baseline_label!r} "
            f"output_changed_from_baseline={str(after_fingerprint != baseline_fingerprint).lower()} "
            f"current_cycle_ready={ready_reason} "
            f"monthly_output_counts={json.dumps(after_counts, separators=(',', ':'))}"
        )
    summary = (
        f"SQS accepted aggregation '{label}', but the MONTH outputs neither changed from "
        f"verification baseline {baseline_label!r} nor reached a priced current-cycle bucket "
        f"within {args.wait_minutes} minutes: queue={DEFAULT_SQS_QUEUE}, "
        f"message_id={details['messageId']}, trigger_id={details['triggerId']}, "
        f"last_output_counts={after_counts}, current_cycle={ready_reason}."
    )
    if required:
        raise SetupError(
            summary + " Search all aggregationMessageProcessor instances for the trigger_id before "
            "sending another message; SQS may deliver it to a different instance."
        )
    # An HBA evaluation pass is legitimately idempotent at the storage layer: the
    # aggregates may already hold the high-usage values, so re-running MONTH
    # rewrites identical rows. The message still drives
    # BillingDataAggregateProcessor.sendNotifications, which is the only reason
    # the pass exists, so an unchanged fingerprint must not end the run before
    # the HBA notification state has been read.
    log(summary + " Continuing; HBA notification state is the authoritative signal.", "WARN")
    details["monthlyOutputVerificationBaseline"] = baseline_label
    details["monthlyOutputVerified"] = False
    details["monthlyOutputVerificationMode"] = "unverified"
    details["monthlyOutputCurrentCycle"] = ready_reason
    details["monthlyOutputFingerprintBefore"] = baseline_fingerprint
    details["monthlyOutputFingerprintAfter"] = after_fingerprint
    details["monthlyOutputCountsBefore"] = baseline_counts
    details["monthlyOutputCountsAtSend"] = at_send_counts
    details["monthlyOutputCountsAfter"] = after_counts
    return details


def rewrite_high_raw(generated: Generated, multiplier: float) -> int:
    """Regenerate the high-usage RAW file at a calibrated multiplier."""
    return write_lines(
        generated.raw_high_file,
        (
            raw_line(generated.ids, ts, interval_value(ts, multiplier=multiplier))
            for ts in iter_intervals(generated.high_start, generated.high_end)
        ),
    )


def invoice_cycle_costs(generated: Generated) -> list[float]:
    """Total cost of each completed cycle in the INVOICE file this run generated."""
    costs: list[float] = []
    for line in generated.invoice_file.read_text(encoding="utf-8").splitlines():
        parts = line.split("|")
        if len(parts) > 12 and parts[9] == "TOTAL":
            try:
                costs.append(float(parts[12]))
            except ValueError:
                continue
    return costs


def cycle_cost_to_date(api: ApiClient, args: argparse.Namespace, generated: Generated, uuid: str) -> float:
    """Priced cost of the active cycle so far, as the platform computes it."""
    query = urllib.parse.urlencode(
        {
            "t0": raw_timestamp_epoch_seconds(datetime.combine(generated.active_cycle.start, dt_time(0, 0))),
            "t1": raw_timestamp_epoch_seconds(generated.high_end),
            "mode": API_AGG_MODE,
            "tz": args.timezone,
            "skipInvoice": "true",
            "measurementType": MEASUREMENT_TYPE,
            "convertConsumptionUnit": "false",
        }
    )
    payload = payload_of(
        api.request(
            "GET",
            f"/billingdata/users/{uuid}/homes/{HID}/consumption/ctypes/BILLING_COST/"
            f"stream-types/{SQS_STREAM_TYPE}?{query}",
        )
    )
    if not isinstance(payload, dict) or not payload:
        return 0.0
    latest = max(payload, key=lambda key: int(key))
    bucket = payload.get(latest) or {}
    return float(bucket.get("costWithFixedCharges") or bucket.get("cost") or 0.0)


def platform_expected_bill(api: ApiClient, uuid: str) -> float | None:
    """The expected bill HighBillAlertUserProcessor compares against.

    ProjectionsEndpoint /hbaExpectedCost calls the same
    BillProjectionCalculator.getHBAExpectedBill that calculateExpectedBill
    stores in hba_expected_bill, so this is the platform's own number rather
    than an estimate derived from the invoice fixture. Read it right after a
    seeding pass, while the trigger period still holds the window open: the
    default comparison mode, SAME_TIME_LAST_YEAR_AND_CURRENT_YEAR, includes the
    current cycle, so the value read before the high-usage upload is not the one
    the evaluation passes will be judged against.
    """
    try:
        value = api.request("GET", f"/2.1/users/{uuid}/homes/{HID}/hbaExpectedCost?measurementType={MEASUREMENT_TYPE}")
    except Exception as error:  # noqa: BLE001 - advisory read, never fatal
        log(f"EXPECTED BILL READ FAILED | uuid={uuid} error={error}", "WARN")
        return None
    if isinstance(value, dict):
        value = value.get("payload", value)
    try:
        expected = float(value)
    except (TypeError, ValueError):
        log(f"EXPECTED BILL UNPARSEABLE | uuid={uuid} value={value!r}", "WARN")
        return None
    return expected if expected > 0 else None


def platform_bill_projection(api: ApiClient, args: argparse.Namespace, uuid: str) -> dict[str, Any] | None:
    """The projected cycle cost, as the dashboard and HBA both compute it."""
    query = urllib.parse.urlencode({"tz": args.timezone, "measurementType": MEASUREMENT_TYPE})
    try:
        payload = api.request("GET", f"/2.1/users/{uuid}/homes/{HID}/billprojections?{query}")
    except Exception as error:  # noqa: BLE001 - advisory read, never fatal
        log(f"PROJECTION READ FAILED | uuid={uuid} error={error}", "WARN")
        return None
    if isinstance(payload, dict) and "projectionPrice" not in payload:
        payload = payload.get("payload") or payload
    return payload if isinstance(payload, dict) and "projectionPrice" in payload else None


def calibrate_high_usage_multiplier(
    seed_cost: float,
    expected: float,
    elapsed_days: int,
    cycle_days: int,
    expected_source: str = "invoice history",
) -> tuple[float, str]:
    """Pick a multiplier that lands the projection inside HBA's accept band.

    HighBillAlertUserProcessor accepts a projection only when it is at least
    ``expected * high_bill_projection_threshold`` and strictly below four times
    that value - ``isHBAEligibleForMTDTrigger`` drops anything higher as
    implausible. The band must be computed from the PRICED cost, not from the
    invoice history: the invoice costs are fixture values while the projection is
    the rate plan applied to raw data, and the two imply very different per-kWh
    rates. A fixed multiplier guesses at that ratio and can miss in either
    direction.
    """
    if seed_cost <= 0 or expected <= 0 or elapsed_days < 2:
        return 0.0, "insufficient data to calibrate"
    floor = expected * HBA_PROJECTION_THRESHOLD
    ceiling = floor * 4
    scale = cycle_days / elapsed_days

    def projection(multiplier: float) -> float:
        return seed_cost * (1 + multiplier * (elapsed_days - 1)) * scale

    target = expected * HBA_TARGET_RATIO
    multiplier = round(max(((target / scale / seed_cost) - 1) / (elapsed_days - 1), 1.1), 2)
    note = (
        f"expected=${expected:.2f} ({expected_source}) band=[${floor:.2f},${ceiling:.2f}) "
        f"seed_day_cost=${seed_cost:.2f} elapsed={elapsed_days}d cycle={cycle_days}d "
        f"projection=${projection(multiplier):.2f}"
    )
    if not floor <= projection(multiplier) < ceiling:
        note += " WARNING: projection falls outside the HBA accept band"
    return multiplier, note


def verify_projection_band(
    api: ApiClient,
    args: argparse.Namespace,
    uuid: str,
    generated: Generated,
    seed_expected: float,
    expected_source: str,
) -> tuple[str, float, float, float]:
    """Check the projection against the band before spending passes on it.

    isHBAEligibleForMTDTrigger silently drops a projection below
    ``expected * threshold`` and, just as silently, one at or above four times
    that value. Neither writes notification state, so both are indistinguishable
    from a render failure afterwards. Reading the two numbers here turns that
    into a named warning while the run can still be interpreted, and returns the
    verdict so the caller can correct a miss instead of only reporting it.

    Returns ``(verdict, projection, floor, ceiling)`` where verdict is one of
    ``ok``, ``below``, ``above`` or ``unverified``; the three prices are 0.0
    when the verdict is ``unverified``.
    """
    projection = platform_bill_projection(api, args, uuid)
    if projection is None or seed_expected <= 0:
        log(f"PROJECTION BAND UNVERIFIED | uuid={uuid} expected={seed_expected}", "WARN")
        return "unverified", 0.0, 0.0, 0.0
    price = float(projection.get("projectionPrice") or 0.0)
    floor = seed_expected * HBA_PROJECTION_THRESHOLD
    ceiling = floor * 4
    update_manifest(
        generated,
        projectionPrice=price,
        projectionBand={"floor": floor, "ceiling": ceiling, "expected": seed_expected},
    )
    detail = (
        f"uuid={uuid} projection=${price:.2f} expected=${seed_expected:.2f} "
        f"({expected_source}) band=[${floor:.2f},${ceiling:.2f}) "
        f"already_triggered={projection.get('hbaTriggeredForCurrentMonth')} "
        f"days_left={projection.get('daysLeft')}"
    )
    if price < floor:
        log(
            f"PROJECTION BELOW BAND | {detail} - isHBAEligibleForMTDTrigger will not trigger; "
            "raise --high-usage-multiplier",
            "WARN",
        )
        return "below", price, floor, ceiling
    if price >= ceiling:
        log(
            f"PROJECTION ABOVE BAND | {detail} - isHBAEligibleForMTDTrigger drops this as "
            "'Projection crossed max threshold' and writes no state; lower --high-usage-multiplier",
            "WARN",
        )
        return "above", price, floor, ceiling
    log(f"PROJECTION BAND OK | {detail}")
    return "ok", price, floor, ceiling


def wait_for_hba_notification(
    api: ApiClient,
    args: argparse.Namespace,
    uuid: str,
    evaluation: dict[str, Any],
    wait_minutes: float | None = None,
) -> Any:
    """Poll the HIGH_BILL_ALERT notification state until it reports sent or times out.

    wait_minutes overrides --notification-wait-minutes. Passes before the last one
    only raise the MTD disagg event and have never been observed to send, so they
    get a short window: long enough to catch the rare case where a prior
    aggregation already landed the disagg, short enough not to burn ten idle
    minutes on a pass that structurally cannot send.
    """
    minutes = args.notification_wait_minutes if wait_minutes is None else wait_minutes
    deadline = time.monotonic() + minutes * 60
    while True:
        state = notification_state(api, uuid)
        log(
            f"HBA STATUS | uuid={uuid} evaluation_label={evaluation['label']!r} "
            f"evaluation_trigger_id={evaluation['triggerId']} "
            f"state={json.dumps(state, separators=(',', ':'))}"
        )
        if was_sent(state) or time.monotonic() >= deadline:
            return state
        time.sleep(args.poll_seconds)


def append_sqs_message(generated: Generated, details: dict[str, Any]) -> None:
    current = json.loads(generated.manifest_file.read_text(encoding="utf-8"))
    current["aggregation"]["messages"].append(details)
    generated.manifest_file.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")


def update_manifest(generated: Generated, **changes: Any) -> None:
    current = json.loads(generated.manifest_file.read_text(encoding="utf-8"))
    current.update(changes)
    generated.manifest_file.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")


def execute(args: argparse.Namespace, generated: Generated) -> bool:
    if boto3 is None:
        raise SetupError("boto3 is required: python3 -m pip install boto3")
    token = os.environ.get("BIDGELY_TOKEN", "").strip()
    if not token:
        token = getpass.getpass(f"ProductQA API token for {args.api_base}: ").strip()
    if not token:
        raise SetupError("no API token supplied (set BIDGELY_TOKEN or enter it at the prompt)")

    api = ApiClient(args.api_base, token)
    step("verify pilot HBA gate", pilot_id=args.pilot_id)
    verify_pilot_gate(api, args.pilot_id)
    if resolve_uuid_once(api, args.pilot_id, generated.ids.external_user):
        raise SetupError("generated external user ID already exists; rerun to generate a new set")

    s3 = boto3.client("s3", region_name=args.region)
    sqs = boto3.client("sqs", region_name=args.region)
    queue_url = resolve_masterpilot_productqa_queue(sqs)
    step("upload USERENROLL", file=generated.user_file.name, external_user=generated.ids.external_user)
    upload_file(s3, args.bucket, generated.user_file, generated.object_keys["user"])
    step("resolve ingested UUID", external_user=generated.ids.external_user)
    uuid = wait_for_uuid(api, args, generated.ids.external_user)
    update_manifest(generated, uuid=uuid)
    step("configure HBA at user level", uuid=uuid)
    seed_period_days = (generated.high_end.date() - generated.active_cycle.start).days + 1
    configured = False
    config_snapshot: dict[str, dict[str, Any]] = {}
    # String resources cannot be deleted through the API, so this is a record for the
    # operator rather than something restore can undo. It still has to be captured: an
    # unrecorded user-level resource is invisible the next time this user is reused.
    resource_snapshot: dict[str, dict[str, Any]] = {}
    try:
        wanted_sections = frozenset(HBA_SECTIONS) - (
            {"MTD_BREAK_DOWN_SECTION"} if args.drop_mtd_section else set()
        )
        configure_hba_user(
            api, uuid, seed_period_days, config_snapshot, resource_snapshot, wanted_sections
        )
        update_manifest(
            generated, configSnapshot=config_snapshot, resourceSnapshot=resource_snapshot
        )
        configured = True
        log(
            f"HBA SEED WINDOW | uuid={uuid} days={seed_period_days} "
            f"through={generated.high_end:%Y-%m-%dT%H:%M:%S}"
        )

        step("upload completed billing history", uuid=uuid, file=generated.invoice_file.name)
        upload_file(s3, args.bucket, generated.invoice_file, generated.object_keys["invoice"])
        settle(args, "billing data")

        # Establish the normal expected-bill baseline before introducing the
        # abnormal usage. Both RAW files remain current-cycle-only, and every
        # explicit aggregation message remains MONTH-only.
        step("upload current-cycle seed raw data", uuid=uuid, file=generated.raw_seed_file.name)
        upload_file(s3, args.bucket, generated.raw_seed_file, generated.object_keys["rawSeed"])
        settle(args, "current-cycle seed raw data")

        step("seed expected HBA bill with monthly SQS aggregation", uuid=uuid)
        seed_aggregation = queue_and_wait(
            sqs,
            queue_url,
            api,
            args,
            generated,
            uuid,
            "seed expected HBA bill",
        )
        evaluation_baseline = (
            seed_aggregation["monthlyOutputFingerprintAfter"],
            seed_aggregation["monthlyOutputCountsAfter"],
            "verified seed output before high-usage upload",
        )

        # Calibrate against the cost the platform actually computed for the seed
        # day. The invoice history is a fixture priced at its own implied rate;
        # the projection HBA compares against is the rate plan applied to raw
        # data. Guessing a fixed multiplier across that gap can land the
        # projection under the trigger or over the implausibility ceiling. The
        # expected bill cannot be read yet - it does not reflect the high-usage
        # readings until they are uploaded - so the invoice mean is the only
        # anchor available here, and the band is re-checked for real afterwards.
        history_costs = invoice_cycle_costs(generated)
        history_mean = sum(history_costs) / len(history_costs) if history_costs else 0.0
        multiplier = args.high_usage_multiplier
        auto_calibrated = multiplier is None
        if auto_calibrated:
            seed_cost = cycle_cost_to_date(api, args, generated, uuid)
            multiplier, note = calibrate_high_usage_multiplier(
                seed_cost,
                history_mean,
                (generated.high_end.date() - generated.active_cycle.start).days + 1,
                generated.active_cycle.days,
                "invoice history",
            )
            if multiplier > 0:
                rows = rewrite_high_raw(generated, multiplier)
                log(f"HIGH USAGE CALIBRATED | uuid={uuid} multiplier={multiplier} rows={rows} {note}")
                update_manifest(generated, highUsageMultiplier=multiplier, calibration=note)
            else:
                log(
                    f"HIGH USAGE CALIBRATION SKIPPED | uuid={uuid} {note}; "
                    "keeping the file as generated",
                    "WARN",
                )
                auto_calibrated = False

        step("upload current-cycle high-usage raw data", uuid=uuid, file=generated.raw_high_file.name)
        upload_file(s3, args.bucket, generated.raw_high_file, generated.object_keys["rawHigh"])
        settle(args, "current-cycle high-usage raw data")

        # The expected bill is a stored snapshot, not a live value.
        # calculateExpectedBill runs only while isTodayWithinRawDataThreshold is
        # true, and isEligibleForHBATrigger returns false for the whole of that
        # window - so a pass taken now seeds without ever evaluating. Seeding
        # before the high-usage upload instead stores an expected built from a
        # nearly empty cycle, and the projection then overshoots
        # expected * threshold * 4, which isHBAEligibleForMTDTrigger drops as
        # implausible without writing any notification state. The window
        # therefore stays open across the high-usage upload and closes only once
        # the stored value reflects the data the evaluation passes will judge.
        stored_expected: float | None = None
        verdict = "unverified"
        for attempt in range(1, CALIBRATION_ATTEMPTS + 1):
            step(
                f"reseed expected HBA bill against the high-usage data "
                f"{attempt}/{CALIBRATION_ATTEMPTS}",
                uuid=uuid,
            )
            queue_and_wait(
                sqs,
                queue_url,
                api,
                args,
                generated,
                uuid,
                f"reseed expected HBA bill {attempt}",
                verification_baseline=evaluation_baseline,
                required=False,
            )
            stored_expected = platform_expected_bill(api, uuid)
            log(
                f"EXPECTED BILL | uuid={uuid} stored={stored_expected} "
                f"invoice_mean={history_mean:.2f}"
            )
            verdict, price, floor, _ceiling = verify_projection_band(
                api, args, uuid, generated, stored_expected or 0.0, "platform hbaExpectedCost"
            )
            update_manifest(
                generated, storedExpectedBill=stored_expected, projectionVerdict=verdict
            )
            correctable = (
                verdict == "below"
                and auto_calibrated
                and price > 0
                and attempt < CALIBRATION_ATTEMPTS
            )
            if not correctable:
                break
            # Raising the multiplier lifts the projection far more than it lifts
            # the expected bill, because expected is a mean across several
            # cycles while the projection is this cycle alone - so one
            # corrective pass converges rather than chasing a moving floor.
            boosted = round(multiplier * (floor / price) * HBA_BAND_SAFETY_MARGIN, 2)
            rows = rewrite_high_raw(generated, boosted)
            log(
                f"HIGH USAGE CORRECTED | uuid={uuid} multiplier={multiplier} -> {boosted} "
                f"rows={rows} projection=${price:.2f} floor=${floor:.2f}"
            )
            multiplier = boosted
            update_manifest(generated, highUsageMultiplier=multiplier)
            upload_file(s3, args.bucket, generated.raw_high_file, generated.object_keys["rawHigh"])
            settle(args, "corrected current-cycle high-usage raw data")

        # Close the seeding window. Every pass from here evaluates rather than
        # seeds, so the stored expected bill stays fixed at the value the band
        # was just checked against.
        set_hba_trigger_period(
            api,
            uuid,
            1,
            "Temporary QA HBA evaluation cadence after expected-bill seeding",
            config_snapshot,
        )
        update_manifest(generated, configSnapshot=config_snapshot)

        # HighBillAlertUserProcessor needs TWO notification passes in a cycle.
        # Pass 1 reaches isHBAEligibleForMTDTrigger, which only calls
        # raiseMTDDisaggEvent and returns without sending. The aggregation that
        # the MTD disagg publishes back carries APPLIANCE_DISAGG as its only
        # consumption type, and BillingDataAggregateProcessor.processAggregates
        # returns checkNotifications=false for that shape, so it never re-runs
        # the processor by itself. Only a further full-consumption-type
        # aggregation does, and on that pass isHBAToBeTriggeredBasedOnMTD matches
        # the stored mtdDisaggTriggerTimestamp and the email is finally sent.
        #
        # Each pass keeps the verified low-usage seed as its comparison baseline
        # rather than a snapshot taken after the high-usage upload, because
        # ingestion may already have refreshed the MONTH outputs by then.
        evaluations: list[dict[str, Any]] = []
        final_state: Any = None
        for attempt in range(1, args.evaluation_passes + 1):
            purpose = "raise the MTD disagg event" if attempt == 1 else "send HBA after the MTD hop"
            step(
                f"run HBA monthly SQS evaluation {attempt}/{args.evaluation_passes}",
                uuid=uuid,
                purpose=purpose,
            )
            evaluation = queue_and_wait(
                sqs,
                queue_url,
                api,
                args,
                generated,
                uuid,
                f"HBA evaluation pass {attempt}",
                verification_baseline=evaluation_baseline,
                required=False,
            )
            evaluations.append(evaluation)

            is_last_pass = attempt == args.evaluation_passes
            final_state = wait_for_hba_notification(
                api,
                args,
                uuid,
                evaluation,
                wait_minutes=(
                    args.notification_wait_minutes
                    if is_last_pass
                    else min(args.mtd_pass_wait_minutes, args.notification_wait_minutes)
                ),
            )
            if was_sent(final_state):
                update_manifest(generated, notification=final_state, evaluationPasses=attempt)
                log(f"SUCCESS: HIGH_BILL_ALERT email is recorded as sent for {uuid} -> {args.email}")
                return True

            if attempt < args.evaluation_passes:
                log(
                    f"HBA MTD HOP | uuid={uuid} pass={attempt} "
                    f"waiting {args.mtd_wait_minutes}m for the MTD disagg raised by this pass to land "
                    "before the next notification pass"
                )
                time.sleep(args.mtd_wait_minutes * 60)

        update_manifest(generated, notification=final_state, evaluationPasses=len(evaluations))
        log(
            "NOT CONFIRMED: files were ingested and the seed output was verified, but HBA notification "
            f"state does not show sent after {len(evaluations)} evaluation passes. In "
            "HighBillAlertUserProcessor logs for this uuid, check in order: 'No high bill data found' "
            "(hba_expected_bill was never seeded), 'today is before mid of bill cycle', 'Today is either "
            "before mid of bill cycle or after max upper bound point', and the "
            "'Today's projection amount ... expected bill amount * threshold value' comparison. "
            "If none of those lines appear, the gates passed and the RENDER failed instead - "
            "an empty notification state cannot distinguish the two, because state is only "
            "written on a successful send. Grep Emailer.log for 'Received exception for user' "
            "and match the top frame:\n"
            "  EmailTeaserSectionContentProvider.getTeaserText -> the teaser id is unresolved\n"
            "  BillProjectionSectionContentProvider.populateBpV2Version -> a projected-cost id "
            "is unresolved, or its %s arity is wrong\n"
            "  EmailFooterSectionContentProvider -> the high_bill_alert_faq_page_url id is "
            "unresolved\n"
            "  MTDBreakDownContentProvider.populateMTDAppliances (IndexOutOfBounds) -> "
            "itemization produced exactly one or two appliances after appliance 17/Others is "
            "filtered out, and that method indexes get(1)/get(2) behind element null checks a "
            "List can never satisfy. Drop MTD_BREAK_DOWN_SECTION from HBA_SECTIONS to get past "
            "it; it is a platform bug, not a setup gap.\n"
            "The first three should be impossible after this run's resource preflight - if one "
            "appears anyway, the id was resolved at setup time but is missing at render time, "
            "which means a cache (string.resource.cache.expirySeconds) rather than a data gap. "
            "Malformed-url ERRORs from EmailLinkUtils.addQueryParams are caught and non-fatal; "
            "so are chart_disclaimer_date_format and member_utility_name. Do not chase those.",
            "WARN",
        )
        return False
    finally:
        # Cleanup must survive an interrupt: the snapshot is already on disk, so
        # a run killed mid-restore can be finished with --restore-from.
        if configured and resource_snapshot:
            report_resource_leftovers(api, uuid, resource_snapshot)
        if configured and config_snapshot:
            try:
                restore_configs(api, uuid, config_snapshot)
                update_manifest(generated, configSnapshot=config_snapshot, configRestored=True)
            except BaseException as exc:  # includes KeyboardInterrupt
                log(
                    f"CONFIG RESTORE INCOMPLETE | uuid={uuid} error={exc}. Finish with: "
                    f"python3 {sys.argv[0]} --restore-from {generated.manifest_file}",
                    "ERROR",
                )
                raise


def restore_from_manifest(api_base: str, manifest_path: Path) -> int:
    """Finish cleanup for a run that was interrupted before restore completed."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    uuid = manifest.get("uuid")
    snapshot = manifest.get("configSnapshot") or {}
    resource_snapshot = manifest.get("resourceSnapshot") or {}
    if not uuid:
        raise SetupError(f"{manifest_path} has no uuid; nothing to restore")
    if not snapshot and not resource_snapshot:
        log(f"Nothing to restore: {manifest_path} recorded no config changes.")
        return 0
    if manifest.get("configRestored"):
        log(f"Already restored: {manifest_path} is marked configRestored=true.")
        return 0
    token = os.environ.get("BIDGELY_TOKEN", "").strip() or getpass.getpass(
        f"ProductQA API token for {api_base}: "
    ).strip()
    if not token:
        raise SetupError("no API token supplied (set BIDGELY_TOKEN or enter it at the prompt)")
    api = ApiClient(api_base, token)
    if resource_snapshot:
        report_resource_leftovers(api, uuid, resource_snapshot)
    if snapshot:
        restore_configs(api, uuid, snapshot)
    manifest["configRestored"] = True
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    log(f"Restore complete for {uuid} from {manifest_path}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--api-base", default=DEFAULT_API)
    parser.add_argument("--pilot-id", type=int, default=DEFAULT_PILOT)
    parser.add_argument(
        "--email",
        help=(
            "recipient override; default is a unique address in the form "
            "bidgelyqa+AUT_MP01_<meter-id>@bidgely.com"
        ),
    )
    parser.add_argument("--first-name", default="STEPHEN")
    parser.add_argument("--last-name", default="CALANDRA")
    parser.add_argument("--address", default="153 2nd St.")
    parser.add_argument("--city", default="Los Altos")
    parser.add_argument("--state", default="CA")
    parser.add_argument("--zipcode", default="11691")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--rate-plan", default="194")
    parser.add_argument(
        "--billing-cycle",
        help="billing-cycle code; default is CDG_<cycle-day>, for example CDG_05",
    )
    parser.add_argument("--cycle-day", type=int, default=DEFAULT_CYCLE_DAY)
    parser.add_argument("--history-cycles", type=int, default=DEFAULT_HISTORY_CYCLES)
    parser.add_argument("--anchor-date", help="YYYY-MM-DD; default is today in --timezone")
    parser.add_argument(
        "--high-usage-multiplier",
        default="auto",
        help=(
            "usage multiplier for the high-usage RAW file. 'auto' (default) measures the priced "
            "seed-day cost after seeding and picks a multiplier that lands the projection inside "
            "HBA's accept band; pass a number to force one."
        ),
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--poll-seconds", type=int, default=20)
    parser.add_argument("--settle-seconds", type=int, default=120)
    parser.add_argument("--wait-minutes", type=int, default=30)
    parser.add_argument("--notification-wait-minutes", type=int, default=10)
    parser.add_argument(
        "--evaluation-passes",
        type=int,
        default=2,
        help=(
            "full-consumption-type aggregation passes to run after seeding. HBA needs at least 2: "
            "the first only raises the MTD disagg event, the second is the one that can send."
        ),
    )
    parser.add_argument(
        "--mtd-wait-minutes",
        type=int,
        default=5,
        help="minutes to wait between evaluation passes so the MTD disagg can land",
    )
    parser.add_argument(
        "--mtd-pass-wait-minutes",
        type=float,
        default=1.5,
        help=(
            "minutes to poll notification state on a pass that only raises the MTD disagg "
            "event. Such a pass has never been observed to send, so it does not get the "
            "full --notification-wait-minutes; only the final pass does."
        ),
    )
    parser.add_argument(
        "--drop-mtd-section",
        action="store_true",
        help=(
            "omit MTD_BREAK_DOWN_SECTION from the HBA section list. Work around the "
            "IndexOutOfBoundsException in MTDBreakDownContentProvider.populateMTDAppliances, "
            "which indexes get(1)/get(2) behind element null checks a List can never satisfy "
            "and so aborts the whole email whenever itemization yields exactly one or two "
            "appliances (after appliance 17/Others is filtered out). Zero appliances is safe - "
            "the section returns null and is skipped - and so is three or more."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="generate and validate files; do not upload or call APIs")
    parser.add_argument(
        "--restore-from",
        type=Path,
        help=(
            "path to a previous run's manifest.json; re-applies only that run's config restore "
            "and exits. Use after a run was interrupted before cleanup finished."
        ),
    )
    args = parser.parse_args()
    if not 1 <= args.cycle_day <= 28:
        parser.error("--cycle-day must be 1..28")
    if not args.billing_cycle:
        args.billing_cycle = f"CDG_{args.cycle_day:02d}"
    if args.history_cycles < 12:
        parser.error("--history-cycles must be at least 12 for a stable HBA baseline")
    if str(args.high_usage_multiplier).strip().lower() == "auto":
        args.high_usage_multiplier = None
    else:
        try:
            args.high_usage_multiplier = float(args.high_usage_multiplier)
        except ValueError:
            parser.error("--high-usage-multiplier must be a number or 'auto'")
        if args.high_usage_multiplier <= 1:
            parser.error("--high-usage-multiplier must be greater than 1")
    if (
        args.poll_seconds < 1
        or args.wait_minutes < 1
        or args.notification_wait_minutes < 1
        or args.mtd_wait_minutes < 1
        or args.settle_seconds < 0
    ):
        parser.error("poll/wait values must be positive and settle delays must be non-negative")
    if args.evaluation_passes < 2:
        parser.error(
            "--evaluation-passes must be at least 2: the first pass only raises the MTD disagg event "
            "and can never send the HBA email"
        )
    try:
        ZoneInfo(args.timezone)
    except Exception as exc:
        parser.error(f"invalid --timezone: {exc}")
    if args.anchor_date:
        try:
            date.fromisoformat(args.anchor_date)
        except ValueError as exc:
            parser.error(f"invalid --anchor-date: {exc}")
    return args


def main() -> int:
    args = parse_args()
    try:
        if args.restore_from:
            return restore_from_manifest(args.api_base, args.restore_from)
        generated = build_files(args)
        if args.dry_run:
            log("DRY RUN complete: no S3 objects or ProductQA configuration were changed.")
            return 0
        return 0 if execute(args, generated) else 2
    except (SetupError, OSError, ValueError) as exc:
        log(f"RUN FAILED | error={exc}", "ERROR")
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
