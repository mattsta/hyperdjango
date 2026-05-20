"""
HyperDjango management commands framework.

Register custom management commands via the @command decorator:

    from hyperdjango.commands import command

    @command(name="seed", help="Seed the database with test data")
    async def seed_command(count: int = 100, verbose: bool = False):
        if verbose:
            print(f"Seeding {count} records...")

    @command(help="Clear expired sessions")
    async def cleanup():
        ...

Run commands programmatically:

    from hyperdjango.commands import run_command
    exit_code = await run_command("seed", ["--count=50", "--verbose"])

Or discover commands from module paths:

    from hyperdjango.commands import discover_commands
    discover_commands(["myapp.commands", "myapp.tasks"])
"""

import importlib
import inspect
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class ArgDefinition:
    """Definition of a single command argument."""

    name: str
    type: type
    default: object = None
    help: str = ""
    required: bool = False
    is_flag: bool = False


@dataclass
class CommandDefinition:
    """A registered command with its metadata and argument definitions."""

    name: str
    help: str
    func: Callable[..., object]
    args: list[ArgDefinition] = field(default_factory=list)


# Module-level command registry (thread-safe for Python 3.14t free-threading)
_command_registry: dict[str, CommandDefinition] = {}
_registry_lock = threading.Lock()


def command(name: str | None = None, help: str = "") -> Callable[..., object]:
    """Decorator to register a function as a management command.

    Inspects the function signature to build ArgDefinitions from
    type annotations and default values.

    Works on both sync and async functions.

    Usage:
        @command(name="seed", help="Seed the database")
        async def seed_command(count: int = 100, verbose: bool = False):
            ...

        @command(help="Clear sessions")
        def cleanup():
            ...
    """

    def decorator(func: Callable[..., object]) -> Callable[..., object]:
        cmd_name = name if name is not None else func.__name__
        sig = inspect.signature(func)
        args: list[ArgDefinition] = []

        for param_name, param in sig.parameters.items():
            annotation = param.annotation
            if annotation is inspect.Parameter.empty:
                annotation = str

            has_default = param.default is not inspect.Parameter.empty
            default_value = param.default if has_default else None
            is_flag = annotation is bool
            required = not has_default

            args.append(
                ArgDefinition(
                    name=param_name,
                    type=annotation,
                    default=default_value,
                    help="",
                    required=required,
                    is_flag=is_flag,
                )
            )

        cmd = CommandDefinition(
            name=cmd_name,
            help=help,
            func=func,
            args=args,
        )
        with _registry_lock:
            _command_registry[cmd_name] = cmd
        return func

    return decorator


def get_command(name: str) -> CommandDefinition | None:
    """Look up a command by name. Returns None if not found."""
    with _registry_lock:
        return _command_registry.get(name)


def list_commands() -> list[CommandDefinition]:
    """Return all registered commands."""
    with _registry_lock:
        return list(_command_registry.values())


def format_help(cmd: CommandDefinition) -> str:
    """Generate help text for a command."""
    lines: list[str] = []
    lines.append(f"Usage: hyper {cmd.name} [OPTIONS]")
    lines.append("")
    if cmd.help:
        lines.append(f"  {cmd.help}")
        lines.append("")

    if cmd.args:
        lines.append("Options:")
        for arg in cmd.args:
            if arg.is_flag:
                opt = f"  --{arg.name}"
            else:
                opt = f"  --{arg.name} <{arg.type.__name__}>"
            if not arg.required and not arg.is_flag:
                opt += f"  (default: {arg.default!r})"
            elif arg.required:
                opt += "  (required)"
            if arg.help:
                opt += f"  {arg.help}"
            lines.append(opt)

    return "\n".join(lines)


def _parse_args(cmd: CommandDefinition, raw_args: list[str]) -> dict[str, object] | str:
    """Parse raw CLI args against a CommandDefinition.

    Returns a dict of parsed kwargs on success, or an error string on failure.
    """
    kwargs: dict[str, object] = {}
    positional_args: list[ArgDefinition] = []
    positional_values: list[str] = []

    # Collect non-flag args for positional matching
    for arg in cmd.args:
        if not arg.is_flag:
            positional_args.append(arg)

    i = 0
    while i < len(raw_args):
        token = raw_args[i]

        if token.startswith("--"):
            if "=" in token:
                key, value = token[2:].split("=", 1)
            else:
                key = token[2:]
                value = None

            # Find the matching arg definition
            matched_arg: ArgDefinition | None = None
            for arg in cmd.args:
                if arg.name == key:
                    matched_arg = arg
                    break

            if matched_arg is None:
                return f"Unknown argument: --{key}"

            if matched_arg.is_flag:
                kwargs[key] = True
            elif value is not None:
                try:
                    kwargs[key] = matched_arg.type(value)
                except ValueError, TypeError:
                    return f"Invalid value for --{key}: expected {matched_arg.type.__name__}, got {value!r}"
            else:
                # Next token is the value
                i += 1
                if i >= len(raw_args):
                    return f"Missing value for --{key}"
                value = raw_args[i]
                try:
                    kwargs[key] = matched_arg.type(value)
                except ValueError, TypeError:
                    return f"Invalid value for --{key}: expected {matched_arg.type.__name__}, got {value!r}"
        else:
            positional_values.append(token)

        i += 1

    # Assign positional values
    for idx, value in enumerate(positional_values):
        if idx >= len(positional_args):
            return "Too many positional arguments"
        arg = positional_args[idx]
        if arg.name not in kwargs:
            try:
                kwargs[arg.name] = arg.type(value)
            except ValueError, TypeError:
                return f"Invalid value for {arg.name}: expected {arg.type.__name__}, got {value!r}"

    # Fill defaults and check required
    for arg in cmd.args:
        if arg.name not in kwargs:
            if arg.is_flag:
                kwargs[arg.name] = False
            elif arg.required:
                return f"Missing required argument: --{arg.name}"
            else:
                kwargs[arg.name] = arg.default

    return kwargs


async def run_command(name: str, args: list[str]) -> int:
    """Parse args, call the command function, and return an exit code.

    Returns 0 on success, 1 on error.
    """
    cmd = get_command(name)
    if cmd is None:
        sys.stderr.write(f"Unknown command: {name}\n")
        return 1

    # Handle --help
    if "--help" in args:
        sys.stdout.write(format_help(cmd) + "\n")
        return 0

    parsed = _parse_args(cmd, args)
    if isinstance(parsed, str):
        sys.stderr.write(f"Error: {parsed}\n")
        return 1

    try:
        result = cmd.func(**parsed)
        if inspect.isawaitable(result):
            await result
    # blind-except: CLI command boundary — a failing command reports to stderr and returns exit code 1 rather than dumping a raw traceback
    except Exception as exc:
        sys.stderr.write(f"Command {name!r} failed: {exc}\n")
        return 1

    return 0


def discover_commands(module_paths: list[str]) -> list[str]:
    """Import each module path, triggering @command decorators.

    Returns list of command names that were discovered (newly registered).
    """
    with _registry_lock:
        before = set(_command_registry.keys())
    for path in module_paths:
        importlib.import_module(path)
    with _registry_lock:
        after = set(_command_registry.keys())
    return sorted(after - before)
