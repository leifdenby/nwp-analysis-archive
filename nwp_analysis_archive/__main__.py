"""Command line entrypoint: ``python -m nwp_analysis_archive``."""

import argparse
import datetime
import sys
import time
from collections.abc import Sequence

from loguru import logger

from . import __version__, envargs, storage
from .scan import (
    DATASET_IDS,
    check_final_update_due,
    list_stale_datasets,
    update_final_archives,
    update_staging_archives,
)

ENV_PREFIX = "NWP_ARCHIVE"


def parse_time_of_day(value: str) -> datetime.time:
    """Parse ``HH:MM`` into a time of day.

    Parameters
    ----------
    value : str
        e.g. ``00:30``.

    Returns
    -------
    datetime.time
        The time of day.
    """
    return datetime.datetime.strptime(value, "%H:%M").time()


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser; every option has an env variable.

    Returns
    -------
    argparse.ArgumentParser
        The parser, with ``[env: NWP_ARCHIVE_...]`` added to each help.
    """
    parser = argparse.ArgumentParser(
        prog="nwp_analysis_archive",
        description=(
            "Archive the analysis time step of DINI forecast zarrs: append "
            "to archive A, and refresh the long-time-chunk archive B."
        ),
    )
    parser.add_argument(
        "--src-uri",
        required=True,
        help="forecast zarrs root, e.g. s3://harmonie-zarr/dini/control",
    )
    parser.add_argument(
        "--archive-a-uri", required=True, help="prefix of archive A (staging)"
    )
    parser.add_argument(
        "--archive-b-uri",
        required=True,
        help="prefix of archive B (28-day time chunks)",
    )
    parser.add_argument(
        "--datasets",
        nargs="+",
        default=list(DATASET_IDS),
        help="datasets to archive",
    )
    parser.add_argument(
        "--interval-hours",
        type=int,
        default=3,
        help="hours between forecast analysis times (divides 24)",
    )
    parser.add_argument(
        "--steps",
        nargs="+",
        choices=("a", "b"),
        default=["a", "b"],
        help="archives to update in a single pass (without --watch)",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="keep running: update A every --poll-minutes, B daily",
    )
    parser.add_argument(
        "--poll-minutes",
        type=float,
        default=15,
        help="minutes between updates of A with --watch",
    )
    parser.add_argument(
        "--final-update-time",
        type=parse_time_of_day,
        default=parse_time_of_day("00:30"),
        help="UTC time of day (HH:MM) from which B is updated with --watch",
    )
    parser.add_argument(
        "--max-staleness-hours",
        type=float,
        default=9,
        help=(
            "log an ERROR when the newest archived cycle of a dataset is "
            "older than this"
        ),
    )
    parser.add_argument(
        "--aws-profile",
        help="AWS profile for s3:// URLs (not needed for mounted paths)",
    )
    parser.add_argument("--log-level", default="INFO", help="loguru level")
    parser.add_argument(
        "--max-cycles", type=int, help="only the newest N cycles (testing)"
    )
    parser.add_argument(
        "--variables", nargs="+", help="only these variables (testing)"
    )
    parser.add_argument(
        "--crop-size", type=int, help="first N points in x and y (testing)"
    )
    envargs.add_env_help(parser, ENV_PREFIX)
    return parser


def run_pass(args: argparse.Namespace, steps: Sequence[str]) -> int:
    """Run one update pass.

    Parameters
    ----------
    args : argparse.Namespace
        The parsed options.
    steps : sequence of str
        ``a`` and/or ``b``.

    Returns
    -------
    int
        Number of problems: cycles skipped or failed, and stale datasets.
        Each is logged at ERROR level.
    """
    datasets = tuple(args.datasets)
    problems = 0
    if "a" in steps:
        report = update_staging_archives(
            args.src_uri,
            args.archive_a_uri,
            datasets,
            args.max_cycles,
            args.variables,
            args.crop_size,
        )
        logger.info(f"Archive A pass: {report}")
        problems += report.incompatible + report.failed
        stale = list_stale_datasets(
            args.archive_a_uri,
            datetime.datetime.now(datetime.UTC),
            datetime.timedelta(hours=args.max_staleness_hours),
            datasets,
        )
        for message in stale:
            logger.error(f"STALE {message}")
        problems += len(stale)
    if "b" in steps:
        update_final_archives(
            args.archive_a_uri,
            args.archive_b_uri,
            datetime.timedelta(hours=args.interval_hours),
            datasets,
        )
    return problems


def run_watch(args: argparse.Namespace) -> None:
    """Run update passes forever.

    Archive A is updated every ``--poll-minutes``; archive B once per UTC
    day, after ``--final-update-time`` and after that pass on A.

    Parameters
    ----------
    args : argparse.Namespace
        The parsed options.
    """
    last_b_date = None
    while True:
        now = datetime.datetime.now(datetime.UTC)
        steps = ["a"]
        if check_final_update_due(now, last_b_date, args.final_update_time):
            steps.append("b")
        try:
            run_pass(args, steps)
            if "b" in steps:
                last_b_date = now.date()
        except Exception:
            logger.exception("Update pass failed, retrying next poll")
        time.sleep(args.poll_minutes * 60)


def main(argv: Sequence[str] | None = None) -> None:
    """Run the entrypoint.

    Parameters
    ----------
    argv : sequence of str, optional
        Command line arguments; default ``sys.argv[1:]``.
    """
    parser = build_parser()
    args, sources = envargs.parse_args(parser, ENV_PREFIX, argv)
    storage.set_profile(args.aws_profile)
    logger.remove()
    logger.add(sys.stderr, level=args.log_level.upper())
    logger.info(f"nwp_analysis_archive {__version__}")
    for line in envargs.build_config_lines(parser, args, sources, ENV_PREFIX):
        logger.info(line)
    if args.watch:
        run_watch(args)
    elif run_pass(args, args.steps):
        sys.exit(1)


if __name__ == "__main__":
    main()
