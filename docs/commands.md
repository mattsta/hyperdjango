# Custom Management Commands

Register and run custom CLI commands with the `@command` decorator. Replaces
Django's management command system.

---

## Overview

Define async or sync functions as management commands with automatic argument
parsing from type annotations. Commands are registered in a global registry
and can be invoked via the `hyper` CLI or programmatically.

```python
from hyperdjango.commands import command, run_command, discover_commands
```

---

## Defining Commands

Use the `@command` decorator on any function. Arguments are inferred from the
function signature's type annotations and default values.

```python
from hyperdjango.commands import command


@command(name="seed", help="Seed the database with test data")
async def seed_command(count: int = 100, verbose: bool = False):
    if verbose:
        print(f"Seeding {count} records...")
    # ... seed logic ...
    print(f"Created {count} records.")


@command(help="Clear expired sessions")
async def cleanup():
    # ... cleanup logic ...
    print("Done.")
```

### Decorator Parameters

| Parameter | Type          | Default       | Description                 |
| --------- | ------------- | ------------- | --------------------------- |
| `name`    | `str \| None` | function name | Command name for CLI        |
| `help`    | `str`         | `""`          | Help text shown in `--help` |

If `name` is not provided, the function name is used as the command name.

### Sync and Async

Both sync and async functions are supported. Async functions are awaited
automatically when invoked via `run_command()`.

```python
@command(name="sync-task", help="A synchronous command")
def my_sync_command(value: str = "default"):
    print(f"Value: {value}")
```

---

## Argument Types

Arguments are parsed from the function signature. Supported types:

| Type    | CLI Syntax                   | Example                                |
| ------- | ---------------------------- | -------------------------------------- |
| `int`   | `--count 50` or `--count=50` | `async def cmd(count: int = 10)`       |
| `str`   | `--name Alice`               | `async def cmd(name: str = "")`        |
| `float` | `--rate 0.5`                 | `async def cmd(rate: float = 1.0)`     |
| `bool`  | `--verbose` (flag)           | `async def cmd(verbose: bool = False)` |

### Boolean Flags

`bool` arguments are treated as flags. They default to `False` and are set to
`True` when the flag is present:

```python
@command(name="migrate", help="Run migrations")
async def migrate(dry_run: bool = False, verbose: bool = False):
    if dry_run:
        print("Dry run mode")
```

```bash
hyper migrate --dry-run --verbose
```

### Required Arguments

Arguments without a default value are required:

```python
@command(name="greet", help="Greet a user")
async def greet(name: str):
    print(f"Hello, {name}!")
```

```bash
hyper greet --name Alice    # works
hyper greet                 # Error: Missing required argument: --name
```

### Positional Arguments

Non-flag arguments can also be passed positionally:

```bash
hyper greet Alice           # same as --name Alice
```

### Default Values

```python
@command(name="fetch", help="Fetch data")
async def fetch(url: str, timeout: int = 30, retries: int = 3): ...
```

```bash
hyper fetch --url https://example.com              # timeout=30, retries=3
hyper fetch --url https://example.com --timeout 60  # timeout=60, retries=3
```

---

## Running Commands

### Via CLI

Commands are available through the `hyper` CLI:

```bash
hyper seed --count 50 --verbose
hyper cleanup
hyper greet --name Alice
```

Built-in `--help` support:

```bash
hyper seed --help
# Usage: hyper seed [OPTIONS]
#
#   Seed the database with test data
#
# Options:
#   --count <int>  (default: 100)
#   --verbose
```

### Programmatically

```python
from hyperdjango.commands import run_command

exit_code = await run_command("seed", ["--count=50", "--verbose"])
# exit_code: 0 on success, 1 on error
```

---

## Command Registry

### Thread-Safe Registry

The command registry is protected by a `threading.Lock` for safe use under Python
3.14t free-threading (GIL disabled). Multiple threads can register and discover
commands concurrently without data races:

- `@command` decorator registration acquires the lock before writing to the registry.
- `list_commands()`, `get_command()`, and `discover_commands()` read from the
  registry safely.

This is relevant for applications that register commands from multiple threads
at startup, or that use `discover_commands()` concurrently with command execution.

### Listing commands

```python
from hyperdjango.commands import list_commands

for cmd in list_commands():
    print(f"{cmd.name}: {cmd.help}")
```

### Looking up a command

```python
from hyperdjango.commands import get_command

cmd = get_command("seed")
if cmd is not None:
    print(cmd.name, cmd.help)
    for arg in cmd.args:
        print(f"  --{arg.name} ({arg.type.__name__})")
```

### Help text generation

```python
from hyperdjango.commands import format_help, get_command

cmd = get_command("seed")
print(format_help(cmd))
```

---

## Command Discovery

Import modules to trigger `@command` decorator registration:

```python
from hyperdjango.commands import discover_commands

new_commands = discover_commands(["myapp.commands", "myapp.tasks"])
print(new_commands)  # ["seed", "cleanup"]
```

This imports each module path with `importlib.import_module()`, which causes
any `@command`-decorated functions in those modules to be registered.

Returns a sorted list of newly discovered command names.

---

## Data Structures

### ArgDefinition

```python
from hyperdjango.commands import ArgDefinition

arg = ArgDefinition(
    name="count",
    type=int,
    default=100,
    help="Number of records to create",
    required=False,
    is_flag=False,
)
```

### CommandDefinition

```python
from hyperdjango.commands import CommandDefinition

cmd = CommandDefinition(
    name="seed",
    help="Seed the database",
    func=seed_command,
    args=[...],
)
```

---

## Django Migration Guide

| Django                             | HyperDjango                        |
| ---------------------------------- | ---------------------------------- |
| `BaseCommand` subclass             | `@command` decorator               |
| `add_arguments(parser)`            | Type annotations + defaults        |
| `handle(self, **options)`          | Function body                      |
| `management/commands/mycommand.py` | Any module + `discover_commands()` |
| `manage.py mycommand`              | `hyper mycommand`                  |
| `call_command("name", **kwargs)`   | `await run_command("name", args)`  |
