"""Set the options of a command line interface from environment variables.

Adapted from ``anna_inference.envargs`` in ``mlwp-inference-container-anna``.
Every option ``--some-option`` can also be set with the variable
``{PREFIX}_SOME_OPTION``. An option given on the command line wins over its
variable, which wins over the option's default.

- The variables are turned into command line arguments placed before the real
  ones, so argparse checks their types and choices, and a required option can
  come from a variable alone.
- A flag (``store_true``) takes ``true``/``false`` (also ``1``/``0``,
  ``yes``/``no``), and an option taking several values a space-separated list.
- A set variable with the prefix that matches no option is an error (a
  misspelling would otherwise have no effect), with a suggestion of close
  names. Variables without the prefix are ignored.
- An empty variable counts as unset.
- Only long options are supported, no positional arguments or subcommands.

This module only uses the standard library.
"""

import argparse
import copy
import difflib
import os
import shlex
import sys
from collections.abc import Mapping, Sequence

TRUE_VALUES = ("true", "1", "yes")
FALSE_VALUES = ("false", "0", "no")

SOURCE_CLI, SOURCE_ENV, SOURCE_DEFAULT = "cli", "env", "default"


def build_env_var_name(prefix: str, option: str) -> str:
    """Build the environment variable name of an option.

    Parameters
    ----------
    prefix : str
        Variable prefix, e.g. ``NWP_ARCHIVE``.
    option : str
        Long option, e.g. ``--src-uri``.

    Returns
    -------
    str
        e.g. ``NWP_ARCHIVE_SRC_URI``.
    """
    return f"{prefix}_{option.lstrip('-').upper().replace('-', '_')}"


def list_options(
    parser: argparse.ArgumentParser,
) -> list[tuple[argparse.Action, str]]:
    """List the options of a parser, except ``--help``.

    Parameters
    ----------
    parser : argparse.ArgumentParser
        The parser to inspect.

    Returns
    -------
    list of (argparse.Action, str)
        Each action with its first long option.

    Raises
    ------
    ValueError
        If the parser has an option without a long form (e.g. a positional
        argument).
    """
    options = []
    for action in parser._actions:
        if isinstance(action, argparse._HelpAction | argparse._VersionAction):
            continue
        long_options = [o for o in action.option_strings if o.startswith("--")]
        if not long_options:
            raise ValueError(
                f"envargs only supports long options, not {action.dest!r} "
                "(a positional argument, subcommand or short-only option)"
            )
        options.append((action, long_options[0]))
    return options


def list_env_var_names(
    parser: argparse.ArgumentParser, prefix: str
) -> dict[str, str]:
    """List the environment variable name of each option, by ``dest``.

    Parameters
    ----------
    parser : argparse.ArgumentParser
        The parser to inspect.
    prefix : str
        Variable prefix.

    Returns
    -------
    dict of str to str
        Variable name by the option's ``dest``.
    """
    return {
        action.dest: build_env_var_name(prefix, option)
        for action, option in list_options(parser)
    }


def add_env_help(parser: argparse.ArgumentParser, prefix: str) -> None:
    """Add ``[env: {PREFIX}_OPTION]`` to the help of each option.

    Parameters
    ----------
    parser : argparse.ArgumentParser
        The parser to change in place.
    prefix : str
        Variable prefix.
    """
    for action, option in list_options(parser):
        if action.help == argparse.SUPPRESS:
            continue
        env_help = f"[env: {build_env_var_name(prefix, option)}]"
        action.help = f"{action.help} {env_help}" if action.help else env_help
    convention = (
        f"Every option --some-option can also be set with the environment "
        f"variable {prefix}_SOME_OPTION; an option on the command line wins "
        f"over its variable."
    )
    parser.epilog = (
        f"{parser.epilog}\n\n{convention}" if parser.epilog else convention
    )


def parse_bool(parser: argparse.ArgumentParser, name: str, value: str) -> bool:
    """Parse a boolean variable value, exiting with a usage error if invalid.

    Parameters
    ----------
    parser : argparse.ArgumentParser
        Used to report the error.
    name : str
        Variable name, for the error message.
    value : str
        The value, e.g. ``true`` or ``0``.

    Returns
    -------
    bool
        The parsed value.
    """
    if value.lower() in TRUE_VALUES:
        return True
    if value.lower() in FALSE_VALUES:
        return False
    parser.error(
        f"{name}={value!r}: expected one of "
        f"{', '.join(TRUE_VALUES + FALSE_VALUES)}"
    )
    raise AssertionError("unreachable: parser.error exits")


def check_value(
    parser: argparse.ArgumentParser,
    action: argparse.Action,
    name: str,
    value: str,
) -> None:
    """Check a variable value as argparse would, naming the variable.

    Parameters
    ----------
    parser : argparse.ArgumentParser
        Used to report the error.
    action : argparse.Action
        The option the variable sets.
    name : str
        Variable name, for the error message.
    value : str
        The value to check.
    """
    if callable(action.type):
        try:
            converted = action.type(value)
        except (TypeError, ValueError, argparse.ArgumentTypeError) as ex:
            parser.error(f"{name}={value!r}: invalid value ({ex})")
    else:
        converted = value
    if action.choices is not None and converted not in action.choices:
        choices = ", ".join(map(repr, action.choices))
        parser.error(
            f"{name}={value!r}: invalid choice (choose from {choices})"
        )


def build_env_tokens(
    parser: argparse.ArgumentParser,
    action: argparse.Action,
    option: str,
    name: str,
    value: str,
) -> list[str]:
    """Build the command line arguments for ``name=value``.

    Parameters
    ----------
    parser : argparse.ArgumentParser
        Used to report errors.
    action : argparse.Action
        The option the variable sets.
    option : str
        The option's long form.
    name : str
        Variable name.
    value : str
        The variable's value.

    Returns
    -------
    list of str
        Arguments equivalent to giving the option on the command line.
    """
    if isinstance(action, argparse.BooleanOptionalAction):
        if parse_bool(parser, name, value):
            return [option]
        return [f"--no-{option[2:]}"]
    if action.nargs == 0:
        # store_true, store_false, store_const, count: given or not
        return [option] if parse_bool(parser, name, value) else []
    if action.nargs in (None, "?"):
        check_value(parser, action, name, value)
        # `--option=value`, so a value starting with "-" isn't an option
        return [f"{option}={value}"]
    values = shlex.split(value)
    for v in values:
        check_value(parser, action, name, v)
    return [option, *values]


def build_env_argv(
    parser: argparse.ArgumentParser,
    prefix: str,
    environ: Mapping[str, str] | None = None,
) -> tuple[list[str], set[str]]:
    """Build command line arguments from the ``{prefix}_*`` variables.

    Parameters
    ----------
    parser : argparse.ArgumentParser
        The parser whose options the variables set.
    prefix : str
        Variable prefix.
    environ : mapping of str to str, optional
        The environment; default ``os.environ``.

    Returns
    -------
    argv : list of str
        The arguments.
    from_env : set of str
        The ``dest`` of each option set by a variable.
    """
    environ = os.environ if environ is None else environ
    names = list_env_var_names(parser, prefix)
    actions = {
        action.dest: (action, option) for action, option in list_options(parser)
    }

    unknown = sorted(
        n
        for n in environ
        if n.startswith(f"{prefix}_") and n not in names.values()
    )
    if unknown:
        problems = []
        for n in unknown:
            close = difflib.get_close_matches(n, names.values(), n=1)
            hint = f" (did you mean {close[0]}?)" if close else ""
            problems.append(f"{n}{hint}")
        parser.error(
            f"unknown environment variable(s) {', '.join(problems)}: "
            f"{prefix}_* variables must match an option (see --help)"
        )

    argv: list[str] = []
    from_env: set[str] = set()
    for dest, name in names.items():
        # an empty variable counts as unset (e.g. `-e NAME=` in docker)
        if environ.get(name, "") != "":
            action, option = actions[dest]
            argv += build_env_tokens(
                parser, action, option, name, environ[name]
            )
            from_env.add(dest)
    return argv, from_env


def list_cli_dests(
    parser: argparse.ArgumentParser, argv: Sequence[str]
) -> set[str]:
    """List the ``dest`` of each option given in ``argv`` alone.

    Parameters
    ----------
    parser : argparse.ArgumentParser
        The parser (not changed).
    argv : sequence of str
        The command line arguments.

    Returns
    -------
    set of str
        The options given.
    """
    cli_parser = copy.deepcopy(parser)
    for action in cli_parser._actions:
        action.default = argparse.SUPPRESS
        action.required = False
    return set(vars(cli_parser.parse_args(list(argv))))


def parse_args(
    parser: argparse.ArgumentParser,
    prefix: str,
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[argparse.Namespace, dict[str, str]]:
    """Parse the command line and the ``{prefix}_*`` variables.

    Parameters
    ----------
    parser : argparse.ArgumentParser
        The parser.
    prefix : str
        Variable prefix.
    argv : sequence of str, optional
        Command line arguments; default ``sys.argv[1:]``.
    environ : mapping of str to str, optional
        The environment; default ``os.environ``.

    Returns
    -------
    args : argparse.Namespace
        The parsed options.
    sources : dict of str to str
        Where each option's value came from, by ``dest``: ``cli``, ``env`` or
        ``default``.
    """
    argv = sys.argv[1:] if argv is None else list(argv)
    from_env_argv, from_env = build_env_argv(parser, prefix, environ)
    args = parser.parse_args(from_env_argv + argv)

    from_cli = list_cli_dests(parser, argv)
    sources = {}
    for dest in vars(args):
        if dest in from_cli:
            sources[dest] = SOURCE_CLI
        elif dest in from_env:
            sources[dest] = SOURCE_ENV
        else:
            sources[dest] = SOURCE_DEFAULT
    return args, sources


def build_config_lines(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    sources: Mapping[str, str],
    prefix: str,
) -> list[str]:
    """Build one line per option with its variable name, value and source.

    Do not use it for options holding secrets.

    Parameters
    ----------
    parser : argparse.ArgumentParser
        The parser.
    args : argparse.Namespace
        The parsed options.
    sources : mapping of str to str
        Where each value came from, from :func:`parse_args`.
    prefix : str
        Variable prefix.

    Returns
    -------
    list of str
        The lines, for logging.
    """
    names = list_env_var_names(parser, prefix)
    width = max(map(len, names.values()), default=0)
    return [
        f"{name:<{width}} = {getattr(args, dest)} ({sources[dest]})"
        for dest, name in names.items()
    ]
