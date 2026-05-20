// HyperGuard bytecode evaluator — sub-50ns permission checks in native Zig.
//
// Evaluates compiled guard conditions against user/resource attribute dicts.
// Called from Python via: _hyperdjango_native._guard_evaluate(bytecode, user_dict, resource_dict)
//
// The VM uses a tagged Value union on the stack to support int, bool, string,
// and none types natively. String comparisons use zero-copy borrowed pointers
// from Python's internal UTF-8 buffers (valid for the duration of the FFI call).
//
// Bytecode format: sequence of 3-byte instructions: [opcode: u8, arg_lo: u8, arg_hi: u8]
// Stack-based: operations push/pop Value items. Final YIELD returns top of stack truthiness.

const std = @import("std");
const py = @import("py.zig");
const c = py.c;

// ── Value type ──────────────────────────────────────────────────────────────

pub const Value = union(enum) {
    int: i64,
    str: StrSlice,
    boolean: bool,
    none,
};

pub const StrSlice = struct {
    ptr: [*c]const u8,
    len: usize,

    fn eql(self: StrSlice, other: StrSlice) bool {
        if (self.len != other.len) return false;
        if (self.len == 0) return true;
        return std.mem.eql(u8, self.slice(), other.slice());
    }

    fn slice(self: StrSlice) []const u8 {
        return self.ptr[0..self.len];
    }
};

// ── Opcodes ─────────────────────────────────────────────────────────────────

pub const Op = enum(u8) {
    LOAD_U = 0x01, // Load user field by index
    LOAD_R = 0x02, // Load resource field by index
    LOAD_C = 0x03, // Load constant by index
    CMP_EQ = 0x10,
    CMP_NE = 0x11,
    CMP_GT = 0x12,
    CMP_GE = 0x13,
    CMP_LT = 0x14,
    CMP_LE = 0x15,
    AND = 0x20,
    OR = 0x21,
    NOT = 0x22,
    JUMP_F = 0x30, // Jump forward if false — PEEKS stack, does NOT pop (reserved for Phase 4 optimizer)
    JUMP_T = 0x31, // Jump forward if true — PEEKS stack, does NOT pop (reserved for Phase 4 optimizer)
    YIELD = 0xFF,
    // Non-exhaustive: an untrusted/corrupt bytecode byte may not be a valid Op.
    // `@enumFromInt` must not hit unreachable — the evaluator fails closed on it
    // (see the `_ =>` arm in evaluate()).
    _,
};

// ── Instruction encoding ────────────────────────────────────────────────────

const INSTR_SIZE = 3;

fn decode_arg(bytecode: []const u8, ip: usize) u16 {
    return @as(u16, bytecode[ip + 1]) | (@as(u16, bytecode[ip + 2]) << 8);
}

// ── Comparison helpers ──────────────────────────────────────────────────────

fn values_equal(a: Value, b: Value) bool {
    return switch (a) {
        .int => |ai| switch (b) {
            .int => |bi| ai == bi,
            .boolean => |bb| ai == @as(i64, @intFromBool(bb)),
            .str, .none => false,
        },
        .boolean => |ab| switch (b) {
            .boolean => |bb| ab == bb,
            .int => |bi| @as(i64, @intFromBool(ab)) == bi,
            .str, .none => false,
        },
        .str => |as_str| switch (b) {
            .str => |bs_str| as_str.eql(bs_str),
            .int, .boolean, .none => false,
        },
        .none => switch (b) {
            .none => true,
            .int, .boolean, .str => false,
        },
    };
}

/// Coerce a value to i64 for ordered comparison. Returns null if not numeric.
fn to_i64(v: Value) ?i64 {
    return switch (v) {
        .int => |i| i,
        .boolean => |b| @as(i64, @intFromBool(b)),
        .str, .none => null,
    };
}

fn is_truthy(v: Value) bool {
    return switch (v) {
        .int => |i| i != 0,
        .boolean => |b| b,
        .str => |s| s.len > 0,
        .none => false,
    };
}

// ── Stack machine ───────────────────────────────────────────────────────────

const MAX_STACK = 64;

pub const EvalResult = enum(u8) {
    allow = 1,
    deny = 0,
    err = 2,
};

pub fn evaluate(
    bytecode: []const u8,
    user_values: []const Value,
    resource_values: []const Value,
    constants: []const Value,
) EvalResult {
    var stack: [MAX_STACK]Value = undefined;
    var sp: usize = 0;
    var ip: usize = 0;

    while (ip + INSTR_SIZE <= bytecode.len) {
        const opcode = bytecode[ip];
        const arg = decode_arg(bytecode, ip);
        ip += INSTR_SIZE;

        const op: Op = @enumFromInt(opcode);
        switch (op) {
            .LOAD_U => {
                if (arg >= user_values.len) return .err;
                if (sp >= MAX_STACK) return .err;
                stack[sp] = user_values[arg];
                sp += 1;
            },
            .LOAD_R => {
                if (arg >= resource_values.len) return .err;
                if (sp >= MAX_STACK) return .err;
                stack[sp] = resource_values[arg];
                sp += 1;
            },
            .LOAD_C => {
                if (arg >= constants.len) return .err;
                if (sp >= MAX_STACK) return .err;
                stack[sp] = constants[arg];
                sp += 1;
            },
            .CMP_EQ => {
                if (sp < 2) return .err;
                const b = stack[sp - 1];
                const a = stack[sp - 2];
                sp -= 2;
                stack[sp] = Value{ .boolean = values_equal(a, b) };
                sp += 1;
            },
            .CMP_NE => {
                if (sp < 2) return .err;
                const b = stack[sp - 1];
                const a = stack[sp - 2];
                sp -= 2;
                stack[sp] = Value{ .boolean = !values_equal(a, b) };
                sp += 1;
            },
            .CMP_GT => {
                if (sp < 2) return .err;
                const b_i = to_i64(stack[sp - 1]) orelse return .err;
                const a_i = to_i64(stack[sp - 2]) orelse return .err;
                sp -= 2;
                stack[sp] = Value{ .boolean = a_i > b_i };
                sp += 1;
            },
            .CMP_GE => {
                if (sp < 2) return .err;
                const b_i = to_i64(stack[sp - 1]) orelse return .err;
                const a_i = to_i64(stack[sp - 2]) orelse return .err;
                sp -= 2;
                stack[sp] = Value{ .boolean = a_i >= b_i };
                sp += 1;
            },
            .CMP_LT => {
                if (sp < 2) return .err;
                const b_i = to_i64(stack[sp - 1]) orelse return .err;
                const a_i = to_i64(stack[sp - 2]) orelse return .err;
                sp -= 2;
                stack[sp] = Value{ .boolean = a_i < b_i };
                sp += 1;
            },
            .CMP_LE => {
                if (sp < 2) return .err;
                const b_i = to_i64(stack[sp - 1]) orelse return .err;
                const a_i = to_i64(stack[sp - 2]) orelse return .err;
                sp -= 2;
                stack[sp] = Value{ .boolean = a_i <= b_i };
                sp += 1;
            },
            .AND => {
                if (sp < 2) return .err;
                const b = is_truthy(stack[sp - 1]);
                const a = is_truthy(stack[sp - 2]);
                sp -= 2;
                stack[sp] = Value{ .boolean = a and b };
                sp += 1;
            },
            .OR => {
                if (sp < 2) return .err;
                const b = is_truthy(stack[sp - 1]);
                const a = is_truthy(stack[sp - 2]);
                sp -= 2;
                stack[sp] = Value{ .boolean = a or b };
                sp += 1;
            },
            .NOT => {
                if (sp < 1) return .err;
                stack[sp - 1] = Value{ .boolean = !is_truthy(stack[sp - 1]) };
            },
            .JUMP_F => {
                if (sp < 1) return .err;
                if (!is_truthy(stack[sp - 1])) {
                    ip += @as(usize, arg) * INSTR_SIZE;
                }
            },
            .JUMP_T => {
                if (sp < 1) return .err;
                if (is_truthy(stack[sp - 1])) {
                    ip += @as(usize, arg) * INSTR_SIZE;
                }
            },
            .YIELD => {
                if (sp < 1) return .deny;
                return if (is_truthy(stack[sp - 1])) .allow else .deny;
            },
            // Invalid/unknown opcode — fail closed with an error, never fall
            // through to a decision (a guard evaluator must not silently allow
            // or deny on corrupt bytecode).
            _ => return .err,
        }
    }

    return .deny;
}

// ── Python C API bridge ─────────────────────────────────────────────────────

pub fn py_guard_evaluate(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var bytecode_ptr: [*c]const u8 = undefined;
    var bytecode_len: c.Py_ssize_t = undefined;
    var user_dict: ?*c.PyObject = null;
    var resource_dict: ?*c.PyObject = null;
    var field_names: ?*c.PyObject = null;
    var constant_values: ?*c.PyObject = null;

    if (c.PyArg_ParseTuple(
        args,
        "s#OOOO",
        &bytecode_ptr,
        &bytecode_len,
        &user_dict,
        &resource_dict,
        &field_names,
        &constant_values,
    ) == 0) return null;

    const bc = bytecode_ptr[0..@intCast(bytecode_len)];

    const n_fields_signed = c.PyTuple_Size(field_names.?);
    if (n_fields_signed < 0) return null; // not a tuple — exception already set
    const n_fields: usize = @intCast(n_fields_signed);
    if (n_fields > 128) {
        c.PyErr_SetString(c.PyExc_ValueError, "too many field names (max 128)");
        return null;
    }

    // Extract field values as typed Values (int, bool, string, none)
    var user_vals: [128]Value = undefined;
    var resource_vals: [128]Value = undefined;

    for (0..n_fields) |i| {
        const name_obj = c.PyTuple_GetItem(field_names.?, @intCast(i)).?;
        user_vals[i] = extract_value_from_dict(user_dict.?, name_obj);
        resource_vals[i] = extract_value_from_dict(resource_dict.?, name_obj);
    }

    const n_consts_signed = c.PyTuple_Size(constant_values.?);
    if (n_consts_signed < 0) return null; // not a tuple — exception already set
    const n_consts: usize = @intCast(n_consts_signed);
    if (n_consts > 128) {
        c.PyErr_SetString(c.PyExc_ValueError, "too many constants (max 128)");
        return null;
    }
    var consts: [128]Value = undefined;
    for (0..n_consts) |i| {
        const val_obj = c.PyTuple_GetItem(constant_values.?, @intCast(i)).?;
        consts[i] = py_obj_to_value(val_obj);
    }

    const result = evaluate(
        bc,
        user_vals[0..n_fields],
        resource_vals[0..n_fields],
        consts[0..n_consts],
    );

    return switch (result) {
        .allow => py.pyTrue(),
        .deny => py.pyFalse(),
        .err => {
            c.PyErr_SetString(c.PyExc_RuntimeError, "guard bytecode evaluation error (type mismatch or stack fault)");
            return null;
        },
    };
}

/// Extract a Value from a Python dict by key. Missing keys → none.
fn extract_value_from_dict(dict: *c.PyObject, key: *c.PyObject) Value {
    const val = c.PyDict_GetItem(dict, key);
    if (val == null) return Value{ .none = {} };
    return py_obj_to_value(val.?);
}

/// Convert a Python object to a typed Value.
/// Bool checked before int (bool is subclass of int in Python).
/// Strings are borrowed zero-copy from Python's internal UTF-8 buffer.
fn py_obj_to_value(obj: *c.PyObject) Value {
    // Bool FIRST (bool is subclass of int)
    if (obj == @as(*c.PyObject, @ptrCast(&c._Py_TrueStruct))) return Value{ .boolean = true };
    if (obj == @as(*c.PyObject, @ptrCast(&c._Py_FalseStruct))) return Value{ .boolean = false };
    if (obj == @as(*c.PyObject, @ptrCast(&c._Py_NoneStruct))) return Value{ .none = {} };

    if (c.PyLong_Check(obj) != 0) {
        const val = c.PyLong_AsLongLong(obj);
        // PyLong_AsLongLong returns -1 on overflow and sets an exception
        if (val == -1 and c.PyErr_Occurred() != null) {
            c.PyErr_Clear(); // Clear the overflow exception
            return Value{ .none = {} }; // Treat overflow as none (safe denial)
        }
        return Value{ .int = val };
    }

    if (c.PyUnicode_Check(obj) != 0) {
        var slen: c.Py_ssize_t = undefined;
        const sptr = c.PyUnicode_AsUTF8AndSize(obj, &slen);
        if (sptr != null) {
            return Value{ .str = .{ .ptr = sptr.?, .len = @intCast(slen) } };
        }
        // Surrogate/non-UTF8 str — clear the pending exception so it doesn't
        // leak out of this (non-error-returning) helper and corrupt a later call.
        c.PyErr_Clear();
    }

    return Value{ .none = {} };
}

// ── Zig-only tests ──────────────────────────────────────────────────────────

fn val_int(v: i64) Value {
    return Value{ .int = v };
}
fn val_bool(v: bool) Value {
    return Value{ .boolean = v };
}
fn val_str(s: []const u8) Value {
    return Value{ .str = .{ .ptr = s.ptr, .len = s.len } };
}
fn val_none() Value {
    return Value{ .none = {} };
}

test "int equality" {
    const bc = [_]u8{
        @intFromEnum(Op.LOAD_U), 0, 0,
        @intFromEnum(Op.LOAD_C), 0, 0,
        @intFromEnum(Op.CMP_EQ), 0, 0,
        @intFromEnum(Op.YIELD),  0, 0,
    };
    const user = [_]Value{val_int(1)};
    const consts = [_]Value{val_int(1)};
    try std.testing.expectEqual(EvalResult.allow, evaluate(&bc, &user, &[_]Value{}, &consts));
}

test "int inequality deny" {
    const bc = [_]u8{
        @intFromEnum(Op.LOAD_U), 0, 0,
        @intFromEnum(Op.LOAD_C), 0, 0,
        @intFromEnum(Op.CMP_EQ), 0, 0,
        @intFromEnum(Op.YIELD),  0, 0,
    };
    const user = [_]Value{val_int(0)};
    const consts = [_]Value{val_int(1)};
    try std.testing.expectEqual(EvalResult.deny, evaluate(&bc, &user, &[_]Value{}, &consts));
}

test "string equality" {
    const bc = [_]u8{
        @intFromEnum(Op.LOAD_U), 0, 0,
        @intFromEnum(Op.LOAD_C), 0, 0,
        @intFromEnum(Op.CMP_EQ), 0, 0,
        @intFromEnum(Op.YIELD),  0, 0,
    };
    const user = [_]Value{val_str("admin")};
    const consts = [_]Value{val_str("admin")};
    try std.testing.expectEqual(EvalResult.allow, evaluate(&bc, &user, &[_]Value{}, &consts));
}

test "string inequality" {
    const bc = [_]u8{
        @intFromEnum(Op.LOAD_U), 0, 0,
        @intFromEnum(Op.LOAD_C), 0, 0,
        @intFromEnum(Op.CMP_NE), 0, 0,
        @intFromEnum(Op.YIELD),  0, 0,
    };
    const user = [_]Value{val_str("user")};
    const consts = [_]Value{val_str("admin")};
    try std.testing.expectEqual(EvalResult.allow, evaluate(&bc, &user, &[_]Value{}, &consts));
}

test "string vs int never equal" {
    const bc = [_]u8{
        @intFromEnum(Op.LOAD_U), 0, 0,
        @intFromEnum(Op.LOAD_C), 0, 0,
        @intFromEnum(Op.CMP_EQ), 0, 0,
        @intFromEnum(Op.YIELD),  0, 0,
    };
    const user = [_]Value{val_str("1")};
    const consts = [_]Value{val_int(1)};
    try std.testing.expectEqual(EvalResult.deny, evaluate(&bc, &user, &[_]Value{}, &consts));
}

test "none != string" {
    const bc = [_]u8{
        @intFromEnum(Op.LOAD_U), 0, 0,
        @intFromEnum(Op.LOAD_C), 0, 0,
        @intFromEnum(Op.CMP_EQ), 0, 0,
        @intFromEnum(Op.YIELD),  0, 0,
    };
    const user = [_]Value{val_none()};
    const consts = [_]Value{val_str("published")};
    try std.testing.expectEqual(EvalResult.deny, evaluate(&bc, &user, &[_]Value{}, &consts));
}

test "bool/int cross-type equality" {
    const bc = [_]u8{
        @intFromEnum(Op.LOAD_U), 0, 0,
        @intFromEnum(Op.LOAD_C), 0, 0,
        @intFromEnum(Op.CMP_EQ), 0, 0,
        @intFromEnum(Op.YIELD),  0, 0,
    };
    const user = [_]Value{val_int(1)};
    const consts = [_]Value{val_bool(true)};
    try std.testing.expectEqual(EvalResult.allow, evaluate(&bc, &user, &[_]Value{}, &consts));
}

test "AND two conditions" {
    const bc = [_]u8{
        @intFromEnum(Op.LOAD_R), 0, 0,
        @intFromEnum(Op.LOAD_C), 0, 0,
        @intFromEnum(Op.CMP_EQ), 0, 0,
        @intFromEnum(Op.LOAD_R), 1, 0,
        @intFromEnum(Op.LOAD_C), 0, 0,
        @intFromEnum(Op.CMP_EQ), 0, 0,
        @intFromEnum(Op.AND),    0, 0,
        @intFromEnum(Op.YIELD),  0, 0,
    };
    const resource_ok = [_]Value{ val_bool(false), val_bool(false) };
    const consts = [_]Value{val_bool(false)};
    try std.testing.expectEqual(EvalResult.allow, evaluate(&bc, &[_]Value{}, &resource_ok, &consts));

    const resource_bad = [_]Value{ val_bool(true), val_bool(false) };
    try std.testing.expectEqual(EvalResult.deny, evaluate(&bc, &[_]Value{}, &resource_bad, &consts));
}

test "ownership: resource.author_id == user.id" {
    const bc = [_]u8{
        @intFromEnum(Op.LOAD_R), 0, 0,
        @intFromEnum(Op.LOAD_U), 0, 0,
        @intFromEnum(Op.CMP_EQ), 0, 0,
        @intFromEnum(Op.YIELD),  0, 0,
    };
    try std.testing.expectEqual(
        EvalResult.allow,
        evaluate(&bc, &[_]Value{val_int(42)}, &[_]Value{val_int(42)}, &[_]Value{}),
    );
    try std.testing.expectEqual(
        EvalResult.deny,
        evaluate(&bc, &[_]Value{val_int(42)}, &[_]Value{val_int(99)}, &[_]Value{}),
    );
}

test "integer comparison: karma >= 50" {
    const bc = [_]u8{
        @intFromEnum(Op.LOAD_U), 0, 0,
        @intFromEnum(Op.LOAD_C), 0, 0,
        @intFromEnum(Op.CMP_GE), 0, 0,
        @intFromEnum(Op.YIELD),  0, 0,
    };
    const consts = [_]Value{val_int(50)};
    try std.testing.expectEqual(EvalResult.allow, evaluate(&bc, &[_]Value{val_int(100)}, &[_]Value{}, &consts));
    try std.testing.expectEqual(EvalResult.deny, evaluate(&bc, &[_]Value{val_int(10)}, &[_]Value{}, &consts));
    try std.testing.expectEqual(EvalResult.allow, evaluate(&bc, &[_]Value{val_int(50)}, &[_]Value{}, &consts));
}

test "NOT operator" {
    const bc = [_]u8{
        @intFromEnum(Op.LOAD_R), 0, 0,
        @intFromEnum(Op.NOT),    0, 0,
        @intFromEnum(Op.YIELD),  0, 0,
    };
    try std.testing.expectEqual(EvalResult.allow, evaluate(&bc, &[_]Value{}, &[_]Value{val_bool(false)}, &[_]Value{}));
    try std.testing.expectEqual(EvalResult.deny, evaluate(&bc, &[_]Value{}, &[_]Value{val_bool(true)}, &[_]Value{}));
}

test "string truthiness" {
    const bc = [_]u8{
        @intFromEnum(Op.LOAD_U), 0, 0,
        @intFromEnum(Op.YIELD),  0, 0,
    };
    // Non-empty string is truthy
    try std.testing.expectEqual(EvalResult.allow, evaluate(&bc, &[_]Value{val_str("hello")}, &[_]Value{}, &[_]Value{}));
    // Empty string is falsy
    try std.testing.expectEqual(EvalResult.deny, evaluate(&bc, &[_]Value{val_str("")}, &[_]Value{}, &[_]Value{}));
}

test "ordered comparison on strings returns error" {
    const bc = [_]u8{
        @intFromEnum(Op.LOAD_U), 0, 0,
        @intFromEnum(Op.LOAD_C), 0, 0,
        @intFromEnum(Op.CMP_GT), 0, 0,
        @intFromEnum(Op.YIELD),  0, 0,
    };
    const user = [_]Value{val_str("abc")};
    const consts = [_]Value{val_str("def")};
    try std.testing.expectEqual(EvalResult.err, evaluate(&bc, &user, &[_]Value{}, &consts));
}

test "empty bytecode denies" {
    try std.testing.expectEqual(EvalResult.deny, evaluate(&[_]u8{}, &[_]Value{}, &[_]Value{}, &[_]Value{}));
}

test "stack underflow returns error" {
    const bc = [_]u8{ @intFromEnum(Op.CMP_EQ), 0, 0 };
    try std.testing.expectEqual(EvalResult.err, evaluate(&bc, &[_]Value{}, &[_]Value{}, &[_]Value{}));
}

test "out of bounds field index returns error" {
    const bc = [_]u8{
        @intFromEnum(Op.LOAD_U), 99, 0,
        @intFromEnum(Op.YIELD),  0,  0,
    };
    try std.testing.expectEqual(EvalResult.err, evaluate(&bc, &[_]Value{val_int(1)}, &[_]Value{}, &[_]Value{}));
}

test "invalid opcode returns error (not UB)" {
    const bc = [_]u8{ 0x00, 0, 0 }; // 0x00 is not a valid Op
    try std.testing.expectEqual(EvalResult.err, evaluate(&bc, &[_]Value{}, &[_]Value{}, &[_]Value{}));
}

test "mixed string and bool conditions with AND" {
    // resource.status == "published" AND resource.is_public == true
    const bc = [_]u8{
        @intFromEnum(Op.LOAD_R), 0, 0, // resource.status
        @intFromEnum(Op.LOAD_C), 0, 0, // "published"
        @intFromEnum(Op.CMP_EQ), 0, 0,
        @intFromEnum(Op.LOAD_R), 1, 0, // resource.is_public
        @intFromEnum(Op.LOAD_C), 1, 0, // true
        @intFromEnum(Op.CMP_EQ), 0, 0,
        @intFromEnum(Op.AND),    0, 0,
        @intFromEnum(Op.YIELD),  0, 0,
    };
    const resource = [_]Value{ val_str("published"), val_bool(true) };
    const consts = [_]Value{ val_str("published"), val_bool(true) };
    try std.testing.expectEqual(EvalResult.allow, evaluate(&bc, &[_]Value{}, &resource, &consts));

    const resource_bad = [_]Value{ val_str("draft"), val_bool(true) };
    try std.testing.expectEqual(EvalResult.deny, evaluate(&bc, &[_]Value{}, &resource_bad, &consts));
}
