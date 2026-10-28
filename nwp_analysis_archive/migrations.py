"""Migrations of archive A from one schema version to the next.

Every zarr creator release that changes its output ships an archiver release
that raises ``TARGET_SCHEMA_VERSION`` (see ``schema.py``), adds the new schema
file and registers a migration here, deployed before or together with that
creator release. Archive B is derived from A and is rebuilt, not migrated.
"""

from collections.abc import Callable, Mapping

import xarray as xr

Migration = Callable[[xr.Dataset], xr.Dataset]

# key n: upgrade a lazy archive A dataset from schema n to schema n + 1
MIGRATIONS: dict[int, Migration] = {}


def apply_migrations(
    ds_old: xr.Dataset,
    from_version: int,
    to_version: int,
    migrations: Mapping[int, Migration] | None = None,
) -> xr.Dataset:
    """Apply the chain of migrations from one schema version to another.

    Parameters
    ----------
    ds_old : xarray.Dataset
        Archive A at ``from_version``.
    from_version : int
        The archive's schema version.
    to_version : int
        The schema version to reach.
    migrations : mapping of int to callable, optional
        Migrations by the version they upgrade from; default
        :data:`MIGRATIONS`.

    Returns
    -------
    xarray.Dataset
        The migrated dataset.

    Raises
    ------
    ValueError
        If the versions are not an upgrade or a step has no migration.
    """
    migrations = MIGRATIONS if migrations is None else migrations
    if from_version > to_version:
        raise ValueError(
            f"cannot downgrade the archive from {from_version} to {to_version}"
        )
    ds_new = ds_old
    for version in range(from_version, to_version):
        if version not in migrations:
            raise ValueError(
                f"no migration from schema {version} to {version + 1}"
            )
        ds_new = migrations[version](ds_new)
    return ds_new
