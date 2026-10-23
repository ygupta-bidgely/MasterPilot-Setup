> Copied from `../MDS_AUTOMATION` for reference: this document describes the SOLAR suite. Non-solar behaviour and
> prerequisites are in `README.md`.

# Budget Billing on MDS (pilot 88002): defects and limitations to raise with the developers

Found while building `create_mds_budget_billing_user.py` / `mds_bb.py` (2026-10-01).
Each item says how it was established: **LIVE** (reproduced on 88002), **CODE** (read in the source, not yet reproduced) or **STATED** (told to us, not re-verified).
Update the status column as items are reproduced or fixed. Nothing here has been reported yet.

| # | Area | Finding | Evidence | Status |
|---|---|---|---|---|
| 1 | BillProjection | BillProjection is not generated for MDS users, so any email/web part that depends on a projection is empty for them. The BB scripts do not check it (check `B08` = INFO). | STATED by the requester | open, needs owner |
| 2 | BB email: strings | `BBCumulativeBalanceSectionContentProvider` dereferences 9 string ids with `stringMap.get(id).getText()` and no null guard (`populateBBCumulativeBalanceData`, `populateHighChartLegends`, `getInsightText`). One missing string aborts the whole Monthly Summary render with no drop reason. Same pattern as the TOU strings defect. | CODE | open |
| 3 | BB email: solar/zero cost | `widthOfDataLabel` uses `Math.log10(Math.min(currentActualCost, currentBudgetBillCost))`. For a cycle whose actual cost is 0 or negative (net-metered solar home) the result is `-Infinity`/`NaN`, so the chart layout values (`diffTextXAxisSeparation`, `xAxisPadding`) become invalid. MDS homes are solar D&R, so this is reachable. | CODE | open, to reproduce with a negative-cost BB cycle |
| 4 | BB manager: measurement type | `getUserBBProgramList` does `getBudgetBillingProgramIdsMap().get(measurementType).contains(...)`. `bb_program_ids_map` on 88002 (and 88001/88009) lists only ELECTRIC and WATER, so a GAS lookup (dual-fuel MDS user) dereferences null. | CODE | open, to reproduce with a dual-fuel user |
| 5 | BB manager: comparator | `appendBillCyclesFromBCSchedules` sorts with `Integer.compare(data1.getValidFrom(), data2.getValidTo())` (from vs to). It is not a valid comparator, so the order of future cycles is only right by accident. | CODE | open |
| 6 | BB chart: empty series | `populateActualAndBBCostData` calls `cumulativeActualCostSeries.get(currentBillIndex)` with `currentBillIndex = size - 1`; if no BB cycle starts on/before the latest invoice the list is empty and this throws `IndexOutOfBoundsException`. | CODE | open |
| 7 | Ingestion: BB_AMOUNT is invisible | The `utilitydata` GET wraps invoices in `UtilityBillingDataWrapper`, which drops `invoiceDataList`, so a BB_AMOUNT row cannot be read back. A wrong CHARGE_TYPE token yields 0.0 with no error. Only `budgetBillingInfo.bbAmount` reveals it. | earlier live work (see memory `budget-billing-invoice-bb-amount-row`) | known, document |
| 8 | Ingestion: silent demotion | A non-positive BB_AMOUNT, or a missing/zero `BB_TrueUp_Period`, makes the user read as non-BB (`isBBUser:false`) with no error anywhere. | earlier live work + CODE (`isValid`, `getBBTrueUpPeriod`) | known, document |

## Live findings (reproduced on pilot 88002, 2026-10-01)

### L1. USERPROGRAM file rejected with 500.2001 "unknown internal error": pilot 88002 has no `program_part_launchpad` config  (LIVE, FIXED at pilot level)
- **Symptom:** the file was accepted by S3 and picked up by the ingester, but the marker `Error/USERPROGRAM_…processed` was 140 bytes (rejection) and `account/programs` stayed `program-data: []`. No user can be enrolled in any programme on 88002.
- **Log (ingestion host 10.2.1.146, 2026-10-01 02:40:51 UTC):**
  `LaunchpadUserProgramParticipationTransformer:transformAndValidate:122 ERROR Error while validating record … java.lang.NullPointerException: Cannot invoke "LaunchpadProgramParticipationFieldsRequest.getFieldPosition()" because the return value of "java.util.Map.get(Object)" is null at …validateDates(LaunchpadUserProgramParticipationTransformer.java:337)`
- **Cause:** the field-position map comes from pilot config type `program_part_launchpad` (10 keys, `customer_id` … `program_attr`). It is present on 88001 and 88009 and **absent on 88002**, so the map is empty.
- **Fix applied (pilot scope, additive, backup in pingpong/backups/):** created `program_part_launchpad` on 88002 with the same 10 keys as 88001 (done by `mds_bb.py --phases pilot`). After a 87 s cache settle the same file was re-uploaded and ingested: programs API row present, web `isBBUser: true`.
- **Bugs to raise:**
  1. **Config gap:** 88002 (and any new masterpilot pilot) is provisioned without `program_part_launchpad`; programme enrolment silently fails. Add it to the masterpilot pilot provisioning checklist.
  2. **Code (ingester):** `validateDates` (and `validateFields`) dereference `programPartFieldMap.get(<field>)` with no null check. A missing/partial config must produce a clear error ("program_part_launchpad missing for pilot N"), not an NPE reported as `500.2001 unknown internal error`. Also `getValidProgramParticipationFieldMap` returns an empty (not null) map here, so the `== null` guard at `LaunchpadUserProgramParticipationTransformer:78` never fires.
  3. **Observability:** the marker only says "unknown internal error"; the real cause is only in the ingestion host log.

### L2. Observations from the web widget (LIVE)
- `budgetBillingInfo` fields are `isBBUser, bbAmount, actualBillAmount, trueUpPeriod, trueUpDate` (not `actualCost`).
- `trueUpDate` came back as `"Mar 04"` for a programme starting 2026-03-01 with `BB_TrueUp_Period` 12 (the 12th cycle should end about 2027-03-01). To confirm whether this is the BC-schedule gap filling in `appendBillCyclesFromBCSchedules` (see #5) or the inclusive/exclusive end-date handling on MDS (`isEndDateInclusive=false`). Not yet established.

### L3. End-to-end result (LIVE, two users, 2026-10-01)
- User `9560de3b-…` (resumed after the L1 fix) and user `68477e34-…` (one fresh command, no manual step): 41 checks, 0 FAIL.
- Monthly Summary rendered on both with variant `MS_SOLAR,MS_BB`, the BB chart (image fetched OK), balance insight equal to budget minus actual from the generated data (+43 and +46), True-up and BB disclaimers, no unrendered template variables, no foreign branding. The web widget and the email agree on bbAmount and the bill.
- No defect found in the email render itself on 88002 once `program_part_launchpad` exists, so code-read items #2-#6 above remain unreproduced (they need a missing string, a non-positive cost cycle, a GAS lookup or an empty series to trigger).
- The only open anomaly is L2 (`trueUpDate` 3 days after the expected cycle end, same on both users).

## Budget Alert (BUDGET_ALERT) on 88002: findings (LIVE, 2026-10-01, built in `mds_ba.py`)

| # | Area | Finding | Evidence | Status |
|---|---|---|---|---|
| BA1 | Trigger path | A `NotificationsProcessorEvent {EventName: BUDGET_ALERT}` is consumed by the notifications daemon but produces **no log line and no email**; the processor only runs inside the aggregation-driven pass (`AggregationReadyUsers` with `sendNotifications=true`). Every other named event (MS, TOU_ONBOARDING) works on that queue, so the behaviour is surprising and undocumented. | notifications host log shows only "Starting notifications processor" / "Invoking EventProcessors"; aggregation host shows `Executing event processor for BUDGET_ALERT` for the same user | LIVE, open |
| BA2 | Solar homes | `EmailDataProvider.populateSolarEnergyFlowBlock:980` NPEs (`itemization.get(ELECTRIC)` is null for the open cycle) so the whole Budget Alert dies for any DCX solar home. Same defect as TOU Onboarding. Workaround in the script: `home.solarUser=false` for the send, restored afterwards. | aggregation log 03:51:18 UTC `Exception in budget alert event processor ... populateSolarEnergyFlowBlock(EmailDataProvider.java:980)` | LIVE, open (code fix needed: null-guard) |
| BA3 | Fuel loop | `BudgetAlertEventProcessor` iterates every allowed measurement type, so an electric-only user is also processed for GAS and logs `ERROR Billing cost for user ... could not be found` on every pass. Harmless but noisy. | aggregation log 03:11:51 UTC | LIVE, open |
| BA4 | Delayed-alert window | `max_threshold_days_for_budget_alert=2` drops the alert when the threshold was crossed more than 2 days before the last data point, so a test budget must be derived from live cost (`B = (cost - c2/2)/threshold`); a fixed budget silently yields `BUDGET_THRESHOLD_WINDOW_CROSSED`. | `checkBudgetAlertWindow` | CODE, handled in script |
| BA5 | Email copy | Subject reads "See your new upgrade! You've reached 75% of your budget": the "See your new upgrade!" prefix looks like a leftover string; confirm with product whether it is intended for Budget Alert. | rendered subject on both emails | LIVE, to confirm |
| BA6 | Branding | Both emails link to `nsp-nonprodqa.bidgely.com` (the settings/feedback links), flagged WARN by the generic-branding check. Environment link, probably expected on ProductQA. | M10/X10 | LIVE, informational |
| BA7 | Email copy | The days-left label uses the singular text for any count `<= 1` (`daysLeft > 1 ? plural : singular`), so zero days renders "0 Day Left in Billing Cycle" (should be "0 Days"). The value itself is right: on the last cycle day `dayEnd(billEnd-1) - dayEnd(today) = 0`. | `EmailBudgetAlertSectionContentProvider.populateBudgetAlertSection` | LIVE (cosmetic), open |
| BA8 | Email subject | Every MDS user gets the "See your new upgrade! You've reached 75% of your budget" subject, forever. 88002 sets `email_template.use_different_subject_line_for_new_users=true` and `welcome_section_hybrid_mails_threshold=1` at PILOT scope; `EmailSubjectProvider.userHasMailAsNewFeature` reads the BUDGET_ALERT state WITHOUT a fuel type (`.../BUDGET_ALERT/Email`, always null), while `BudgetAlertEventProcessor` writes it per fuel (`.../BUDGET_ALERT/ELECTRIC/Email`, sentCount reached 12 in testing), so the 'seen before' test can never be true. Workaround in the script: user-scope `use_different_subject_line_for_new_users=false` (`--user-kind returning`, default). | state GETs on user 81138c66 + both subjects | LIVE, open (code: read the same state key the processor writes) |
