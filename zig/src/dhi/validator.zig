const std = @import("std");

/// ValidationError represents a single validation failure with field path and message.
/// Represents a single validation failure with field path and message.
pub const ValidationError = struct {
    field: []const u8,
    message: []const u8,
    path: []const []const u8,
    allocator: std.mem.Allocator,

    pub fn init(allocator: std.mem.Allocator, field: []const u8, message: []const u8) !ValidationError {
        const field_copy = try allocator.dupe(u8, field);
        const message_copy = try allocator.dupe(u8, message);
        return .{
            .field = field_copy,
            .message = message_copy,
            .path = &.{},
            .allocator = allocator,
        };
    }

    pub fn initWithPath(allocator: std.mem.Allocator, field: []const u8, message: []const u8, path: []const []const u8) !ValidationError {
        const field_copy = try allocator.dupe(u8, field);
        const message_copy = try allocator.dupe(u8, message);
        const path_copy = try allocator.alloc([]const u8, path.len);
        for (path, 0..) |segment, i| {
            path_copy[i] = try allocator.dupe(u8, segment);
        }
        return .{
            .field = field_copy,
            .message = message_copy,
            .path = path_copy,
            .allocator = allocator,
        };
    }

    pub fn deinit(self: *ValidationError) void {
        self.allocator.free(self.field);
        self.allocator.free(self.message);
        for (self.path) |segment| {
            self.allocator.free(segment);
        }
        if (self.path.len > 0) self.allocator.free(self.path);
    }

    /// Format error as "field: message" or "path.to.field: message"
    pub fn format(
        self: ValidationError,
        writer: anytype,
    ) !void {
        if (self.path.len > 0) {
            for (self.path, 0..) |segment, i| {
                try writer.writeAll(segment);
                if (i < self.path.len - 1) try writer.writeAll(".");
            }
            try writer.writeAll(".");
            try writer.writeAll(self.field);
        } else {
            try writer.writeAll(self.field);
        }
        try writer.writeAll(": ");
        try writer.writeAll(self.message);
    }
};

/// ValidationErrors collects multiple validation failures.
/// Supports non-fail-fast validation — collects all errors for a single struct.
pub const ValidationErrors = struct {
    errors: std.ArrayList(ValidationError),
    allocator: std.mem.Allocator,

    pub fn init(allocator: std.mem.Allocator) ValidationErrors {
        return .{
            .errors = std.ArrayList(ValidationError).init(allocator),
            .allocator = allocator,
        };
    }

    pub fn deinit(self: *ValidationErrors) void {
        for (self.errors.items) |*err| {
            err.deinit();
        }
        self.errors.deinit(self.allocator);
    }

    pub fn add(self: *ValidationErrors, field: []const u8, message: []const u8) !void {
        const err = try ValidationError.init(self.allocator, field, message);
        try self.errors.append(self.allocator, err);
    }

    pub fn addWithPath(self: *ValidationErrors, field: []const u8, message: []const u8, path: []const []const u8) !void {
        const err = try ValidationError.initWithPath(self.allocator, field, message, path);
        try self.errors.append(self.allocator, err);
    }

    pub fn hasErrors(self: ValidationErrors) bool {
        return self.errors.items.len > 0;
    }

    pub fn count(self: ValidationErrors) usize {
        return self.errors.items.len;
    }

    /// Print all errors to writer, one per line
    pub fn format(
        self: ValidationErrors,
        writer: anytype,
    ) !void {
        for (self.errors.items, 0..) |err, i| {
            try err.format(writer);
            if (i < self.errors.items.len - 1) try writer.writeAll("\n");
        }
    }
};

/// BoundedInt creates a validated integer type with compile-time bounds.
/// Creates a validated integer type with compile-time bounds (ge=min, le=max).
///
/// Example:
///   const Age = BoundedInt(u8, 0, 130);
///   const age = try Age.init(27);  // OK
///   const bad = try Age.init(200); // error.OutOfRange
pub fn BoundedInt(comptime T: type, comptime min: T, comptime max: T) type {
    return struct {
        const Self = @This();
        value: T,

        pub fn init(v: T) !Self {
            if (v < min or v > max) return error.OutOfRange;
            return .{ .value = v };
        }

        pub fn validate(v: T, errors: *ValidationErrors, field_name: []const u8) !T {
            if (v < min or v > max) {
                const msg = try std.fmt.allocPrint(
                    errors.allocator,
                    "Value {d} must be >= {d} and <= {d}",
                    .{ v, min, max },
                );
                defer errors.allocator.free(msg);
                try errors.add(field_name, msg);
                return error.ValidationFailed;
            }
            return v;
        }

        pub fn bounds() struct { min: T, max: T } {
            return .{ .min = min, .max = max };
        }
    };
}

/// BoundedString creates a validated string type with length constraints.
/// Creates a validated string type with length constraints (min_length, max_length).
///
/// Example:
///   const Name = BoundedString(1, 40);
///   const name = try Name.init("Rach");  // OK
///   const bad = try Name.init("");       // error.TooShort
pub fn BoundedString(comptime min_len: usize, comptime max_len: usize) type {
    return struct {
        const Self = @This();
        slice: []const u8,

        pub fn init(s: []const u8) !Self {
            if (s.len < min_len) return error.TooShort;
            if (s.len > max_len) return error.TooLong;
            return .{ .slice = s };
        }

        pub fn validate(s: []const u8, errors: *ValidationErrors, field_name: []const u8) ![]const u8 {
            if (s.len < min_len) {
                const msg = try std.fmt.allocPrint(
                    errors.allocator,
                    "String length {d} must be >= {d}",
                    .{ s.len, min_len },
                );
                defer errors.allocator.free(msg);
                try errors.add(field_name, msg);
                return error.ValidationFailed;
            }
            if (s.len > max_len) {
                const msg = try std.fmt.allocPrint(
                    errors.allocator,
                    "String length {d} must be <= {d}",
                    .{ s.len, max_len },
                );
                defer errors.allocator.free(msg);
                try errors.add(field_name, msg);
                return error.ValidationFailed;
            }
            return s;
        }

        pub fn bounds() struct { min_len: usize, max_len: usize } {
            return .{ .min_len = min_len, .max_len = max_len };
        }
    };
}

/// Email validates email format using a simplified RFC 5322 check.
/// Validates email format using a simplified RFC 5322 check.
pub const Email = struct {
    value: []const u8,

    pub fn init(s: []const u8) !Email {
        if (!isValidEmail(s)) return error.InvalidEmail;
        return .{ .value = s };
    }

    pub fn validate(s: []const u8, errors: *ValidationErrors, field_name: []const u8) ![]const u8 {
        if (!isValidEmail(s)) {
            try errors.add(field_name, "Invalid email format (expected: local@domain)");
            return error.ValidationFailed;
        }
        return s;
    }

    fn isValidEmail(s: []const u8) bool {
        if (s.len < 3 or s.len > 320) return false;

        var has_at = false;
        var has_dot_after_at = false;
        var at_pos: usize = 0;

        // SIMD scan: process 16 bytes at a time using @Vector(16, u8)
        // Checks for '@' and '.' characters in parallel across 16 bytes
        var i: usize = 0;
        while (i + 16 <= s.len) : (i += 16) {
            const chunk: @Vector(16, u8) = s[i..][0..16].*;
            const at_cmp = chunk == @as(@Vector(16, u8), @splat(@as(u8, '@')));
            const dot_cmp = chunk == @as(@Vector(16, u8), @splat(@as(u8, '.')));
            const at_bits: u16 = @bitCast(at_cmp);
            const dot_bits: u16 = @bitCast(dot_cmp);

            if (at_bits != 0) {
                if (has_at) return false; // Multiple @ (a prior chunk already had one)
                // Two '@' in THIS 16-byte chunk: @ctz only finds the first, so a
                // second '@' would be silently accepted (SIMD/scalar divergence).
                if (@popCount(at_bits) > 1) return false;
                has_at = true;
                at_pos = i + @ctz(at_bits);
                // Check dots after @ in this chunk. The scalar remainder rule
                // (`i > at_pos + 1`) requires at least one domain char before
                // the first dot, so a dot at exactly at_pos+1 (`user@.com`) is
                // NOT a valid dot-after-@. Keep only bits strictly past
                // at_local+1 → shift by at_local+2 (mirrors the scalar rule).
                if (dot_bits != 0) {
                    const at_local = @ctz(at_bits);
                    // Shift in a u32 (not u16): at_local+2 can reach 17, which
                    // would over-shift a u16. Truncate the mask back to u16.
                    const shift: u5 = @intCast(at_local + 2);
                    const keep_after: u16 = @truncate(~((@as(u32, 1) << shift) - 1));
                    const dots_after = dot_bits & keep_after;
                    if (dots_after != 0) has_dot_after_at = true;
                }
            } else if (has_at and dot_bits != 0) {
                // Later chunk after the @ chunk. Any dot here is past @, but the
                // scalar rule still excludes a dot at exactly at_pos+1: that
                // happens only when @ ended the previous chunk (at_pos == i-1)
                // and the dot is this chunk's first byte (bit 0).
                var dots = dot_bits;
                if (at_pos == i - 1) dots &= ~@as(u16, 1);
                if (dots != 0) has_dot_after_at = true;
            }
        }

        // Scalar remainder
        while (i < s.len) : (i += 1) {
            if (s[i] == '@') {
                if (has_at) return false;
                has_at = true;
                at_pos = i;
            }
            if (has_at and s[i] == '.' and i > at_pos + 1) {
                has_dot_after_at = true;
            }
        }

        return has_at and has_dot_after_at and at_pos > 0 and at_pos < s.len - 1;
    }
};

/// Pattern validates strings against a character-class pattern at runtime.
///
/// Supports a subset of regex sufficient for field validation:
///   ^        — anchor to start (implicit if pattern starts with ^)
///   $        — anchor to end (implicit if pattern ends with $)
///   [a-zA-Z] — character ranges
///   [^...]   — negated character class
///   \d       — digit [0-9]
///   \w       — word char [a-zA-Z0-9_]
///   \s       — whitespace [ \t\n\r]
///   .        — any character
///   +        — one or more
///   *        — zero or more
///   ?        — zero or one
///   {n}      — exactly n
///   {n,m}    — between n and m
///   literal  — exact character match
///
/// Comptime-parsed pattern compiled into recursive matching functions.
/// Uses @Vector(16, u8) SIMD fast paths for quantified character classes:
///   \d+, \w+, \s+, .+, [a-z]+, [A-Za-z0-9]+ etc. process 16 bytes per cycle.
///   Falls back to scalar matching for remainder bytes and complex patterns.
pub fn Pattern(comptime pattern: []const u8) type {
    return struct {
        const Self = @This();
        value: []const u8,

        pub fn init(s: []const u8) !Self {
            if (!matchPattern(s)) return error.PatternMismatch;
            return .{ .value = s };
        }

        pub fn validate(s: []const u8, errors: *ValidationErrors, field_name: []const u8) ![]const u8 {
            if (!matchPattern(s)) {
                try errors.add(field_name, "Value does not match required pattern");
                return error.ValidationFailed;
            }
            return s;
        }

        pub fn getPattern() []const u8 {
            return pattern;
        }

        /// Runtime pattern matching — interprets the comptime pattern against input.
        fn matchPattern(input: []const u8) bool {
            // Strip anchors — we always match the full string (anchored by default)
            comptime var pat = pattern;
            comptime {
                if (pat.len > 0 and pat[0] == '^') pat = pat[1..];
                if (pat.len > 0 and pat[pat.len - 1] == '$') pat = pat[0 .. pat.len - 1];
            }
            return matchAt(input, 0, pat, 0);
        }

        fn matchAt(input: []const u8, i_pos: usize, comptime pat: []const u8, comptime p_pos: usize) bool {
            // Pattern exhausted — must also exhaust input for full match
            if (comptime p_pos >= pat.len) {
                return i_pos == input.len;
            }

            // Parse current pattern element
            const elem = comptime parseElement(pat, p_pos);
            const next_p = comptime elem.end_pos;

            // Parse quantifier if present
            const quant = comptime parseQuantifier(pat, next_p);
            const after_quant = comptime quant.end_pos;

            // Try matching with quantifier
            return matchQuantified(input, i_pos, pat, after_quant, elem, quant.min, quant.max);
        }

        fn matchQuantified(
            input: []const u8,
            i_pos: usize,
            comptime pat: []const u8,
            comptime next_p: usize,
            comptime elem: PatternElement,
            comptime min: usize,
            comptime max: usize,
        ) bool {
            // Greedy matching: try max matches first, then backtrack
            var matched: usize = 0;
            var pos = i_pos;

            // SIMD fast path: for simple character classes with unbounded quantifiers,
            // validate 16 bytes at a time using @Vector(16, u8) range comparisons.
            // This covers common patterns like \d+, \w+, [a-zA-Z]+, [0-9]+.
            if (comptime max >= 16 and canSimdMatch(elem)) {
                while (matched + 16 <= max and pos + 16 <= input.len) {
                    const chunk: @Vector(16, u8) = input[pos..][0..16].*;
                    const all_match = simdMatchChunk(chunk, elem);
                    if (!all_match) break;
                    pos += 16;
                    matched += 16;
                }
            }

            // Scalar remainder: match one byte at a time
            while (matched < max and pos < input.len) {
                if (!matchChar(input[pos], elem)) break;
                pos += 1;
                matched += 1;
            }

            // Backtrack from max to min
            while (matched >= min) {
                if (matchAt(input, i_pos + matched, pat, next_p)) return true;
                if (matched == 0) break;
                matched -= 1;
            }
            return false;
        }

        /// Returns true if this element kind can use the 16-byte SIMD fast path.
        fn canSimdMatch(comptime elem: PatternElement) bool {
            return switch (elem.kind) {
                .digit, .word, .space, .dot => true,
                .char_class => elem.class_spec.len > 0,
                .neg_char_class => elem.class_spec.len > 0,
                .literal => false, // Single literal — scalar is fine
            };
        }

        /// Check if all 16 bytes in a chunk match the pattern element.
        /// Uses @Vector comparisons for parallel range checks.
        fn simdMatchChunk(chunk: @Vector(16, u8), comptime elem: PatternElement) bool {
            return switch (elem.kind) {
                .dot => true, // '.' matches everything
                .digit => blk: {
                    // [0-9]: chunk >= '0' AND chunk <= '9'
                    const ge_0 = chunk >= @as(@Vector(16, u8), @splat(@as(u8, '0')));
                    const le_9 = chunk <= @as(@Vector(16, u8), @splat(@as(u8, '9')));
                    const valid = @as(@Vector(16, bool), ge_0) and @as(@Vector(16, bool), le_9);
                    break :blk @reduce(.And, valid);
                },
                .word => blk: {
                    // [a-zA-Z0-9_]: four ranges OR'd together
                    const ge_a = chunk >= @as(@Vector(16, u8), @splat(@as(u8, 'a')));
                    const le_z = chunk <= @as(@Vector(16, u8), @splat(@as(u8, 'z')));
                    const lower = @as(@Vector(16, bool), ge_a) and @as(@Vector(16, bool), le_z);

                    const ge_A = chunk >= @as(@Vector(16, u8), @splat(@as(u8, 'A')));
                    const le_Z = chunk <= @as(@Vector(16, u8), @splat(@as(u8, 'Z')));
                    const upper = @as(@Vector(16, bool), ge_A) and @as(@Vector(16, bool), le_Z);

                    const ge_0 = chunk >= @as(@Vector(16, u8), @splat(@as(u8, '0')));
                    const le_9 = chunk <= @as(@Vector(16, u8), @splat(@as(u8, '9')));
                    const digit = @as(@Vector(16, bool), ge_0) and @as(@Vector(16, bool), le_9);

                    const underscore = chunk == @as(@Vector(16, u8), @splat(@as(u8, '_')));

                    const valid = lower or upper or digit or @as(@Vector(16, bool), underscore);
                    break :blk @reduce(.And, valid);
                },
                .space => blk: {
                    // [ \t\n\r]
                    const sp = chunk == @as(@Vector(16, u8), @splat(@as(u8, ' ')));
                    const tab = chunk == @as(@Vector(16, u8), @splat(@as(u8, '\t')));
                    const nl = chunk == @as(@Vector(16, u8), @splat(@as(u8, '\n')));
                    const cr = chunk == @as(@Vector(16, u8), @splat(@as(u8, '\r')));
                    const valid = @as(@Vector(16, bool), sp) or @as(@Vector(16, bool), tab) or @as(@Vector(16, bool), nl) or @as(@Vector(16, bool), cr);
                    break :blk @reduce(.And, valid);
                },
                .char_class => blk: {
                    // Build SIMD check from comptime character class spec
                    break :blk simdCharClass(chunk, elem.class_spec, false);
                },
                .neg_char_class => blk: {
                    break :blk simdCharClass(chunk, elem.class_spec, true);
                },
                .literal => false, // Shouldn't reach here (canSimdMatch returns false)
            };
        }

        /// SIMD character class matching: check all 16 bytes against [spec].
        /// Parses ranges (a-z) and single chars from the comptime spec string.
        fn simdCharClass(chunk: @Vector(16, u8), comptime spec: []const u8, comptime negate: bool) bool {
            var any_match: @Vector(16, bool) = @splat(false);
            comptime var si: usize = 0;
            inline while (si < spec.len) {
                if (si + 2 < spec.len and spec[si + 1] == '-') {
                    // Range: spec[si]-spec[si+2]
                    const ge = chunk >= @as(@Vector(16, u8), @splat(spec[si]));
                    const le = chunk <= @as(@Vector(16, u8), @splat(spec[si + 2]));
                    any_match = any_match or (@as(@Vector(16, bool), ge) and @as(@Vector(16, bool), le));
                    si += 3;
                } else {
                    // Single char
                    const eq = chunk == @as(@Vector(16, u8), @splat(spec[si]));
                    any_match = any_match or @as(@Vector(16, bool), eq);
                    si += 1;
                }
            }
            if (negate) {
                // Negated: ALL bytes must NOT match any range
                return !@reduce(.Or, any_match);
            } else {
                // Normal: ALL bytes must match at least one range
                return @reduce(.And, any_match);
            }
        }

        const PatternElement = struct {
            kind: enum { literal, dot, digit, word, space, char_class, neg_char_class },
            literal_char: u8,
            // For character classes: comptime ranges
            class_spec: []const u8,
            end_pos: usize,
        };

        fn parseElement(comptime pat: []const u8, comptime pos: usize) PatternElement {
            if (pos >= pat.len) return .{
                .kind = .literal,
                .literal_char = 0,
                .class_spec = "",
                .end_pos = pos,
            };

            const c = pat[pos];

            // Escape sequences
            if (c == '\\' and pos + 1 < pat.len) {
                const next = pat[pos + 1];
                return switch (next) {
                    'd' => .{ .kind = .digit, .literal_char = 0, .class_spec = "", .end_pos = pos + 2 },
                    'w' => .{ .kind = .word, .literal_char = 0, .class_spec = "", .end_pos = pos + 2 },
                    's' => .{ .kind = .space, .literal_char = 0, .class_spec = "", .end_pos = pos + 2 },
                    else => .{ .kind = .literal, .literal_char = next, .class_spec = "", .end_pos = pos + 2 },
                };
            }

            // Dot — any character
            if (c == '.') return .{
                .kind = .dot,
                .literal_char = 0,
                .class_spec = "",
                .end_pos = pos + 1,
            };

            // Character class [...]
            if (c == '[') {
                var end = pos + 1;
                const negated = end < pat.len and pat[end] == '^';
                if (negated) end += 1;
                // Find closing ]
                while (end < pat.len and pat[end] != ']') : (end += 1) {}
                if (end < pat.len) end += 1; // skip ]
                const spec_start = if (negated) pos + 2 else pos + 1;
                const spec_end = if (end > 0) end - 1 else end;
                return .{
                    .kind = if (negated) .neg_char_class else .char_class,
                    .literal_char = 0,
                    .class_spec = pat[spec_start..spec_end],
                    .end_pos = end,
                };
            }

            // Literal character
            return .{
                .kind = .literal,
                .literal_char = c,
                .class_spec = "",
                .end_pos = pos + 1,
            };
        }

        const Quantifier = struct {
            min: usize,
            max: usize,
            end_pos: usize,
        };

        fn parseQuantifier(comptime pat: []const u8, comptime pos: usize) Quantifier {
            if (pos >= pat.len) return .{ .min = 1, .max = 1, .end_pos = pos };

            return switch (pat[pos]) {
                '+' => .{ .min = 1, .max = 65536, .end_pos = pos + 1 },
                '*' => .{ .min = 0, .max = 65536, .end_pos = pos + 1 },
                '?' => .{ .min = 0, .max = 1, .end_pos = pos + 1 },
                '{' => parseBraceQuantifier(pat, pos),
                else => .{ .min = 1, .max = 1, .end_pos = pos },
            };
        }

        fn parseBraceQuantifier(comptime pat: []const u8, comptime pos: usize) Quantifier {
            // Parse {n} or {n,m}
            var end = pos + 1;
            var n: usize = 0;
            while (end < pat.len and pat[end] >= '0' and pat[end] <= '9') {
                n = n * 10 + (pat[end] - '0');
                end += 1;
            }
            if (end < pat.len and pat[end] == '}') {
                return .{ .min = n, .max = n, .end_pos = end + 1 };
            }
            if (end < pat.len and pat[end] == ',') {
                end += 1;
                var m: usize = 0;
                var has_m = false;
                while (end < pat.len and pat[end] >= '0' and pat[end] <= '9') {
                    m = m * 10 + (pat[end] - '0');
                    has_m = true;
                    end += 1;
                }
                if (end < pat.len and pat[end] == '}') {
                    return .{
                        .min = n,
                        .max = if (has_m) m else 65536,
                        .end_pos = end + 1,
                    };
                }
            }
            // Malformed — treat { as literal
            return .{ .min = 1, .max = 1, .end_pos = pos };
        }

        /// Match a single character against a pattern element.
        /// Scalar byte comparison — called per-character in the matchQuantified loop.
        fn matchChar(c: u8, comptime elem: PatternElement) bool {
            return switch (elem.kind) {
                .literal => c == elem.literal_char,
                .dot => true,
                .digit => c >= '0' and c <= '9',
                .word => (c >= 'a' and c <= 'z') or (c >= 'A' and c <= 'Z') or (c >= '0' and c <= '9') or c == '_',
                .space => c == ' ' or c == '\t' or c == '\n' or c == '\r',
                .char_class => matchCharClass(c, elem.class_spec),
                .neg_char_class => !matchCharClass(c, elem.class_spec),
            };
        }

        fn matchCharClass(c: u8, comptime spec: []const u8) bool {
            var i: usize = 0;
            while (i < spec.len) {
                if (i + 2 < spec.len and spec[i + 1] == '-') {
                    // Range: a-z
                    if (c >= spec[i] and c <= spec[i + 2]) return true;
                    i += 3;
                } else {
                    // Single char
                    if (c == spec[i]) return true;
                    i += 1;
                }
            }
            return false;
        }
    };
}

/// ValidationResult represents the outcome of validation.
/// Contains either a valid value or a list of errors.
pub fn ValidationResult(comptime T: type) type {
    return union(enum) {
        valid: T,
        invalid: ValidationErrors,

        pub fn isValid(self: @This()) bool {
            return self == .valid;
        }

        pub fn value(self: @This()) ?T {
            return switch (self) {
                .valid => |v| v,
                .invalid => null,
            };
        }

        pub fn errors(self: @This()) ?ValidationErrors {
            return switch (self) {
                .valid => null,
                .invalid => |e| e,
            };
        }

        pub fn deinit(self: *@This()) void {
            switch (self.*) {
                .valid => {},
                .invalid => |*e| e.deinit(),
            }
        }
    };
}

/// validateStruct uses @typeInfo to validate struct fields based on naming conventions.
/// Uses @typeInfo to validate struct fields based on naming conventions.
///
/// Field naming conventions:
///   - "*_ne": Non-empty string (min_length=1)
///   - "*_email": Email format
///   - Can be extended with more conventions
///
/// Example:
///   const User = struct {
///       name_ne: []const u8,
///       email: []const u8,
///       age: u8,
///   };
pub fn validateStruct(comptime T: type, val: T, errors: *ValidationErrors) !void {
    const info = @typeInfo(T);
    if (info != .@"struct") @compileError("validateStruct expects a struct");

    inline for (info.@"struct".fields) |f| {
        const field_val = @field(val, f.name);

        // Convention: fields ending with "_ne" must be non-empty strings
        if (std.mem.endsWith(u8, f.name, "_ne")) {
            if (@TypeOf(field_val) == []const u8 and field_val.len == 0) {
                try errors.add(f.name, "Field cannot be empty");
            }
        }

        // Convention: fields named "email" or ending with "_email" must be valid emails
        if (std.mem.eql(u8, f.name, "email") or std.mem.endsWith(u8, f.name, "_email")) {
            if (@TypeOf(field_val) == []const u8) {
                _ = Email.validate(field_val, errors, f.name) catch {};
            }
        }

        // Pattern validation is handled by the Pattern() type above.
        // Min/max constraints are enforced by the model validation layer.
    }
}

/// deriveValidator generates a validation function for a struct at comptime.
/// This is a more advanced pattern that can be extended with field tags or attributes.
pub fn deriveValidator(comptime T: type) type {
    return struct {
        pub fn validate(val: anytype, allocator: std.mem.Allocator) !ValidationResult(T) {
            var errors = ValidationErrors.init(allocator);

            // Use @typeInfo to walk fields and apply validation rules
            validateStruct(T, val, &errors) catch {
                // Validation failed, but errors are already collected
            };

            if (errors.hasErrors()) {
                return ValidationResult(T){ .invalid = errors };
            } else {
                errors.deinit();
                return ValidationResult(T){ .valid = val };
            }
        }
    };
}

// ============================================================================
// Tests
// ============================================================================

test "BoundedInt - valid range" {
    const Age = BoundedInt(u8, 0, 130);
    const age = try Age.init(27);
    try std.testing.expectEqual(@as(u8, 27), age.value);
}

test "BoundedInt - out of range" {
    const Age = BoundedInt(u8, 0, 130);
    const result = Age.init(200);
    try std.testing.expectError(error.OutOfRange, result);
}

test "BoundedInt - validate with errors" {
    const Age = BoundedInt(u8, 18, 90);
    var errors = ValidationErrors.init(std.testing.allocator);
    defer errors.deinit();

    _ = Age.validate(15, &errors, "age") catch {};
    try std.testing.expect(errors.hasErrors());
    try std.testing.expectEqual(@as(usize, 1), errors.count());
}

test "BoundedString - valid length" {
    const Name = BoundedString(1, 40);
    const name = try Name.init("Rach");
    try std.testing.expectEqualStrings("Rach", name.slice);
}

test "BoundedString - too short" {
    const Name = BoundedString(1, 40);
    const result = Name.init("");
    try std.testing.expectError(error.TooShort, result);
}

test "BoundedString - too long" {
    const Name = BoundedString(1, 10);
    const result = Name.init("ThisIsWayTooLongForTheLimit");
    try std.testing.expectError(error.TooLong, result);
}

test "Email - valid format" {
    const email = try Email.init("rach@example.com");
    try std.testing.expectEqualStrings("rach@example.com", email.value);
}

test "Email - invalid format (no @)" {
    const result = Email.init("notanemail");
    try std.testing.expectError(error.InvalidEmail, result);
}

test "Email - invalid format (no domain)" {
    const result = Email.init("rach@");
    try std.testing.expectError(error.InvalidEmail, result);
}

test "Email - two @ in one SIMD chunk rejected" {
    // "aa@bb@cc.example" is 16 bytes: both '@' land in the same 16-byte SIMD
    // chunk. @ctz only sees the first, so without the @popCount guard this
    // was silently accepted (SIMD/scalar divergence).
    try std.testing.expectError(error.InvalidEmail, Email.init("aa@bb@cc.example"));
    // Cross-chunk double-@ (second '@' in the scalar remainder / next chunk).
    try std.testing.expectError(error.InvalidEmail, Email.init("aaaaaaaaaaaaaaaa@b@.com"));
    // Sanity: the single-@ near-boundary case still validates.
    const ok = try Email.init("aaaaaaaaaaaaa@b.com");
    try std.testing.expectEqualStrings("aaaaaaaaaaaaa@b.com", ok.value);
}

test "Email - empty first domain label rejected (SIMD/scalar parity)" {
    // Bug: the SIMD chunk path accepted a '.' immediately after '@' as a valid
    // dot-after-@, while the scalar remainder (i > at_pos + 1) rejects it. The
    // verdict must not depend on 16-byte alignment.

    // Short input — handled entirely by the scalar remainder.
    try std.testing.expectError(error.InvalidEmail, Email.init("user@.com"));

    // Same-chunk case: 30-char local part puts '@' at local pos 14 and '.' at
    // local pos 15 inside the fully-processed second 16-byte chunk.
    const same_chunk = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa@.com"; // 30 a's
    try std.testing.expectError(error.InvalidEmail, Email.init(same_chunk));

    // Cross-chunk case: '@' is the last byte of chunk 0 (pos 15) and '.' is the
    // first byte of the fully-processed chunk 1 (pos 16 == at_pos+1). Exercises
    // the `else if (has_at and dot_bits != 0)` boundary exclusion.
    const cross_chunk = "aaaaaaaaaaaaaaa@." ++ ("c" ** 16); // len 33
    try std.testing.expectError(error.InvalidEmail, Email.init(cross_chunk));

    // Valid control: same 30-char local part but a real domain char before the
    // dot must still validate (dot legitimately after @).
    const ok = try Email.init("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa@example.com");
    try std.testing.expectEqualStrings("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa@example.com", ok.value);
}

test "ValidationError - format with path" {
    var err = try ValidationError.initWithPath(
        std.testing.allocator,
        "age",
        "Must be >= 18",
        &.{ "user", "profile" },
    );
    defer err.deinit();

    var buf: [100]u8 = undefined;
    const formatted = try std.fmt.bufPrint(&buf, "{f}", .{err});
    try std.testing.expectEqualStrings("user.profile.age: Must be >= 18", formatted);
}

test "ValidationErrors - collect multiple" {
    var errors = ValidationErrors.init(std.testing.allocator);
    defer errors.deinit();

    try errors.add("age", "Must be >= 18");
    try errors.add("email", "Invalid format");

    try std.testing.expectEqual(@as(usize, 2), errors.count());
    try std.testing.expect(errors.hasErrors());
}

test "ValidationResult - valid case" {
    const Result = ValidationResult(u32);
    const result = Result{ .valid = 42 };

    try std.testing.expect(result.isValid());
    try std.testing.expectEqual(@as(u32, 42), result.value().?);
}

test "ValidationResult - invalid case" {
    const Result = ValidationResult(u32);
    var errors = ValidationErrors.init(std.testing.allocator);
    try errors.add("value", "Too large");

    var result = Result{ .invalid = errors };
    defer result.deinit();

    try std.testing.expect(!result.isValid());
    try std.testing.expectEqual(@as(?u32, null), result.value());
}

test "validateStruct - non-empty convention" {
    const User = struct {
        name_ne: []const u8,
        age: u8,
    };

    var errors = ValidationErrors.init(std.testing.allocator);
    defer errors.deinit();

    const user = User{ .name_ne = "", .age = 27 };
    try validateStruct(User, user, &errors);

    try std.testing.expect(errors.hasErrors());
    try std.testing.expectEqual(@as(usize, 1), errors.count());
}

test "validateStruct - email convention" {
    const User = struct {
        email: []const u8,
        age: u8,
    };

    var errors = ValidationErrors.init(std.testing.allocator);
    defer errors.deinit();

    const user = User{ .email = "not-an-email", .age = 27 };
    try validateStruct(User, user, &errors);

    try std.testing.expect(errors.hasErrors());
}

test "deriveValidator - happy path" {
    const User = struct {
        name: []const u8,
        age: u8,
    };

    const Validator = deriveValidator(User);
    const user = User{ .name = "Rach", .age = 27 };

    var result = try Validator.validate(user, std.testing.allocator);
    defer result.deinit();

    try std.testing.expect(result.isValid());
}
