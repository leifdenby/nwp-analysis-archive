"""Reading the analysis time step from the forecast zarrs."""

import datetime

import fsspec
import numpy as np
import xarray as xr

from . import storage

PREFIX_FORMAT = "%Y-%m-%dT%H%M%SZ"
# forecast attribute -> per-cycle coordinate in the archive
PROVENANCE_ATTRS = {
    "zarr_creator_version": "zarr_creator_version",
    "source_zarr_creation_time": "zarr_creation_time",
}
STRING_DTYPE = "<U64"


def list_analysis_times(src_root: str) -> list[datetime.datetime]:
    """List the analysis times with a forecast directory under a root.

    Parameters
    ----------
    src_root : str
        e.g. ``s3://harmonie-zarr/dini/control``; subdirectories are named
        like ``2025-03-03T060000Z``.

    Returns
    -------
    list of datetime.datetime
        Timezone-aware UTC times, ascending.
    """
    fs, path = fsspec.core.url_to_fs(
        src_root, **storage.build_storage_options(src_root)
    )
    times = []
    for entry in fs.ls(path, detail=False):
        name = entry.rstrip("/").rsplit("/", 1)[-1]
        try:
            t = datetime.datetime.strptime(name, PREFIX_FORMAT)
        except ValueError:
            continue
        times.append(t.replace(tzinfo=datetime.UTC))
    return sorted(times)


def build_prefix(t_analysis: datetime.datetime) -> str:
    """Build the directory name of an analysis time.

    Parameters
    ----------
    t_analysis : datetime.datetime
        The analysis time.

    Returns
    -------
    str
        e.g. ``2025-03-03T060000Z``.
    """
    return t_analysis.strftime(PREFIX_FORMAT)


def check_forecast_ready(url: str) -> bool:
    """Check whether a forecast zarr has been completely written.

    The zarr creator consolidates the metadata last, so a directory without
    ``.zmetadata`` is missing, leftover or still being written.

    Parameters
    ----------
    url : str
        The forecast zarr.

    Returns
    -------
    bool
        True if the consolidated metadata exists.
    """
    fs, path = fsspec.core.url_to_fs(url, **storage.build_storage_options(url))
    return fs.exists(f"{path}/.zmetadata")


def open_forecast(url: str) -> xr.Dataset:
    """Open a forecast zarr lazily.

    Parameters
    ----------
    url : str
        The forecast zarr.

    Returns
    -------
    xarray.Dataset
        The forecast, with all lead times.
    """
    return xr.open_zarr(
        url,
        consolidated=True,
        storage_options=storage.build_storage_options(url) or None,
    )


def build_analysis_step(
    ds_forecast: xr.Dataset,
    t_analysis: datetime.datetime,
    variables: list[str] | None = None,
    crop_size: int | None = None,
) -> xr.Dataset:
    """Build the lead-time-zero step of a forecast for the archive.

    Only variables with a ``time`` dimension get an ``analysis_time``
    dimension; static variables (e.g. ``lsm``) are kept as they are. The
    creator version and creation time of the forecast are recorded per cycle
    as the string coordinates ``zarr_creator_version`` and
    ``source_zarr_creation_time``. ``variables`` and ``crop_size`` only exist
    to make small test runs.

    Parameters
    ----------
    ds_forecast : xarray.Dataset
        The forecast, from :func:`open_forecast`.
    t_analysis : datetime.datetime
        The forecast's analysis time (timezone-aware).
    variables : list of str, optional
        Keep only these variables.
    crop_size : int, optional
        Keep only the first N points in x and y.

    Returns
    -------
    xarray.Dataset
        Lazily loaded, with an ``analysis_time`` dimension of length 1 on
        the time-dependent variables.
    """
    attrs = ds_forecast.attrs
    if variables:
        ds_forecast = ds_forecast[[v for v in variables if v in ds_forecast]]
    if crop_size:
        ds_forecast = ds_forecast.isel(
            x=slice(0, crop_size), y=slice(0, crop_size)
        )
    time_dependent = [
        v for v in ds_forecast.data_vars if "time" in ds_forecast[v].dims
    ]
    t = np.datetime64(
        t_analysis.astimezone(datetime.UTC).replace(tzinfo=None), "ns"
    )
    if "time" in ds_forecast.dims:
        ds_dynamic = ds_forecast[time_dependent].isel(time=0, drop=True)
        ds_dynamic = ds_dynamic.expand_dims(analysis_time=[t])
        ds_static = ds_forecast.drop_vars(
            time_dependent + ["time"], errors="ignore"
        )
        ds_step = xr.merge([ds_dynamic, ds_static], compat="override")
    else:
        ds_step = ds_forecast.expand_dims(analysis_time=[t])
    return ds_step.assign_coords(
        {
            name: (
                "analysis_time",
                np.array([str(attrs.get(key, ""))], dtype=STRING_DTYPE),
            )
            for name, key in PROVENANCE_ATTRS.items()
        }
    )
