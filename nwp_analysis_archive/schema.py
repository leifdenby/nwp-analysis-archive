"""Structural schema of the zarr creator's output, and checks against it.

The archive always matches the *current* output of the zarr creator. What
counts as "the same output" is decided from the structure of a forecast zarr
(variables, dimensions, dtypes, units, grid size, projection), not from the
creator's version string, which is derived from ``git describe`` at build
time and does not reliably identify an output format. The version is only
recorded as provenance.

``schemas/v{N}.json`` holds the schema of version ``N`` for each dataset. It
is generated from a real forecast with ``python -m
nwp_analysis_archive.schema``.
"""

import argparse
import json
from importlib import resources
from pathlib import Path

import xarray as xr

from . import source, storage

TARGET_SCHEMA_VERSION = 1
# the order they are archived in: smallest and most used first
DATASET_IDS = ("single_levels", "height_levels", "pressure_levels")
# the forecast length (time) differs between runs and does not matter
IGNORED_DIMS = ("time",)


class SchemaVersionError(Exception):
    """An archive's schema version differs from the one this code targets."""


def describe_dataset_schema(ds_source: xr.Dataset) -> dict:
    """Describe the structure of a forecast dataset.

    Parameters
    ----------
    ds_source : xarray.Dataset
        A forecast zarr from the zarr creator.

    Returns
    -------
    dict
        JSON-serialisable: ``data_vars`` (dims, dtype, units, standard_name),
        ``coords`` (dims, dtype), ``sizes`` of every dimension except
        ``time`` and the ``crs_wkt`` of each projection variable.
    """
    data_vars = {
        name: {
            "dims": list(da_var.dims),
            "dtype": str(da_var.dtype),
            "units": da_var.attrs.get("units"),
            "standard_name": da_var.attrs.get("standard_name"),
        }
        for name, da_var in ds_source.data_vars.items()
    }
    coords = {
        name: {"dims": list(da_coord.dims), "dtype": str(da_coord.dtype)}
        for name, da_coord in ds_source.coords.items()
    }
    sizes = {
        dim: int(n)
        for dim, n in ds_source.sizes.items()
        if dim not in IGNORED_DIMS
    }
    projections = {
        name: da_var.attrs["crs_wkt"]
        for name, da_var in ds_source.variables.items()
        if "crs_wkt" in da_var.attrs
    }
    return {
        "data_vars": data_vars,
        "coords": coords,
        "sizes": sizes,
        "projections": projections,
    }


def list_mapping_differences(
    label: str, expected: dict, actual: dict
) -> list[str]:
    """List the differences between two mappings of name to description.

    Parameters
    ----------
    label : str
        What the names are (e.g. ``variable``), for the messages.
    expected : dict
        The schema's mapping.
    actual : dict
        The dataset's mapping.

    Returns
    -------
    list of str
        One message per missing, extra or differing entry.
    """
    differences = []
    for name in sorted(expected.keys() - actual.keys()):
        differences.append(f"{label} {name!r} is missing")
    for name in sorted(actual.keys() - expected.keys()):
        differences.append(f"{label} {name!r} is not in the schema")
    for name in sorted(expected.keys() & actual.keys()):
        if expected[name] == actual[name]:
            continue
        if isinstance(expected[name], dict):
            for key in expected[name]:
                if expected[name][key] != actual[name].get(key):
                    differences.append(
                        f"{label} {name!r} {key}: expected "
                        f"{expected[name][key]!r}, got "
                        f"{actual[name].get(key)!r}"
                    )
        else:
            differences.append(
                f"{label} {name!r}: expected {expected[name]!r}, got "
                f"{actual[name]!r}"
            )
    return differences


def check_schema_matches(ds_source: xr.Dataset, schema: dict) -> list[str]:
    """Check a forecast dataset against a schema.

    Parameters
    ----------
    ds_source : xarray.Dataset
        A forecast zarr from the zarr creator.
    schema : dict
        One dataset's schema, as returned by :func:`describe_dataset_schema`.

    Returns
    -------
    list of str
        Human-readable differences; empty if the dataset matches.
    """
    actual = describe_dataset_schema(ds_source)
    differences = []
    for key, label in (
        ("data_vars", "variable"),
        ("coords", "coordinate"),
        ("sizes", "dimension"),
        ("projections", "projection"),
    ):
        differences += list_mapping_differences(label, schema[key], actual[key])
    return differences


def load_schemas(version: int = TARGET_SCHEMA_VERSION) -> dict[str, dict]:
    """Load the packaged schemas of a version.

    Parameters
    ----------
    version : int
        Schema version.

    Returns
    -------
    dict of str to dict
        Each dataset's schema by dataset id.
    """
    text = (
        resources.files("nwp_analysis_archive")
        .joinpath(f"schemas/v{version}.json")
        .read_text()
    )
    return json.loads(text)


def build_schemas_from_source(
    src_root: str, dataset_ids: tuple[str, ...] = DATASET_IDS
) -> dict[str, dict]:
    """Describe the newest forecast that has all the datasets.

    Parameters
    ----------
    src_root : str
        Forecast zarrs root, e.g. ``s3://harmonie-zarr/dini/control``.
    dataset_ids : tuple of str
        Datasets to describe.

    Returns
    -------
    dict of str to dict
        Each dataset's schema by dataset id.

    Raises
    ------
    RuntimeError
        If no forecast has all the datasets.
    """
    for t in reversed(source.list_analysis_times(src_root)):
        urls = {
            d: f"{src_root}/{source.build_prefix(t)}/{d}.zarr"
            for d in dataset_ids
        }
        if all(source.check_forecast_ready(u) for u in urls.values()):
            return {
                d: describe_dataset_schema(source.open_forecast(u))
                for d, u in urls.items()
            }
    raise RuntimeError(f"no complete forecast under {src_root}")


def main() -> None:
    """Write a schema file from a real forecast (``--out``)."""
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--src-root", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--aws-profile")
    args = parser.parse_args()
    storage.set_profile(args.aws_profile)
    schemas = build_schemas_from_source(args.src_root)
    args.out.write_text(json.dumps(schemas, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
