> Copied from `../MDS_AUTOMATION` for reference: this document describes the SOLAR suite. Non-solar behaviour and
> prerequisites are in `README.md`.

# TOU Onboarding fix — packaging, rollout, verification, rollback (masterpilot ProductQA, pilot 88002)

Status: **prepared, not executed.** Nothing below has been deployed. Every step that writes to a host or the pilot
needs explicit authorization.

## 0. What ships and where

| Change | Module (jar) | Runs in |
|---|---|---|
| `SectionConditionValidator.isEligible`; `EmailDataProvider` (rate structure / bill saving / TOU usage population, DCX per-fuel solar rule, null-safe ELECTRIC itemization) | core-lib | notificationsProcessorService (normal + priority), aggregationServices (normal + priority: the Monthly Summary pass runs there), emailServices |
| `TouRateStructureSectionContentProvider`, `TouBillSavingRecoSectionContentProvider` (+ `core-lib.xml` wiring) | core-lib | emailServices |
| `OauthTokenManager.invalidateToken`, `APIBasedConfigUpdater` renew-and-retry-once on 401, null-safe `getNotificationEventConfig` | core-utils | every service (exercised by emailServices) |
| `UserNotificationDetailsDao`: unavailable config no longer nulls the notification id | core-cass | emailServices |
| `TouOnboardingEmailTrigger` in `LaunchpadMeterCreator` (METERENROLL path) | ingester-transformers | ingesterJobs (not needed for the USERENROLL recovery below) |

Package modules (jdeb, each `.deb` ships `target/lib` to `/opt/bidgely/<service>/lib`):
`core/notifications-processor` → notificationsProcessorService, `core/core-aggregations` → aggregationServices,
`core/core-emails` → emailServices, `ingester/ingester-jobs` → ingesterJobs.

## 1. Build from a recorded commit

The fix is uncommitted on `ProductQAMaster-278`. Commit it first so the build has an identity (`git rev-parse HEAD`).
Maven 3.10 rejects the root `pom.xml` (duplicate declarations); use 3.9.x.

```bash
cd ~/Documents/pingpong
git rev-parse HEAD > /tmp/tou_fix_build_sha.txt
mvn -T 4 -pl core/notifications-processor,core/core-aggregations,core/core-emails,ingester/ingester-jobs -am \
    -DskipTests package                       # tests: see section 6, run them before packaging
ls core/notifications-processor/target/*.deb core/core-aggregations/target/*.deb \
   core/core-emails/target/*.deb ingester/ingester-jobs/target/*.deb
```

## 1a. Local reference build (2026-10-05, uncommitted working tree on top of e15a7330a04 — NOT a deploy artifact)

Built with Maven 3.9.9 `-o -DskipTests` to check packaging; rebuild from the committed SHA for the real rollout and
record those hashes instead.

| Package | .deb sha256 | lib jars | dependency-set sha256 (`ls target/lib \| sort`) |
|---|---|---|---|
| notificationsProcessorService_4.0~SNAPSHOT_all.deb | 4c77f849…f2af1556 | 329 | 4d9fe19f312d5a47… |
| aggregationServices_4.0~SNAPSHOT_all.deb | 31b0310c…8f7d66ef58d1 | 364 | 9556486506e00a41… |
| emailServices_4.0~SNAPSHOT_all.deb | 721170df…877b289 | 393 | e69083e5ebe75cea… |
| ingesterJobs_4.0~SNAPSHOT_all.deb | edb4f79e…c718c4db | 382 | 978799c1edb8b904… |

Shared jars (identical in every package): core-lib e4f5deaac231e84f…, core-utils 7e10f44d94769ecf…,
core-cass 444aeda469714cb1…; velocity-resources 13c675354cfbe7eb… (emailServices, aggregationServices);
ingester-transformers 8c5d67011b374477… (ingesterJobs). The packaged core-lib contains
`SectionConditionValidator.isEligible`, the packaged core-utils `OauthTokenManager.invalidateToken`.

## 2. Record artifact and dependency hashes (local)

```bash
for m in core/notifications-processor core/core-aggregations core/core-emails ingester/ingester-jobs; do
  echo "== $m"
  sha256sum $m/target/*.deb
  # dependency set = the exact lib dir the .deb installs
  dpkg-deb -c $m/target/*.deb | awk '{print $NF}' | grep '/lib/.*\.jar$' | sed 's#^\./##' | sort | sha256sum
  for j in core-lib core-utils core-cass core-api core-emails ingester-transformers velocity-resources; do
    f=$(dpkg-deb -c $m/target/*.deb | awk '{print $NF}' | grep "/lib/$j-4.0-SNAPSHOT.jar$") || continue
    dpkg-deb --fsys-tarfile $m/target/*.deb | tar -xO "$f" | sha256sum | sed "s#-#$j#"
  done
done | tee /tmp/tou_fix_artifact_hashes.txt
```
(on macOS without `dpkg-deb`: hash `target/lib/*.jar` of each module instead — it is what jdeb packs.)

## 3. Host rollout (order) — per host: back up, install, restart, verify

Order: **emailServices → notificationsProcessorService (priority, then normal) → aggregationServices (priority, then
normal) → ingesterJobs.** The emailer goes first so a notification built by the new processor is rendered by the new
section providers.

```bash
S=/opt/bidgely/<service>
sudo tar czf /tmp/<service>_lib_before_$(date +%Y%m%d%H%M).tgz -C $S lib           # rollback copy
( cd $S/lib && ls | sort | sha256sum; sha256sum core-lib-4.0-SNAPSHOT.jar core-utils-4.0-SNAPSHOT.jar ) \
    | tee /tmp/<service>_lib_before.sha                                               # pre-rollout fingerprint
# install the .deb through the standard masterpilot ProductQA deploy job (preferred) or: sudo dpkg -i <pkg>.deb
# restart through the service's own script, e.g. sudo $S/sh/<daemon> restart
```

## 4. Prove the running JVM loaded the new artifact (per host)

A jar on disk containing `isEligible` proves nothing about the process. All four must hold:

```bash
S=/opt/bidgely/<service>; PID=$(pgrep -f "$S/lib" | head -1)
ps -o pid,lstart,args -p $PID | cut -c1-120                    # (a) started AFTER the install time
sha256sum $S/lib/core-lib-4.0-SNAPSHOT.jar                      # (b) == the hash recorded in section 2
( cd $S/lib && ls | sort | sha256sum )                         # (c) == the dependency-set hash in section 2
J=$S/lib/core-lib-4.0-SNAPSHOT.jar
stat -c %i $J; sudo ls -lL /proc/$PID/fd 2>/dev/null | grep -c core-lib      # (d) the JVM holds the installed jar:
sudo readlink /proc/$PID/fd/* 2>/dev/null | grep core-lib-4.0-SNAPSHOT.jar   #     no "(deleted)" suffix, and
sudo stat -L -c %i /proc/$PID/fd/$(sudo ls -l /proc/$PID/fd | awk '/core-lib-4.0-SNAPSHOT.jar/{print $9; exit}')  # same inode
```
Runtime behaviour (section 5) is the final proof.

## 5. Runtime verification = the recovery transition (one ingestion switch)

**Blocked until restored:** `mds_user_automation/results/dual_fuel/run_20261001_145643_9e203b/` (manifest + the
generated USERENROLL file) is no longer on disk. The script needs that exact ELECTRIC USERENROLL row; it is not
reconstructed from API data (a USERENROLL update rewrites profile fields). Restore the run directory, or create a new
dual-fuel account with `create_mds_dual_fuel_user.py` and use its manifest.

```bash
export BOPS_PW_FILE=$TMPDIR/bops_pw_$USER
M=mds_user_automation/results/dual_fuel/run_20261001_145643_9e203b/manifest.json
python3 mds_user_automation/transition_mds_user_to_tou.py --manifest $M --dry-run   # live read-only preflight only
python3 mds_user_automation/transition_mds_user_to_tou.py --manifest $M             # the one ingestion transition
```
Preflight (read-only, blocks on any failure): pilot 88002 + uuid identity, ELECTRIC + GAS meters present and active,
genuine solar ELECTRIC (home + D and R meters solar, GAS not), current ELECTRIC plan non-TOU, GAS schedule baseline,
target plan has On-Peak/Off-Peak, effective date ≤ today and a billing-cycle start, not already transitioned,
TOU_ONBOARDING not already sent, pilot flag `true` with no unresolved window, no `--manual-trigger`.
The transition is ingestion-only: on an ingestion timeout it fails with **no home API schedule write**.

## 6. Acceptance — each proven separately

| # | Evidence | Source |
|---|---|---|
| 1 | deployed fix running | section 4 (a)–(d) on every host |
| 2 | dual-fuel + solar identity | preflight PASS line + `reports/recovery_preflight_*.json` |
| 3 | event publication | ingester `TouEmailAlertManager:sendMessageToQueue` "Successfully published tou onboarding" for the uuid, after the upload |
| 4 | processed, TOU data populated | notifications-priority pass for `EventName=TOU_ONBOARDING`: no "tou rate structure data not available"; `populateTouRateStructure` reached |
| 5 | rendered HTML + images + units | `email/TOU_ONBOARDING_*.html`: O03 plan-907 image, peak window, $/kWh (electric) — review by hand |
| 6 | GAS unchanged | `recoveryGasScheduleUnchanged: true` in the manifest; GAS schedule before == after |
| 7 | notification stored | evidence chain `storage: verified notificationId=...` (stored row from 2.1/utility_notifications) |
| 8 | provider acceptance | emailer `SparkPostEmailSender` "Successfully injected email to sparkpost for id <uuid>, emailType TOU_ONBOARDING" |
| 9 | inbox receipt | a person opens the account's bidgelyqa+ alias in the bidgelyqa@bidgely.com mailbox — never inferred from 3–8 |

Kryo: the "Encountered unregistered class" errors suggest version skew between services. A consistent build may remove
it; that is only shown if the errors stop for users cached after the rollout — it is not implied by section 4.

## 7. Rollback (per host, reverse order)

```bash
S=/opt/bidgely/<service>
# reinstall the previous .deb through the deploy job, or:
sudo tar xzf /tmp/<service>_lib_before_<ts>.tgz -C $S && sudo $S/sh/<daemon> restart
( cd $S/lib && ls | sort | sha256sum; sha256sum core-lib-4.0-SNAPSHOT.jar core-utils-4.0-SNAPSHOT.jar ) \
    | diff - /tmp/<service>_lib_before.sha && echo "rolled back to the recorded fingerprint"
```
A recovery transition that already ran is not rolled back by a code rollback: the user stays on plan 907 for ELECTRIC.
