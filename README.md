# nwp-analysis-archive

Keeps an archive of the **analysis time step** (lead time 0) of every DINI
forecast produced by
[nwp-forecast-zarr-creator](https://github.com/dmidk/nwp-forecast-zarr-creator),
in a separate location. It runs as a container next to the zarr creator and
reads and writes zarr stores on S3 (or on paths such as a mounted bucket).

The archive comes in two forms, built from the same source:

| | Archive A (staging) | Archive B (final) |
|---|---|---|
| Layout | one growing zarr per dataset | one growing zarr per dataset |
| Time chunk | 1 analysis time | 1 block = 28 days (224 cycles at 3-hourly) |
| Spatial chunks | as the forecast zarrs | `x` and `y`: 64 points |
| Good for | appending cheaply; reading maps | point time series (e.g. verification, interpolation to stations) |
| Updated | every poll (default 15 min) | once a day (default 00:30 UTC) |

Datasets, as written by the zarr creator: `pressure_levels`, `height_levels`
and `single_levels`. Each is archived to `{archive}/{dataset}.zarr`.

## How it works

Nothing triggers the archiver, so it scans:

1. **List** the forecasts under `--src-uri` (directories named like
   `2025-03-03T060000Z`, each holding `{dataset}.zarr`).
2. **Archive A**: every analysis time that is not yet in A is read (lead time
   zero only) and appended along `analysis_time`, oldest first. The state is
   what is in A, so a crashed or skipped pass is repaired by the next one and
   gaps are back-filled.
3. **Archive B**, once a day: the 28-day block that holds B's latest analysis
   time, and any newer block A has, is rebuilt from A (see below).

Variables without a `time` dimension in the forecasts (e.g. `lsm`) are
stored once, without an `analysis_time` dimension.

Before a cycle is appended it is checked against the archive's **schema**
(see [Versioning and migrations](#versioning-migrations-and-re-ingest)): a
forecast zarr that is missing, leftover or still being written is skipped
quietly, and one whose structure differs is skipped with an ERROR. Each
archived cycle also records which creator produced it, in the string
coordinates `zarr_creator_version` and `source_zarr_creation_time` along
`analysis_time` (empty in B for slots without a cycle).

### Archive B: nightly overwrite of the current block

B is laid out in fixed **28-day blocks**, aligned to a fixed epoch (Monday
2000-01-03), so the block of an analysis time is a floor division. A block
is one chunk along time. Because forecasts start every 3 hours (8 a day),
the last block is nearly always **incomplete**: it fills by 8 cycles per
night for 28 nights.

- When the first analysis time of a block arrives, the block is allocated
  **whole**, with its full regular time axis. Cycles that have not happened
  yet are **NaN**; so is a cycle that never reaches A, so the time axis stays
  evenly spaced.
- Each daily update **overwrites the chunks of the current block in place**
  with everything A now has for it. Finished blocks are not rewritten by the daily update, so a
  cycle that arrives in A after its block was passed is not picked up (use
  [re-ingest](#re-ingesting-corrected-or-back-filled-cycles)).
- The array shape only changes when a new block starts. The group attribute
  `last_complete_analysis_time` is set **last**, after all chunks are written
  and the metadata is consolidated: take B's data up to that time as final.
  Slots after it are NaN, not missing data.

The overwrite is meant to run when nobody reads B, hence the default of
**00:30 UTC**: the 21 UTC cycle is converted around 23 UTC (the zarr
creator's analysis lag), so an update at exactly 00:00 could miss it. Readers
are not locked out. A reader that overlaps the update can see some spatial
chunks already updated and others not, within the current block only.

Consequences for readers:
- To get the most recent time series, slice B to `last_complete_analysis_time`.
- B has a time coordinate that extends into the future by up to 27 days
  (NaN data).

## Running

### Container

The image's entrypoint is `python -m nwp_analysis_archive` with
`NWP_ARCHIVE_WATCH=true`, so it keeps running (A every poll, B daily). On the
EC2 host (`dini-to-zarr.dmidev.org`) the zarr creator reads and writes
`s3://harmonie-zarr` directly with the instance role, so the archiver does the
same: S3 URLs, no mount, no AWS profile.

```bash
docker run -d --name nwp-analysis-archive --restart unless-stopped \
  --memory 2500m --log-opt max-size=50m --log-opt max-file=5 \
  -e NWP_ARCHIVE_SRC_URI=s3://harmonie-zarr/dini/control \
  -e NWP_ARCHIVE_ARCHIVE_A_URI=s3://harmonie-zarr-archive/dini/staging \
  -e NWP_ARCHIVE_ARCHIVE_B_URI=s3://harmonie-zarr-archive/dini/final \
  ghcr.io/leifdenby/nwp-analysis-archive:<tag>
```

- Use `--restart unless-stopped`: an archiver that is down for about a week
  loses data (see [retention](#why-the-archive-matters-retention)).
- The role needs read access to the source bucket and read, write and delete
  access to the archive bucket (B overwrites objects in place).
- Plain paths also work for all three locations (e.g. a FUSE mount volume
  mounted into the container). Archive B overwrites existing chunk and
  metadata files in place, so such a mount must allow overwriting objects.

### Locally

```bash
uv sync
NWP_ARCHIVE_SRC_URI=s3://harmonie-zarr/dini/control \
NWP_ARCHIVE_ARCHIVE_A_URI=/tmp/archive/a \
NWP_ARCHIVE_ARCHIVE_B_URI=/tmp/archive/b \
NWP_ARCHIVE_AWS_PROFILE=<profile> \
uv run python -m nwp_analysis_archive
```

Without `--watch`, one pass updates the archives named by `--steps` (default
both) and exits, so it can also be run from cron.

`--aws-profile` applies to `s3://` URLs only; plain paths and mounts never use
it. Do not run the full source against a new location to try it out: a pass
reads every variable over the full domain for every forecast. For a small test
use the testing options, e.g.:

```bash
NWP_ARCHIVE_DATASETS=single_levels NWP_ARCHIVE_MAX_CYCLES=3 \
NWP_ARCHIVE_VARIABLES="hcc lcc lsm" NWP_ARCHIVE_CROP_SIZE=200 ...
```

## Configuration

Every command line option `--some-option` can also be set with the
environment variable `NWP_ARCHIVE_SOME_OPTION`. An option on the command line
wins over its variable, which wins over the default (the same convention as
the ANNA inference container; the mapping is in
[nwp_analysis_archive/envargs.py](nwp_analysis_archive/envargs.py)).

| Option | Environment variable | Default | |
|---|---|---|---|
| `--src-uri` | `NWP_ARCHIVE_SRC_URI` | (required) | forecast zarrs root, e.g. `s3://harmonie-zarr/dini/control` or a mounted path |
| `--archive-a-uri` | `NWP_ARCHIVE_ARCHIVE_A_URI` | (required) | prefix of archive A |
| `--archive-b-uri` | `NWP_ARCHIVE_ARCHIVE_B_URI` | (required) | prefix of archive B |
| `--datasets` | `NWP_ARCHIVE_DATASETS` | `pressure_levels height_levels single_levels` | space-separated |
| `--interval-hours` | `NWP_ARCHIVE_INTERVAL_HOURS` | `3` | hours between analysis times; must divide 24 |
| `--steps` | `NWP_ARCHIVE_STEPS` | `a b` | archives to update in a single pass (without `--watch`) |
| `--watch` | `NWP_ARCHIVE_WATCH` | `false` (`true` in the image) | keep running |
| `--poll-minutes` | `NWP_ARCHIVE_POLL_MINUTES` | `15` | minutes between updates of A with `--watch` |
| `--final-update-time` | `NWP_ARCHIVE_FINAL_UPDATE_TIME` | `00:30` | UTC time of day (`HH:MM`) from which B is updated, once per UTC day, with `--watch` |
| `--max-staleness-hours` | `NWP_ARCHIVE_MAX_STALENESS_HOURS` | `9` | an ERROR `STALE ...` is logged when the newest archived cycle of a dataset is older than this (a one-shot pass then exits with 1) |
| `--aws-profile` | `NWP_ARCHIVE_AWS_PROFILE` | (AWS config) | profile for `s3://` URLs |
| `--log-level` | `NWP_ARCHIVE_LOG_LEVEL` | `INFO` | loguru level |
| `--max-cycles` | `NWP_ARCHIVE_MAX_CYCLES` | | testing: only the newest N forecasts |
| `--variables` | `NWP_ARCHIVE_VARIABLES` | | testing: only these variables |
| `--crop-size` | `NWP_ARCHIVE_CROP_SIZE` | | testing: first N points in x and y |

- Flags take `true`/`false` (also `1`/`0`, `yes`/`no`); lists are
  space-separated; an empty variable counts as unset.
- A set `NWP_ARCHIVE_*` variable that matches no option is an error, so a
  misspelling can't go unnoticed.
- The run starts by logging every option with its variable name, value and
  where the value came from (`cli`, `env` or `default`).

## Assumptions and limits

- Forecast zarrs have a `time` dimension (valid time) with lead time zero
  first, and are in `{src}/{YYYY-MM-DDTHHMMSSZ}/{dataset}.zarr`. The member
  is whatever `--src-uri` points at (`.../dini/control`).
- Archives use zarr format 2, like the forecast zarrs, which has no
  sharding. Data keep the forecast zarrs' dtype (float64 in the samples
  tried); see [Compression and storage](#compression-and-storage).
- Run **one instance** per archive: updates are not locked.
- Archive A is never pruned (open question, see [Compression and
  storage](#compression-and-storage)).
- Block boundaries are by analysis time in UTC, not calendar months.

## Why the archive matters: retention

The forecast zarrs in `harmonie-zarr` are deleted after a short time (and the
source GRIB files go to tape). Measured on 2026-10-28: the oldest cycle was
8.6 days old, with a continuous run of about 7.9 days. So:

- Anything the archiver does not copy within **about a week** is gone from the
  source. A pass that skips or misses a cycle has that long to be repaired.
  Backfilling is only possible for what the source still holds.
- For older cycles **archive A is the only copy** (and B, which is derived
  from it). Treat it as a record: snapshot it or enable bucket versioning
  before changing it.
- Run the archiver continuously and watch that it does; an outage of more
  than about a week loses data permanently.

## Compression and storage

Data variables in A and B are compressed with **Blosc zstd (level 5) with
bitshuffle**, which is lossless. On a sample (one `single_levels` analysis
step, 17 variables, 600 x 600 points, 49 MB raw as float64):

| Variant | Size |
|---|---|
| Blosc lz4 + shuffle | 15.4 MB |
| Blosc zstd 5 + bitshuffle (used) | 13.0 MB |
| float32, then zstd 5 + bitshuffle | 12.8 MB |
| float64 + bit rounding at 99% information, zstd | 12.0 MB |
| float32 + bit rounding at 99% information, zstd | 6.6 MB |

- **float32 is not used.** Once compressed it saves almost nothing (12.8 vs
  13.0 MB); the source seems to hold float32 values in float64, which
  compress well anyway. The dtype of the forecast zarrs is kept.
- **Bit rounding (xbitinfo) is not used.** It could roughly halve the size,
  but it is lossy, and the archive is the only copy of old cycles. In the
  sample, the automatic 99% setting on float32 kept a single mantissa bit for
  `u10m`, `v10m`, `hcc` and `mcc` (up to 20% relative error), which is not
  acceptable and not understood. If it is ever adopted, use a fixed
  per-variable table of mantissa bits validated against the use cases
  (verification, training), never an automatic per-run choice. Only
  `single_levels` was tested.
- **Size estimate** for A, from the compressed size of one forecast divided
  by its 37 time steps (the source's own compression; not measured on the
  archive): `single_levels` ~100 MB, `height_levels` ~105 MB and
  `pressure_levels` ~460 MB per cycle, about 660 MB per cycle in total, 5.3 GB
  a day and roughly 1.9 TB a year. B duplicates A, so keeping both about
  doubles that. Whether to drop finished blocks from A once they are in B
  (leaving B, chunked for time series, as the only copy) is undecided.

## Versioning, migrations and re-ingest

**Rule: the archive always matches the current output of the zarr creator.**
Every zarr creator release that changes its output (renamed or added
variables, units, values, grid) ships with an archiver release that targets
the new schema and contains a migration, and the archiver release is deployed
**before or together with** the creator release. Archive B is derived from A,
so it is rebuilt, never migrated.

### Schema

The output format is identified by a structural **schema**, not by the
creator's version string (that is derived from `git describe` at build time,
and does not reliably identify a format). `nwp_analysis_archive/schemas/v1.json`
holds, for each dataset, the variables (dimensions, dtype, `units`,
`standard_name`), the coordinates, the size of every dimension except `time`
and the projection (`crs_wkt`). It was generated from a real forecast with:

```bash
uv run python -m nwp_analysis_archive.schema \
  --src-root s3://harmonie-zarr/dini/control --aws-profile <profile> \
  --out nwp_analysis_archive/schemas/v<N>.json
```

All 63 forecasts on the source on 2026-10-28 matched it.

- Each archive stores `archive_schema_version` as a group attribute. The
  update commands refuse to touch an archive with another version (or none),
  and tell you to run the migration.
- A source cycle that does not match is **not archived**. It is logged at
  ERROR level (`INCOMPATIBLE ...` with the differences) and counted in the
  pass report, and tried again on every pass. The source is deleted after
  about a week, so an incompatible cycle needs a fixed archiver within that
  time or it is lost.

### Changing the schema (a creator release with new output)

1. Generate `schemas/v<N+1>.json` from a forecast made by the new creator and
   set `TARGET_SCHEMA_VERSION = N+1` in `schema.py`.
2. Add the migration to `MIGRATIONS` in `migrations.py`: key `N`, a function
   from a lazy archive A dataset (schema N) to one at schema N+1 (renames,
   unit conversions, a NaN-filled variable for an additive change, ...).
3. Release the archiver, and deploy it before or with the creator.
4. Migrate the existing archive A to a **new** location (never in place; the
   old A is the only copy of old cycles until you have checked the new one):

   ```bash
   docker run --rm --entrypoint python \
     -e NWP_ARCHIVE_MIGRATE_FROM_A_URI=s3://harmonie-zarr-archive/dini/staging \
     -e NWP_ARCHIVE_MIGRATE_TO_A_URI=s3://harmonie-zarr-archive/dini/staging-v<N+1> \
     ghcr.io/leifdenby/nwp-analysis-archive:<tag> \
     -m nwp_analysis_archive.migrate
   ```

   It refuses to write
   to an existing location and checks that the new archive holds the same
   analysis times.
5. Build B from the new A with a one-off `--steps b` pass into a new B prefix,
   point the container at the two new prefixes, restart it, and delete the old
   prefixes once everything is verified.

Cycles from the forecast zarrs that were produced before the creator changed
(still on the source when the new archiver starts) fail the new schema and are
skipped; by then they are normally already in A and migrated with it.

### Re-ingesting corrected or back-filled cycles

Not run automatically. When a creator bug is fixed and old cycles must be
corrected, or a gap must be filled, regenerate the forecast zarrs with the
zarr creator (the GRIB files are on tape, so restore them first) into **any**
location, then:

```bash
python -m nwp_analysis_archive.reingest \
  --src-uri <where the regenerated zarrs are> \
  --archive-a-uri ... --archive-b-uri ... \
  --from-time 2026-10-01T00:00Z --to-time 2026-10-07T21:00Z
```

(options are also `NWP_ARCHIVE_REINGEST_*` variables). The cycles in the range
are checked against the schema, overwritten in A (or appended if A does not
have them; A's time axis is then not sorted, which nothing relies on), and the
affected blocks of B, finished ones included, are rebuilt. The archiver does
not know about tape; restoring and converting is a separate procedure.

### Monitoring

Nothing alerts yet. Watch for `INCOMPATIBLE`, `STALE` and any `ERROR` line in
`docker logs`, at least once within the retention period. The pass report is
logged at INFO after every pass on A.

## Not yet implemented

- Alerting on the ERROR lines above (how the creator is monitored is unknown).
- An explicit `output_schema_version` written by the zarr creator itself,
  which would make the lockstep rule easier to apply than a structural check.
- Pruning A for finished blocks that are in B (undecided, see above).

## Development

```bash
uv sync --group dev
uv run pytest
uv run pre-commit install
```

Code style (also checked by ruff in pre-commit and CI): type annotations on
all arguments and return values, function names that are verb sentences,
numpy-style docstrings, `ds_`/`da_` prefixes for `xr.Dataset`/`xr.DataArray`
variables. pre-commit runs ruff (lint and format); pytest runs in
GitHub Actions together with pre-commit
([.github/workflows](.github/workflows)).
