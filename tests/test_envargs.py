"""Tests of the environment variable configuration."""

import datetime

import pytest

from nwp_analysis_archive import envargs
from nwp_analysis_archive.__main__ import ENV_PREFIX, build_parser

REQUIRED = {
    "NWP_ARCHIVE_SRC_URI": "/src",
    "NWP_ARCHIVE_ARCHIVE_A_URI": "/a",
    "NWP_ARCHIVE_ARCHIVE_B_URI": "/b",
}


def test_options_come_from_environment() -> None:
    """Required options, lists and flags can all be set by variables."""
    environ = {
        **REQUIRED,
        "NWP_ARCHIVE_DATASETS": "single_levels height_levels",
        "NWP_ARCHIVE_WATCH": "true",
        "NWP_ARCHIVE_FINAL_UPDATE_TIME": "01:15",
    }
    args, sources = envargs.parse_args(build_parser(), ENV_PREFIX, [], environ)
    assert args.src_uri == "/src"
    assert args.datasets == ["single_levels", "height_levels"]
    assert args.watch is True
    assert args.final_update_time == datetime.time(1, 15)
    assert sources["src_uri"] == "env"
    assert sources["interval_hours"] == "default"


def test_command_line_wins_over_environment() -> None:
    """A command line option overrides its variable."""
    args, sources = envargs.parse_args(
        build_parser(),
        ENV_PREFIX,
        ["--interval-hours", "6"],
        {**REQUIRED, "NWP_ARCHIVE_INTERVAL_HOURS": "3"},
    )
    assert args.interval_hours == 6
    assert sources["interval_hours"] == "cli"


def test_misspelled_variable_is_an_error() -> None:
    """An unknown NWP_ARCHIVE_* variable exits with a suggestion."""
    environ = {**REQUIRED, "NWP_ARCHIVE_INTERVAL_HOUR": "3"}
    with pytest.raises(SystemExit) as excinfo:
        envargs.parse_args(build_parser(), ENV_PREFIX, [], environ)
    assert excinfo.value.code == 2


def test_empty_variable_counts_as_unset() -> None:
    """Empty variables (e.g. docker ``-e NAME=``) use the default."""
    args, _ = envargs.parse_args(
        build_parser(),
        ENV_PREFIX,
        [],
        {**REQUIRED, "NWP_ARCHIVE_INTERVAL_HOURS": ""},
    )
    assert args.interval_hours == 3
