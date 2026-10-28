# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

A release that changes the schema the archive targets (see the README,
"Versioning, migrations and re-ingest") says so here, together with the
migration to run.

## [Unreleased]

## [v0.1.2]

### Changed

- The version is derived from the git tag (hatch-vcs) and exposed as
  `nwp_analysis_archive.__version__`; it is logged at startup. The container
  image gets it through the `VERSION` build argument.
  [\#1](https://github.com/leifdenby/nwp-analysis-archive/pull/1),
  @leifdenby

## [v0.1.1]

### Fixed

- Logs did not show in `docker logs` because stdout was block-buffered when
  not attached to a terminal. The container now runs with `PYTHONUNBUFFERED=1`
  and the application logs to stderr.
  [290299e](https://github.com/leifdenby/nwp-analysis-archive/commit/290299e)
  (pushed to `main` before PRs were required), @leifdenby
  [290299e](https://github.com/leifdenby/nwp-analysis-archive/commit/290299e)
  (pushed to `main` before PRs were required), @leifdenby

## [v0.1.0]

First release. Archive schema version: 1 (the output of the zarr creator
`0.8.1.dev1+g656ebde`, as of 2026-10-28).

### Added

- Archive A: per-dataset zarr (`pressure_levels`, `height_levels`,
  `single_levels`) with the analysis time step of every DINI forecast, one
  chunk per analysis time, appended by a periodic scan of the source.
- Archive B: the same data in 28-day blocks with 64 x 64 spatial chunks for
  point time series. The current block is allocated whole (NaN for cycles to
  come) and overwritten in place by a daily update; the group attribute
  `last_complete_analysis_time` is set last.
- zstd + bitshuffle compression (lossless).
- Configuration of every option by command line or `NWP_ARCHIVE_*`
  environment variables (`envargs.py`, adapted from the ANNA inference
  container).
- Schema versioning: structural schema per dataset (`schemas/v1.json`),
  `archive_schema_version` attribute, per-cycle `zarr_creator_version` and
  `source_zarr_creation_time` coordinates, skipping of incompatible or
  unfinished source cycles, `STALE` errors (`--max-staleness-hours`).
- `python -m nwp_analysis_archive.migrate` and
  `python -m nwp_analysis_archive.reingest`.
- Container image on GHCR (`ghcr.io/leifdenby/nwp-analysis-archive`), and
  pytest and pre-commit (ruff) workflows.
  [c00837d](https://github.com/leifdenby/nwp-analysis-archive/commit/c00837d)
  (pushed to `main` before PRs were required), @leifdenby

[Unreleased]: https://github.com/leifdenby/nwp-analysis-archive/compare/v0.1.2...HEAD
[v0.1.2]: https://github.com/leifdenby/nwp-analysis-archive/compare/v0.1.1...v0.1.2
[v0.1.1]: https://github.com/leifdenby/nwp-analysis-archive/compare/v0.1.0...v0.1.1
[v0.1.0]: https://github.com/leifdenby/nwp-analysis-archive/releases/tag/v0.1.0
