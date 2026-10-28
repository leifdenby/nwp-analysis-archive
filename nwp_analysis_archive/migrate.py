"""Migrate archive A to the schema version this code targets.

Writes the migrated archive to a **new** location and leaves the old one
untouched (object stores cannot rename, and archive A is the only copy of
old cycles). Afterwards point the container at the new prefix, build archive
B from it with a normal ``--steps b`` pass into a new B prefix, and delete the
old prefixes once everything is verified.

Environment variables use the prefix ``NWP_ARCHIVE_MIGRATE``.
"""

import argparse
from collections.abc import Mapping, Sequence

from loguru import logger

from . import archive, envargs, migrations, schema, storage

ENV_PREFIX = "NWP_ARCHIVE_MIGRATE"


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser; every option has an env variable.

    Returns
    -------
    argparse.ArgumentParser
        The parser.
    """
    parser = argparse.ArgumentParser(
        prog="nwp_analysis_archive.migrate", description=__doc__
    )
    parser.add_argument(
        "--from-a-uri", required=True, help="prefix of the existing archive A"
    )
    parser.add_argument(
        "--to-a-uri",
        required=True,
        help="prefix of the migrated archive A (must not exist)",
    )
    parser.add_argument(
        "--datasets",
        nargs="+",
        default=list(schema.DATASET_IDS),
        help="datasets to migrate",
    )
    parser.add_argument("--aws-profile", help="AWS profile for s3:// URLs")
    envargs.add_env_help(parser, ENV_PREFIX)
    return parser


def migrate_archive(
    url_from: str,
    url_to: str,
    target_version: int = schema.TARGET_SCHEMA_VERSION,
    migration_chain: Mapping[int, migrations.Migration] | None = None,
) -> None:
    """Migrate one archive A dataset to a new location.

    Parameters
    ----------
    url_from : str
        The existing archive's zarr store.
    url_to : str
        Where to write the migrated archive; must not exist.
    target_version : int
        The schema version to migrate to.
    migration_chain : mapping of int to callable, optional
        Migrations by the version they upgrade from; default the registry.

    Raises
    ------
    FileExistsError
        If ``url_to`` exists.
    ValueError
        If the archive has no version or the migration is impossible.
    RuntimeError
        If the migrated archive does not hold the same analysis times.
    """
    if archive.check_exists(url_to):
        raise FileExistsError(f"{url_to} exists; refusing to overwrite")
    ds_old = archive.open_archive(url_from)
    from_version = ds_old.attrs.get(archive.SCHEMA_VERSION_ATTR)
    if from_version is None:
        raise ValueError(f"{url_from} has no {archive.SCHEMA_VERSION_ATTR}")
    ds_new = migrations.apply_migrations(
        ds_old, int(from_version), target_version, migration_chain
    )
    ds_new = archive.rechunk_dataset(ds_new, archive.A_CHUNKS)
    ds_new = ds_new.assign_attrs({archive.SCHEMA_VERSION_ATTR: target_version})
    logger.info(f"Migrating {url_from} (schema {from_version}) to {url_to}")
    ds_new.to_zarr(
        url_to,
        mode="w",
        consolidated=True,
        zarr_format=archive.ZARR_FORMAT,
        storage_options=storage.build_storage_options(url_to) or None,
    )
    if archive.read_archive_times(url_to) != archive.read_archive_times(
        url_from
    ):
        raise RuntimeError(f"{url_to} does not hold the same analysis times")


def main(argv: Sequence[str] | None = None) -> None:
    """Run the migration of every selected dataset.

    Parameters
    ----------
    argv : sequence of str, optional
        Command line arguments; default ``sys.argv[1:]``.
    """
    parser = build_parser()
    args, _ = envargs.parse_args(parser, ENV_PREFIX, argv)
    storage.set_profile(args.aws_profile)
    for dataset_id in args.datasets:
        migrate_archive(
            f"{args.from_a_uri}/{dataset_id}.zarr",
            f"{args.to_a_uri}/{dataset_id}.zarr",
        )


if __name__ == "__main__":
    main()
