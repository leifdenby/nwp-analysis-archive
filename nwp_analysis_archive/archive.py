"""Archive A (one chunk per analysis time) and archive B (long time chunks).

Archive A is appended to as forecasts arrive. Archive B is laid out in
28-day blocks, each one time chunk. A block is allocated whole, with its
full regular time axis, when its first analysis time arrives; cycles that
have not happened yet (or never arrive) are NaN. Each update rewrites the
chunks of the current block in place, so the array shape only changes when
a new block starts, and ``last_complete_analysis_time`` in the group
attributes is updated last.
"""

import datetime

import fsspec
import numcodecs
import numpy as np
import pandas as pd
import xarray as xr
import zarr

from . import blocks, schema, source, storage

ZARR_FORMAT = 2  # same as the forecast zarrs written by zarr_creator
A_CHUNKS = {"analysis_time": 1}
B_SPATIAL_CHUNKS = {"x": 64, "y": 64}
LAST_COMPLETE_ATTR = "last_complete_analysis_time"
SCHEMA_VERSION_ATTR = "archive_schema_version"
# lossless; ~16% smaller than lz4 + shuffle on a DINI single-level sample
COMPRESSOR = numcodecs.Blosc(
    cname="zstd", clevel=5, shuffle=numcodecs.Blosc.BITSHUFFLE
)
TIME_ENCODING = {"units": "seconds since 1970-01-01", "dtype": "int64"}


def build_final_chunks(interval: datetime.timedelta) -> dict[str, int]:
    """Build the chunk sizes of archive B.

    Parameters
    ----------
    interval : datetime.timedelta
        Time between analysis times.

    Returns
    -------
    dict of str to int
        One block along ``analysis_time``, 64 points in x and y.
    """
    return {
        "analysis_time": blocks.get_cycles_per_block(interval),
        **B_SPATIAL_CHUNKS,
    }


def check_exists(url: str) -> bool:
    """Check whether a zarr store exists.

    Parameters
    ----------
    url : str
        Local path, ``file://`` or ``s3://`` URL.

    Returns
    -------
    bool
        True if it exists.
    """
    fs, path = fsspec.core.url_to_fs(url, **storage.build_storage_options(url))
    return fs.exists(path)


def open_archive(url: str) -> xr.Dataset:
    """Open an archive lazily.

    Parameters
    ----------
    url : str
        The archive's zarr store.

    Returns
    -------
    xarray.Dataset
        The archive, with consolidated metadata.
    """
    return xr.open_zarr(
        url,
        consolidated=True,
        storage_options=storage.build_storage_options(url) or None,
    )


def read_archive_times(url: str) -> set[datetime.datetime]:
    """Read the analysis times in archive A.

    Parameters
    ----------
    url : str
        The archive's zarr store.

    Returns
    -------
    set of datetime.datetime
        Timezone-aware UTC times; empty if the archive does not exist.
    """
    if not check_exists(url):
        return set()
    ds_archive = open_archive(url)
    return {
        datetime.datetime.fromisoformat(str(t)[:19]).replace(
            tzinfo=datetime.UTC
        )
        for t in ds_archive.analysis_time.values.astype("datetime64[s]")
    }


def read_last_complete(url: str) -> datetime.datetime | None:
    """Read the latest analysis time written to archive B.

    Parameters
    ----------
    url : str
        Archive B's zarr store.

    Returns
    -------
    datetime.datetime or None
        Timezone-aware UTC time, or None if the archive or the attribute is
        missing (e.g. a first write that crashed before recording it).
    """
    if not check_exists(url):
        return None
    value = open_archive(url).attrs.get(LAST_COMPLETE_ATTR)
    return None if value is None else datetime.datetime.fromisoformat(value)


def check_archive_schema_version(
    url: str, expected: int = schema.TARGET_SCHEMA_VERSION
) -> None:
    """Check that an existing archive has the schema version this code targets.

    Parameters
    ----------
    url : str
        The archive's zarr store; nothing is checked if it does not exist.
    expected : int
        The schema version the archive must have.

    Raises
    ------
    SchemaVersionError
        If the archive has another version, or none (written before
        versioning), with a pointer to the migration.
    """
    if not check_exists(url):
        return
    found = open_archive(url).attrs.get(SCHEMA_VERSION_ATTR)
    if found != expected:
        raise schema.SchemaVersionError(
            f"{url} has schema version {found}, this code targets "
            f"{expected}: migrate it with python -m "
            "nwp_analysis_archive.migrate"
        )


def rechunk_dataset(
    ds_chunked: xr.Dataset, chunks: dict[str, int]
) -> xr.Dataset:
    """Chunk a dataset for writing and reset its encodings.

    Parameters
    ----------
    ds_chunked : xarray.Dataset
        The dataset (not changed).
    chunks : dict of str to int
        Chunk size by dimension; dimensions the dataset lacks are ignored.

    Returns
    -------
    xarray.Dataset
        Chunked, with zstd + bitshuffle compression on the data variables and
        a fixed integer time encoding (otherwise xarray picks units from the
        first value and later writes would be mis-encoded).
    """
    ds_chunked = ds_chunked.chunk(
        {d: c for d, c in chunks.items() if d in ds_chunked.dims}
    )
    for v in list(ds_chunked.data_vars) + list(ds_chunked.coords):
        ds_chunked[v].encoding = {}
    for v in ds_chunked.data_vars:
        ds_chunked[v].encoding = {"compressors": (COMPRESSOR,)}
    ds_chunked["analysis_time"].encoding = dict(TIME_ENCODING)
    for name in source.PROVENANCE_ATTRS:
        if name in ds_chunked.coords:
            # fixed width, so later appends of longer strings still fit
            ds_chunked = ds_chunked.assign_coords(
                {name: ds_chunked[name].astype(source.STRING_DTYPE)}
            )
    return ds_chunked


def append_to_staging(ds_step: xr.Dataset, url: str) -> None:
    """Create archive A from ``ds_step``, or append it along ``analysis_time``.

    Static variables (without ``analysis_time``) are written once, with the
    first step.

    Parameters
    ----------
    ds_step : xarray.Dataset
        One or more analysis times.
    url : str
        Archive A's zarr store.
    """
    ds_step = rechunk_dataset(ds_step, A_CHUNKS)
    options = storage.build_storage_options(url) or None
    if not check_exists(url):
        ds_step = ds_step.assign_attrs(
            {SCHEMA_VERSION_ATTR: schema.TARGET_SCHEMA_VERSION}
        )
        ds_step.to_zarr(
            url,
            mode="w",
            consolidated=True,
            zarr_format=ZARR_FORMAT,
            storage_options=options,
        )
        return
    time_dependent = [
        v for v in ds_step.data_vars if "analysis_time" in ds_step[v].dims
    ]
    # appending rewrites the group attributes from the appended dataset
    ds_step = ds_step.assign_attrs(
        {SCHEMA_VERSION_ATTR: schema.TARGET_SCHEMA_VERSION}
    )
    ds_step[time_dependent].to_zarr(
        url,
        mode="a",
        append_dim="analysis_time",
        consolidated=True,
        zarr_format=ZARR_FORMAT,
        storage_options=options,
    )


def replace_in_staging(ds_step: xr.Dataset, url: str) -> bool:
    """Overwrite a cycle in archive A, or append it if A does not have it.

    Parameters
    ----------
    ds_step : xarray.Dataset
        One analysis time, from :func:`source.build_analysis_step`.
    url : str
        Archive A's zarr store.

    Returns
    -------
    bool
        True if an existing cycle was overwritten, False if it was appended.
    """
    t = ds_step.analysis_time.values[0]
    times = open_archive(url).analysis_time.values if check_exists(url) else []
    matches = np.flatnonzero(times == t)
    if len(matches) == 0:
        append_to_staging(ds_step, url)
        return False
    position = int(matches[0])
    ds_step = rechunk_dataset(ds_step, A_CHUNKS)
    ds_step = ds_step.drop_vars(
        [v for v in ds_step.variables if "analysis_time" not in ds_step[v].dims]
    )
    ds_step.to_zarr(
        url,
        mode="r+",
        region={"analysis_time": slice(position, position + 1)},
        storage_options=storage.build_storage_options(url) or None,
    )
    return True


def build_block_dataset(
    ds_a: xr.Dataset, block_index: int, interval: datetime.timedelta
) -> xr.Dataset:
    """Build one block of archive B from archive A.

    Parameters
    ----------
    ds_a : xarray.Dataset
        Archive A.
    block_index : int
        The block to build.
    interval : datetime.timedelta
        Time between analysis times.

    Returns
    -------
    xarray.Dataset
        The block on its full regular time axis, NaN where A has no cycle,
        chunked as archive B.
    """
    grid = blocks.build_block_grid(block_index, interval)
    present = np.isin(ds_a.analysis_time.values, grid)
    ds_present = ds_a.isel(analysis_time=present)
    # reindexing fills string coordinates with NaN; use an empty string
    strings = {
        name: ds_present[name].values
        for name in source.PROVENANCE_ATTRS
        if name in ds_present.coords
    }
    positions = pd.Index(ds_present.analysis_time.values).get_indexer(grid)
    ds_block = ds_present.drop_vars(list(strings)).reindex(analysis_time=grid)
    ds_block = ds_block.assign_coords(
        {
            name: (
                "analysis_time",
                np.where(positions >= 0, values[positions.clip(0)], "").astype(
                    source.STRING_DTYPE
                ),
            )
            for name, values in strings.items()
        }
    )
    return rechunk_dataset(ds_block, build_final_chunks(interval))


def set_last_complete(url: str, t_analysis: datetime.datetime) -> None:
    """Record the latest analysis time written to archive B.

    Parameters
    ----------
    url : str
        Archive B's zarr store.
    t_analysis : datetime.datetime
        Timezone-aware UTC time.
    """
    group = zarr.open_group(
        url,
        mode="r+",
        zarr_format=ZARR_FORMAT,
        storage_options=storage.build_storage_options(url) or None,
    )
    group.attrs[LAST_COMPLETE_ATTR] = t_analysis.isoformat()
    zarr.consolidate_metadata(group.store, zarr_format=ZARR_FORMAT)


def read_final_layout(
    url: str, interval: datetime.timedelta
) -> tuple[int, int] | None:
    """Read which blocks archive B holds.

    Parameters
    ----------
    url : str
        Archive B's zarr store.
    interval : datetime.timedelta
        Time between analysis times.

    Returns
    -------
    tuple of (int, int) or None
        Index of the first block and the number of blocks; None if the
        archive does not exist.
    """
    if not check_exists(url):
        return None
    times = open_archive(url).analysis_time
    first = datetime.datetime.fromisoformat(
        str(times.values[0].astype("datetime64[s]"))
    ).replace(tzinfo=datetime.UTC)
    return (
        blocks.get_block_index(first),
        len(times) // blocks.get_cycles_per_block(interval),
    )


def write_block(
    ds_block: xr.Dataset,
    url: str,
    position: int | None,
    first_write: bool,
) -> None:
    """Write one block to archive B.

    Parameters
    ----------
    ds_block : xarray.Dataset
        The block, from :func:`build_block_dataset`.
    url : str
        Archive B's zarr store.
    position : int or None
        Index along ``analysis_time`` where an existing block starts, to
        overwrite it in place; None to append a new block.
    first_write : bool
        Whether this creates the archive (static variables are written then).
    """
    options = storage.build_storage_options(url) or None
    if first_write:
        ds_block = ds_block.assign_attrs(
            {SCHEMA_VERSION_ATTR: schema.TARGET_SCHEMA_VERSION}
        )
        ds_block.to_zarr(
            url,
            mode="w",
            consolidated=True,
            zarr_format=ZARR_FORMAT,
            storage_options=options,
        )
        return
    # static variables were written with the first block
    ds_block = ds_block.drop_vars(
        [
            v
            for v in ds_block.variables
            if "analysis_time" not in ds_block[v].dims
        ]
    )
    if position is None:
        ds_block = ds_block.assign_attrs(
            {SCHEMA_VERSION_ATTR: schema.TARGET_SCHEMA_VERSION}
        )
        ds_block.to_zarr(
            url,
            mode="a",
            append_dim="analysis_time",
            consolidated=True,
            zarr_format=ZARR_FORMAT,
            storage_options=options,
        )
        return
    length = ds_block.sizes["analysis_time"]
    ds_block.to_zarr(
        url,
        mode="r+",
        region={"analysis_time": slice(position, position + length)},
        storage_options=options,
    )


def update_final_archive(
    url_a: str,
    url_b: str,
    interval: datetime.timedelta,
    rebuild_from: datetime.datetime | None = None,
) -> list[int]:
    """Bring archive B up to date with archive A.

    Rewrites the block holding B's latest analysis time, and any later
    blocks A has, as whole blocks. Finished blocks are not touched again, so
    a cycle that arrives in A after its block was passed is not picked up.

    Parameters
    ----------
    url_a : str
        Archive A's zarr store.
    url_b : str
        Archive B's zarr store; created if missing.
    interval : datetime.timedelta
        Time between analysis times.
    rebuild_from : datetime.datetime, optional
        Also rewrite the finished blocks from the one holding this time
        (after cycles in A were replaced or back-filled).

    Returns
    -------
    list of int
        The block indexes written.
    """
    times_a = read_archive_times(url_a)
    if not times_a:
        return []
    latest = max(times_a)
    ds_a = open_archive(url_a)
    layout = read_final_layout(url_b, interval)
    last_b = read_last_complete(url_b)
    first_block = blocks.get_block_index(min(times_a))
    if layout is not None and last_b is not None:
        first_block = max(first_block, blocks.get_block_index(last_b))
        if rebuild_from is not None:
            first_block = max(
                layout[0],
                min(first_block, blocks.get_block_index(rebuild_from)),
            )
    written = []
    for i in range(first_block, blocks.get_block_index(latest) + 1):
        ds_block = build_block_dataset(ds_a, i, interval)
        if layout is None:
            write_block(ds_block, url_b, None, first_write=True)
            layout = (i, 1)
        elif i < layout[0] + layout[1]:
            position = (i - layout[0]) * ds_block.sizes["analysis_time"]
            write_block(ds_block, url_b, position, first_write=False)
        else:
            write_block(ds_block, url_b, None, first_write=False)
            layout = (layout[0], layout[1] + 1)
        written.append(i)
    set_last_complete(url_b, latest)
    return written
