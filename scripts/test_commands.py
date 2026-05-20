#!/usr/bin/env python3
"""Tests for hyperdjango.commands management command framework.

Tests:
 1. @command registers function in registry
 2. @command with explicit name
 3. @command with auto name (function name)
 4. ArgDefinition from int param
 5. ArgDefinition from str param
 6. ArgDefinition from bool param
 7. ArgDefinition from float param
 8. Default values preserved
 9. Required args (no default) marked required
10. Bool args marked as flags
11. get_command returns correct definition
12. get_command returns None for unknown
13. list_commands returns all registered
14. run_command with valid args calls function
15. run_command with --help returns help text
16. run_command with missing required arg returns error
17. run_command with wrong type returns error
18. Async function support
19. Sync function support
20. format_help output includes name
21. format_help output includes help text
22. format_help output includes arg descriptions
23. Argument parsing: --key=value
24. Argument parsing: --key value
25. Argument parsing: --flag
26. Argument parsing: positional args
27. discover_commands imports modules
28. Multiple args parsed correctly
29. Unknown argument returns error
30. Too many positional args returns error
"""

# hyper-test: unit

import asyncio
import sys
from dataclasses import fields as dataclass_fields
from io import StringIO

from hyperdjango.commands import (
    ArgDefinition,
    CommandDefinition,
    _command_registry,
    _parse_args,
    command,
    discover_commands,
    format_help,
    get_command,
    list_commands,
    run_command,
)


def main():
    passed = 0
    failed = 0

    def check(name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            print(f"  PASS: {name}")
            passed += 1
        else:
            print(f"  FAIL: {name} -- {detail}")
            failed += 1

    # Clear registry for clean tests
    _command_registry.clear()

    # ── Test 1: @command registers function in registry ──────────────────
    print("\n=== Test 1: @command registers function in registry ===")

    @command(name="test_cmd", help="A test command")
    def my_test_cmd():
        pass

    check("registered in registry", "test_cmd" in _command_registry)

    # ── Test 2: @command with explicit name ──────────────────────────────
    print("\n=== Test 2: @command with explicit name ===")

    @command(name="explicit_name", help="Explicit")
    def some_func():
        pass

    check("explicit name used", "explicit_name" in _command_registry)
    check("function name not used", "some_func" not in _command_registry)

    # ── Test 3: @command with auto name (function name) ──────────────────
    print("\n=== Test 3: @command with auto name (function name) ===")

    @command(help="Auto named")
    def auto_named():
        pass

    check("auto name from function", "auto_named" in _command_registry)

    # ── Test 4: ArgDefinition from int param ─────────────────────────────
    print("\n=== Test 4: ArgDefinition from int param ===")

    @command(name="int_test", help="Int test")
    def int_cmd(count: int = 10):
        pass

    cmd = _command_registry["int_test"]
    arg = cmd.args[0]
    check("int arg name", arg.name == "count", f"got {arg.name}")
    check("int arg type", arg.type is int, f"got {arg.type}")
    check("int arg not flag", arg.is_flag is False)

    # ── Test 5: ArgDefinition from str param ─────────────────────────────
    print("\n=== Test 5: ArgDefinition from str param ===")

    @command(name="str_test", help="Str test")
    def str_cmd(label: str = "default"):
        pass

    arg = _command_registry["str_test"].args[0]
    check("str arg type", arg.type is str, f"got {arg.type}")
    check("str arg not flag", arg.is_flag is False)

    # ── Test 6: ArgDefinition from bool param ────────────────────────────
    print("\n=== Test 6: ArgDefinition from bool param ===")

    @command(name="bool_test", help="Bool test")
    def bool_cmd(verbose: bool = False):
        pass

    arg = _command_registry["bool_test"].args[0]
    check("bool arg type", arg.type is bool, f"got {arg.type}")
    check("bool arg is flag", arg.is_flag is True)

    # ── Test 7: ArgDefinition from float param ───────────────────────────
    print("\n=== Test 7: ArgDefinition from float param ===")

    @command(name="float_test", help="Float test")
    def float_cmd(rate: float = 1.5):
        pass

    arg = _command_registry["float_test"].args[0]
    check("float arg type", arg.type is float, f"got {arg.type}")
    check("float arg not flag", arg.is_flag is False)

    # ── Test 8: Default values preserved ─────────────────────────────────
    print("\n=== Test 8: Default values preserved ===")
    arg_int = _command_registry["int_test"].args[0]
    arg_str = _command_registry["str_test"].args[0]
    arg_bool = _command_registry["bool_test"].args[0]
    arg_float = _command_registry["float_test"].args[0]
    check("int default", arg_int.default == 10, f"got {arg_int.default}")
    check("str default", arg_str.default == "default", f"got {arg_str.default}")
    check("bool default", arg_bool.default is False, f"got {arg_bool.default}")
    check("float default", arg_float.default == 1.5, f"got {arg_float.default}")

    # ── Test 9: Required args (no default) marked required ───────────────
    print("\n=== Test 9: Required args marked required ===")

    @command(name="req_test", help="Required test")
    def req_cmd(path: str, count: int = 5):
        pass

    cmd = _command_registry["req_test"]
    check(
        "path is required", cmd.args[0].required is True, f"got {cmd.args[0].required}"
    )
    check(
        "count is not required",
        cmd.args[1].required is False,
        f"got {cmd.args[1].required}",
    )

    # ── Test 10: Bool args marked as flags ───────────────────────────────
    print("\n=== Test 10: Bool args marked as flags ===")

    @command(name="flags_test", help="Flags test")
    def flags_cmd(verbose: bool = False, dry_run: bool = False, count: int = 1):
        pass

    cmd = _command_registry["flags_test"]
    check("verbose is flag", cmd.args[0].is_flag is True)
    check("dry_run is flag", cmd.args[1].is_flag is True)
    check("count is not flag", cmd.args[2].is_flag is False)

    # ── Test 11: get_command returns correct definition ──────────────────
    print("\n=== Test 11: get_command returns correct definition ===")
    result = get_command("int_test")
    check(
        "get_command returns CommandDefinition", isinstance(result, CommandDefinition)
    )
    check("correct name", result.name == "int_test", f"got {result.name}")
    check("correct help", result.help == "Int test", f"got {result.help}")

    # ── Test 12: get_command returns None for unknown ────────────────────
    print("\n=== Test 12: get_command returns None for unknown ===")
    result = get_command("nonexistent_command_xyz")
    check("returns None", result is None, f"got {result}")

    # ── Test 13: list_commands returns all registered ────────────────────
    print("\n=== Test 13: list_commands returns all registered ===")
    all_cmds = list_commands()
    names = [c.name for c in all_cmds]
    check("list returns list", isinstance(all_cmds, list))
    check("contains int_test", "int_test" in names, f"got {names}")
    check("contains auto_named", "auto_named" in names)
    check("count matches registry", len(all_cmds) == len(_command_registry))

    # ── Test 14: run_command with valid args calls function ──────────────
    print("\n=== Test 14: run_command with valid args calls function ===")
    call_log: list[dict[str, object]] = []

    @command(name="logged_cmd", help="Logged")
    def logged_cmd(count: int = 5, label: str = "x"):
        call_log.append({"count": count, "label": label})

    exit_code = asyncio.run(run_command("logged_cmd", ["--count=42", "--label=hello"]))
    check("exit code 0", exit_code == 0, f"got {exit_code}")
    check("function called", len(call_log) == 1, f"got {len(call_log)}")
    check("count arg", call_log[0]["count"] == 42, f"got {call_log[0]['count']}")
    check("label arg", call_log[0]["label"] == "hello", f"got {call_log[0]['label']}")

    # ── Test 15: run_command with --help returns help text ───────────────
    print("\n=== Test 15: run_command with --help ===")
    old_stdout = sys.stdout
    sys.stdout = captured = StringIO()
    exit_code = asyncio.run(run_command("logged_cmd", ["--help"]))
    sys.stdout = old_stdout
    output = captured.getvalue()
    check("exit code 0", exit_code == 0, f"got {exit_code}")
    check(
        "help contains command name", "logged_cmd" in output, f"output: {output[:200]}"
    )
    check("help contains help text", "Logged" in output)

    # ── Test 16: run_command with missing required arg ───────────────────
    print("\n=== Test 16: run_command with missing required arg ===")
    old_stderr = sys.stderr
    sys.stderr = captured = StringIO()
    exit_code = asyncio.run(run_command("req_test", []))
    sys.stderr = old_stderr
    output = captured.getvalue()
    check("exit code 1", exit_code == 1, f"got {exit_code}")
    check(
        "error mentions missing arg",
        "Missing required" in output,
        f"output: {output[:200]}",
    )

    # ── Test 17: run_command with wrong type ─────────────────────────────
    print("\n=== Test 17: run_command with wrong type ===")
    old_stderr = sys.stderr
    sys.stderr = captured = StringIO()
    exit_code = asyncio.run(run_command("int_test", ["--count=notanumber"]))
    sys.stderr = old_stderr
    output = captured.getvalue()
    check("exit code 1", exit_code == 1, f"got {exit_code}")
    check(
        "error mentions invalid value",
        "Invalid value" in output,
        f"output: {output[:200]}",
    )

    # ── Test 18: Async function support ──────────────────────────────────
    print("\n=== Test 18: Async function support ===")
    async_log: list[int] = []

    @command(name="async_cmd", help="Async")
    async def async_cmd(value: int = 0):
        async_log.append(value)

    exit_code = asyncio.run(run_command("async_cmd", ["--value=99"]))
    check("exit code 0", exit_code == 0, f"got {exit_code}")
    check("async function called", len(async_log) == 1)
    check("async value correct", async_log[0] == 99, f"got {async_log[0]}")

    # ── Test 19: Sync function support ───────────────────────────────────
    print("\n=== Test 19: Sync function support ===")
    sync_log: list[str] = []

    @command(name="sync_cmd", help="Sync")
    def sync_cmd(msg: str = "hi"):
        sync_log.append(msg)

    exit_code = asyncio.run(run_command("sync_cmd", ["--msg=world"]))
    check("exit code 0", exit_code == 0, f"got {exit_code}")
    check("sync function called", len(sync_log) == 1)
    check("sync value correct", sync_log[0] == "world", f"got {sync_log[0]}")

    # ── Test 20: format_help includes name ───────────────────────────────
    print("\n=== Test 20: format_help includes name ===")
    cmd = get_command("logged_cmd")
    help_text = format_help(cmd)
    check("help contains name", "logged_cmd" in help_text, f"help: {help_text[:200]}")

    # ── Test 21: format_help includes help text ──────────────────────────
    print("\n=== Test 21: format_help includes help text ===")
    check("help contains description", "Logged" in help_text)

    # ── Test 22: format_help includes arg descriptions ───────────────────
    print("\n=== Test 22: format_help includes arg descriptions ===")
    check("help contains --count", "--count" in help_text, f"help: {help_text[:300]}")
    check("help contains --label", "--label" in help_text)
    check("help contains type info", "int" in help_text)

    # ── Test 23: Argument parsing: --key=value ───────────────────────────
    print("\n=== Test 23: Argument parsing: --key=value ===")
    cmd = get_command("int_test")
    result = _parse_args(cmd, ["--count=42"])
    check("parse result is dict", isinstance(result, dict), f"got {type(result)}")
    check("parsed value", result["count"] == 42, f"got {result}")

    # ── Test 24: Argument parsing: --key value ───────────────────────────
    print("\n=== Test 24: Argument parsing: --key value ===")
    result = _parse_args(cmd, ["--count", "99"])
    check("parsed value", result["count"] == 99, f"got {result}")

    # ── Test 25: Argument parsing: --flag ────────────────────────────────
    print("\n=== Test 25: Argument parsing: --flag ===")
    cmd = get_command("bool_test")
    result = _parse_args(cmd, ["--verbose"])
    check("flag is True", result["verbose"] is True, f"got {result}")

    result_no_flag = _parse_args(cmd, [])
    check(
        "flag default False",
        result_no_flag["verbose"] is False,
        f"got {result_no_flag}",
    )

    # ── Test 26: Argument parsing: positional args ───────────────────────
    print("\n=== Test 26: Argument parsing: positional args ===")
    cmd = get_command("req_test")
    result = _parse_args(cmd, ["/tmp/file.txt"])
    check("positional parsed", result["path"] == "/tmp/file.txt", f"got {result}")
    check("default kept", result["count"] == 5, f"got {result}")

    # ── Test 27: discover_commands imports modules ───────────────────────
    print("\n=== Test 27: discover_commands imports modules ===")
    # We test with a known module that has @command decorators
    # Using our own module as a test since it already has commands registered
    before_count = len(_command_registry)
    # discover_commands on an already-imported module should return empty
    # (all decorators already fired)
    result = discover_commands(["hyperdjango.commands"])
    check("returns list", isinstance(result, list), f"got {type(result)}")
    check("no new commands from already imported", len(result) == 0, f"got {result}")

    # ── Test 28: Multiple args parsed correctly ──────────────────────────
    print("\n=== Test 28: Multiple args parsed correctly ===")
    cmd = get_command("flags_test")
    result = _parse_args(cmd, ["--verbose", "--count=7"])
    check("verbose True", result["verbose"] is True, f"got {result}")
    check("dry_run False", result["dry_run"] is False, f"got {result}")
    check("count 7", result["count"] == 7, f"got {result}")

    # ── Test 29: Unknown argument returns error ──────────────────────────
    print("\n=== Test 29: Unknown argument returns error ===")
    cmd = get_command("int_test")
    result = _parse_args(cmd, ["--nonexistent=5"])
    check("returns error string", isinstance(result, str), f"got {type(result)}")
    check("error mentions unknown", "Unknown" in result, f"got {result}")

    # ── Test 30: Too many positional args ────────────────────────────────
    print("\n=== Test 30: Too many positional args ===")

    @command(name="no_args_cmd", help="No args")
    def no_args_cmd():
        pass

    cmd = get_command("no_args_cmd")
    result = _parse_args(cmd, ["extra", "values"])
    check("returns error string", isinstance(result, str), f"got {type(result)}")
    check("error mentions too many", "Too many" in result, f"got {result}")

    # ── Test 31: run_command unknown command ──────────────────────────────
    print("\n=== Test 31: run_command unknown command ===")
    old_stderr = sys.stderr
    sys.stderr = StringIO()
    exit_code = asyncio.run(run_command("totally_unknown_cmd", []))
    sys.stderr = old_stderr
    check("exit code 1", exit_code == 1, f"got {exit_code}")

    # ── Test 32: CommandDefinition is dataclass ──────────────────────────
    print("\n=== Test 32: CommandDefinition and ArgDefinition are dataclasses ===")
    cmd_fields = [f.name for f in dataclass_fields(CommandDefinition)]
    arg_fields = [f.name for f in dataclass_fields(ArgDefinition)]
    check("CommandDefinition has name", "name" in cmd_fields)
    check("CommandDefinition has func", "func" in cmd_fields)
    check("ArgDefinition has is_flag", "is_flag" in arg_fields)
    check("ArgDefinition has required", "required" in arg_fields)

    # ── Test 33: Unannotated params default to str ───────────────────────
    print("\n=== Test 33: Unannotated params default to str ===")

    @command(name="untyped_cmd", help="Untyped")
    def untyped_cmd(value="hello"):
        pass

    arg = _command_registry["untyped_cmd"].args[0]
    check("defaults to str type", arg.type is str, f"got {arg.type}")

    # ── Test 34: Command exception returns exit code 1 ───────────────────
    print("\n=== Test 34: Command exception returns exit code 1 ===")

    @command(name="failing_cmd", help="Fails")
    def failing_cmd():
        raise RuntimeError("boom")

    old_stderr = sys.stderr
    sys.stderr = StringIO()
    exit_code = asyncio.run(run_command("failing_cmd", []))
    sys.stderr = old_stderr
    check("exit code 1 on exception", exit_code == 1, f"got {exit_code}")

    # ── Test 35: Decorator returns original function ─────────────────────
    print("\n=== Test 35: Decorator returns original function ===")

    @command(name="identity_test", help="Identity")
    def identity_func(x: int = 1):
        return x

    check(
        "original function returned",
        identity_func(42) == 42,
        f"got {identity_func(42)}",
    )

    # ── Summary ──────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed > 0:
        sys.exit(1)
    print("All commands tests passed!")


if __name__ == "__main__":
    main()
