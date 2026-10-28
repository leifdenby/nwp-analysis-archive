"""Tests of archives A and B on synthetic local data."""

import datetime
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

from nwp_analysis_archive import archive, blocks, schema, source
from nwp_analysis_archive.scan import (
    check_final_update_due,
    update_final_archives,
    update_staging_archives,
)

UTC = datetime.UTC
DAILY = datetime.timedelta(days=1)  # 28 cycles per block keeps tests small
DATASET = "single_levels"


def build_time(t: datetime.datetime) -> np.datetime64:
    """Build a naive ``datetime64[ns]`` from a timezone-aware time."""
    return np.datetime64(t.replace(tzinfo=None), "ns")


def write_forecast(
    root: Path,
    t: datetime.datetime,
    offset: float = 0.0,
    version: str = "0.8.1.dev1+gtest",
    dataset_id: str = DATASET,
) -> None:
    """Write a 3-step forecast zarr whose value is the hour count + offset."""
    valid = np.array([build_time(t) + np.timedelta64(h, "h") for h in range(3)])
    value = t.timestamp() / 3600 + offset
    ds_forecast = xr.Dataset(
        {
            "t2m": (
                ("time", "y", "x"),
                np.full((3, 4, 5), value, dtype="float64"),
            ),
            "lsm": (("y", "x"), np.ones((4, 5), dtype="float64")),
        },
        coords={"time": valid, "x": np.arange(5), "y": np.arange(4)},
        attrs={
            "zarr_creator_version": version,
            "zarr_creation_time": f"created-{version}",
        },
    )
    path = root / source.build_prefix(t) / f"{dataset_id}.zarr"
    ds_forecast.to_zarr(str(path), mode="w", zarr_format=2)


def write_staging(url_a: str, times: list[datetime.datetime]) -> None:
    """Append one step per time to archive A, with value = hours since epoch."""
    for t in times:
        ds_step = xr.Dataset(
            {
                "t2m": (
                    ("analysis_time", "y", "x"),
                    np.full((1, 4, 5), t.timestamp() / 3600),
                ),
                "lsm": (("y", "x"), np.ones((4, 5))),
            },
            coords={
                "analysis_time": [build_time(t)],
                "x": np.arange(5),
                "zarr_creator_version": ("analysis_time", np.array(["v-test"])),
                "source_zarr_creation_time": (
                    "analysis_time",
                    np.array(["c-test"]),
                ),
            },
        )
        archive.append_to_staging(ds_step, url_a)


def build_test_schemas(root: Path, t: datetime.datetime) -> dict[str, dict]:
    """Build the target schema from the test forecast written at ``t``."""
    url = f"{root}/{source.build_prefix(t)}/{DATASET}.zarr"
    ds_forecast = source.open_forecast(url)
    return {DATASET: schema.describe_dataset_schema(ds_forecast)}


def test_block_index_and_times() -> None:
    """A block index is a floor division from the epoch."""
    t = blocks.EPOCH + datetime.timedelta(days=28, hours=3)
    assert blocks.get_block_index(t) == 1
    three_hourly = datetime.timedelta(hours=3)
    assert len(blocks.get_block_times(1, three_hourly)) == 224


def test_staging_appends_once_and_keeps_static_variable_static(
    tmp_path: Path,
) -> None:
    """Missing cycles are appended; a second pass appends nothing."""
    t0 = blocks.EPOCH + 5 * blocks.BLOCK_LENGTH
    times = [t0 + i * datetime.timedelta(hours=3) for i in range(4)]
    for t in times:
        write_forecast(tmp_path / "src", t)
    src, a_root = f"{tmp_path}/src", f"{tmp_path}/a"
    schemas = build_test_schemas(tmp_path / "src", times[0])
    for _ in range(2):
        update_staging_archives(src, a_root, (DATASET,), schemas=schemas)
    url_a = f"{a_root}/{DATASET}.zarr"
    assert archive.read_archive_times(url_a) == set(times)
    ds_a = xr.open_zarr(url_a)
    assert ds_a.t2m.sizes["analysis_time"] == 4
    assert ds_a.lsm.dims == ("y", "x")
    expected = times[2].timestamp() / 3600
    assert float(ds_a.t2m.isel(analysis_time=2, x=0, y=0)) == expected


def test_final_archive_rewrites_current_block_nightly(tmp_path: Path) -> None:
    """B holds the full block axis, NaN ahead, and fills in over updates."""
    url_a, url_b = f"{tmp_path}/a.zarr", f"{tmp_path}/b.zarr"
    start = blocks.EPOCH
    days = [start + i * DAILY for i in range(40)]

    write_staging(url_a, days[:10])
    assert archive.update_final_archive(url_a, url_b, DAILY) == [0]
    ds_b = xr.open_zarr(url_b)
    assert ds_b.sizes["analysis_time"] == 28
    assert ds_b.t2m.chunks[0] == (28,)
    assert ds_b.lsm.dims == ("y", "x")
    assert archive.read_last_complete(url_b) == days[9]
    values = ds_b.t2m.isel(x=0, y=0).values
    assert not np.isnan(values[:10]).any()
    assert np.isnan(values[10:]).all()

    write_staging(url_a, days[10:30])  # block 0 completes, block 1 starts
    assert archive.update_final_archive(url_a, url_b, DAILY) == [0, 1]
    ds_b = xr.open_zarr(url_b)
    assert ds_b.sizes["analysis_time"] == 56
    values = ds_b.t2m.isel(x=0, y=0).values
    expected = [d.timestamp() / 3600 for d in days[:30]]
    np.testing.assert_allclose(values[:30], expected)
    assert np.isnan(values[30:]).all()
    assert archive.read_last_complete(url_b) == days[29]

    # nothing new in A: rewrites only the current block, same content
    assert archive.update_final_archive(url_a, url_b, DAILY) == [1]
    values_again = xr.open_zarr(url_b).t2m.isel(x=0, y=0).values
    np.testing.assert_array_equal(values, values_again)


def test_archives_use_zstd_bitshuffle_compression(tmp_path: Path) -> None:
    """Data variables of A and B are compressed with zstd + bitshuffle."""
    url_a, url_b = f"{tmp_path}/a.zarr", f"{tmp_path}/b.zarr"
    write_staging(url_a, [blocks.EPOCH + i * DAILY for i in range(3)])
    archive.update_final_archive(url_a, url_b, DAILY)
    for url in (url_a, url_b):
        compressor = xr.open_zarr(url).t2m.encoding["compressors"][0]
        assert compressor.cname == "zstd"
        assert compressor.shuffle == 2  # bitshuffle


def test_final_archive_leaves_nan_for_missing_cycle(tmp_path: Path) -> None:
    """A cycle missing from A is NaN on B's regular time axis."""
    url_a, url_b = f"{tmp_path}/a.zarr", f"{tmp_path}/b.zarr"
    days = [blocks.EPOCH + i * DAILY for i in range(6)]
    write_staging(url_a, [d for i, d in enumerate(days) if i != 3])
    archive.update_final_archive(url_a, url_b, DAILY)
    values = xr.open_zarr(url_b).t2m.isel(x=0, y=0).values
    assert np.isnan(values[3])
    assert not np.isnan(values[[0, 1, 2, 4, 5]]).any()


def test_update_final_archives_skips_missing_staging(tmp_path: Path) -> None:
    """With no archive A, nothing is created in B."""
    update_final_archives(f"{tmp_path}/a", f"{tmp_path}/b", DAILY, (DATASET,))
    assert not (tmp_path / "b").exists()


@pytest.mark.parametrize(
    ("now_hhmm", "last_date", "expected"),
    [
        ("00:10", None, False),
        ("00:30", None, True),
        ("05:00", datetime.date(2026, 10, 28), False),
        ("05:00", datetime.date(2026, 10, 27), True),
    ],
)
def test_check_final_update_due(
    now_hhmm: str, last_date: datetime.date | None, expected: bool
) -> None:
    """B updates once per UTC day, from the configured time."""
    hour, minute = map(int, now_hhmm.split(":"))
    now = datetime.datetime(2026, 10, 28, hour, minute, tzinfo=UTC)
    due = check_final_update_due(now, last_date, datetime.time(0, 30))
    assert due is expected
