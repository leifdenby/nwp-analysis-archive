"""Tests of schema checks, provenance, migration, staleness and re-ingest."""

import datetime
from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from loguru import logger

from nwp_analysis_archive import (
    archive,
    blocks,
    migrations,
    schema,
    shutdown,
    source,
)
from nwp_analysis_archive.migrate import migrate_archive
from nwp_analysis_archive.reingest import reingest_cycles
from nwp_analysis_archive.scan import (
    list_stale_datasets,
    update_staging_archives,
)

from .test_archive import (
    DAILY,
    DATASET,
    build_test_schemas,
    write_forecast,
    write_staging,
)

DAYS = [blocks.EPOCH + i * DAILY for i in range(8)]


def open_test_forecast(root: Path, t: datetime.datetime) -> xr.Dataset:
    """Open a forecast written by ``write_forecast``."""
    url = f"{root}/{source.build_prefix(t)}/{DATASET}.zarr"
    return source.open_forecast(url)


def test_schema_accepts_same_structure_and_describes_differences(
    tmp_path: Path,
) -> None:
    """A rename, a unit change and a new grid size each give a message."""
    write_forecast(tmp_path, DAYS[0])
    ds_forecast = open_test_forecast(tmp_path, DAYS[0])
    target = schema.describe_dataset_schema(ds_forecast)
    assert schema.check_schema_matches(ds_forecast, target) == []

    ds_renamed = ds_forecast.rename({"t2m": "t2m_new"})
    differences = schema.check_schema_matches(ds_renamed, target)
    assert "variable 't2m' is missing" in differences
    assert "variable 't2m_new' is not in the schema" in differences

    ds_units = ds_forecast.copy()
    ds_units["t2m"].attrs["units"] = "degC"
    assert any(
        "units" in d for d in schema.check_schema_matches(ds_units, target)
    )

    ds_cropped = ds_forecast.isel(x=slice(0, 3))
    assert any(
        d.startswith("dimension 'x'")
        for d in schema.check_schema_matches(ds_cropped, target)
    )


def test_archives_carry_schema_version_and_refuse_other_versions(
    tmp_path: Path,
) -> None:
    """New archives get the version; a different or missing one is refused."""
    url_a, url_b = f"{tmp_path}/a.zarr", f"{tmp_path}/b.zarr"
    write_staging(url_a, DAYS[:3])
    archive.update_final_archive(url_a, url_b, DAILY)
    for url in (url_a, url_b):
        assert (
            archive.open_archive(url).attrs[archive.SCHEMA_VERSION_ATTR]
            == schema.TARGET_SCHEMA_VERSION
        )
        archive.check_archive_schema_version(url)
        with pytest.raises(schema.SchemaVersionError, match="migrate"):
            archive.check_archive_schema_version(url, expected=2)
    archive.check_archive_schema_version(f"{tmp_path}/missing.zarr")

    # an archive written before versioning has no attribute
    url_old = f"{tmp_path}/old.zarr"
    xr.Dataset({"v": ("analysis_time", [1.0])}).to_zarr(url_old)
    with pytest.raises(schema.SchemaVersionError):
        archive.check_archive_schema_version(url_old)


def test_scan_skips_incompatible_and_unready_cycles(tmp_path: Path) -> None:
    """Only matching, finished cycles are archived; the others are reported."""
    src_root = tmp_path / "src"
    write_forecast(src_root, DAYS[0])
    schemas = build_test_schemas(src_root, DAYS[0])
    write_forecast(src_root, DAYS[1])
    # DAYS[2]: incompatible (variable renamed), DAYS[3]: leftover directory
    write_forecast(src_root, DAYS[2])
    url = f"{src_root}/{source.build_prefix(DAYS[2])}/{DATASET}.zarr"
    ds_bad = xr.open_zarr(url).rename({"t2m": "t2m_new"}).load()
    ds_bad.to_zarr(url, mode="w", zarr_format=2)
    (src_root / source.build_prefix(DAYS[3])).mkdir()

    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(str(m)), level="ERROR")
    try:
        report = update_staging_archives(
            str(src_root), f"{tmp_path}/a", (DATASET,), schemas=schemas
        )
    finally:
        logger.remove(sink)

    assert (report.appended, report.incompatible, report.not_ready) == (2, 1, 1)
    assert report.failed == 0
    assert any("INCOMPATIBLE" in m and "t2m_new" in m for m in messages)
    present = archive.read_archive_times(f"{tmp_path}/a/{DATASET}.zarr")
    assert present == {DAYS[0], DAYS[1]}


def test_provenance_is_recorded_per_cycle_and_reaches_b(
    tmp_path: Path,
) -> None:
    """Creator versions of different lengths are kept; gaps in B are empty."""
    src_root = tmp_path / "src"
    versions = ["0.8.1", "0.9.0.dev12+gabcdef0.d20261028", "1.0"]
    for t, version in zip(DAYS[:3], versions, strict=True):
        write_forecast(src_root, t, version=version)
    schemas = build_test_schemas(src_root, DAYS[0])
    a_root = f"{tmp_path}/a"
    update_staging_archives(str(src_root), a_root, (DATASET,), schemas=schemas)

    url_a, url_b = f"{a_root}/{DATASET}.zarr", f"{tmp_path}/b/{DATASET}.zarr"
    ds_a = xr.open_zarr(url_a)
    assert list(ds_a.zarr_creator_version.values) == versions
    assert list(ds_a.source_zarr_creation_time.values) == [
        f"created-{v}" for v in versions
    ]

    archive.update_final_archive(url_a, url_b, DAILY)
    ds_b = xr.open_zarr(url_b)
    assert list(ds_b.zarr_creator_version.values[:3]) == versions
    assert set(ds_b.zarr_creator_version.values[3:]) == {""}


def test_stale_datasets_are_listed(tmp_path: Path) -> None:
    """A dataset is stale when empty or when its newest cycle is too old."""
    a_root = f"{tmp_path}/a"
    write_staging(f"{a_root}/{DATASET}.zarr", DAYS[:2])
    limit = datetime.timedelta(hours=9)
    fresh = DAYS[1] + datetime.timedelta(hours=1)
    assert list_stale_datasets(a_root, fresh, limit, (DATASET,)) == []
    old = DAYS[1] + datetime.timedelta(hours=10)
    stale = list_stale_datasets(a_root, old, limit, (DATASET,))
    assert len(stale) == 1
    assert "single_levels" in stale[0]
    empty = list_stale_datasets(f"{tmp_path}/none", old, limit, (DATASET,))
    assert "holds no cycles" in empty[0]


def rename_t2m_and_add_extra(ds_old: xr.Dataset) -> xr.Dataset:
    """Migrate schema 1 to 2: rename ``t2m`` and add a NaN variable."""
    ds_new = ds_old.rename({"t2m": "t2m_k"})
    ds_new["extra"] = xr.full_like(ds_new["t2m_k"], np.nan)
    return ds_new


def test_migrate_writes_a_new_archive_and_leaves_the_old_one(
    tmp_path: Path,
) -> None:
    """The chain is applied to a new location; the old archive is untouched."""
    url_from, url_to = f"{tmp_path}/v1.zarr", f"{tmp_path}/v2.zarr"
    write_staging(url_from, DAYS[:4])
    chain = {1: rename_t2m_and_add_extra}
    migrate_archive(url_from, url_to, target_version=2, migration_chain=chain)

    ds_new = xr.open_zarr(url_to)
    assert ds_new.attrs[archive.SCHEMA_VERSION_ATTR] == 2
    assert {"t2m_k", "extra", "lsm"} <= set(ds_new.data_vars)
    assert "t2m" not in ds_new
    assert ds_new.t2m_k.sizes["analysis_time"] == 4
    assert bool(ds_new.extra.isnull().all())
    ds_old = xr.open_zarr(url_from)
    assert ds_old.attrs[archive.SCHEMA_VERSION_ATTR] == 1
    assert "t2m" in ds_old
    with pytest.raises(schema.SchemaVersionError):
        archive.check_archive_schema_version(url_from, expected=2)
    with pytest.raises(FileExistsError):
        migrate_archive(url_from, url_to, 2, chain)


def test_apply_migrations_rejects_downgrades_and_missing_steps() -> None:
    """Only a complete upward chain of migrations is applied."""
    ds_empty = xr.Dataset()
    with pytest.raises(ValueError, match="downgrade"):
        migrations.apply_migrations(ds_empty, 2, 1, {})
    with pytest.raises(ValueError, match="no migration from schema 1"):
        migrations.apply_migrations(ds_empty, 1, 2, {})
    same = migrations.apply_migrations(ds_empty, 1, 1, {})
    assert same is ds_empty


def test_reingest_overwrites_and_appends_then_rebuilds_b(
    tmp_path: Path,
) -> None:
    """A corrected cycle replaces the old values; a back-filled one is added."""
    url_a, url_b = (
        f"{tmp_path}/a/{DATASET}.zarr",
        f"{tmp_path}/b/{DATASET}.zarr",
    )
    write_staging(url_a, DAYS[1:4])
    archive.update_final_archive(url_a, url_b, DAILY)

    src_root = tmp_path / "regenerated"
    write_forecast(src_root, DAYS[2], offset=1000.0)  # corrected cycle
    write_forecast(src_root, DAYS[0])  # missing from A, older than the rest
    schemas = build_test_schemas(src_root, DAYS[0])
    written = reingest_cycles(
        str(src_root),
        f"{tmp_path}/a",
        f"{tmp_path}/b",
        DAYS[0],
        DAYS[2],
        DAILY,
        (DATASET,),
        schemas,
    )
    assert written == 2

    hours = [t.timestamp() / 3600 for t in DAYS[:4]]
    ds_a = xr.open_zarr(url_a)
    assert ds_a.sizes["analysis_time"] == 4  # appended out of order
    assert archive.read_archive_times(url_a) == set(DAYS[:4])
    ds_b = xr.open_zarr(url_b)
    values = ds_b.t2m.isel(x=0, y=0).values
    np.testing.assert_allclose(
        values[:4], [hours[0], hours[1], hours[2] + 1000.0, hours[3]]
    )
    assert np.isnan(values[4:]).all()
    assert list(ds_b.zarr_creator_version.values[:4]) == [
        "0.8.1.dev1+gtest",
        "v-test",
        "0.8.1.dev1+gtest",
        "v-test",
    ]


def test_datasets_are_archived_smallest_first() -> None:
    """Single levels first, then height levels, then pressure levels."""
    assert schema.DATASET_IDS == (
        "single_levels",
        "height_levels",
        "pressure_levels",
    )


def test_stop_request_ends_the_pass_between_cycles(tmp_path: Path) -> None:
    """After a stop request, no further cycle is appended."""
    src_root = tmp_path / "src"
    for t in DAYS[:3]:
        write_forecast(src_root, t)
    schemas = build_test_schemas(src_root, DAYS[0])
    shutdown.request_stop()
    try:
        report = update_staging_archives(
            str(src_root), f"{tmp_path}/a", (DATASET,), schemas=schemas
        )
    finally:
        shutdown.reset_stop()
    assert report.appended == 0
    assert not (tmp_path / "a").exists()
    report = update_staging_archives(
        str(src_root), f"{tmp_path}/a", (DATASET,), schemas=schemas
    )
    assert report.appended == 3
