"""One update pass: append new cycles to archive A, refresh archive B."""

import datetime
import time
from collections.abc import Mapping
from dataclasses import dataclass

import xarray as xr
from loguru import logger

from . import archive, schema, shutdown, source

DATASET_IDS = schema.DATASET_IDS


@dataclass
class StagingReport:
    """Outcome of one pass over the source forecasts.

    Attributes
    ----------
    appended : int
        Cycle-datasets appended to archive A.
    incompatible : int
        Cycle-datasets skipped because their structure does not match the
        schema this code targets. Their source is deleted after about a
        week, so each one needs an archiver release with a migration.
    not_ready : int
        Cycle-datasets skipped because the source zarr is missing or not
        finished yet.
    failed : int
        Cycle-datasets that could not be read or written.
    """

    appended: int = 0
    incompatible: int = 0
    not_ready: int = 0
    failed: int = 0


def read_cycle_step(
    src_url: str,
    t_analysis: datetime.datetime,
    schema_dataset: dict,
    variables: list[str] | None = None,
    crop_size: int | None = None,
) -> xr.Dataset | list[str] | None:
    """Read and check one cycle of one dataset from the source.

    Parameters
    ----------
    src_url : str
        The forecast zarr.
    t_analysis : datetime.datetime
        The forecast's analysis time.
    schema_dataset : dict
        The target schema of this dataset.
    variables : list of str, optional
        Keep only these variables (for testing).
    crop_size : int, optional
        Keep only the first N points in x and y (for testing).

    Returns
    -------
    xarray.Dataset or list of str or None
        The analysis step; the list of schema differences if the forecast
        does not match the schema; None if the forecast is not ready.
    """
    if not source.check_forecast_ready(src_url):
        return None
    ds_forecast = source.open_forecast(src_url)
    differences = schema.check_schema_matches(ds_forecast, schema_dataset)
    if differences:
        return differences
    return source.build_analysis_step(
        ds_forecast, t_analysis, variables, crop_size
    )


def update_staging_archives(
    src_root: str,
    a_root: str,
    dataset_ids: tuple[str, ...] = DATASET_IDS,
    max_cycles: int | None = None,
    variables: list[str] | None = None,
    crop_size: int | None = None,
    schemas: Mapping[str, dict] | None = None,
) -> StagingReport:
    """Append the analysis times missing from archive A, oldest first.

    Cycles whose structure does not match the target schema are skipped and
    logged at ERROR level; they are tried again on every pass, so they are
    appended once this code targets their schema (before the source is
    deleted).

    Parameters
    ----------
    src_root : str
        Forecast zarrs root, e.g. ``s3://harmonie-zarr/dini/control``.
    a_root : str
        Prefix of archive A; each dataset is ``{a_root}/{dataset_id}.zarr``.
    dataset_ids : tuple of str
        Datasets to archive.
    max_cycles : int, optional
        Only consider the newest N forecasts (for testing).
    variables : list of str, optional
        Only archive these variables (for testing).
    crop_size : int, optional
        Only archive the first N points in x and y (for testing).
    schemas : mapping of str to dict, optional
        Target schema by dataset id; default the packaged ones.

    Returns
    -------
    StagingReport
        Counts of what happened.

    Raises
    ------
    SchemaVersionError
        If an existing archive A has another schema version.
    """
    schemas = schema.load_schemas() if schemas is None else schemas
    report = StagingReport()
    available = source.list_analysis_times(src_root)
    if max_cycles:
        available = available[-max_cycles:]
    for dataset_id in dataset_ids:
        url_a = f"{a_root}/{dataset_id}.zarr"
        archive.check_archive_schema_version(url_a)
        present = archive.read_archive_times(url_a)
        for t in (t for t in available if t not in present):
            if shutdown.check_stop_requested():
                logger.info("Stop requested, leaving archive A as it is")
                return report
            src_url = f"{src_root}/{source.build_prefix(t)}/{dataset_id}.zarr"
            try:
                result = read_cycle_step(
                    src_url, t, schemas[dataset_id], variables, crop_size
                )
                if result is None:
                    logger.debug(f"{src_url} is not ready")
                    report.not_ready += 1
                    continue
                if isinstance(result, list):
                    logger.error(
                        f"INCOMPATIBLE {src_url} does not match schema "
                        f"{schema.TARGET_SCHEMA_VERSION}, not archived: "
                        + "; ".join(result)
                    )
                    report.incompatible += 1
                    continue
                logger.info(f"Appending {t.isoformat()} to {url_a}")
                started = time.monotonic()
                archive.append_to_staging(result, url_a)
                report.appended += 1
                logger.info(
                    f"Appended {t.isoformat()} to {url_a} in "
                    f"{time.monotonic() - started:.0f} s"
                )
            except Exception:
                logger.exception(f"Failed on {src_url}, retrying next pass")
                report.failed += 1
    return report


def list_stale_datasets(
    a_root: str,
    now: datetime.datetime,
    max_staleness: datetime.timedelta,
    dataset_ids: tuple[str, ...] = DATASET_IDS,
) -> list[str]:
    """List the datasets whose newest archived cycle is too old.

    Parameters
    ----------
    a_root : str
        Prefix of archive A.
    now : datetime.datetime
        Current time, timezone-aware UTC.
    max_staleness : datetime.timedelta
        Largest acceptable age of the newest analysis time.
    dataset_ids : tuple of str
        Datasets to check.

    Returns
    -------
    list of str
        One message per stale (or empty) dataset.
    """
    stale = []
    for dataset_id in dataset_ids:
        times = archive.read_archive_times(f"{a_root}/{dataset_id}.zarr")
        if not times:
            stale.append(f"{dataset_id}: archive A holds no cycles")
        elif now - max(times) > max_staleness:
            stale.append(
                f"{dataset_id}: newest archived cycle is "
                f"{max(times).isoformat()}, {now - max(times)} old"
            )
    return stale


def update_final_archives(
    a_root: str,
    b_root: str,
    interval: datetime.timedelta,
    dataset_ids: tuple[str, ...] = DATASET_IDS,
) -> None:
    """Bring archive B up to date with archive A for each dataset.

    Parameters
    ----------
    a_root : str
        Prefix of archive A.
    b_root : str
        Prefix of archive B; each dataset is ``{b_root}/{dataset_id}.zarr``.
    interval : datetime.timedelta
        Time between analysis times.
    dataset_ids : tuple of str
        Datasets to update.

    Raises
    ------
    SchemaVersionError
        If an existing archive has another schema version.
    """
    for dataset_id in dataset_ids:
        if shutdown.check_stop_requested():
            logger.info("Stop requested, leaving archive B as it is")
            return
        url_a = f"{a_root}/{dataset_id}.zarr"
        url_b = f"{b_root}/{dataset_id}.zarr"
        archive.check_archive_schema_version(url_a)
        archive.check_archive_schema_version(url_b)
        written = archive.update_final_archive(url_a, url_b, interval)
        if written:
            logger.info(f"Wrote blocks {written} of {url_b}")


def check_final_update_due(
    now: datetime.datetime,
    last_update_date: datetime.date | None,
    update_time: datetime.time,
) -> bool:
    """Check whether archive B should be updated now.

    Parameters
    ----------
    now : datetime.datetime
        Current time, timezone-aware UTC.
    last_update_date : datetime.date or None
        UTC date of the last update, if any.
    update_time : datetime.time
        Time of day (UTC) from which the daily update is due.

    Returns
    -------
    bool
        True once per UTC day, from ``update_time``.
    """
    return now.time() >= update_time and last_update_date != now.date()
