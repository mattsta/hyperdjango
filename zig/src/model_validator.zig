// Model-level native validation — single FFI call validates entire model
//
// Reimplements dhi's _native.c compile_model_specs + init_model_full in Zig.
// Zero per-field Python overhead: all constraints pre-parsed at class definition
// time into Zig structs, then validated in one tight loop at instantiation.
//
// API:
//   compile_model_specs(field_specs_tuple) -> PyCapsule
//   init_model_full(self, kwargs, capsule, extra_mode) -> None | [(field, msg)]

const std = @import("std");
pub const py = @import("py.zig");
const c = py.c;
const validator = @import("validator");

// ── Compiled field spec (mirrors dhi's CompiledFieldSpec) ────────────────────

pub const CompiledFieldSpec = struct {
    // Field identity
    name_obj: *c.PyObject,
    name_hash: c.Py_hash_t,
    alias_obj: *c.PyObject,

    // Requirements
    required: bool,
    default_val: *c.PyObject,

    // Type code: 0=any, 1=int, 2=float, 3=str, 4=bool, 5=bytes, 6=nested, 7=list-of-models, 8=union
    type_code: i32,
    strict: bool,

    // Numeric constraints (pre-parsed as native values)
    has_gt: bool,
    has_ge: bool,
    has_lt: bool,
    has_le: bool,
    has_mul: bool,
    gt_long: c_long,
    ge_long: c_long,
    lt_long: c_long,
    le_long: c_long,
    mul_long: c_long,
    gt_dbl: f64,
    ge_dbl: f64,
    lt_dbl: f64,
    le_dbl: f64,
    mul_dbl: f64,

    // Length constraints
    has_minl: bool,
    has_maxl: bool,
    min_len: c.Py_ssize_t,
    max_len: c.Py_ssize_t,

    // String transforms
    strip_ws: bool,
    to_lower: bool,
    to_upper: bool,

    // Float special
    allow_inf_nan: bool,

    // Format validation: 0=none, 1=email, 2=url, 3=uuid, 4=ipv4, 5=ipv6, 6=base64, 7=iso_date, 8=iso_datetime
    format_code: i32,

    // Nested model support
    nested_model_type: ?*c.PyObject,
    union_types_tuple: ?*c.PyObject,
};

pub const CompiledModelSpecs = struct {
    n_fields: usize,
    specs: [*]CompiledFieldSpec,
};

// ── Helpers ──────────────────────────────────────────────────────────────────

fn asLongCoerce(obj: *c.PyObject) c_long {
    if (c.PyLong_Check(obj) != 0) {
        return c.PyLong_AsLong(obj);
    }
    if (c.PyFloat_Check(obj) != 0) {
        return @intFromFloat(c.PyFloat_AsDouble(obj));
    }
    return 0;
}

fn asDoubleCoerce(obj: *c.PyObject) f64 {
    if (c.PyFloat_Check(obj) != 0) {
        return c.PyFloat_AsDouble(obj);
    }
    if (c.PyLong_Check(obj) != 0) {
        return @floatFromInt(c.PyLong_AsLong(obj));
    }
    return 0.0;
}

pub fn pyNone() *c.PyObject {
    return @as(*c.PyObject, @ptrCast(&c._Py_NoneStruct));
}

pub fn isNone(obj: *c.PyObject) bool {
    return obj == pyNone();
}

// ── Multi-word field bitset ──────────────────────────────────────────────────
// Tracks which field indices were seen (for defaults / required / fields_set).
// A single u64 silently dropped every field ≥64, AND `@as(u64, 1) << @intCast(i)`
// for i ≥ 64 is an illegal @intCast (UB in ReleaseFast, panic in ReleaseSafe).
// One bit per field across an []u64 fixes both: the shift amount is always the
// word-relative index (0..63).
inline fn bitsetWordCount(n_fields: usize) usize {
    return (n_fields + 63) / 64;
}
inline fn bitsetSet(words: []u64, i: usize) void {
    words[i >> 6] |= (@as(u64, 1) << @intCast(i & 63));
}
inline fn bitsetIsSet(words: []const u64, i: usize) bool {
    return (words[i >> 6] & (@as(u64, 1) << @intCast(i & 63))) != 0;
}

// Lazy-allocate error list and append (field_name_obj, msg_str) tuple
fn appendError(errors: *?*c.PyObject, name_obj: *c.PyObject, msg: *c.PyObject) void {
    if (errors.* == null) {
        errors.* = c.PyList_New(0);
        if (errors.* == null) return;
    }
    const err = c.Py_BuildValue("(OO)", name_obj, msg);
    if (err) |e| {
        _ = c.PyList_Append(errors.*.?, e);
        c.Py_DECREF(e);
    }
    c.Py_DECREF(msg);
}

pub fn appendErrorStr(errors: *?*c.PyObject, name_obj: *c.PyObject, msg_str: [*c]const u8) void {
    const msg = c.PyUnicode_FromString(msg_str) orelse return;
    appendError(errors, name_obj, msg);
}

// Inline email validation (same as dhi)
const FormatCheck = struct { valid: bool, name: [*c]const u8 };

// Dispatch a string-format code to its validator. Codes 5-8 (ipv6/base64/
// iso_date/iso_datetime) are defined (see CompiledFieldSpec.format_code) but not
// yet implemented, so the `else` arm FAILS CLOSED: an unknown/unimplemented
// format code rejects rather than silently accepting any string. This keeps
// future native-format wiring from bypassing validation unnoticed.
fn checkFormat(format_code: i32, str_val: [*c]const u8) FormatCheck {
    return switch (format_code) {
        1 => .{ .valid = inlineValidateEmail(str_val), .name = "email" },
        2 => .{ .valid = inlineValidateUrl(str_val), .name = "URL" },
        3 => .{ .valid = inlineValidateUuid(str_val), .name = "UUID" },
        4 => .{ .valid = inlineValidateIpv4(str_val), .name = "IPv4" },
        else => .{ .valid = false, .name = "unimplemented" },
    };
}

fn inlineValidateEmail(str_val: [*c]const u8) bool {
    if (str_val == null) return false;
    const s = std.mem.span(str_val);
    if (s.len == 0) return false;

    // Find @
    const at_pos = std.mem.indexOf(u8, s, "@") orelse return false;
    if (at_pos == 0) return false;

    // Domain part
    const domain = s[at_pos + 1 ..];
    if (domain.len == 0) return false;

    // Must have dot in domain, not at start/end
    const dot_pos = std.mem.indexOf(u8, domain, ".") orelse return false;
    if (dot_pos == 0 or dot_pos == domain.len - 1) return false;

    return true;
}

// Inline URL validation
fn inlineValidateUrl(str_val: [*c]const u8) bool {
    const s = std.mem.span(str_val);
    if (s.len < 8) return false;
    // Must start with http:// or https://
    if (std.mem.startsWith(u8, s, "https://") or std.mem.startsWith(u8, s, "http://")) {
        // Must have something after the scheme
        const after = if (std.mem.startsWith(u8, s, "https://")) s[8..] else s[7..];
        return after.len > 0;
    }
    return false;
}

// Inline UUID validation (8-4-4-4-12 hex format)
fn inlineValidateUuid(str_val: [*c]const u8) bool {
    const s = std.mem.span(str_val);
    if (s.len != 36) return false;
    // Check dash positions
    if (s[8] != '-' or s[13] != '-' or s[18] != '-' or s[23] != '-') return false;
    // Check all other chars are hex
    for (s, 0..) |ch, i| {
        if (i == 8 or i == 13 or i == 18 or i == 23) continue;
        if (!std.ascii.isHex(ch)) return false;
    }
    return true;
}

// Inline IPv4 validation
fn inlineValidateIpv4(str_val: [*c]const u8) bool {
    const s = std.mem.span(str_val);
    var parts: u8 = 0;
    var num: u16 = 0;
    var digits: u8 = 0;
    for (s) |ch| {
        if (ch == '.') {
            if (digits == 0 or num > 255) return false;
            parts += 1;
            num = 0;
            digits = 0;
        } else if (ch >= '0' and ch <= '9') {
            num = num * 10 + @as(u16, ch - '0');
            digits += 1;
            if (digits > 3) return false;
        } else return false;
    }
    if (digits == 0 or num > 255) return false;
    return parts == 3;
}

// ── PyCapsule destructor ─────────────────────────────────────────────────────

// Release the Python refs the capsule retained for each field spec (see the
// INCREFs in py_compile_model_specs). Mirrors that INCREF set exactly.
fn releaseSpecRefs(specs: []CompiledFieldSpec) void {
    for (specs) |*fs| {
        c.Py_DECREF(fs.name_obj);
        c.Py_DECREF(fs.alias_obj);
        c.Py_DECREF(fs.default_val);
        if (fs.nested_model_type) |nm| c.Py_DECREF(nm);
        if (fs.union_types_tuple) |ut| c.Py_DECREF(ut);
    }
}

fn capsuleDestructor(capsule: ?*c.PyObject) callconv(.c) void {
    if (capsule == null) return;
    const ptr = c.PyCapsule_GetPointer(capsule, "hyperdjango.compiled_specs");
    if (ptr) |p| {
        const ms: *CompiledModelSpecs = @ptrCast(@alignCast(p));
        releaseSpecRefs(ms.specs[0..ms.n_fields]);
        std.heap.c_allocator.free(ms.specs[0..ms.n_fields]);
        std.heap.c_allocator.destroy(ms);
    }
}

// ── compile_model_specs ──────────────────────────────────────────────────────
// Parses Python field spec tuples into native Zig structs at class definition.
// Returns a PyCapsule wrapping CompiledModelSpecs.

pub fn py_compile_model_specs(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var field_specs: ?*c.PyObject = null;
    if (c.PyArg_ParseTuple(args, "O!", &c.PyTuple_Type, &field_specs) == 0) return null;

    const specs_obj = field_specs.?;
    const n: usize = @intCast(c.PyTuple_Size(specs_obj));

    // Allocate specs array
    const specs = std.heap.c_allocator.alloc(CompiledFieldSpec, n) catch {
        _ = c.PyErr_NoMemory();
        return null;
    };

    for (0..n) |i| {
        const spec = c.PyTuple_GetItem(specs_obj, @as(c.Py_ssize_t, @intCast(i))).?;
        const spec_len = c.PyTuple_Size(spec);
        var fs = &specs[i];

        fs.name_obj = c.PyTuple_GetItem(spec, 0).?;
        fs.name_hash = c.PyObject_Hash(fs.name_obj);
        fs.alias_obj = c.PyTuple_GetItem(spec, 1).?;
        fs.required = c.PyObject_IsTrue(c.PyTuple_GetItem(spec, 2)) == 1;
        fs.default_val = c.PyTuple_GetItem(spec, 3).?;

        const constraints = c.PyTuple_GetItem(spec, 4).?;

        // Check for nested model type or union types (6th element)
        fs.nested_model_type = null;
        fs.union_types_tuple = null;
        if (spec_len >= 6) {
            const sixth = c.PyTuple_GetItem(spec, 5).?;
            if (!isNone(sixth)) {
                if (c.PyType_Check(sixth) != 0) {
                    fs.nested_model_type = sixth;
                } else if (c.PyTuple_Check(sixth) != 0) {
                    fs.union_types_tuple = sixth;
                }
            }
        }

        // Pre-parse constraint values
        fs.type_code = @intCast(c.PyLong_AsLong(c.PyTuple_GetItem(constraints, 0)));
        // Override type_code for nested fields
        if (fs.nested_model_type != null) {
            fs.type_code = 6;
        }
        // type_code 7/8 kept from constraints for list-of-models/union

        fs.strict = c.PyLong_AsLong(c.PyTuple_GetItem(constraints, 1)) != 0;

        const gt = c.PyTuple_GetItem(constraints, 2).?;
        const ge = c.PyTuple_GetItem(constraints, 3).?;
        const lt = c.PyTuple_GetItem(constraints, 4).?;
        const le = c.PyTuple_GetItem(constraints, 5).?;
        const mul = c.PyTuple_GetItem(constraints, 6).?;
        const minl = c.PyTuple_GetItem(constraints, 7).?;
        const maxl = c.PyTuple_GetItem(constraints, 8).?;

        fs.has_gt = !isNone(gt);
        fs.has_ge = !isNone(ge);
        fs.has_lt = !isNone(lt);
        fs.has_le = !isNone(le);
        fs.has_mul = !isNone(mul);
        fs.has_minl = !isNone(minl);
        fs.has_maxl = !isNone(maxl);

        fs.gt_long = if (fs.has_gt) asLongCoerce(gt) else 0;
        fs.ge_long = if (fs.has_ge) asLongCoerce(ge) else 0;
        fs.lt_long = if (fs.has_lt) asLongCoerce(lt) else 0;
        fs.le_long = if (fs.has_le) asLongCoerce(le) else 0;
        fs.mul_long = if (fs.has_mul) asLongCoerce(mul) else 0;
        fs.gt_dbl = if (fs.has_gt) asDoubleCoerce(gt) else 0.0;
        fs.ge_dbl = if (fs.has_ge) asDoubleCoerce(ge) else 0.0;
        fs.lt_dbl = if (fs.has_lt) asDoubleCoerce(lt) else 0.0;
        fs.le_dbl = if (fs.has_le) asDoubleCoerce(le) else 0.0;
        fs.mul_dbl = if (fs.has_mul) asDoubleCoerce(mul) else 0.0;
        fs.min_len = if (fs.has_minl) c.PyLong_AsSsize_t(minl) else 0;
        fs.max_len = if (fs.has_maxl) c.PyLong_AsSsize_t(maxl) else 0;

        fs.allow_inf_nan = c.PyLong_AsLong(c.PyTuple_GetItem(constraints, 9)) != 0;
        fs.format_code = @intCast(c.PyLong_AsLong(c.PyTuple_GetItem(constraints, 10)));
        fs.strip_ws = c.PyLong_AsLong(c.PyTuple_GetItem(constraints, 11)) != 0;
        fs.to_lower = c.PyLong_AsLong(c.PyTuple_GetItem(constraints, 12)) != 0;
        fs.to_upper = c.PyLong_AsLong(c.PyTuple_GetItem(constraints, 13)) != 0;

        // The capsule outlives the field-spec tuple passed in: model.py calls
        // compile_model_specs(tuple(native_init_specs)) with a TEMPORARY tuple
        // (not stored on the class), and its inline default / union-type-tuple
        // items are held only by that soon-freed tuple. PyTuple_GetItem returns
        // BORROWED refs, so retain everything the capsule keeps alive; the
        // capsule destructor (releaseSpecRefs) releases them.
        c.Py_INCREF(fs.name_obj);
        c.Py_INCREF(fs.alias_obj);
        c.Py_INCREF(fs.default_val);
        if (fs.nested_model_type) |nm| c.Py_INCREF(nm);
        if (fs.union_types_tuple) |ut| c.Py_INCREF(ut);
    }

    // Create wrapper struct
    const ms = std.heap.c_allocator.create(CompiledModelSpecs) catch {
        releaseSpecRefs(specs);
        std.heap.c_allocator.free(specs);
        _ = c.PyErr_NoMemory();
        return null;
    };
    ms.n_fields = n;
    ms.specs = specs.ptr;

    const capsule = c.PyCapsule_New(ms, "hyperdjango.compiled_specs", capsuleDestructor);
    if (capsule == null) {
        // Capsule creation failed → the destructor will never run, so release
        // the retained refs and the backing allocations here.
        releaseSpecRefs(ms.specs[0..ms.n_fields]);
        std.heap.c_allocator.free(ms.specs[0..ms.n_fields]);
        std.heap.c_allocator.destroy(ms);
        return null;
    }
    return capsule;
}

// ── init_model_full ──────────────────────────────────────────────────────────
// METH_FASTCALL: validates entire model in one native call.
// Args: (self, kwargs_dict, capsule, extra_mode_int)
// Returns: None on success, list of (field, msg) tuples on error.
// Sets __pydantic_fields_set__, __pydantic_private__, __pydantic_extra__ directly.

// Interned string keys (initialized once, cached forever)
var g_fields_set_key: ?*c.PyObject = null;
var g_extra_key: ?*c.PyObject = null;
var g_private_key: ?*c.PyObject = null;
var g_empty_tuple: ?*c.PyObject = null;

pub fn py_init_model_full(_: ?*c.PyObject, args: [*c]const ?*c.PyObject, nargs: c.Py_ssize_t) callconv(.c) ?*c.PyObject {
    if (nargs != 4) {
        c.PyErr_SetString(c.PyExc_TypeError, "init_model_full requires 4 arguments");
        return null;
    }

    const model_self = args[0] orelse {
        c.PyErr_SetString(c.PyExc_TypeError, "self is NULL");
        return null;
    };
    const kwargs = args[1] orelse {
        c.PyErr_SetString(c.PyExc_TypeError, "kwargs is NULL");
        return null;
    };
    const capsule = args[2] orelse {
        c.PyErr_SetString(c.PyExc_TypeError, "capsule is NULL");
        return null;
    };
    const extra_mode_obj = args[3] orelse {
        c.PyErr_SetString(c.PyExc_TypeError, "extra_mode is NULL");
        return null;
    };

    if (c.PyDict_Check(kwargs) == 0) {
        c.PyErr_SetString(c.PyExc_TypeError, "kwargs must be a dict");
        return null;
    }

    const extra_mode: i32 = @intCast(c.PyLong_AsLong(extra_mode_obj));

    const ms_ptr = c.PyCapsule_GetPointer(capsule, "hyperdjango.compiled_specs") orelse return null;
    const ms: *CompiledModelSpecs = @ptrCast(@alignCast(ms_ptr));

    const obj_dict = c.PyObject_GenericGetDict(model_self, null) orelse return null;
    defer c.Py_DECREF(obj_dict);

    // Multi-word "field seen" bitset — tracks fields ≥64 (a plain u64 could not).
    const bitset_words = std.heap.c_allocator.alloc(u64, @max(1, bitsetWordCount(ms.n_fields))) catch {
        _ = c.PyErr_NoMemory();
        return null;
    };
    defer std.heap.c_allocator.free(bitset_words);
    @memset(bitset_words, 0);

    var errors: ?*c.PyObject = null;
    var found_count: c.Py_ssize_t = 0;
    const kwargs_size = c.PyDict_Size(kwargs);

    var i: usize = 0;
    while (i < ms.n_fields) : (i += 1) {
        const fs = &ms.specs[i];

        // --- Extract value from kwargs ---
        var value: ?*c.PyObject = null;
        if (!isNone(fs.alias_obj)) {
            value = c.PyDict_GetItem(kwargs, fs.alias_obj);
        }
        if (value == null) {
            value = c._PyDict_GetItem_KnownHash(kwargs, fs.name_obj, fs.name_hash);
        }

        if (value == null) {
            if (!fs.required) {
                _ = c.PyDict_SetItem(obj_dict, fs.name_obj, fs.default_val);
                continue;
            }
            appendErrorStr(&errors, fs.name_obj, "Field required");
            continue;
        }

        found_count += 1;
        bitsetSet(bitset_words, i);

        // --- Type checking + validation ---
        const result: *c.PyObject = value.?;
        c.Py_INCREF(result);

        const validated = validateField(fs, result, &errors);
        if (validated) |v| {
            _ = c.PyDict_SetItem(obj_dict, fs.name_obj, v);
            c.Py_DECREF(v);
        }
        // if null, error was already appended
    }

    // --- Handle extra fields ---
    var extra_data: ?*c.PyObject = null;
    if (extra_mode != 0 and found_count < kwargs_size) {
        var key: ?*c.PyObject = null;
        var val: ?*c.PyObject = null;
        var pos: c.Py_ssize_t = 0;
        while (c.PyDict_Next(kwargs, &pos, &key, &val) != 0) {
            var is_known = false;
            var j: usize = 0;
            while (j < ms.n_fields) : (j += 1) {
                const fs = &ms.specs[j];
                if (c.PyObject_RichCompareBool(key, fs.name_obj, c.Py_EQ) == 1) {
                    is_known = true;
                    break;
                }
                if (!isNone(fs.alias_obj) and c.PyObject_RichCompareBool(key, fs.alias_obj, c.Py_EQ) == 1) {
                    is_known = true;
                    break;
                }
            }
            if (!is_known) {
                if (extra_mode == 1) { // forbid
                    appendErrorStr(&errors, key.?, "Extra inputs are not permitted");
                } else if (extra_mode == 2) { // allow
                    if (extra_data == null) {
                        extra_data = c.PyDict_New();
                    }
                    if (extra_data) |ed| {
                        _ = c.PyDict_SetItem(ed, key, val);
                    }
                }
            }
        }
    }

    // --- Set pydantic internal attributes ---
    if (g_fields_set_key == null) {
        g_fields_set_key = c.PyUnicode_InternFromString("__pydantic_fields_set__");
        g_extra_key = c.PyUnicode_InternFromString("__pydantic_extra__");
        g_private_key = c.PyUnicode_InternFromString("__pydantic_private__");
    }

    // Build fields_set from bitmask
    const fields_set = c.PySet_New(null) orelse {
        if (extra_data) |ed| c.Py_DECREF(ed);
        if (errors) |e| c.Py_DECREF(e);
        return null;
    };
    {
        var j: usize = 0;
        while (j < ms.n_fields) : (j += 1) {
            if (bitsetIsSet(bitset_words, j)) {
                _ = c.PySet_Add(fields_set, ms.specs[j].name_obj);
            }
        }
    }

    // Re-get obj_dict for setting pydantic attrs
    const obj_dict2 = c.PyObject_GenericGetDict(model_self, null) orelse {
        c.Py_DECREF(fields_set);
        if (extra_data) |ed| c.Py_DECREF(ed);
        if (errors) |e| c.Py_DECREF(e);
        return null;
    };
    defer c.Py_DECREF(obj_dict2);

    _ = c.PyDict_SetItem(obj_dict2, g_fields_set_key, fields_set);
    c.Py_DECREF(fields_set);

    if (extra_data) |ed| {
        _ = c.PyDict_SetItem(obj_dict2, g_extra_key, ed);
        c.Py_DECREF(ed);
    } else {
        _ = c.PyDict_SetItem(obj_dict2, g_extra_key.?, pyNone());
    }
    _ = c.PyDict_SetItem(obj_dict2, g_private_key.?, pyNone());

    // Return errors or None
    if (errors) |e| {
        if (c.PyList_Size(e) > 0) return e;
        c.Py_DECREF(e);
    }
    return py.pyNone();
}

// ── Per-field validation (called from init_model_full loop) ──────────────────
// Returns validated (possibly transformed) value, or null on error (error appended to errors list)

pub fn validateField(fs: *const CompiledFieldSpec, result_in: *c.PyObject, errors: *?*c.PyObject) ?*c.PyObject {
    var result = result_in;

    // --- TYPE CHECKING ---
    switch (fs.type_code) {
        1 => { // int
            if (fs.strict) {
                if (c.PyLong_CheckExact(result) == 0 or c.PyBool_Check(result) != 0) {
                    const tname = c.Py_TYPE(result).*.tp_name;
                    c.Py_DECREF(result);
                    const msg = c.PyUnicode_FromFormat(
                        "Expected exactly int, got %s",
                        tname,
                    );
                    if (msg) |m| appendError(errors, fs.name_obj, m);
                    return null;
                }
            } else {
                if (c.PyBool_Check(result) != 0) {
                    c.Py_DECREF(result);
                    const msg = c.PyUnicode_FromFormat("Expected int, got bool");
                    if (msg) |m| appendError(errors, fs.name_obj, m);
                    return null;
                }
                if (c.PyLong_Check(result) == 0) {
                    if (c.PyFloat_Check(result) != 0) {
                        // Reject fractional / non-finite floats for int fields
                        // instead of silently truncating (1.5 -> 1). Whole-valued
                        // floats such as 5.0 -> 5 are still accepted.
                        const dval = c.PyFloat_AsDouble(result);
                        if (!std.math.isFinite(dval) or dval != std.math.floor(dval)) {
                            c.Py_DECREF(result);
                            const msg = c.PyUnicode_FromFormat("Expected int, got float with fractional part");
                            if (msg) |m| appendError(errors, fs.name_obj, m);
                            return null;
                        }
                        const new_val = c.PyNumber_Long(result);
                        if (new_val == null) {
                            c.PyErr_Clear();
                            c.Py_DECREF(result);
                            const msg = c.PyUnicode_FromFormat("Cannot convert float to int");
                            if (msg) |m| appendError(errors, fs.name_obj, m);
                            return null;
                        }
                        c.Py_DECREF(result);
                        result = new_val.?;
                    } else {
                        const tname = c.Py_TYPE(result).*.tp_name;
                        c.Py_DECREF(result);
                        const msg = c.PyUnicode_FromFormat(
                            "Expected int, got %s",
                            tname,
                        );
                        if (msg) |m| appendError(errors, fs.name_obj, m);
                        return null;
                    }
                }
            }
        },
        2 => { // float
            if (fs.strict) {
                if (c.PyFloat_CheckExact(result) == 0) {
                    const tname = c.Py_TYPE(result).*.tp_name;
                    c.Py_DECREF(result);
                    const msg = c.PyUnicode_FromFormat(
                        "Expected exactly float, got %s",
                        tname,
                    );
                    if (msg) |m| appendError(errors, fs.name_obj, m);
                    return null;
                }
            } else {
                if (c.PyBool_Check(result) != 0) {
                    c.Py_DECREF(result);
                    const msg = c.PyUnicode_FromFormat("Expected float, got bool");
                    if (msg) |m| appendError(errors, fs.name_obj, m);
                    return null;
                }
                if (c.PyFloat_Check(result) == 0) {
                    if (c.PyLong_Check(result) != 0) {
                        const new_val = c.PyNumber_Float(result);
                        if (new_val == null) {
                            c.PyErr_Clear();
                            c.Py_DECREF(result);
                            const msg = c.PyUnicode_FromFormat("Cannot convert int to float");
                            if (msg) |m| appendError(errors, fs.name_obj, m);
                            return null;
                        }
                        c.Py_DECREF(result);
                        result = new_val.?;
                    } else {
                        const tname = c.Py_TYPE(result).*.tp_name;
                        c.Py_DECREF(result);
                        const msg = c.PyUnicode_FromFormat(
                            "Expected float, got %s",
                            tname,
                        );
                        if (msg) |m| appendError(errors, fs.name_obj, m);
                        return null;
                    }
                }
            }
        },
        3 => { // str
            if (c.PyUnicode_Check(result) == 0) {
                const tname = c.Py_TYPE(result).*.tp_name;
                c.Py_DECREF(result);
                const msg = c.PyUnicode_FromFormat(
                    "Expected str, got %s",
                    tname,
                );
                if (msg) |m| appendError(errors, fs.name_obj, m);
                return null;
            }
        },
        4 => { // bool
            if (c.PyBool_Check(result) == 0) {
                const tname = c.Py_TYPE(result).*.tp_name;
                c.Py_DECREF(result);
                const msg = c.PyUnicode_FromFormat(
                    "Expected bool, got %s",
                    tname,
                );
                if (msg) |m| appendError(errors, fs.name_obj, m);
                return null;
            }
        },
        5 => { // bytes
            if (c.PyBytes_Check(result) == 0) {
                const tname = c.Py_TYPE(result).*.tp_name;
                c.Py_DECREF(result);
                const msg = c.PyUnicode_FromFormat(
                    "Expected bytes, got %s",
                    tname,
                );
                if (msg) |m| appendError(errors, fs.name_obj, m);
                return null;
            }
        },
        6 => { // nested model
            return validateNestedModel(fs, result, errors);
        },
        7 => { // list of models
            return validateListOfModels(fs, result, errors);
        },
        8 => { // union of model types
            return validateUnionOfModels(fs, result, errors);
        },
        else => {},
    }

    // --- STRING TRANSFORMS ---
    if (c.PyUnicode_Check(result) != 0) {
        if (fs.strip_ws) {
            const s = c.PyObject_CallMethod(result, "strip", null);
            if (s == null) {
                // Clear the pending exception: the caller keeps validating other
                // fields after a null return, so a leftover error would corrupt a
                // later C-API call.
                c.PyErr_Clear();
                c.Py_DECREF(result);
                return null;
            }
            c.Py_DECREF(result);
            result = s.?;
        }
        if (fs.to_lower) {
            const s = c.PyObject_CallMethod(result, "lower", null);
            if (s == null) {
                c.PyErr_Clear();
                c.Py_DECREF(result);
                return null;
            }
            c.Py_DECREF(result);
            result = s.?;
        }
        if (fs.to_upper) {
            const s = c.PyObject_CallMethod(result, "upper", null);
            if (s == null) {
                c.PyErr_Clear();
                c.Py_DECREF(result);
                return null;
            }
            c.Py_DECREF(result);
            result = s.?;
        }
    }

    // --- NUMERIC CONSTRAINTS (inlined) ---
    if (c.PyLong_Check(result) != 0 and c.PyBool_Check(result) == 0) {
        const val = c.PyLong_AsLong(result);
        if (val == -1 and c.PyErr_Occurred() != null) {
            // Huge int overflows C long. Clear the pending OverflowError so it
            // can't corrupt the next C-API call, and treat as unconstrained: the
            // gt/ge/lt/le/mul bounds are all C-long-sized, so a value outside
            // that range cannot meaningfully violate them here.
            c.PyErr_Clear();
        } else {
            if (fs.has_gt and val <= fs.gt_long) {
            c.Py_DECREF(result);
            const msg = c.PyUnicode_FromFormat("Value must be > %ld, got %ld", fs.gt_long, val);
            if (msg) |m| appendError(errors, fs.name_obj, m);
            return null;
        }
        if (fs.has_ge and val < fs.ge_long) {
            c.Py_DECREF(result);
            const msg = c.PyUnicode_FromFormat("Value must be >= %ld, got %ld", fs.ge_long, val);
            if (msg) |m| appendError(errors, fs.name_obj, m);
            return null;
        }
        if (fs.has_lt and val >= fs.lt_long) {
            c.Py_DECREF(result);
            const msg = c.PyUnicode_FromFormat("Value must be < %ld, got %ld", fs.lt_long, val);
            if (msg) |m| appendError(errors, fs.name_obj, m);
            return null;
        }
        if (fs.has_le and val > fs.le_long) {
            c.Py_DECREF(result);
            const msg = c.PyUnicode_FromFormat("Value must be <= %ld, got %ld", fs.le_long, val);
            if (msg) |m| appendError(errors, fs.name_obj, m);
            return null;
        }
        // Defense in depth: Python-side MultipleOf rejects 0 at construction,
        // but guard here too — @mod(x, 0) is undefined in ReleaseFast and panics in Debug.
        if (fs.has_mul and fs.mul_long != 0 and @mod(val, fs.mul_long) != 0) {
            c.Py_DECREF(result);
            const msg = c.PyUnicode_FromFormat("Value must be a multiple of %ld, got %ld", fs.mul_long, val);
            if (msg) |m| appendError(errors, fs.name_obj, m);
            return null;
        }
        }
    } else if (c.PyFloat_Check(result) != 0) {
        const val = c.PyFloat_AsDouble(result);
        if (!fs.allow_inf_nan and !std.math.isFinite(val)) {
            c.Py_DECREF(result);
            const msg = c.PyUnicode_FromFormat("Value must be finite");
            if (msg) |m| appendError(errors, fs.name_obj, m);
            return null;
        }
        var failed: i32 = 0;
        if (fs.has_gt and val <= fs.gt_dbl) failed = 1;
        if (failed == 0 and fs.has_ge and val < fs.ge_dbl) failed = 2;
        if (failed == 0 and fs.has_lt and val >= fs.lt_dbl) failed = 3;
        if (failed == 0 and fs.has_le and val > fs.le_dbl) failed = 4;
        if (failed == 0 and fs.has_mul and fs.mul_dbl != 0.0) {
            const remainder = @mod(val, fs.mul_dbl);
            if (remainder != 0.0 and @abs(remainder) > 1e-9) failed = 5;
        }
        if (failed != 0) {
            c.Py_DECREF(result);
            // Format error using Zig fmt — each branch is comptime-known
            var buf: [256]u8 = undefined;
            const formatted: []const u8 = switch (failed) {
                1 => std.fmt.bufPrint(&buf, "Value must be > {d}, got {d}", .{ fs.gt_dbl, val }) catch "Validation failed",
                2 => std.fmt.bufPrint(&buf, "Value must be >= {d}, got {d}", .{ fs.ge_dbl, val }) catch "Validation failed",
                3 => std.fmt.bufPrint(&buf, "Value must be < {d}, got {d}", .{ fs.lt_dbl, val }) catch "Validation failed",
                4 => std.fmt.bufPrint(&buf, "Value must be <= {d}, got {d}", .{ fs.le_dbl, val }) catch "Validation failed",
                5 => std.fmt.bufPrint(&buf, "Value must be a multiple of {d}, got {d}", .{ fs.mul_dbl, val }) catch "Validation failed",
                else => "Validation failed",
            };
            const msg = c.PyUnicode_FromStringAndSize(formatted.ptr, @intCast(formatted.len));
            if (msg) |m| appendError(errors, fs.name_obj, m);
            return null;
        }
    }

    // --- LENGTH CONSTRAINTS ---
    if (fs.has_minl or fs.has_maxl) {
        const length = c.PyObject_Length(result);
        if (length == -1 and c.PyErr_Occurred() != null) {
            c.Py_DECREF(result);
            c.PyErr_Clear();
            return null;
        }
        if (fs.has_minl and length < fs.min_len) {
            c.Py_DECREF(result);
            const msg = c.PyUnicode_FromFormat("Length must be >= %zd, got %zd", fs.min_len, length);
            if (msg) |m| appendError(errors, fs.name_obj, m);
            return null;
        }
        if (fs.has_maxl and length > fs.max_len) {
            c.Py_DECREF(result);
            const msg = c.PyUnicode_FromFormat("Length must be <= %zd, got %zd", fs.max_len, length);
            if (msg) |m| appendError(errors, fs.name_obj, m);
            return null;
        }
    }

    // --- FORMAT VALIDATION ---
    if (fs.format_code > 0 and c.PyUnicode_Check(result) != 0) {
        const str_val = c.PyUnicode_AsUTF8(result);
        if (str_val == null) {
            // Surrogate/non-UTF8 str — clear the pending exception so it can't
            // corrupt the caller's next field validation.
            c.PyErr_Clear();
            c.Py_DECREF(result);
            return null;
        }
        const fc = checkFormat(fs.format_code, str_val);
        if (!fc.valid) {
            c.Py_DECREF(result);
            const msg = c.PyUnicode_FromFormat("Invalid %s format", fc.name);
            if (msg) |m| appendError(errors, fs.name_obj, m);
            return null;
        }
    }

    return result;
}

// ── Nested model validation ──────────────────────────────────────────────────

fn validateNestedModel(fs: *const CompiledFieldSpec, result: *c.PyObject, errors: *?*c.PyObject) ?*c.PyObject {
    const model_type = fs.nested_model_type orelse {
        c.Py_DECREF(result);
        return null;
    };

    // Already correct type? Just pass through
    if (@as(*c.PyObject, @ptrCast(c.Py_TYPE(result))) == model_type) {
        return result;
    }

    // Dict → create nested model
    if (c.PyDict_Check(result) != 0) {
        if (g_empty_tuple == null) {
            g_empty_tuple = c.PyTuple_New(0);
            if (g_empty_tuple == null) {
                c.Py_DECREF(result);
                return null;
            }
        }
        const nested_obj = c.PyObject_Call(model_type, g_empty_tuple, result);
        c.Py_DECREF(result);
        if (nested_obj == null) {
            // Extract nested validation error
            var exc_type: ?*c.PyObject = null;
            var exc_value: ?*c.PyObject = null;
            var exc_tb: ?*c.PyObject = null;
            c.PyErr_Fetch(&exc_type, &exc_value, &exc_tb);
            if (exc_value) |ev| {
                const err_str = c.PyObject_Str(ev);
                const msg = c.PyUnicode_FromFormat("%S", err_str);
                if (err_str) |es| c.Py_DECREF(es);
                if (msg) |m| appendError(errors, fs.name_obj, m);
            }
            if (exc_type) |et| c.Py_DECREF(et);
            if (exc_value) |ev| c.Py_DECREF(ev);
            if (exc_tb) |tb| c.Py_DECREF(tb);
            return null;
        }
        return nested_obj;
    }

    // Wrong type
    const expected_name = @as(*c.PyTypeObject, @ptrCast(@alignCast(model_type))).*.tp_name;
    const tname = c.Py_TYPE(result).*.tp_name;
    c.Py_DECREF(result);
    const msg = c.PyUnicode_FromFormat(
        "Expected %s or dict, got %s",
        expected_name,
        tname,
    );
    if (msg) |m| appendError(errors, fs.name_obj, m);
    return null;
}

// ── List-of-models validation ────────────────────────────────────────────────

fn validateListOfModels(fs: *const CompiledFieldSpec, result: *c.PyObject, errors: *?*c.PyObject) ?*c.PyObject {
    if (c.PyList_Check(result) == 0) {
        const tname = c.Py_TYPE(result).*.tp_name;
        c.Py_DECREF(result);
        const msg = c.PyUnicode_FromFormat("Expected list, got %s", tname);
        if (msg) |m| appendError(errors, fs.name_obj, m);
        return null;
    }

    // Length constraints
    const list_len = c.PyList_Size(result);
    if (fs.has_minl and list_len < fs.min_len) {
        c.Py_DECREF(result);
        const msg = c.PyUnicode_FromFormat("Length must be >= %zd, got %zd", fs.min_len, list_len);
        if (msg) |m| appendError(errors, fs.name_obj, m);
        return null;
    }
    if (fs.has_maxl and list_len > fs.max_len) {
        c.Py_DECREF(result);
        const msg = c.PyUnicode_FromFormat("Length must be <= %zd, got %zd", fs.max_len, list_len);
        if (msg) |m| appendError(errors, fs.name_obj, m);
        return null;
    }

    // Check for dict items that need coercion
    const union_types = fs.union_types_tuple orelse return result;
    var has_dicts = false;
    var j: c.Py_ssize_t = 0;
    while (j < list_len) : (j += 1) {
        const item = c.PyList_GetItem(result, j);
        if (c.PyDict_Check(item) != 0) {
            has_dicts = true;
            break;
        }
    }

    if (!has_dicts) return result;

    // Coerce dict items
    if (g_empty_tuple == null) {
        g_empty_tuple = c.PyTuple_New(0);
        if (g_empty_tuple == null) {
            c.Py_DECREF(result);
            return null;
        }
    }

    const new_list = c.PyList_New(list_len) orelse {
        c.Py_DECREF(result);
        return null;
    };

    j = 0;
    while (j < list_len) : (j += 1) {
        const item = c.PyList_GetItem(result, j).?;
        if (c.PyDict_Check(item) != 0) {
            // Try each union type
            const n_types = c.PyTuple_Size(union_types);
            var coerced: ?*c.PyObject = null;
            var t: c.Py_ssize_t = 0;
            while (t < n_types) : (t += 1) {
                const model_type = c.PyTuple_GetItem(union_types, t);
                coerced = c.PyObject_Call(model_type, g_empty_tuple, item);
                if (coerced != null) break;
                c.PyErr_Clear();
            }
            if (coerced) |co| {
                _ = c.PyList_SetItem(new_list, j, co); // steals ref
            } else {
                const msg = c.PyUnicode_FromFormat("Item %zd: cannot coerce dict to model", j);
                if (msg) |m| appendError(errors, fs.name_obj, m);
                c.Py_INCREF(item);
                _ = c.PyList_SetItem(new_list, j, item);
            }
        } else {
            c.Py_INCREF(item);
            _ = c.PyList_SetItem(new_list, j, item);
        }
    }

    c.Py_DECREF(result);
    return new_list;
}

// ── Union-of-models validation ───────────────────────────────────────────────

fn validateUnionOfModels(fs: *const CompiledFieldSpec, result: *c.PyObject, errors: *?*c.PyObject) ?*c.PyObject {
    const union_types = fs.union_types_tuple orelse {
        c.Py_DECREF(result);
        return null;
    };

    // Check if already an instance of any union type
    if (c.PyObject_IsInstance(result, union_types) == 1) {
        return result;
    }

    // Try dict coercion
    if (c.PyDict_Check(result) != 0) {
        if (g_empty_tuple == null) {
            g_empty_tuple = c.PyTuple_New(0);
            if (g_empty_tuple == null) {
                c.Py_DECREF(result);
                return null;
            }
        }
        const n_types = c.PyTuple_Size(union_types);
        var t: c.Py_ssize_t = 0;
        while (t < n_types) : (t += 1) {
            const model_type = c.PyTuple_GetItem(union_types, t);
            const coerced = c.PyObject_Call(model_type, g_empty_tuple, result);
            if (coerced) |co| {
                c.Py_DECREF(result);
                return co;
            }
            c.PyErr_Clear();
        }
    }

    c.Py_DECREF(result);
    const msg = c.PyUnicode_FromFormat("Value does not match any expected type");
    if (msg) |m| appendError(errors, fs.name_obj, m);
    return null;
}

// ── validate_field ───────────────────────────────────────────────────────────
// Per-field validation: validate_field(value, field_name, constraints_tuple)
// constraints_tuple: (type_code, strict, gt, ge, lt, le, multiple_of,
//                     min_len, max_len, allow_inf_nan, format_code,
//                     strip_ws, to_lower, to_upper)
// Returns: validated (possibly transformed) value
// Raises: ValueError on validation failure

pub fn py_validate_field(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var value: ?*c.PyObject = null;
    var field_name_obj: ?*c.PyObject = null;
    var constraints: ?*c.PyObject = null;
    if (c.PyArg_ParseTuple(args, "OOO!", &value, &field_name_obj, &c.PyTuple_Type, &constraints) == 0)
        return null;

    const val = value.?;
    const cons = constraints.?;
    const fname = field_name_obj.?;

    // Parse constraints tuple
    var fs: CompiledFieldSpec = undefined;
    fs.name_obj = fname;
    fs.name_hash = 0;
    fs.alias_obj = pyNone();
    fs.required = true;
    fs.default_val = pyNone();

    fs.type_code = @intCast(c.PyLong_AsLong(c.PyTuple_GetItem(cons, 0)));
    fs.strict = c.PyLong_AsLong(c.PyTuple_GetItem(cons, 1)) != 0;

    const gt = c.PyTuple_GetItem(cons, 2).?;
    const ge = c.PyTuple_GetItem(cons, 3).?;
    const lt = c.PyTuple_GetItem(cons, 4).?;
    const le = c.PyTuple_GetItem(cons, 5).?;
    const mul = c.PyTuple_GetItem(cons, 6).?;
    const minl = c.PyTuple_GetItem(cons, 7).?;
    const maxl = c.PyTuple_GetItem(cons, 8).?;

    fs.has_gt = !isNone(gt);
    fs.has_ge = !isNone(ge);
    fs.has_lt = !isNone(lt);
    fs.has_le = !isNone(le);
    fs.has_mul = !isNone(mul);
    fs.has_minl = !isNone(minl);
    fs.has_maxl = !isNone(maxl);

    fs.gt_long = if (fs.has_gt) asLongCoerce(gt) else 0;
    fs.ge_long = if (fs.has_ge) asLongCoerce(ge) else 0;
    fs.lt_long = if (fs.has_lt) asLongCoerce(lt) else 0;
    fs.le_long = if (fs.has_le) asLongCoerce(le) else 0;
    fs.mul_long = if (fs.has_mul) asLongCoerce(mul) else 0;
    fs.gt_dbl = if (fs.has_gt) asDoubleCoerce(gt) else 0.0;
    fs.ge_dbl = if (fs.has_ge) asDoubleCoerce(ge) else 0.0;
    fs.lt_dbl = if (fs.has_lt) asDoubleCoerce(lt) else 0.0;
    fs.le_dbl = if (fs.has_le) asDoubleCoerce(le) else 0.0;
    fs.mul_dbl = if (fs.has_mul) asDoubleCoerce(mul) else 0.0;
    fs.min_len = if (fs.has_minl) c.PyLong_AsSsize_t(minl) else 0;
    fs.max_len = if (fs.has_maxl) c.PyLong_AsSsize_t(maxl) else 0;

    fs.allow_inf_nan = c.PyLong_AsLong(c.PyTuple_GetItem(cons, 9)) != 0;
    fs.format_code = @intCast(c.PyLong_AsLong(c.PyTuple_GetItem(cons, 10)));
    fs.strip_ws = c.PyLong_AsLong(c.PyTuple_GetItem(cons, 11)) != 0;
    fs.to_lower = c.PyLong_AsLong(c.PyTuple_GetItem(cons, 12)) != 0;
    fs.to_upper = c.PyLong_AsLong(c.PyTuple_GetItem(cons, 13)) != 0;
    fs.nested_model_type = null;
    fs.union_types_tuple = null;

    // Validate using the same core function as init_model_full
    c.Py_INCREF(val);
    var errors: ?*c.PyObject = null;
    const result = validateField(&fs, val, &errors);

    if (result) |r| {
        // Success — clean up any empty error list
        if (errors) |e| c.Py_DECREF(e);
        return r;
    }

    // Validation failed — raise ValueError with the error message
    if (errors) |e| {
        if (c.PyList_Size(e) > 0) {
            const err_tuple = c.PyList_GetItem(e, 0); // borrowed
            if (err_tuple) |et| {
                const err_msg = c.PyTuple_GetItem(et, 1); // borrowed
                if (err_msg) |em| {
                    c.PyErr_SetObject(c.PyExc_ValueError, em);
                }
            }
        }
        c.Py_DECREF(e);
    } else {
        c.PyErr_SetString(c.PyExc_ValueError, "Validation failed");
    }
    return null;
}

// ── dump_model_compiled ──────────────────────────────────────────────────────
// Ultra-fast model_dump using pre-compiled specs.
// dump_model_compiled(self, capsule) -> dict
// Recursively dumps nested models and lists of models.

pub fn py_dump_model_compiled(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var model_self: ?*c.PyObject = null;
    var capsule: ?*c.PyObject = null;
    if (c.PyArg_ParseTuple(args, "OO", &model_self, &capsule) == 0)
        return null;

    return dumpModelRecursive(model_self.?);
}

fn dumpModelRecursive(model_self: *c.PyObject) ?*c.PyObject {
    const obj_dict = c.PyObject_GenericGetDict(model_self, null) orelse return null;
    defer c.Py_DECREF(obj_dict);

    // Get compiled specs from the model's class
    const compiled_attr = c.PyObject_GetAttrString(
        @as(*c.PyObject, @ptrCast(c.Py_TYPE(model_self))),
        "__dhi_compiled_specs__",
    );
    if (compiled_attr == null or isNone(compiled_attr.?)) {
        if (compiled_attr) |ca| c.Py_DECREF(ca);
        // Fallback: call Python model_dump
        return c.PyObject_CallMethod(model_self, "model_dump", null);
    }
    defer c.Py_DECREF(compiled_attr.?);

    const ms_ptr = c.PyCapsule_GetPointer(compiled_attr.?, "hyperdjango.compiled_specs") orelse {
        return c.PyObject_CallMethod(model_self, "model_dump", null);
    };
    const ms: *CompiledModelSpecs = @ptrCast(@alignCast(ms_ptr));

    const result = c.PyDict_New() orelse return null;

    var i: usize = 0;
    while (i < ms.n_fields) : (i += 1) {
        const fs = &ms.specs[i];
        const value = c.PyDict_GetItem(obj_dict, fs.name_obj) orelse continue;

        if (fs.type_code == 6 or fs.type_code == 7 or fs.type_code == 8) {
            // Nested model, list of models, or union — recurse
            const dumped = dumpValueRecursive(value) orelse {
                c.Py_DECREF(result);
                return null;
            };
            _ = c.PyDict_SetItem(result, fs.name_obj, dumped);
            c.Py_DECREF(dumped);
        } else {
            // Simple value — direct copy
            _ = c.PyDict_SetItem(result, fs.name_obj, value);
        }
    }

    return result;
}

fn dumpValueRecursive(value: *c.PyObject) ?*c.PyObject {
    // Check if value is a BaseModel (has __dhi_fields__)
    if (c.PyObject_HasAttrString(value, "__dhi_fields__") == 1) {
        return dumpModelRecursive(value);
    }

    // Check if value is a list — recurse into items
    if (c.PyList_Check(value) != 0) {
        const len = c.PyList_Size(value);
        const new_list = c.PyList_New(len) orelse return null;
        var j: c.Py_ssize_t = 0;
        while (j < len) : (j += 1) {
            const item = c.PyList_GetItem(value, j).?; // borrowed
            var dumped: *c.PyObject = undefined;
            if (c.PyObject_HasAttrString(item, "__dhi_fields__") == 1) {
                dumped = dumpModelRecursive(item) orelse {
                    c.Py_DECREF(new_list);
                    return null;
                };
            } else {
                c.Py_INCREF(item);
                dumped = item;
            }
            _ = c.PyList_SetItem(new_list, j, dumped); // steals ref
        }
        return new_list;
    }

    // Simple value — return as-is
    c.Py_INCREF(value);
    return value;
}

// ── JSON → Model (single pass, no intermediate dict) ─────────────────────────
//
// json_loads_model: parses JSON bytes directly into a validated model instance,
// skipping the intermediate Python dict. Compares JSON key bytes directly against
// compiled field spec names using byte comparison — no Python string creation or
// hash lookup for known fields.
//
// Flow: JSON bytes → SIMD parse → type check → validate → store on __dict__
// vs old: JSON → dict → init_model_full → store on __dict__

const simd = @import("dhi/simd_json_parser.zig");
const json_parser = @import("json_parser.zig");

/// Pre-extracted field name bytes for O(1) byte comparison against JSON keys.
/// Cached per-spec to avoid repeated PyUnicode_AsUTF8AndSize calls.
const FieldNameCache = struct {
    ptr: [*]const u8,
    len: usize,
};

fn getFieldNameBytes(spec: *const CompiledFieldSpec) FieldNameCache {
    var len: c.Py_ssize_t = 0;
    const ptr = c.PyUnicode_AsUTF8AndSize(spec.name_obj, &len);
    if (ptr == null) return .{ .ptr = "".ptr, .len = 0 };
    return .{ .ptr = @ptrCast(ptr.?), .len = @intCast(len) };
}

fn getAliasBytes(spec: *const CompiledFieldSpec) ?FieldNameCache {
    if (isNone(spec.alias_obj)) return null;
    var len: c.Py_ssize_t = 0;
    const ptr = c.PyUnicode_AsUTF8AndSize(spec.alias_obj, &len);
    if (ptr == null) return null;
    return .{ .ptr = @ptrCast(ptr.?), .len = @intCast(len) };
}

/// Match a JSON key (raw bytes) against compiled field specs.
/// Returns field index or null if no match.
fn matchFieldSpec(key: []const u8, ms: *const CompiledModelSpecs, name_cache: []const FieldNameCache, alias_cache: []const ?FieldNameCache) ?usize {
    for (0..ms.n_fields) |i| {
        // Check alias first (if present)
        if (alias_cache[i]) |ac| {
            if (ac.len == key.len and std.mem.eql(u8, key, ac.ptr[0..ac.len])) return i;
        }
        // Then check name
        const nc = name_cache[i];
        if (nc.len == key.len and std.mem.eql(u8, key, nc.ptr[0..nc.len])) return i;
    }
    return null;
}

/// json_loads_model(json_bytes, model_self, capsule, extra_mode) → None | [(field, msg)]
///
/// Single-pass JSON → validated model. Parses JSON bytes directly into model
/// instance __dict__, skipping the intermediate Python dict. Matches JSON keys
/// against compiled field specs via byte comparison — no Python string creation
/// or hash lookup for known fields.
///
/// Uses json_parser.parseValue (with shared position pointer) for value parsing,
/// ensuring exactly one parse pass with consistent position tracking.
pub fn py_json_loads_model(_: ?*c.PyObject, args: [*c]const ?*c.PyObject, nargs: c.Py_ssize_t) callconv(.c) ?*c.PyObject {
    if (nargs != 4) {
        c.PyErr_SetString(c.PyExc_TypeError, "json_loads_model requires 4 arguments: json_bytes, model_self, capsule, extra_mode");
        return null;
    }

    const json_obj = args[0] orelse return null;
    const model_self = args[1] orelse return null;
    const capsule = args[2] orelse return null;
    const extra_mode_obj = args[3] orelse return null;

    // Extract JSON bytes
    var json_len: c.Py_ssize_t = 0;
    const json_ptr: ?[*]const u8 = if (c.PyBytes_Check(json_obj) != 0)
        @ptrCast(c.PyBytes_AsString(json_obj))
    else if (c.PyUnicode_Check(json_obj) != 0)
        @ptrCast(c.PyUnicode_AsUTF8AndSize(json_obj, &json_len))
    else {
        c.PyErr_SetString(c.PyExc_TypeError, "json_loads_model: first argument must be str or bytes");
        return null;
    };
    if (json_ptr == null) return null;

    if (c.PyBytes_Check(json_obj) != 0) {
        json_len = c.PyBytes_Size(json_obj);
    }
    const json = (json_ptr.?)[0..@intCast(json_len)];

    // Extract compiled specs
    const extra_mode: i32 = @intCast(c.PyLong_AsLong(extra_mode_obj));
    const ms_ptr = c.PyCapsule_GetPointer(capsule, "hyperdjango.compiled_specs") orelse return null;
    const ms: *CompiledModelSpecs = @ptrCast(@alignCast(ms_ptr));

    // Get model __dict__
    const obj_dict = c.PyObject_GenericGetDict(model_self, null) orelse return null;
    defer c.Py_DECREF(obj_dict);

    // Pre-extract field name bytes for fast byte-level matching
    const alloc = std.heap.c_allocator;
    const name_cache = alloc.alloc(FieldNameCache, ms.n_fields) catch return null;
    defer alloc.free(name_cache);
    const alias_cache = alloc.alloc(?FieldNameCache, ms.n_fields) catch return null;
    defer alloc.free(alias_cache);

    for (0..ms.n_fields) |i| {
        name_cache[i] = getFieldNameBytes(&ms.specs[i]);
        alias_cache[i] = getAliasBytes(&ms.specs[i]);
    }

    // State tracking. Multi-word bitset so fields ≥64 are tracked (a plain u64
    // could not, and shifting by ≥64 is UB).
    const bitset_words = alloc.alloc(u64, @max(1, bitsetWordCount(ms.n_fields))) catch return null;
    defer alloc.free(bitset_words);
    @memset(bitset_words, 0);
    var errors: ?*c.PyObject = null;
    var extra_dict: ?*c.PyObject = null;

    // Single position pointer shared across all parsing
    var pos: usize = 0;
    pos = simd.skipWhitespaceSIMD(json, pos);
    if (pos >= json.len or json[pos] != '{') {
        c.PyErr_SetString(c.PyExc_ValueError, "json_loads_model: expected JSON object");
        return null;
    }
    pos += 1; // skip '{'
    pos = simd.skipWhitespaceSIMD(json, pos);

    if (!(pos < json.len and json[pos] == '}')) {
        // Parse key-value pairs
        while (pos < json.len) {
            pos = simd.skipWhitespaceSIMD(json, pos);
            if (pos >= json.len or json[pos] != '"') break;

            // SIMD-extract key bytes — no Python string created
            pos += 1; // skip opening quote
            const key_result = simd.extractString(json, pos) catch break;
            const key_bytes = key_result.slice; // content between quotes
            pos = key_result.end; // already past closing quote

            pos = simd.skipWhitespaceSIMD(json, pos);
            if (pos >= json.len or json[pos] != ':') break;
            pos += 1; // skip ':'

            // Parse value through json_parser.parseValue — the single, iterative
            // JSON→PyObject materializer shared across the codebase. It tracks
            // nesting on an explicit heap stack (not the native call stack) and
            // enforces the depth policy internally, so a deeply-nested field
            // value yields a normal parse error here instead of crashing.
            const value = json_parser.parseValue(json, &pos) orelse {
                // parseValue set a Python exception (e.g. the depth-policy error
                // on a pathologically-nested value). Propagate it cleanly rather
                // than returning with a dangling exception set (which CPython
                // reports as a confusing SystemError). obj_dict/name caches are
                // freed by their defers above.
                if (errors) |e| c.Py_DecRef(e);
                if (extra_dict) |ed| c.Py_DecRef(ed);
                return null;
            };

            // Match key against compiled field specs
            if (matchFieldSpec(key_bytes, ms, name_cache, alias_cache)) |field_idx| {
                const fs = &ms.specs[field_idx];

                bitsetSet(bitset_words, field_idx);

                // Validate and store directly on model __dict__
                const validated = validateField(fs, value, &errors);
                if (validated) |v| {
                    _ = c.PyDict_SetItem(obj_dict, fs.name_obj, v);
                    c.Py_DECREF(v);
                }
            } else {
                // Unknown field
                if (extra_mode == 1) {
                    // forbid
                    if (py.newString(key_bytes)) |k| {
                        appendErrorStr(&errors, k, "Extra fields not permitted");
                        c.Py_DecRef(k);
                    }
                    c.Py_DecRef(value);
                } else if (extra_mode == 2) {
                    // allow — collect in extra dict
                    if (extra_dict == null) extra_dict = c.PyDict_New();
                    if (extra_dict) |ed| {
                        if (py.newString(key_bytes)) |k| {
                            _ = c.PyDict_SetItem(ed, k, value);
                            c.Py_DecRef(k);
                        }
                    }
                    c.Py_DecRef(value);
                } else {
                    c.Py_DecRef(value); // ignore
                }
            }

            pos = simd.skipWhitespaceSIMD(json, pos);
            if (pos >= json.len) break;
            if (json[pos] == '}') break;
            if (json[pos] == ',') {
                pos += 1;
                continue;
            }
            break;
        }
    }

    // Set defaults for missing fields, report missing required
    for (0..ms.n_fields) |i| {
        if (bitsetIsSet(bitset_words, i)) continue;
        const fs = &ms.specs[i];
        if (!fs.required) {
            _ = c.PyDict_SetItem(obj_dict, fs.name_obj, fs.default_val);
        } else {
            appendErrorStr(&errors, fs.name_obj, "Field required");
        }
    }

    // Set pydantic attributes
    initInternedKeys();
    setFieldsSet(obj_dict, bitset_words, ms);
    if (extra_dict) |ed| {
        _ = c.PyDict_SetItem(obj_dict, g_extra_key.?, ed);
        c.Py_DecRef(ed);
    } else {
        _ = c.PyDict_SetItem(obj_dict, g_extra_key.?, py.pyNone());
    }
    _ = c.PyDict_SetItem(obj_dict, g_private_key.?, py.pyNone());

    if (errors) |e| return e;
    return py.pyNone();
}

fn initInternedKeys() void {
    if (g_fields_set_key == null) g_fields_set_key = c.PyUnicode_InternFromString("__pydantic_fields_set__");
    if (g_extra_key == null) g_extra_key = c.PyUnicode_InternFromString("__pydantic_extra__");
    if (g_private_key == null) g_private_key = c.PyUnicode_InternFromString("__pydantic_private__");
}

fn setFieldsSet(obj_dict: *c.PyObject, bits: []const u64, ms: *const CompiledModelSpecs) void {
    const fields_set = c.PySet_New(null) orelse return;
    for (0..ms.n_fields) |i| {
        if (bitsetIsSet(bits, i)) {
            _ = c.PySet_Add(fields_set, ms.specs[i].name_obj);
        }
    }
    _ = c.PyDict_SetItem(obj_dict, g_fields_set_key.?, fields_set);
    c.Py_DecRef(fields_set);
}

// Proves the multi-word bitset tracks fields ≥64 (the old u64 could not, and a
// `1 << i` for i≥64 was illegal-cast UB). 200 fields across 4 words.
test "field bitset tracks indices beyond 63" {
    const n = 200;
    try std.testing.expectEqual(@as(usize, 4), bitsetWordCount(n));
    var words = [_]u64{0} ** bitsetWordCount(n);

    const set_indices = [_]usize{ 0, 1, 63, 64, 65, 127, 128, 199 };
    for (set_indices) |idx| bitsetSet(&words, idx);

    for (0..n) |i| {
        var expected = false;
        for (set_indices) |idx| {
            if (idx == i) expected = true;
        }
        try std.testing.expectEqual(expected, bitsetIsSet(&words, i));
    }
}

test "checkFormat - unimplemented codes fail closed" {
    // Implemented codes still validate correctly.
    try std.testing.expect(checkFormat(1, "user@example.com").valid);
    try std.testing.expect(!checkFormat(1, "user@.com").valid); // parity w/ SIMD fix
    try std.testing.expect(checkFormat(2, "https://example.com").valid);
    try std.testing.expect(checkFormat(3, "12345678-1234-1234-1234-123456789abc").valid);
    try std.testing.expect(checkFormat(4, "192.168.0.1").valid);

    // Codes 5-8 (ipv6/base64/iso_date/iso_datetime) are declared but not yet
    // implemented — they MUST reject, never pass an arbitrary string through.
    var code: i32 = 5;
    while (code <= 8) : (code += 1) {
        const fc = checkFormat(code, "anything at all");
        try std.testing.expect(!fc.valid);
        try std.testing.expectEqualStrings("unimplemented", std.mem.span(fc.name));
    }
    // Any other out-of-range code also fails closed.
    try std.testing.expect(!checkFormat(99, "x").valid);
    try std.testing.expect(!checkFormat(-1, "x").valid);
}
