# HER persona setup

Sets up a HER (Home Energy Report) persona for one user against a pilot, end to
end. `setup` runs these five steps in order:

1. **Pilot config** - fixes `her_moderation`'s `her_base_url` /
   `her_vendor_bucket` if they've drifted, and pushes
   `neighbourhood_comparison.data_point_threshold` so SHC will generate.
2. **DB rows** - renders `nbi_asset_data.sql.template` for the pilot, then
   inserts the rows this pilot is missing (over an SSH tunnel - see below).
   The `.sql` file is still written so you can run it by hand instead.
3. **Interactions payload** - fills in the user's last **completed** billing
   cycle and uploads `<uuid>.json` to the pilot's `profile=interaction` prefix.
4. **String resources** - pushes title/shortText/longText for every mock
   tip/insight/program via the `stringResources` API.
5. **SHC 2.0 mock pipeline** ([Confluence PM/1455620097](https://bidgely.atlassian.net/wiki/spaces/PM/pages/1455620097)) -
   fetches the pilot's full user list, splits it into clusters, uploads the
   mock `diff`/`total`/`cluster_info` files plus the feature-metadata yaml, and
   renders the `NeighbourhoodPostProcessingRunner` command. This is what backs
   the HER report's SHC (neighbourhood-comparison) insights.

Every S3 upload is skipped if that exact object already exists.

## What you still do by hand

The script can't do these, so it prints/writes what you need:

| Step | Where |
|------|-------|
| Run the `nbi_asset_data` SQL by hand — **only** if the `DB_*` config is unset or you pass `--skip-db` | `output/nbi_asset_data_<PILOT_ID>.sql` |
| Run the `NeighbourhoodPostProcessingRunner` java command | `output/run_nhood_postprocessing_<PILOT_ID>_<BATCH_ID>.sh`, also echoed to the log - copy it to the machine where `nhoodServices` is installed |
| Trigger `ncRunner` to compute nMetrics | On the `datacubejobs` machine: `sudo -u bprod /opt/bidgely/datacubejobs/sh/ncRunner run` |

## Setup

The real config file holds live tokens and is git-ignored. Copy the template
and fill it in:

```bash
cp config.json.example config.json
```

```json
{
  "BASE_URL": "https://api-server-masterpilot-productqa.bidgely.com",
  "AUTH_TOKEN": "<api-server bearer token>",
  "PILOT_ID": "88009",
  "ENVIRONMENT": "productqa",
  "FUEL_TYPE": "ELECTRIC",
  "UUID": "<destination user uuid>",
  "DETO_BASE_URL": "https://deto-productqa-api.bidgely.com",
  "DETO_TOKEN": "<deto bearer token>",
  "HOME_ID": "1",
  "NHOOD_JAR_PATH": "/opt/bidgely/nhoodServices/onelib/OneJar-core-nhoods-4.0-SNAPSHOT.jar"
}
```

### Required

| Key | Used for |
|-----|----------|
| `BASE_URL` | Every non-DETO call: pilot config read/write, billing-cycle lookup, the `stringResources` push (passed as `$1` to the rendered script), and the `verify` calls. Also **derives** the expected `her_base_url` and the java `-Dqueue.suffix`. |
| `AUTH_TOKEN` | Bearer token for all `BASE_URL` calls (passed as `$2` to the string-resources script). |
| `PILOT_ID` | SQL `entity_id`, the `stringResources` URL path, pilot config calls, SHC cluster ids, all SHC S3 paths, and the runner's `-ntype`. |
| `ENVIRONMENT` | The HER payload bucket (`bidgely-profile-data-<env>`), the SHC warehouse bucket (`bidgely-data-warehouse-<env>`), and the runner's `-Dmy.env`. |
| `FUEL_TYPE` | The payload's `fuelType=` path segment; picks the `measurementType: GAS` header and which billing block gets filled; the SHC `cluster_info` `fuelTypeTag`/`name`. |
| `UUID` | The HER persona user - payload filename, `uuid=` path segment, and the billing-cycle lookup. Unrelated to the SHC user list, which comes from DETO. |
| `DETO_BASE_URL` | The SHC user-list fetch only. |
| `DETO_TOKEN` | Bearer for the two DETO calls (**separate** from `AUTH_TOKEN`). |

### Optional

| Key | Default | Used for |
|-----|---------|----------|
| `HOME_ID` | `1` | Billing-cycle lookup (`homes/<HOME_ID>/billingcycles`). |
| `NHOOD_JAR_PATH` | the 4.0 jar | The runner's `-cp`. Use `OneJar-core-nhoods-3.1-SNAPSHOT.jar` if the target machine runs Java 8. |
| `FEATURE_METADATA_S3_PATH` | `s3://bidgely-artifacts2/yash/demo_shc.yaml` | Where `templates/demo_shc.yaml` is uploaded, and the runner's `-featureMetaDataFilePath`. |
| `DATA_POINT_THRESHOLD` | `0` | Value pushed for `neighbourhood_comparison.data_point_threshold`. `0` = fully relaxed SHC generation; **push `20` before a pilot goes to prod**. |

### DB access (optional — the DB step is skipped without it)

| Key | Default | Notes |
|-----|---------|-------|
| `DB_HOST` | — | RDS hostname, e.g. `productqa-rds.cmlamxremgnb.us-west-2.rds.amazonaws.com`. |
| `DB_NAME` | — | Schema holding `nbi_asset_data`, e.g. `bidgelydbqa_masterpilot`. |
| `DB_USER` / `DB_PASSWORD` | — | Needs `INSERT` on that schema. |
| `DB_PORT` | `3306` | |
| `DB_SSH_USER` | — | SSH user on the jumphost (**not** your local username — it's typically your email local part, e.g. `ygupta`). |
| `DB_SSH_HOST` | `jumphost-<ENVIRONMENT>.bidgely.com` | Derived from `ENVIRONMENT`. |
| `DB_SSH_KEY` | `~/.ssh/id_ed25519` | Key for the jumphost. |

### Derived at runtime (deliberately not configurable)

| Value | How |
|-------|-----|
| HER payload `batch=` | The **oldest** billing-cycle start (`min` of every cycle's `key`). |
| SHC `batch_id=` | Highest existing batch id in S3 **plus one**; `111` if the pilot has none. Checks both the top level *and* `processed/`, since completed runs get moved into `processed/`. |
| Cluster count | One cluster per 20 users, minimum 1 (38 users → 1 cluster, 40 → 2, 52 → 2), then split evenly. |
| `-Dqueue.suffix` | The project name inside `BASE_URL`'s host - between `api-server-` and `-<ENVIRONMENT>`. So `api-server-masterpilot-productqa` → `masterpilot`. |
| `-Dmy.env` | `ENVIRONMENT`. |
| SHC bucket | `bidgely-data-warehouse-<ENVIRONMENT>`. The Confluence doc hardcodes the `uat` bucket because that's the env it was written against. |

## Running

From this folder, after `uv sync` at the repo root (see the
[root README](../../README.md)):

**Interactive** — run with no arguments and it asks what you want:

```bash
uv run python setup_her.py
```

```
Which HER do you want to set up?
  1) Monthly HER  - pilot config, DB SQL, payload -> S3, strings, SHC
  2) Seasonal HER - derive + upload the Summer/Winter NBIs
  3) Both         - monthly first, then seasonal
```

It confirms the pilot/env/fuel/UUID from `config.json` first, offers a dry run,
then asks only the questions relevant to your choice. Pick Seasonal and it asks
which season:

```
  Summer or Winter?
  1) Summer  (HER_SEASONAL_SUMMER / nbiType=SummerSeasonal)
  2) Winter  (HER_SEASONAL_WINTER / nbiType=WinterSeasonal)
  3) Both    (upload both seasonal files)
```

followed by the target batch, the payload source (monthly NBI vs bundled
templates), and whether to overwrite. It runs the exact same code paths as the
explicit subcommands below — nothing special-cased.

**Non-interactive** — any flag or subcommand skips the prompts entirely:

```bash
uv run python setup_her.py setup                 # the full monthly flow
uv run python setup_her.py --dry-run             # build files + print planned actions, no writes
uv run python setup_her.py --skip-s3             # skip the HER payload upload
uv run python setup_her.py --skip-db             # don't touch the DB; just write the .sql
uv run python setup_her.py --skip-strings        # skip the stringResources push
uv run python setup_her.py --skip-config-sync    # skip both pilot-config updates
uv run python setup_her.py --skip-shc            # skip the whole SHC step

# seasonal HER — derive Summer/Winter NBIs from the monthly one:
uv run python setup_her.py seasonal                            # batch derived like monthly
uv run python setup_her.py seasonal --batch 1787062655         # override the batch
uv run python setup_her.py seasonal --season summer            # one season only
uv run python setup_her.py seasonal --force                    # overwrite existing objects
uv run python setup_her.py seasonal --from-template            # no monthly NBI needed

# after running the rendered java command on the nhoodServices machine:
uv run python setup_her.py verify --uuid <a pilot user's uuid>

# threshold control (also pushed automatically during setup):
uv run python setup_her.py set-threshold 20      # revert to the prod value
```

`--dry-run` still performs read-only calls (billing cycles, pilot config, the
DETO user fetch, S3 existence checks) so the preview shows real paths and
values; only writes are suppressed.

### Outputs

Written to `output/` (git-ignored):

| File | Purpose |
|------|---------|
| `nbi_asset_data_<PILOT_ID>.sql` | The rows for this pilot; applied automatically unless `--skip-db`. |
| `<UUID>.json` | The interactions payload uploaded to S3. |
| `diff_dataframe.csv` / `output_dataframe.csv` | Identical uuid/cluster_id mock files → the `diff`/`total` prefixes. |
| `cluster_id_name_mapping.json` | → the `cluster_info` prefix. |
| `run_nhood_postprocessing_<PILOT_ID>_<BATCH_ID>.sh` | The runner command, ready to copy to the nhoodServices machine. |
| `set_string_resources_<PILOT_ID>.sh` | What the tool executes to push string resources. |

### Heads-up: each run mints a new SHC batch id

Because the batch id is "highest in S3 + 1", running `setup` twice without
running the java command in between leaves two batch folders under
`.../ntype=<pilot>/`. The runner scans that prefix, so stale batches can be
picked up. Delete the ones you don't want, or use `--skip-shc` when you only
need to redo the persona half.

## Seasonal HER (`seasonal`)

Recovery path for ProductQA when the upstream NBI pipeline hasn't produced the
seasonal objects. It derives them from the user's **existing monthly NBI** —
so run `setup` first, or point at a monthly batch with `--source-batch`.

1. **Source**: by default, finds the newest `batch=` under the user's
   `HER_MONTHLY_REPORT` prefix and downloads that NBI
   (`output/monthly-nbi.json`). With `--from-template` it instead uses the
   pre-built `templates/interactions_<season>_seasonal.json`, refreshing
   `billing_info` for this user — use that when the user has no monthly NBI yet.
2. Rewrites **every** interaction's `nbiType` to `SummerSeasonal` /
   `WinterSeasonal`. Nothing else is touched — scores, hashes, `insight`/`action`
   blocks and `nbi_delivery_helper_dict` carry over verbatim.
3. Validates each file the way the runbook's `jq -e` checks do: interactions
   must be non-empty, and the set of `nbiType` values must be exactly the one
   expected. A file that fails is **not** uploaded.
4. Uploads to `deliveryType=HER_SEASONAL_SUMMER` / `HER_SEASONAL_WINTER` with
   `--content-type application/json`, under a `batch=` derived the same way the
   monthly flow derives its own (oldest billing-cycle start) unless `--batch`
   overrides it.
5. Checks `report_type_nbi_run_mapping` contains all three report types and
   **appends** any that are missing (keeping whatever else is configured):
   `HER_MONTHLY_REPORT|HER,HER_SEASONAL_SUMMER|HER,HER_SEASONAL_WINTER|HER`.

| Flag | Purpose |
|------|---------|
| `--batch` | Target `batch=` segment. Default: derived exactly like the monthly flow — the **oldest** billing-cycle start for this user. Pass a value to override, e.g. when the aggregation request expects a specific batch. |
| `--source-batch` | Monthly batch to derive from. Default: the newest for this user. |
| `--from-template` | Build from `templates/interactions_<season>_seasonal.json` instead of downloading the monthly NBI. Billing values are still refreshed per user. |
| `--season summer\|winter\|both` | Default `both`. |
| `--force` | Overwrite the seasonal object if it already exists (default skips, like every other upload). |
| `--skip-config-sync` | Skip the `report_type_nbi_run_mapping` check/update. |
| `--dry-run` | Build + validate, upload nothing. |

Afterwards, rerun aggregation and confirm the logs contain
`Added [...] EE NBIs to the layout` plus `report-type=HER_SEASONAL_SUMMER` and
`report-type=HER_SEASONAL_WINTER`. The command prints this reminder on success.

Production should rely on the upstream NBI pipeline instead of this command.

## Details

### Pilot config sync

`GET`s `{BASE_URL}/entities/pilot/{PILOT_ID}/configs`, which
returns several configType blocks, each a JSON-encoded string. (It must be a
GET with no body - POSTing an empty body to this path makes the server's
`ConfigFilter` return a 500, `entityConfig is null`.) From
`her_moderation` it reads the current `her_base_url` / `her_vendor_bucket`;
from `s3_pull` it reads `s3DestinationBucket`. Expected values:

- `her_base_url` = `BASE_URL` with the `api-server-` prefix dropped, e.g.
  `https://api-server-masterpilot-productqa.bidgely.com` →
  `https://masterpilot-productqa.bidgely.com`.
- `her_vendor_bucket` = the pilot's own `s3_pull.s3DestinationBucket`.

Only keys that actually differ are pushed to
`POST {BASE_URL}/entities/{PILOT_ID}/configs`; if both match, nothing is sent.
`data_point_threshold` is then pushed under `configType:
neighbourhood_comparison`.

### DB rows (`nbi_asset_data`)

The RDS instances live on private VPC addresses — the VPN alone doesn't reach
them, so the script opens an SSH tunnel through the env's jumphost, exactly like
DBeaver's per-connection tunnel. It picks a free local port, forwards it, and
tears the tunnel down when done.

`(entity_id, asset_id, asset_key)` is the table's **PRIMARY KEY**, so that
triple decides whether a row is already there. The step:

1. `SELECT asset_id, asset_key, asset_value ... WHERE entity_id = <PILOT_ID>`
2. Inserts only the rows whose key isn't present, via a single parameterized
   `executemany` in one transaction.
3. **Existing rows are never modified** — not even when `asset_value` differs
   from the template (e.g. a changed icon URL). Those are counted and the first
   few printed, so a drift doesn't pass silently.

The whole step is skipped, with a note, if any of `DB_HOST` / `DB_NAME` /
`DB_USER` / `DB_PASSWORD` / `DB_SSH_USER` is unset — so the tool still works for
anyone without DB access. `--skip-db` skips it explicitly.

### Billing cycle lookup

```
GET {BASE_URL}/billingdata/users/{UUID}/homes/{HOME_ID}/billingcycles?t0=1&t1=<far future>
Authorization: bearer {AUTH_TOKEN}
measurementType: GAS   # only when FUEL_TYPE is GAS; omitted for electric
```

Cycles come back as `{"key": start, "value": end}`. Two values are taken from
them: the **last completed** cycle (latest `end` that's already in the past) goes
into the payload's `billing_info`, and the **oldest** `key` becomes the S3
`batch=` segment. Only the block matching `FUEL_TYPE` is filled -
`last_electric_billing_cycle_info` or `last_gas_billing_cycle_info` - and the
other stays at `-1`/`-1`.

### SHC user list

The union of two DETO APIs, deduped and sorted:

- `GET {DETO_BASE_URL}/v1/fetch-user-attribute?destination_pilot_id={PILOT_ID}`
  - ported/CDG users; only `status: success` rows are kept.
- `GET {DETO_BASE_URL}/v1/ingested-users?projectId={PILOT_ID}&limit=100&page=N`
  - organically-ingested users; paginated until `pagination.hasNext` is false.

## Requirements

- The `aws` CLI on `PATH`, configured with credentials that can read/write
  `bidgely-profile-data-<env>`, `bidgely-data-warehouse-<env>`, and whatever
  bucket `FEATURE_METADATA_S3_PATH` points at.
- Network access to `BASE_URL` and `DETO_BASE_URL`.

## Templates

`templates/` holds the source artifacts rendered/uploaded per pilot+user:

- `interactions.json` - the mock HER **monthly** interactions + insights payload.
  Generic; only `billing_info` is filled in per run.
- `interactions_summer_seasonal.json` / `interactions_winter_seasonal.json` -
  the same payload with every `nbiType` already set to `SummerSeasonal` /
  `WinterSeasonal`. Used by `seasonal --from-template`, which is the way to
  produce seasonal files for a user who has **no monthly NBI in S3** to derive
  from. `billing_info` is still refreshed per user, so the values baked into
  these files are never shipped as-is.
- `nbi_asset_data.sql.template` - `INSERT INTO nbi_asset_data` statements with
  `{{PILOT_ID}}` standing in for the entity id.
- `set_string_resources.sh.template` - the `stringResources` PUT calls with
  `{{PILOT_ID}}` in the URL path.
- `demo_shc.yaml` - the SHC feature-metadata config, uploaded to
  `FEATURE_METADATA_S3_PATH`. The runner fails with
  `NullPointerException: ... yamlJsonNode is null` if this object is missing.
  Upload is skipped when it already exists, so a hand-tuned S3 copy is never
  clobbered - delete the S3 object if you want a local edit to take effect.
