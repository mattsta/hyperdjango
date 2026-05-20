// Batch validation — SIMD-parallel validation of N objects
//
// Validates arrays of values/objects in single FFI calls using SIMD vectorization.
// Eliminates per-item Python↔Zig overhead for bulk operations.
//
// API:
//   validate_int_batch_simd(list, min, max) -> (results_list, valid_count)
//   validate_string_batch(list, min_len, max_len) -> (results_list, valid_count)
//   validate_email_batch(list) -> (results_list, valid_count)
//   validate_batch_direct(list_of_dicts, field_specs) -> (results_list, valid_count)
//   validate_model_batch(list_of_dicts, compiled_specs) -> list of None|errors

const std = @import("std");
pub const py = @import("py.zig");
const c = py.c;
const model_validator = @import("model_validator.zig");

// ── validate_int_batch_simd ──────────────────────────────────────────────────
// Validates a Python list of ints against min/max bounds using SIMD.
// Returns: (list[bool], valid_count)

pub fn py_validate_int_batch_simd(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var list_obj: ?*c.PyObject = null;
    var min_val: c_long = undefined;
    var max_val: c_long = undefined;
    if (c.PyArg_ParseTuple(args, "O!ll", &c.PyList_Type, &list_obj, &min_val, &max_val) == 0)
        return null;

    const list = list_obj.?;
    const count = c.PyList_Size(list);
    if (count == 0) {
        return buildBatchResult(0, 0, null);
    }

    // Extract all ints into a contiguous array for SIMD processing
    const values = std.heap.c_allocator.alloc(i64, @intCast(count)) catch {
        _ = c.PyErr_NoMemory();
        return null;
    };
    defer std.heap.c_allocator.free(values);

    const results = std.heap.c_allocator.alloc(u8, @intCast(count)) catch {
        _ = c.PyErr_NoMemory();
        return null;
    };
    defer std.heap.c_allocator.free(results);

    // Extract Python ints to i64 array
    var i: c.Py_ssize_t = 0;
    while (i < count) : (i += 1) {
        const item = c.PyList_GetItem(list, i).?;
        if (c.PyLong_Check(item) != 0) {
            const v = c.PyLong_AsLongLong(item);
            if (v == -1 and c.PyErr_Occurred() != null) {
                // Int too large for i64 — clear the pending OverflowError so it
                // can't corrupt a later C-API call, and mark the value invalid.
                c.PyErr_Clear();
                values[@intCast(i)] = std.math.minInt(i64); // Will fail validation
            } else {
                values[@intCast(i)] = @intCast(v);
            }
        } else {
            values[@intCast(i)] = std.math.minInt(i64); // Will fail validation
        }
    }

    // SIMD validation — process 4 values at a time using @Vector
    const min_i64: i64 = @intCast(min_val);
    const max_i64: i64 = @intCast(max_val);
    var valid_count: usize = 0;
    var idx: usize = 0;
    const n: usize = @intCast(count);

    while (idx + 4 <= n) : (idx += 4) {
        const vec: @Vector(4, i64) = values[idx..][0..4].*;
        const min_vec: @Vector(4, i64) = @splat(min_i64);
        const max_vec: @Vector(4, i64) = @splat(max_i64);

        const ge_min = vec >= min_vec;
        const le_max = vec <= max_vec;

        // Combine: valid = ge_min AND le_max
        inline for (0..4) |j| {
            const valid = ge_min[j] and le_max[j];
            results[idx + j] = @intFromBool(valid);
            valid_count += @intFromBool(valid);
        }
    }

    // Handle remainder
    while (idx < n) : (idx += 1) {
        const valid = values[idx] >= min_i64 and values[idx] <= max_i64;
        results[idx] = @intFromBool(valid);
        valid_count += @intFromBool(valid);
    }

    return buildBatchResult(@intCast(count), valid_count, results[0..n]);
}

// ── validate_string_batch ────────────────────────────────────────────────────
// Validates a Python list of strings against min/max length bounds.
// Returns: (list[bool], valid_count)

pub fn py_validate_string_batch(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var list_obj: ?*c.PyObject = null;
    var min_len: c.Py_ssize_t = undefined;
    var max_len: c.Py_ssize_t = undefined;
    if (c.PyArg_ParseTuple(args, "O!nn", &c.PyList_Type, &list_obj, &min_len, &max_len) == 0)
        return null;

    const list = list_obj.?;
    const count = c.PyList_Size(list);
    if (count == 0) {
        return buildBatchResult(0, 0, null);
    }

    const n: usize = @intCast(count);
    const results = std.heap.c_allocator.alloc(u8, n) catch {
        _ = c.PyErr_NoMemory();
        return null;
    };
    defer std.heap.c_allocator.free(results);

    var valid_count: usize = 0;
    var i: c.Py_ssize_t = 0;
    while (i < count) : (i += 1) {
        const item = c.PyList_GetItem(list, i).?;
        var valid = false;
        if (c.PyUnicode_Check(item) != 0) {
            const slen = c.PyUnicode_GetLength(item);
            valid = slen >= min_len and slen <= max_len;
        } else if (c.PyBytes_Check(item) != 0) {
            const slen = c.PyBytes_Size(item);
            valid = slen >= min_len and slen <= max_len;
        }
        results[@intCast(i)] = @intFromBool(valid);
        valid_count += @intFromBool(valid);
    }

    return buildBatchResult(@intCast(count), valid_count, results[0..n]);
}

// ── validate_email_batch ─────────────────────────────────────────────────────
// Validates a Python list of email strings.
// Returns: (list[bool], valid_count)

pub fn py_validate_email_batch(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var list_obj: ?*c.PyObject = null;
    if (c.PyArg_ParseTuple(args, "O!", &c.PyList_Type, &list_obj) == 0)
        return null;

    const list = list_obj.?;
    const count = c.PyList_Size(list);
    if (count == 0) {
        return buildBatchResult(0, 0, null);
    }

    const n: usize = @intCast(count);
    const results = std.heap.c_allocator.alloc(u8, n) catch {
        _ = c.PyErr_NoMemory();
        return null;
    };
    defer std.heap.c_allocator.free(results);

    var valid_count: usize = 0;
    var i: c.Py_ssize_t = 0;
    while (i < count) : (i += 1) {
        const item = c.PyList_GetItem(list, i).?;
        var valid = false;

        // Accept both str and bytes
        var str_ptr: ?[*c]const u8 = null;
        if (c.PyUnicode_Check(item) != 0) {
            str_ptr = c.PyUnicode_AsUTF8(item);
        } else if (c.PyBytes_Check(item) != 0) {
            str_ptr = @ptrCast(c.PyBytes_AsString(item));
        }

        if (str_ptr) |ptr| {
            valid = validateEmailSIMD(std.mem.span(ptr));
        }

        results[@intCast(i)] = @intFromBool(valid);
        valid_count += @intFromBool(valid);
    }

    return buildBatchResult(@intCast(count), valid_count, results[0..n]);
}

// SIMD email validation — scans for @ and . using 16-byte SIMD
fn validateEmailSIMD(email: []const u8) bool {
    if (email.len < 3 or email.len > 320) return false;

    var has_at = false;
    var has_dot_after_at = false;
    var at_pos: usize = 0;

    // 16-byte SIMD scan for @ and .
    var i: usize = 0;
    while (i + 16 <= email.len) : (i += 16) {
        const chunk: @Vector(16, u8) = email[i..][0..16].*;
        const at_mask = chunk == @as(@Vector(16, u8), @splat('@'));
        const dot_mask = chunk == @as(@Vector(16, u8), @splat('.'));
        const at_bits: u16 = @bitCast(at_mask);
        const dot_bits: u16 = @bitCast(dot_mask);

        if (at_bits != 0) {
            if (has_at) return false; // Multiple @ (a prior chunk already had one)
            // Two '@' in THIS 16-byte chunk: @ctz only finds the first, so a
            // second '@' would be silently accepted (SIMD/scalar divergence).
            if (@popCount(at_bits) > 1) return false;
            has_at = true;
            at_pos = i + @ctz(at_bits);

            // Check for dots AFTER @ within this same chunk. The scalar
            // remainder rule (`i > at_pos + 1`) requires a domain char before
            // the first dot, so a dot at exactly at_pos+1 (`user@.com`) is NOT a
            // valid dot-after-@. Keep only bits strictly past at_local+1 → shift
            // by at_local+2 (mirrors the scalar rule).
            if (dot_bits != 0) {
                const at_local = @ctz(at_bits);
                // Shift in a u32 (not u16): at_local+2 can reach 17, which would
                // over-shift a u16. Truncate the mask back to u16.
                const shift: u5 = @intCast(at_local + 2);
                const keep_after: u16 = @truncate(~((@as(u32, 1) << shift) - 1));
                const dots_after = dot_bits & keep_after;
                if (dots_after != 0) has_dot_after_at = true;
            }
        } else if (has_at and dot_bits != 0) {
            // Later chunk after the @ chunk. Any dot is past @, but the scalar
            // rule still excludes a dot at exactly at_pos+1: that happens only
            // when @ ended the previous chunk (at_pos == i-1) and the dot is
            // this chunk's first byte (bit 0).
            var dots = dot_bits;
            if (at_pos == i - 1) dots &= ~@as(u16, 1);
            if (dots != 0) has_dot_after_at = true;
        }
    }

    // Remainder
    while (i < email.len) : (i += 1) {
        if (email[i] == '@') {
            if (has_at) return false;
            has_at = true;
            at_pos = i;
        }
        if (has_at and email[i] == '.' and i > at_pos + 1) {
            has_dot_after_at = true;
        }
    }

    return has_at and has_dot_after_at and at_pos > 0 and at_pos < email.len - 1;
}

// ── validate_batch_direct ────────────────────────────────────────────────────
// Validates a list of dicts against field specs in a single call.
// field_specs: dict mapping field_name -> ('type', constraints...)
// Returns: (list[bool], valid_count)

pub fn py_validate_batch_direct(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var list_obj: ?*c.PyObject = null;
    var specs_obj: ?*c.PyObject = null;
    if (c.PyArg_ParseTuple(args, "O!O!", &c.PyList_Type, &list_obj, &c.PyDict_Type, &specs_obj) == 0)
        return null;

    const list = list_obj.?;
    const specs = specs_obj.?;
    const count = c.PyList_Size(list);
    if (count == 0) {
        return buildBatchResult(0, 0, null);
    }

    const n: usize = @intCast(count);
    const results = std.heap.c_allocator.alloc(u8, n) catch {
        _ = c.PyErr_NoMemory();
        return null;
    };
    defer std.heap.c_allocator.free(results);

    // Parse field specs once
    const FieldSpec = struct {
        name: ?*c.PyObject,
        kind: enum { int_range, string_len, email },
        min_long: c_long,
        max_long: c_long,
        min_len: c.Py_ssize_t,
        max_len: c.Py_ssize_t,
    };

    // Stack for <=64 field specs, heap for more
    var stack_specs: [64]FieldSpec = undefined;
    var n_specs: usize = 0;

    // Count fields first to size the buffer
    var count_key: ?*c.PyObject = null;
    var count_val: ?*c.PyObject = null;
    var count_pos: c.Py_ssize_t = 0;
    var total_fields: usize = 0;
    while (c.PyDict_Next(specs, &count_pos, &count_key, &count_val) != 0) total_fields += 1;

    var heap_specs: ?[]FieldSpec = null;
    defer if (heap_specs) |h| std.heap.c_allocator.free(h);
    const field_specs_buf: []FieldSpec = if (total_fields <= 64)
        &stack_specs
    else blk: {
        heap_specs = std.heap.c_allocator.alloc(FieldSpec, total_fields) catch {
            py.setError("OOM allocating field specs for {d} fields", .{total_fields});
            return null;
        };
        break :blk heap_specs.?;
    };

    var key: ?*c.PyObject = null;
    var value: ?*c.PyObject = null;
    var pos: c.Py_ssize_t = 0;
    while (c.PyDict_Next(specs, &pos, &key, &value) != 0) {
        var fs = &field_specs_buf[n_specs];
        fs.name = key;

        // Parse spec tuple
        const spec_type = c.PyTuple_GetItem(value.?, 0);
        if (spec_type == null) continue;

        var type_ptr: ?[*c]const u8 = null;
        if (c.PyUnicode_Check(spec_type.?) != 0) {
            type_ptr = c.PyUnicode_AsUTF8(spec_type.?);
        }
        if (type_ptr == null) continue;
        const type_str = std.mem.span(type_ptr.?);

        if (std.mem.eql(u8, type_str, "int")) {
            fs.kind = .int_range;
            // PyLong_As* returns -1 with an OverflowError set when the bound
            // does not fit c_long. Leaving that exception pending corrupts the
            // next C-API call (CPython contract violation). Clear it and treat
            // an unrepresentable bound as unconstrained on that side.
            fs.min_long = c.PyLong_AsLong(c.PyTuple_GetItem(value.?, 1).?);
            if (c.PyErr_Occurred() != null) {
                c.PyErr_Clear();
                fs.min_long = std.math.minInt(c_long);
            }
            fs.max_long = c.PyLong_AsLong(c.PyTuple_GetItem(value.?, 2).?);
            if (c.PyErr_Occurred() != null) {
                c.PyErr_Clear();
                fs.max_long = std.math.maxInt(c_long);
            }
        } else if (std.mem.eql(u8, type_str, "string")) {
            fs.kind = .string_len;
            fs.min_len = c.PyLong_AsSsize_t(c.PyTuple_GetItem(value.?, 1).?);
            if (c.PyErr_Occurred() != null) {
                c.PyErr_Clear();
                fs.min_len = 0;
            }
            fs.max_len = c.PyLong_AsSsize_t(c.PyTuple_GetItem(value.?, 2).?);
            if (c.PyErr_Occurred() != null) {
                c.PyErr_Clear();
                fs.max_len = std.math.maxInt(c.Py_ssize_t);
            }
        } else if (std.mem.eql(u8, type_str, "email")) {
            fs.kind = .email;
        } else continue;

        n_specs += 1;
    }

    // Validate each dict
    var valid_count: usize = 0;
    var i: c.Py_ssize_t = 0;
    while (i < count) : (i += 1) {
        const dict = c.PyList_GetItem(list, i).?;
        var all_valid = true;

        if (c.PyDict_Check(dict) == 0) {
            results[@intCast(i)] = 0;
            continue;
        }

        for (field_specs_buf[0..n_specs]) |fs| {
            const field_val = c.PyDict_GetItem(dict, fs.name) orelse {
                all_valid = false;
                break;
            };

            switch (fs.kind) {
                .int_range => {
                    if (c.PyLong_Check(field_val) == 0) {
                        all_valid = false;
                        break;
                    }
                    const v = c.PyLong_AsLong(field_val);
                    // A data-controlled int that overflows c_long returns -1 with
                    // OverflowError set. Clear it (else it bleeds into the next
                    // C-API call) and treat the value as out of range — it cannot
                    // fit any c_long [min,max] window anyway.
                    if (c.PyErr_Occurred() != null) {
                        c.PyErr_Clear();
                        all_valid = false;
                        break;
                    }
                    if (v < fs.min_long or v > fs.max_long) {
                        all_valid = false;
                        break;
                    }
                },
                .string_len => {
                    if (c.PyUnicode_Check(field_val) == 0) {
                        all_valid = false;
                        break;
                    }
                    const slen = c.PyUnicode_GetLength(field_val);
                    if (slen < fs.min_len or slen > fs.max_len) {
                        all_valid = false;
                        break;
                    }
                },
                .email => {
                    if (c.PyUnicode_Check(field_val) == 0) {
                        all_valid = false;
                        break;
                    }
                    const str_ptr = c.PyUnicode_AsUTF8(field_val) orelse {
                        all_valid = false;
                        break;
                    };
                    if (!validateEmailSIMD(std.mem.span(str_ptr))) {
                        all_valid = false;
                        break;
                    }
                },
            }
        }

        results[@intCast(i)] = @intFromBool(all_valid);
        valid_count += @intFromBool(all_valid);
    }

    return buildBatchResult(@intCast(count), valid_count, results[0..n]);
}

// ── validate_model_batch ─────────────────────────────────────────────────────
// Validates N dicts against compiled model specs.
// Uses the same compiled specs as init_model_full.
// validate_model_batch(list_of_dicts, capsule) -> list of (None | error_list)

pub fn py_validate_model_batch(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var list_obj: ?*c.PyObject = null;
    var capsule: ?*c.PyObject = null;
    if (c.PyArg_ParseTuple(args, "O!O", &c.PyList_Type, &list_obj, &capsule) == 0)
        return null;

    const list = list_obj.?;
    const count = c.PyList_Size(list);

    const ms_ptr = c.PyCapsule_GetPointer(capsule.?, "hyperdjango.compiled_specs") orelse return null;
    const ms: *model_validator.CompiledModelSpecs = @ptrCast(@alignCast(ms_ptr));

    // Create result list
    const result_list = c.PyList_New(count) orelse return null;

    var i: c.Py_ssize_t = 0;
    while (i < count) : (i += 1) {
        const dict = c.PyList_GetItem(list, i).?;

        if (c.PyDict_Check(dict) == 0) {
            // Not a dict — error
            const err = c.PyList_New(1) orelse {
                c.Py_DECREF(result_list);
                return null;
            };
            const err_tuple = c.Py_BuildValue("(ss)", "input", "Expected dict");
            if (err_tuple) |et| {
                _ = c.PyList_SetItem(err, 0, et);
            }
            _ = c.PyList_SetItem(result_list, i, err);
            continue;
        }

        // Validate this dict against compiled specs
        var errors: ?*c.PyObject = null;
        var j: usize = 0;
        while (j < ms.n_fields) : (j += 1) {
            const fs = &ms.specs[j];

            var value: ?*c.PyObject = null;
            if (!model_validator.isNone(fs.alias_obj)) {
                value = c.PyDict_GetItem(dict, fs.alias_obj);
            }
            if (value == null) {
                value = c._PyDict_GetItem_KnownHash(dict, fs.name_obj, fs.name_hash);
            }

            if (value == null) {
                if (fs.required) {
                    model_validator.appendErrorStr(&errors, fs.name_obj, "Field required");
                }
                continue;
            }

            // Validate field
            c.Py_INCREF(value.?);
            const validated = model_validator.validateField(fs, value.?, &errors);
            if (validated) |v| {
                c.Py_DECREF(v);
            }
        }

        if (errors) |e| {
            if (c.PyList_Size(e) > 0) {
                _ = c.PyList_SetItem(result_list, i, e); // steals ref
            } else {
                c.Py_DECREF(e);
                c.Py_INCREF(model_validator.pyNone());
                _ = c.PyList_SetItem(result_list, i, model_validator.pyNone());
            }
        } else {
            c.Py_INCREF(model_validator.pyNone());
            _ = c.PyList_SetItem(result_list, i, model_validator.pyNone());
        }
    }

    return result_list;
}

// ── Helper: build (results_list, valid_count) tuple ──────────────────────────

fn buildBatchResult(count: c.Py_ssize_t, valid_count: usize, results: ?[]const u8) ?*c.PyObject {
    const result_list = c.PyList_New(count) orelse return null;

    if (results) |r| {
        var i: c.Py_ssize_t = 0;
        while (i < count) : (i += 1) {
            const val = if (r[@intCast(i)] != 0) py.pyTrue() else py.pyFalse();
            _ = c.PyList_SetItem(result_list, i, val); // steals ref
        }
    }

    const valid_count_obj = c.PyLong_FromLong(@intCast(valid_count)) orelse {
        c.Py_DECREF(result_list);
        return null;
    };

    const tuple = c.PyTuple_New(2) orelse {
        c.Py_DECREF(result_list);
        c.Py_DECREF(valid_count_obj);
        return null;
    };
    _ = c.PyTuple_SetItem(tuple, 0, result_list); // steals ref
    _ = c.PyTuple_SetItem(tuple, 1, valid_count_obj); // steals ref

    return tuple;
}

// ── Fuzz tests ───────────────────────────────────────────────────────────

/// Bridge the Zig 0.16 `std.testing.fuzz` `*Smith` callback to a byte slice:
/// replay a concrete corpus entry verbatim (`smith.in`), or draw an
/// arbitrary-length byte string when actively fuzzing (`in == null`). `buf`
/// backs the active-fuzz draw and must outlive the returned slice.
fn fuzzInput(smith: *std.testing.Smith, buf: []u8) []const u8 {
    if (smith.in) |in| return in;
    return buf[0..smith.sliceWithHash(buf, 0)];
}

fn fuzz_validateEmailSIMD(_: void, smith: *std.testing.Smith) anyerror!void {
    var in_buf: [512]u8 = undefined;
    const input = fuzzInput(smith, &in_buf);
    // validateEmailSIMD must never panic on any input — just return true/false
    const result = validateEmailSIMD(input);
    // Basic invariant: empty and very short strings should never be valid
    if (input.len < 3) {
        try std.testing.expect(!result);
    }
    // If valid, must contain @ and .
    if (result) {
        try std.testing.expect(std.mem.indexOf(u8, input, "@") != null);
        try std.testing.expect(std.mem.indexOf(u8, input, ".") != null);
    }
}

test "validateEmailSIMD: two @ in one SIMD chunk rejected" {
    // 16-byte input: both '@' fall in the same SIMD chunk. @ctz finds only the
    // first; the @popCount guard is what rejects the second.
    try std.testing.expect(!validateEmailSIMD("aa@bb@cc.example"));
    // Cross-chunk double '@' (second '@' lands in the scalar remainder).
    try std.testing.expect(!validateEmailSIMD("aaaaaaaaaaaaaaaa@b@.com"));
    // Sanity: a single '@' with a dot after it still passes.
    try std.testing.expect(validateEmailSIMD("user@example.com"));
}

test "validateEmailSIMD: empty first domain label rejected (SIMD/scalar parity)" {
    // A '.' immediately after '@' is not a valid dot-after-@ (the scalar rule is
    // `i > at_pos + 1`). The verdict must not depend on 16-byte alignment.

    // Scalar-remainder path.
    try std.testing.expect(!validateEmailSIMD("user@.com"));

    // Same-chunk: 30-char local part → '@' at local 14, '.' at local 15 inside
    // the fully-processed second chunk.
    try std.testing.expect(!validateEmailSIMD("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa@.com"));

    // Cross-chunk: '@' ends chunk 0 (pos 15), '.' is first byte of chunk 1
    // (pos 16 == at_pos+1). Exercises the else-if boundary exclusion.
    try std.testing.expect(!validateEmailSIMD("aaaaaaaaaaaaaaa@." ++ ("c" ** 16)));

    // Valid control: real domain char before the dot still passes.
    try std.testing.expect(validateEmailSIMD("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa@example.com"));
}

test "fuzz: validateEmailSIMD — no panic on adversarial input" {
    try std.testing.fuzz({}, fuzz_validateEmailSIMD, .{
        .corpus = &.{
            "user@example.com",
            "a@b.c",
            "",
            "@",
            "@@",
            "user@",
            "@domain",
            "user@domain",
            "user@.com",
            ".@domain.com",
            "user@domain.com.",
            &([_]u8{'a'} ** 321), // exceeds 320 char limit
            "user@" ++ &([_]u8{'x'} ** 300) ++ ".com",
            "\x00@\x00.\x00",
            "a@b.c\x00injected",
            "user\xFF@domain\xFE.com",
            "a" ** 16 ++ "@" ++ "b" ** 16 ++ "." ++ "c" ** 16, // exactly SIMD chunk boundary
            "@@@@@@@@@@@@@@@@", // 16 @s — SIMD chunk
            "................", // 16 dots
            "user@exam ple.com", // space in domain
            "user @example.com", // space before @
        },
    });
}
