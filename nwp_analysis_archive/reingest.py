"""Re-ingest corrected or back-filled cycles into archives A and B.

Not run automatically. Use it after regenerating forecast zarrs with the
zarr creator (e.g. after restoring the GRIB files from tape) into an explicit
``--src-uri``: the cycles in the range are overwritten in archive A (or
appended if A does not have them) and the affected blocks of archive B are
rebuilt. The forecasts must match the schema this code targets.

Environment variables use the prefix ``NWP_ARCHIVE_REINGEST``.
"""

import argparse
import datetime
from collections.abc import Mapping, Sequence

from loguru import logger

from . import archive, envargs, schema, source, storage
from .scan import read_cycle_step

ENV_PREFIX = "NWP_ARCHIVE_REINGEST"


def parse_utc_datetime(value: str) -> datetime.datetime:
    """Parse an ISO8601 time, taking naive times as UTC.

    Parameters
    ----------
    value : str
        e.g. ``2026-10-01T00:00Z``.

    Returns
    -------
    datetime.datetime
        Timezone-aware UTC time.
    """
    t = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return t.replace(tzinfo=datetime.UTC) if t.tzinfo is None else t


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser; every option has an env variable.

    Returns
    -------
    argparse.ArgumentParser
        The parser.
    """
    parser = argparse.ArgumentParser(
        prog="nwp_analysis_archive.reingest", description=__doc__
    )
    parser.add_argument(
        "--src-uri", required=True, help="forecast zarrs root to read from"
    )
    parser.add_argument("--archive-a-uri", required=True)
    parser.add_argument("--archive-b-uri", required=True)
    parser.add_argument(
        "--from-time",
        type=parse_utc_datetime,
        required=True,
        help="first analysis time to re-ingest (ISO8601, UTC)",
    )
    parser.add_argument(
        "--to-time",
        type=parse_utc_datetime,
        required=True,
        help="last analysis time to re-ingest (ISO8601, UTC)",
    )
    parser.add_argument(
        "--datasets", nargs="+", default=list(schema.DATASET_IDS)
    )
    parser.add_argument("--interval-hours", type=int, default=3)
    parser.add_argument("--aws-profile", help="AWS profile for s3:// URLs")
    envargs.add_env_help(parser, ENV_PREFIX)
    return parser


def reingest_cycles(
    src_root: str,
    a_root: str,
    b_root: str,
    from_time: datetime.datetime,
    to_time: datetime.datetime,
    interval: datetime.timedelta,
    dataset_ids: tuple[str, ...] = schema.DATASET_IDS,
    schemas: Mapping[str, dict] | None = None,
) -> int:
    """Overwrite or append the cycles in a range, then rebuild archive B.

    Parameters
    ----------
    src_root : str
        Forecast zarrs root to read from.
    a_root : str
        Prefix of archive A.
    b_root : str
        Prefix of archive B.
    from_time : datetime.datetime
        First analysis time (inclusive).
    to_time : datetime.datetime
        Last analysis time (inclusive).
    interval : datetime.timedelta
        Time between analysis times.
    dataset_ids : tuple of str
        Datasets to re-ingest.
    schemas : mapping of str to dict, optional
        Target schema by dataset id; default the packaged ones.

    Returns
    -------
    int
        Number of cycle-datasets written.
    """
    schemas = schema.load_schemas() if schemas is None else schemas
    written = 0
    for dataset_id in dataset_ids:
        url_a = f"{a_root}/{dataset_id}.zarr"
        url_b = f"{b_root}/{dataset_id}.zarr"
        archive.check_archive_schema_version(url_a)
        archive.check_archive_schema_version(url_b)
        replaced = []
        times = [
            t
            for t in source.list_analysis_times(src_root)
            if from_time <= t <= to_time
        ]
        for t in times:
            src_url = f"{src_root}/{source.build_prefix(t)}/{dataset_id}.zarr"
            result = read_cycle_step(src_url, t, schemas[dataset_id])
            if result is None or isinstance(result, list):
                logger.error(f"Not re-ingested: {src_url}: {result}")
                continue
            was_overwritten = archive.replace_in_staging(result, url_a)
            logger.info(
                f"{'Overwrote' if was_overwritten else 'Appended'} "
                f"{t.isoformat()} in {url_a}"
            )
            replaced.append(t)
            written += 1
        if replaced:
            archive.update_final_archive(
                url_a, url_b, interval, rebuild_from=min(replaced)
            )
    return written


def main(argv: Sequence[str] | None = None) -> None:
    """Run the re-ingest.

    Parameters
    ----------
    argv : sequence of str, optional
        Command line arguments; default ``sys.argv[1:]``.
    """
    parser = build_parser()
    args, _ = envargs.parse_args(parser, ENV_PREFIX, argv)
    storage.set_profile(args.aws_profile)
    reingest_cycles(
        args.src_uri,
        args.archive_a_uri,
        args.archive_b_uri,
        args.from_time,
        args.to_time,
        datetime.timedelta(hours=args.interval_hours),
        tuple(args.datasets),
    )


if __name__ == "__main__":
    main()
