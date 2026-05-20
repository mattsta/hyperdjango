const std = @import("std");
const lib = @import("lib.zig");

const types = lib.types;
const proto = lib.proto;
const Conn = lib.Conn;
const Allocator = std.mem.Allocator;
const ArenaAllocator = std.heap.ArenaAllocator;

pub const Result = struct {
    number_of_columns: usize,

    // will be empty unless the query was executed with the column_names = true option
    column_names: [][]const u8,

    _conn: *Conn,
    _arena: *ArenaAllocator,

    // a sliced version of _state.oids (so we don't have to keep reslicing it to
    // number_of_columns on each row)
    _oids: []i32,

    // a sliced version of _state.values (so we don't have to keep reslicing it to
    // number_of_columns on each row)
    _values: []State.Value,

    // When true, result.deinit() will call conn.release()
    // Used when the result came directly from the pool.query() helper.
    _release_conn: bool,

    pub fn deinit(self: *const Result) void {
        // value.data references the buffer of the reader, this buffer is potentially
        // reused and potentially discarded. There are at least a few very good
        // reasons why the least we can do is blank it out.
        for (self._values) |*value| {
            value.data = &[_]u8{};
        }

        self._conn._reader.endFlow() catch {
            // this can only fail in extreme conditions (OOM) and it will only impact
            // the next query (and if the app is using the pool, the pool will try to
            // recover from this anyways)
            self._conn._state = .fail;
        };

        if (self._release_conn) {
            self._conn.release();
        }

        const arena = self._arena;
        const allocator = arena.child_allocator;
        arena.deinit();
        allocator.destroy(arena);
    }

    // Caller should typically call next() until null is returned.
    // But in some cases, that might not be desirable. So they can
    // "drain" to empty the rest of the result.
    // I don't want to do this implictly in deinit because it can fail
    // and returning an error union in deinit is a pain for the caller.
    pub fn drain(self: *Result) !void {
        var conn = self._conn;
        if (conn._state == .idle) {
            return;
        }

        while (true) {
            const msg = try conn.read();
            switch (msg.type) {
                'C' => {}, // CommandComplete
                'D' => {}, // DataRow
                'Z' => return,
                else => return error.UnexpectedDBMessage,
            }
        }
    }

    pub fn next(self: *Result) !?Row {
        return self._next(.safe);
    }
    pub fn nextUnsafe(self: *Result) !?RowUnsafe {
        return self._next(.unsafe);
    }

    fn _next(self: *Result, comptime fail_mode: lib.FailMode) !(if (fail_mode == .safe) ?Row else ?RowUnsafe) {
        if (self._conn._state != .query) {
            // Possibly weird state. Most likely cause is calling next() multiple times
            // despite null being returned.
            return null;
        }

        const msg = try self._conn.read();
        switch (msg.type) {
            'D' => {
                const data = msg.data;
                // Since our Row API gets data by column #, we need translate the column
                // # to a slice within msg.data. We could do this on the fly within Row,
                // but creating this mapping up front simplifies things and, in normal
                // cases, performs best. "Normal case" here assumes that the client app
                // is going to fetch most/all columns.

                // first column starts at position 2
                var offset: usize = 2;
                const values = self._values;
                for (values) |*value| {
                    // The 4-byte field-length prefix is attacker-controlled wire
                    // data: validate it fits in `data` before reading, reject any
                    // length < -1, and confirm the sliced payload stays in bounds.
                    // reader.zig only validates the OUTER frame; this is the sole
                    // guard for the INNER per-column lengths every row flows through.
                    const data_start = offset + 4;
                    if (data_start > data.len) return error.InvalidDataRow;
                    const length = std.mem.readInt(i32, data[offset..data_start][0..4], .big);
                    if (length == -1) {
                        value.is_null = true;
                        value.data = &[_]u8{};
                        offset = data_start;
                    } else {
                        if (length < -1) return error.InvalidDataRow;
                        const data_end = data_start + @as(usize, @intCast(length));
                        if (data_end > data.len) return error.InvalidDataRow;
                        value.is_null = false;
                        value.data = data[data_start..data_end];
                        offset = data_end;
                    }
                }

                return .{
                    .values = values,
                    .oids = self._oids,
                    ._result = self,
                };
            },
            'C' => {
                try self._conn.readyForQuery();
                return null;
            },
            else => return error.UnexpectedDBMessage,
        }
    }

    pub fn columnIndex(self: *const Result, column_name: []const u8) ?usize {
        for (self.column_names, 0..) |n, i| {
            if (std.mem.eql(u8, n, column_name)) {
                return i;
            }
        }
        return null;
    }

    const MapperOpts = struct {
        dupe: bool = false,
        allocator: ?Allocator = null,
    };

    pub fn mapper(self: *Result, comptime T: type, opts: MapperOpts) Mapper(T) {
        var column_indexes: [std.meta.fields(T).len]?usize = undefined;

        inline for (std.meta.fields(T), 0..) |field, i| {
            column_indexes[i] = self.columnIndex(field.name);
        }

        // if we're given an allocator, use that.
        // if we're not given an allocator, but asked to dupe use our arena and thus
        // tie the lifetime of the returned T to the lifetime of the DB result object.
        var allocator: ?Allocator = null;
        if (opts.allocator) |a| {
            allocator = a;
        } else if (opts.dupe) {
            allocator = self._arena.allocator();
        }

        return .{
            .result = self,
            .allocator = allocator,
            .column_indexes = column_indexes,
        };
    }

    // For every query, we need to store the type of each column (so we know
    // how to parse the data). Optionally, we might need the name of each column.
    // The connection has a default Result.State for a max # of columns, and we'll use
    // that whenever we can. Otherwise, we'll create this dynamically.
    pub const State = struct {
        // The name for each returned column, we only populate this if we're told
        // to (since it requires us to dupe the data)
        names: [][]const u8,

        // This is different than the above. The above are set once per query
        // from the RowDescription response of our Describe message. This is set for
        // each DataRow message we receive. It maps a column position with the encoded
        // value.
        values: []Value,

        // The OID for each returned column
        oids: []i32,

        pub const Value = struct {
            is_null: bool,
            data: []const u8,
        };

        pub fn init(allocator: Allocator, size: usize) !State {
            const names = try allocator.alloc([]const u8, size);
            errdefer allocator.free(names);

            const values = try allocator.alloc(Value, size);
            errdefer allocator.free(values);

            const oids = try allocator.alloc(i32, size);
            errdefer allocator.free(oids);

            return .{
                .names = names,
                .values = values,
                .oids = oids,
            };
        }

        // Populates the State from the RowDescription payload
        // We already read the number_of_columns from data, so we pass it in here
        // We also already know that number_of_columns fits within our arrays
        pub fn from(self: *State, number_of_columns: u16, data: []const u8, allocator: ?Allocator) !void {
            // skip the column count, which we already know as number_of_columns
            var pos: usize = 2;

            for (0..number_of_columns) |i| {
                const end_pos = std.mem.indexOfScalarPos(u8, data, pos, 0) orelse return error.InvalidDataRow;
                if (data.len < (end_pos + 19)) {
                    return error.InvalidDataRow;
                }
                if (allocator) |a| {
                    self.names[i] = try a.dupe(u8, data[pos..end_pos]);
                }

                // skip the name null terminator (1)
                // skip the table object_id this table belongs to (4)
                // skip the attribute number of this table column (2)
                pos = end_pos + 7;

                {
                    const end = pos + 4;
                    self.oids[i] = std.mem.readInt(i32, data[pos..end][0..4], .big);
                    pos = end;
                }

                // skip date type size (2), type modifier (4) format code (2)
                pos += 8;
            }
        }

        pub fn deinit(self: State, allocator: Allocator) void {
            allocator.free(self.names);
            allocator.free(self.values);
            allocator.free(self.oids);
        }
    };
};

pub const Row = RowT(.safe);
pub const RowUnsafe = RowT(.unsafe);

pub fn RowT(comptime fail_mode: lib.FailMode) type {
    return struct {
        _result: *Result,
        oids: []i32,
        values: []Result.State.Value,

        const Self = @This();

        pub fn get(self: *const Self, comptime T: type, col: usize) if (fail_mode == .safe) lib.TypeError!T else T {
            const value = self.values[col];
            const TT = switch (@typeInfo(T)) {
                .optional => |opt| {
                    if (value.is_null) {
                        return null;
                    }
                    const val = self.get(opt.child, col);
                    if (comptime fail_mode == .safe) {
                        return try val;
                    }
                    return val;
                },
                .@"struct" => blk: {
                    if (@hasDecl(T, "fromPgzRow") == true) {
                        return T.fromPgzRow(value.data, self.oids[col]) catch |err| {
                            if (comptime fail_mode == .safe) {
                                return err;
                            }
                            std.debug.panic("PostgreSQL value of type {s} could not be read into a " ++ @typeName(T) ++ ".", .{types.oidToString(self.oids[col])});
                        };
                    }
                    break :blk T;
                },
                else => blk: {
                    lib.verifyNotNull(fail_mode, T, value.is_null) catch |err| {
                        if (comptime fail_mode == .unsafe) unreachable;
                        return err;
                    };
                    break :blk T;
                },
            };

            return getScalar(fail_mode, TT, value.data, self.oids[col]);
        }

        pub fn getCol(self: *const Self, comptime T: type, name: []const u8) if (fail_mode == .safe) lib.TypeError!T else T {
            const col = self._result.columnIndex(name);
            try lib.verifyColumnName(fail_mode, name, col != null);
            return self.get(T, col.?);
        }

        pub fn iterator(self: *const Self, comptime T: type, col: usize) if (fail_mode == .safe) lib.TypeError!Iterator(T) else IteratorUnsafe(T) {
            const value = self.values[col];
            if (value.is_null) {
                return IteratorT(fail_mode, T).asNull();
            }
            return IteratorT(fail_mode, T).fromPgzRow(value.data, self.oids[col]) catch |err| {
                if (comptime fail_mode == .safe) {
                    return err;
                }
                @panic("Could not get iterator of type " ++ @typeName(T) ++ " for row.");
            };
        }

        pub fn iteratorCol(self: *const Self, comptime T: type, name: []const u8) if (fail_mode == .safe) lib.TypeError!Iterator(T) else IteratorUnsafe(T) {
            const col = self._result.columnIndex(name);
            try lib.verifyColumnName(fail_mode, name, col != null);
            return self.iterator(T, col.?);
        }

        pub fn record(self: *const Self, col: usize) RecordT(fail_mode) {
            const data = self.values[col].data;
            // The 4-byte column count is a prefix of peer-controlled composite
            // wire data. record() has no error channel, so guard the read and a
            // negative count and hand back an empty record (next() yields nothing)
            // rather than over-reading / wrapping the @intCast.
            if (data.len < 4) {
                return .{ .data = &.{}, .number_of_columns = 0 };
            }
            const number_of_columns = std.mem.readInt(i32, data[0..4], .big);
            if (number_of_columns < 0) {
                return .{ .data = &.{}, .number_of_columns = 0 };
            }
            return .{
                .data = data[4..],
                .number_of_columns = @intCast(number_of_columns),
            };
        }

        pub fn recordCol(self: *const Self, name: []const u8) if (fail_mode == .safe) lib.TypeError!Record else RecordUnsafe {
            const col = self._result.columnIndex(name);
            try lib.verifyColumnName(fail_mode, name, col != null);
            return self.record(col);
        }

        const ToOpts = struct {
            dupe: bool = false,
            map: Mapping = .ordinal,
            allocator: ?Allocator = null,

            const Mapping = enum {
                name,
                ordinal,
            };
        };

        pub fn to(self: *const Self, T: type, opts: ToOpts) !T {
            // if we're given an allocator, use that.
            // if we're not given an allocator, but asked to dupe use our arena and thus
            // tie the lifetime of the returned T to the lifetime of the DB result object.
            var allocator: ?Allocator = null;
            if (opts.allocator) |a| {
                allocator = a;
            } else if (opts.dupe) {
                allocator = self._result._arena.allocator();
            }

            return switch (opts.map) {
                .ordinal => self.toUsingOrdinal(T, allocator),
                .name => return self.toUsingName(T, allocator),
            };
        }

        fn toUsingOrdinal(self: *const Self, T: type, allocator: ?Allocator) !T {
            var value: T = undefined;
            inline for (std.meta.fields(T), 0..) |field, column_index| {
                @field(value, field.name) = try self.mapColumn(field, column_index, allocator);
            }
            return value;
        }

        fn toUsingName(self: *const Self, T: type, allocator: ?Allocator) !T {
            var value: T = undefined;
            const result = self._result;
            inline for (std.meta.fields(T)) |field| {
                const name = field.name;
                @field(value, name) = try self.mapColumn(field, result.columnIndex(name), allocator);
            }
            return value;
        }

        fn mapColumn(self: *const Self, comptime field: std.builtin.Type.StructField, optional_column_index: ?usize, allocator: ?Allocator) !field.type {
            const T = field.type;
            const column_index = optional_column_index orelse {
                if (field.default_value_ptr) |dflt| {
                    return @as(*align(1) const field.type, @ptrCast(dflt)).*;
                }
                return error.FieldColumnMismatch;
            };

            if (comptime isSlice(T)) |S| {
                const slice = blk: {
                    if (@typeInfo(T) == .optional) {
                        break :blk self.get(?Iterator(S), column_index) orelse return null;
                    } else {
                        break :blk self.get(Iterator(S), column_index);
                    }
                };
                return try slice.alloc(allocator orelse return error.AllocatorRequiredForSliceMapping);
            }

            const value = self.get(field.type, column_index);
            const a = allocator orelse return value;
            return mapValue(T, if (comptime fail_mode == .safe) try value else value, a);
        }

        /// Write a single column's value as JSON to a buffer.
        /// Handles all Postgres types: int, float, numeric, bool, text, jsonb, arrays, timestamps.
        /// Returns the number of bytes written.
        pub fn writeJsonValue(self: *const Self, col: usize, buf: []u8) usize {
            const value = self.values[col];
            if (value.is_null) {
                if (buf.len < 4) return 0;
                @memcpy(buf[0..4], "null");
                return 4;
            }

            const data = value.data;
            const oid = self.oids[col];
            var pos: usize = 0;

            switch (oid) {
                // Integers
                21 => { // int2
                    // Wire payload is peer-controlled: verify the fixed width is
                    // present before the read (guards a truncated/desynced field).
                    // Too-short -> emit JSON null rather than reading OOB.
                    if (data.len < 2) return jsonNull(buf[pos..]);
                    const v = std.mem.readInt(i16, data[0..2], .big);
                    const s = std.fmt.bufPrint(buf[pos..], "{d}", .{v}) catch return 0;
                    pos += s.len;
                },
                23 => { // int4
                    if (data.len < 4) return jsonNull(buf[pos..]);
                    const v = std.mem.readInt(i32, data[0..4], .big);
                    const s = std.fmt.bufPrint(buf[pos..], "{d}", .{v}) catch return 0;
                    pos += s.len;
                },
                20 => { // int8
                    if (data.len < 8) return jsonNull(buf[pos..]);
                    const v = std.mem.readInt(i64, data[0..8], .big);
                    const s = std.fmt.bufPrint(buf[pos..], "{d}", .{v}) catch return 0;
                    pos += s.len;
                },
                // Floats
                700 => { // float4
                    if (data.len < 4) return jsonNull(buf[pos..]);
                    const n = std.mem.readInt(i32, data[0..4], .big);
                    const v: f32 = @bitCast(n);
                    // NaN / ±Inf have no JSON numeric literal — Zig's "{d}" would
                    // emit the bare tokens nan/inf/-inf, corrupting the document.
                    // Emit the LOSSLESS quoted form ("NaN"/"Infinity"/"-Infinity")
                    // rather than null, which would hide the value entirely.
                    if (!std.math.isFinite(v)) return jsonNonFinite(buf[pos..], v);
                    const s = std.fmt.bufPrint(buf[pos..], "{d}", .{v}) catch return 0;
                    pos += s.len;
                },
                701 => { // float8
                    if (data.len < 8) return jsonNull(buf[pos..]);
                    const n = std.mem.readInt(i64, data[0..8], .big);
                    const v: f64 = @bitCast(n);
                    if (!std.math.isFinite(v)) return jsonNonFinite(buf[pos..], v);
                    const s = std.fmt.bufPrint(buf[pos..], "{d}", .{v}) catch return 0;
                    pos += s.len;
                },
                // NUMERIC/DECIMAL — exact canonical decimal STRING (byte-identical
                // to db.zig's sibling serializer and str(Decimal)). Rendering it via
                // a float here would SILENTLY LOSE precision past ~15-17 significant
                // digits — corrupting the exact-decimal data NUMERIC exists to carry.
                // Non-finite values fall out of numericCanonical as the lossless
                // "NaN"/"Infinity"/"-Infinity" tokens. Emitted as a *quoted* JSON
                // string so arbitrary precision survives (JSON numbers are IEEE-754
                // doubles in JS/most parsers). A malformed wire value → null.
                1700 => {
                    // Zero-init (not `undefined`): numericCanonical returns only
                    // the prefix it writes, but zeroing removes any dependence on
                    // uninitialized bytes under ReleaseFast (arch-dependent
                    // garbage that ReleaseSafe masks with 0xAA). Cheap; 288 bytes.
                    var nbuf: [288]u8 = @splat(0);
                    const s = numericCanonical(data, &nbuf) orelse return jsonNull(buf[pos..]);
                    const w = writeQuotedAscii(buf[pos..], s);
                    if (w == 0) return 0;
                    pos += w;
                },
                // Bool
                16 => {
                    if (data.len < 1) return jsonNull(buf[pos..]);
                    const v = data[0] != 0;
                    const s = if (v) "true" else "false";
                    if (buf.len - pos < s.len) return 0;
                    @memcpy(buf[pos..][0..s.len], s);
                    pos += s.len;
                },
                // JSON — already valid JSON, pass through
                114 => {
                    if (buf.len - pos < data.len) return 0;
                    @memcpy(buf[pos..][0..data.len], data);
                    pos += data.len;
                },
                // JSONB — strip version byte, pass through
                3802 => {
                    if (data.len == 0) return 0;
                    const json_data = data[1..];
                    if (buf.len - pos < json_data.len) return 0;
                    @memcpy(buf[pos..][0..json_data.len], json_data);
                    pos += json_data.len;
                },
                // Integer arrays
                1005, 1007, 1016 => { // int2[], int4[], int8[]
                    pos += writeIntArrayJson(data, oid, buf[pos..]);
                },
                // Text arrays
                1009, 1015 => { // text[], varchar[]
                    pos += writeTextArrayJson(data, buf[pos..]);
                },
                // UUID — 16 binary bytes → canonical lowercase hyphenated string.
                // (The wire format is BINARY; quoting the raw bytes produced
                // corrupt/invalid JSON.)
                2950 => {
                    var ubuf: [36]u8 = undefined;
                    const s = uuidToStr(data, &ubuf) orelse return jsonNull(buf[pos..]);
                    const n = writeQuotedAscii(buf[pos..], s);
                    if (n == 0) return 0;
                    pos += n;
                },
                // DATE — i32 days since 2000-01-01 (binary) → "YYYY-MM-DD".
                1082 => {
                    if (data.len < 4) return jsonNull(buf[pos..]);
                    const days = std.mem.readInt(i32, data[0..4], .big);
                    var dbuf: [16]u8 = undefined;
                    const s = isoDate(&dbuf, days) orelse return jsonNull(buf[pos..]);
                    const n = writeQuotedAscii(buf[pos..], s);
                    if (n == 0) return 0;
                    pos += n;
                },
                // TIME — i64 microseconds since midnight (binary) → "HH:MM:SS[.ffffff]".
                1083 => {
                    if (data.len < 8) return jsonNull(buf[pos..]);
                    const usec = std.mem.readInt(i64, data[0..8], .big);
                    var tbuf: [20]u8 = undefined;
                    const s = isoTime(&tbuf, usec) orelse return jsonNull(buf[pos..]);
                    const n = writeQuotedAscii(buf[pos..], s);
                    if (n == 0) return 0;
                    pos += n;
                },
                // BYTEA — raw binary → `\xDEADBEEF` hex JSON string (matches PG's
                // ::text rendering and db.zig's writeJsonHexString; never raw bytes).
                17 => {
                    const n = writeJsonHex(buf[pos..], data);
                    if (n == 0) return 0;
                    pos += n;
                },
                // TIMESTAMP / TIMESTAMPTZ — i64 microseconds since 2000-01-01
                // (binary) → naive ISO-8601 "YYYY-MM-DDTHH:MM:SS[.ffffff]". The old
                // branch emitted a bare quoted epoch-SECONDS integer, truncating
                // sub-second precision and not being ISO-8601 at all. Naive (no 'Z')
                // to stay byte-identical with db.zig / pg_render.writeIsoTimestamp.
                1114, 1184 => {
                    if (data.len < 8) return jsonNull(buf[pos..]);
                    const usec = std.mem.readInt(i64, data[0..8], .big);
                    var tbuf: [40]u8 = undefined;
                    const s = isoTimestamp(&tbuf, usec) orelse return jsonNull(buf[pos..]);
                    const n = writeQuotedAscii(buf[pos..], s);
                    if (n == 0) return 0;
                    pos += n;
                },
                // Everything else — check for pgvector, then fall back to a string.
                else => {
                    // pgvector support — dynamic OID, check at runtime
                    if (types.Vector.oid_decimal != 0 and oid == types.Vector.oid_decimal) {
                        const vec = types.Vector.decode(data);
                        const n = vec.writeJson(buf[pos..]);
                        // writeJson returns 0 only when the destination is too
                        // small (a legit empty vector still returns 2 for "[]").
                        if (n == 0) return 0;
                        pos += n;
                        return pos;
                    }
                    // Text-like types (text/varchar/name/enum/…) arrive as valid
                    // UTF-8 even in binary mode → emit an escaped JSON string. But
                    // resultEncodingFor defaults UNLISTED oids to BINARY, so a
                    // genuinely-binary type (inet, interval, macaddr, timetz, …)
                    // whose bytes are NOT valid UTF-8 would produce INVALID JSON if
                    // quoted. Emit those as a lossless `\x`hex string instead —
                    // identical to db.zig's sibling serializer (no raw binary).
                    if (std.unicode.utf8ValidateSlice(data)) {
                        if (buf.len - pos < 1) return 0;
                        buf[pos] = '"';
                        pos += 1;
                        pos += simdJsonEscape(data, buf[pos..]) orelse return 0;
                        if (buf.len - pos < 1) return 0;
                        buf[pos] = '"';
                        pos += 1;
                    } else {
                        const n = writeJsonHex(buf[pos..], data);
                        if (n == 0) return 0;
                        pos += n;
                    }
                },
            }
            return pos;
        }

        /// Write an entire row as a JSON object: {"col1":val1,"col2":val2,...}
        /// Requires column_names to be populated (queryOpts with .column_names = true).
        pub fn writeJsonRow(self: *const Self, col_names: []const []const u8, buf: []u8) usize {
            var pos: usize = 0;
            if (buf.len - pos < 1) return 0;
            buf[pos] = '{';
            pos += 1;

            const ncols = @min(col_names.len, self.values.len);
            for (0..ncols) |i| {
                if (i > 0) {
                    if (buf.len - pos < 1) return 0;
                    buf[pos] = ',';
                    pos += 1;
                }
                // Column name: opening quote + name + closing quote + colon
                const name = col_names[i];
                if (buf.len - pos < name.len + 3) return 0;
                buf[pos] = '"';
                pos += 1;
                @memcpy(buf[pos..][0..name.len], name);
                pos += name.len;
                buf[pos] = '"';
                pos += 1;
                buf[pos] = ':';
                pos += 1;
                // Value — writeJsonValue returns 0 when it doesn't fit; propagate
                // that so serializeRow's grow-on-failure path engages.
                const vlen = self.writeJsonValue(i, buf[pos..]);
                if (vlen == 0) return 0;
                pos += vlen;
            }

            if (buf.len - pos < 1) return 0;
            buf[pos] = '}';
            pos += 1;
            return pos;
        }
    };
}

/// Emit a JSON `null` literal, used as the placeholder when a scalar column's
/// wire payload is too short to decode (malformed/truncated field). Returns 0
/// only when the output buffer itself can't hold 4 bytes, which propagates to
/// the caller's grow-and-retry path.
fn jsonNull(buf: []u8) usize {
    if (buf.len < 4) return 0;
    @memcpy(buf[0..4], "null");
    return 4;
}

/// Emit a non-finite IEEE-754 value as a LOSSLESS JSON *string* — `"NaN"`,
/// `"Infinity"`, or `"-Infinity"` — into `buf`. JSON has no numeric literal for
/// these, and the naive alternative (emit `null`) silently DESTROYS the value:
/// downstream a consumer cannot distinguish an infinite/NaN measurement from a
/// missing one, which is exactly the kind of data-loss bug that costs weeks to
/// trace. The quoted spellings are valid JSON in every parser AND round-trip
/// through Python `float(...)` / `Decimal(...)`. This is the single convention
/// shared with db.zig's `writeJsonFloat` and the NUMERIC path (`pgNumericToStr`
/// emits the same three tokens). Caller guarantees `v` is non-finite. Returns
/// bytes written, or 0 when it doesn't fit (→ caller's grow-and-retry).
fn jsonNonFinite(buf: []u8, v: f64) usize {
    const s = if (std.math.isNan(v)) "\"NaN\"" else if (v < 0) "\"-Infinity\"" else "\"Infinity\"";
    if (buf.len < s.len) return 0;
    @memcpy(buf[0..s.len], s);
    return s.len;
}

fn numCopyLiteral(buf: []u8, literal: []const u8) ?[]const u8 {
    if (buf.len < literal.len) return null;
    @memcpy(buf[0..literal.len], literal);
    return buf[0..literal.len];
}

/// Reformat the plain decimal magnitude in `buf[0..pos]` into the exact form
/// Python's Decimal.__str__ produces (scientific once the adjusted exponent drops
/// below -6). Byte-identical inline twin of pg_render.canonicalizeDecimal.
fn numCanonicalizeDecimal(buf: []u8, pos: usize, negative: bool) []const u8 {
    const sign_len: usize = if (negative) 1 else 0;
    if (pos < sign_len + 2) return buf[0..pos];
    const mag = buf[sign_len..pos];
    if (mag[0] != '0' or mag[1] != '.') return buf[0..pos];
    const frac = mag[2..];
    var first: usize = 0;
    while (first < frac.len and frac[first] == '0') first += 1;
    if (first >= frac.len) return buf[0..pos];
    const k = first + 1;
    if (k <= 6) return buf[0..pos];

    const coeff = frac[first..];
    var tmp: [512]u8 = undefined;
    var n: usize = 0;
    if (negative) {
        tmp[n] = '-';
        n += 1;
    }
    if (n + 1 > tmp.len) return buf[0..pos];
    tmp[n] = coeff[0];
    n += 1;
    if (coeff.len > 1) {
        if (n + 1 + (coeff.len - 1) > tmp.len) return buf[0..pos];
        tmp[n] = '.';
        n += 1;
        @memcpy(tmp[n..][0 .. coeff.len - 1], coeff[1..]);
        n += coeff.len - 1;
    }
    const es = std.fmt.bufPrint(tmp[n..], "E-{d}", .{k}) catch return buf[0..pos];
    n += es.len;
    if (n > buf.len) return buf[0..pos];
    @memcpy(buf[0..n], tmp[0..n]);
    return buf[0..n];
}

/// Render a PostgreSQL NUMERIC binary value into its canonical decimal string —
/// the exact text `str(Decimal(...))` produces (trailing zeros padded to dscale;
/// scientific below adjusted exponent -6; "NaN"/"Infinity"/"-Infinity" for the
/// specials). This is a BYTE-FOR-BYTE inline twin of pg_render.pgNumericToStr:
/// result.zig lives in the `pg` module and cannot @import the top-level
/// pg_render.zig, so the algorithm is duplicated (the same trade-off already made
/// for the writeIso* renderers). The two MUST stay identical so this JSON path and
/// db.zig's sibling serializer agree — the shared Zig test battery locks that in.
/// `buf` must cover 64 base-10000 groups (256 digits) + sign + '.'; use >= 288.
fn numericCanonical(data: []const u8, buf: []u8) ?[]const u8 {
    if (data.len < 8) return null;
    const ndigits = std.mem.readInt(i16, data[0..2], .big);
    const weight = std.mem.readInt(i16, data[2..4], .big);
    const sign = std.mem.readInt(u16, data[4..6], .big);
    const dscale = std.mem.readInt(i16, data[6..8], .big);

    switch (sign) {
        0xC000 => return numCopyLiteral(buf, "NaN"),
        0xD000 => return numCopyLiteral(buf, "Infinity"),
        0xF000 => return numCopyLiteral(buf, "-Infinity"),
        else => {},
    }

    if (ndigits == 0) {
        const udscale: usize = @intCast(@max(0, dscale));
        if (udscale == 0) {
            if (buf.len < 1) return null;
            buf[0] = '0';
            return buf[0..1];
        }
        if (udscale >= 7) {
            return std.fmt.bufPrint(buf, "0E-{d}", .{udscale}) catch return null;
        }
        if (buf.len < 2 + udscale) return null;
        buf[0] = '0';
        buf[1] = '.';
        @memset(buf[2 .. 2 + udscale], '0');
        return buf[0 .. 2 + udscale];
    }

    // Zero-initialized, not `undefined`: this function's leading-zero-fraction
    // (weight < -1) and short-ndigits branches index `digits` in ways that must
    // never read an unwritten slot. Under ReleaseFast an `undefined` slot holds
    // arch-dependent garbage (ReleaseSafe fills 0xAA, hiding it) — so any missed
    // guard becomes a platform-specific wrong value / serialize failure. Zeroing
    // is a handful of bytes and makes an unwritten digit a harmless 0.
    var digits: [64]i16 = @splat(0);
    const nd: usize = @intCast(@max(0, ndigits));
    if (nd > digits.len) return null;
    for (0..nd) |i| {
        const offset = 8 + i * 2;
        if (offset + 2 > data.len) break;
        digits[i] = std.mem.readInt(i16, data[offset..][0..2], .big);
    }

    var pos: usize = 0;
    if (sign == 0x4000) { // negative
        if (pos >= buf.len) return buf[0..pos];
        buf[pos] = '-';
        pos += 1;
    }

    const int_digits: usize = @intCast(@max(0, @as(i32, weight) + 1));
    if (int_digits == 0) {
        if (pos >= buf.len) return buf[0..pos];
        buf[pos] = '0';
        pos += 1;
    } else {
        for (0..int_digits) |i| {
            const dd: i16 = if (i < nd) digits[i] else 0;
            if (i == 0) {
                const s = std.fmt.bufPrint(buf[pos..], "{d}", .{dd}) catch break;
                pos += s.len;
            } else {
                const s = std.fmt.bufPrint(buf[pos..], "{d:0>4}", .{@as(u16, @intCast(@max(0, dd)))}) catch break;
                pos += s.len;
            }
        }
    }

    if (dscale > 0) {
        if (pos >= buf.len) return buf[0..pos];
        buf[pos] = '.';
        pos += 1;
        const udscale: usize = @intCast(@max(0, dscale));
        var frac_written: usize = 0;

        if (weight < -1) {
            const lead_groups: i32 = -@as(i32, weight) - 1;
            var lead: usize = @intCast(lead_groups * 4);
            while (lead > 0 and frac_written < udscale and pos < buf.len) {
                buf[pos] = '0';
                pos += 1;
                frac_written += 1;
                lead -= 1;
            }
        }

        var i: usize = int_digits;
        while (i < nd and frac_written < udscale) : (i += 1) {
            const dd: u16 = @intCast(@max(0, digits[i]));
            var tmp: [4]u8 = undefined;
            const s = std.fmt.bufPrint(&tmp, "{d:0>4}", .{dd}) catch break;
            const remaining = udscale - frac_written;
            const avail = @min(remaining, s.len);
            if (pos + avail > buf.len) break;
            @memcpy(buf[pos .. pos + avail], s[0..avail]);
            pos += avail;
            frac_written += avail;
        }
        while (frac_written < udscale and pos < buf.len) {
            buf[pos] = '0';
            pos += 1;
            frac_written += 1;
        }
    }

    return numCanonicalizeDecimal(buf, pos, sign == 0x4000);
}

/// Escape a single byte into `buf` at `pos` per RFC 8259 and return the new
/// position, or null if the destination can't hold the escape (caller must then
/// bail so the grow-and-retry path engages). Control characters (< 0x20) MUST be
/// escaped — the old escaper silently DROPPED them, corrupting the value and (for
/// e.g. 0x00) losing data. \b \t \n \f \r get their short forms; every other
/// control byte becomes `\u00XX`. Mirrors db.zig's writeJsonString so both JSON
/// serializers escape identically.
fn writeEscapedByte(buf: []u8, pos: usize, ch: u8) ?usize {
    const two = struct {
        fn w(b: []u8, p: usize, second: u8) ?usize {
            if (p + 2 > b.len) return null;
            b[p] = '\\';
            b[p + 1] = second;
            return p + 2;
        }
    }.w;
    return switch (ch) {
        '"' => two(buf, pos, '"'),
        '\\' => two(buf, pos, '\\'),
        0x08 => two(buf, pos, 'b'),
        0x09 => two(buf, pos, 't'),
        0x0a => two(buf, pos, 'n'),
        0x0c => two(buf, pos, 'f'),
        0x0d => two(buf, pos, 'r'),
        else => blk: {
            if (ch < 0x20) {
                // Remaining control chars → \u00XX (six bytes).
                if (pos + 6 > buf.len) break :blk null;
                const hex = "0123456789abcdef";
                buf[pos] = '\\';
                buf[pos + 1] = 'u';
                buf[pos + 2] = '0';
                buf[pos + 3] = '0';
                buf[pos + 4] = hex[ch >> 4];
                buf[pos + 5] = hex[ch & 0x0f];
                break :blk pos + 6;
            }
            if (pos + 1 > buf.len) break :blk null;
            buf[pos] = ch;
            break :blk pos + 1;
        },
    };
}

/// SIMD-accelerated JSON string escaping.
/// Scans 16 bytes at a time for chars needing escape (", \, control chars).
/// Falls back to scalar (writeEscapedByte) for the remainder and any chunk that
/// contains an escape-needing byte. Returns the number of bytes written, or null
/// if `buf` was too small — callers propagate that as a 0 to trigger grow-retry
/// (the old version silently truncated on overflow AND dropped control chars).
fn simdJsonEscape(data: []const u8, buf: []u8) ?usize {
    const simd_width = 16;
    var pos: usize = 0;
    var i: usize = 0;

    // SIMD fast path: check 16 bytes at a time for escape-needing chars
    while (i + simd_width <= data.len) {
        const chunk: @Vector(simd_width, u8) = data[i..][0..simd_width].*;
        // Check for chars needing escape: control (<0x20), quote, backslash
        const ctrl_mask = chunk < @as(@Vector(simd_width, u8), @splat(0x20));
        const quote_mask = chunk == @as(@Vector(simd_width, u8), @splat('"'));
        const bslash_mask = chunk == @as(@Vector(simd_width, u8), @splat('\\'));

        if (!@reduce(.Or, ctrl_mask) and !@reduce(.Or, quote_mask) and !@reduce(.Or, bslash_mask)) {
            // Fast path: no escaping needed, bulk copy
            if (pos + simd_width > buf.len) return null;
            @memcpy(buf[pos..][0..simd_width], data[i..][0..simd_width]);
            pos += simd_width;
        } else {
            // Slow path: at least one char needs escaping, do scalar
            for (data[i..][0..simd_width]) |ch| {
                pos = writeEscapedByte(buf, pos, ch) orelse return null;
            }
        }
        i += simd_width;
    }

    // Scalar remainder
    while (i < data.len) : (i += 1) {
        pos = writeEscapedByte(buf, pos, data[i]) orelse return null;
    }

    return pos;
}

/// Parse Postgres binary int array and write as JSON: [1,2,3]
fn writeIntArrayJson(data: []const u8, oid: i32, buf: []u8) usize {
    // Postgres binary array format:
    // 4 bytes: ndim, 4 bytes: flags, 4 bytes: element OID
    // per dimension: 4 bytes length, 4 bytes lower bound
    // then: per element: 4 bytes length (or -1 for null), then data
    //
    // A real 1-D array header is 20 bytes (ndim/flags/oid + one dim's
    // length/lower-bound). The old `< 12` guard let a 12..19 byte payload reach
    // `readInt(..., data[12..16])` and the element loop OOB. Bail to "[]" for
    // anything that can't hold a full 1-D header (an empty array is 12 bytes and
    // also renders as "[]", so this is behaviourally identical for valid input).
    if (data.len < 20) {
        if (buf.len < 2) return 0;
        @memcpy(buf[0..2], "[]");
        return 2;
    }

    const ndim = std.mem.readInt(i32, data[0..4], .big);
    if (ndim == 0) {
        if (buf.len < 2) return 0;
        @memcpy(buf[0..2], "[]");
        return 2;
    }

    // Element size based on array OID
    const elem_size: usize = switch (oid) {
        1005 => 2, // int2[]
        1007 => 4, // int4[]
        1016 => 8, // int8[]
        else => 4,
    };

    const nelems = std.mem.readInt(i32, data[12..16], .big);
    var pos: usize = 0;
    if (buf.len < 1) return 0;
    buf[pos] = '[';
    pos += 1;

    var offset: usize = 20; // skip header (12 + 4 length + 4 lower bound)
    var i: i32 = 0;
    while (i < nelems and offset + 4 <= data.len) : (i += 1) {
        if (i > 0) {
            if (pos >= buf.len) return 0;
            buf[pos] = ',';
            pos += 1;
        }
        const elem_len = std.mem.readInt(i32, data[offset..][0..4], .big);
        offset += 4;
        if (elem_len == -1) {
            if (buf.len - pos < 4) return 0;
            @memcpy(buf[pos..][0..4], "null");
            pos += 4;
        } else if (elem_size == 2 and offset + 2 <= data.len) {
            const v = std.mem.readInt(i16, data[offset..][0..2], .big);
            const s = std.fmt.bufPrint(buf[pos..], "{d}", .{v}) catch return 0;
            pos += s.len;
            offset += 2;
        } else if (elem_size == 4 and offset + 4 <= data.len) {
            const v = std.mem.readInt(i32, data[offset..][0..4], .big);
            const s = std.fmt.bufPrint(buf[pos..], "{d}", .{v}) catch return 0;
            pos += s.len;
            offset += 4;
        } else if (elem_size == 8 and offset + 8 <= data.len) {
            const v = std.mem.readInt(i64, data[offset..][0..8], .big);
            const s = std.fmt.bufPrint(buf[pos..], "{d}", .{v}) catch return 0;
            pos += s.len;
            offset += 8;
        } else {
            // Element length we can't interpret against the remaining payload.
            // A negative (non-NULL) length is malformed; anything else we skip,
            // which terminates the loop on the next bounds check.
            if (elem_len < 0) return 0;
            offset += @intCast(@as(u32, @bitCast(elem_len)));
        }
    }

    if (pos >= buf.len) return 0;
    buf[pos] = ']';
    pos += 1;
    return pos;
}

/// Parse Postgres binary text array and write as JSON: ["a","b","c"]
fn writeTextArrayJson(data: []const u8, buf: []u8) usize {
    // See writeIntArrayJson: a full 1-D array header is 20 bytes; the old `< 12`
    // guard let 12..19 byte payloads reach `data[12..16]` OOB.
    if (data.len < 20) {
        if (buf.len < 2) return 0;
        @memcpy(buf[0..2], "[]");
        return 2;
    }

    const ndim = std.mem.readInt(i32, data[0..4], .big);
    if (ndim == 0) {
        if (buf.len < 2) return 0;
        @memcpy(buf[0..2], "[]");
        return 2;
    }

    const nelems = std.mem.readInt(i32, data[12..16], .big);
    var pos: usize = 0;
    if (buf.len < 1) return 0;
    buf[pos] = '[';
    pos += 1;

    var offset: usize = 20;
    var i: i32 = 0;
    while (i < nelems and offset + 4 <= data.len) : (i += 1) {
        if (i > 0) {
            if (pos >= buf.len) return 0;
            buf[pos] = ',';
            pos += 1;
        }
        const elem_len = std.mem.readInt(i32, data[offset..][0..4], .big);
        offset += 4;
        if (elem_len == -1) {
            if (buf.len - pos < 4) return 0;
            @memcpy(buf[pos..][0..4], "null");
            pos += 4;
        } else {
            // Reject a negative (non-NULL) length before it @bitCasts into a
            // giant usize; then confirm the element payload is fully present.
            if (elem_len < 0) return 0;
            const slen: usize = @intCast(elem_len);
            if (offset + slen > data.len) break;
            if (pos >= buf.len) return 0;
            buf[pos] = '"';
            pos += 1;
            for (data[offset..][0..slen]) |ch| {
                // Route through the shared escaper so control bytes become valid
                // JSON escapes (the old inline path emitted raw control bytes,
                // producing invalid JSON). Bail to grow-and-retry on overflow.
                pos = writeEscapedByte(buf, pos, ch) orelse return 0;
            }
            if (pos >= buf.len) return 0;
            buf[pos] = '"';
            pos += 1;
            offset += slen;
        }
    }

    if (pos >= buf.len) return 0;
    buf[pos] = ']';
    pos += 1;
    return pos;
}

// ── Binary-value → canonical-string renderers (JSON scalar decoders) ─────────
//
// These reimplement pg_render.zig's writeIso* / pgUuidToStr INLINE because
// result.zig lives in the `pg` module and cannot @import the top-level
// pg_render.zig across the module boundary. They are kept byte-identical to
// pg_render so the writeJsonValue path and db.zig's sibling serializer agree.

/// Wrap an already-ASCII value `s` in JSON quotes into `buf`. Returns bytes
/// written, or 0 when it doesn't fit (→ grow-and-retry). No escaping: callers
/// pass UUID/date/time/timestamp strings that contain only `[0-9:-.T]`.
fn writeQuotedAscii(buf: []u8, s: []const u8) usize {
    if (buf.len < s.len + 2) return 0;
    buf[0] = '"';
    @memcpy(buf[1..][0..s.len], s);
    buf[1 + s.len] = '"';
    return s.len + 2;
}

/// Emit `data` as a JSON `"\xDEADBEEF"` hex string (lossless, valid JSON, never
/// raw binary). Byte-for-byte identical to db.zig's writeJsonHexString: the JSON
/// source carries two backslashes + 'x' so the decoded string is `\x`+hex,
/// matching PostgreSQL's own bytea ::text form. Returns 0 when it doesn't fit.
fn writeJsonHex(buf: []u8, data: []const u8) usize {
    const need = 2 + 3 + data.len * 2; // 2 quotes + `\\x` + two hex nibbles/byte
    if (buf.len < need) return 0;
    const hex = "0123456789abcdef";
    var p: usize = 0;
    buf[p] = '"';
    p += 1;
    buf[p] = '\\';
    p += 1;
    buf[p] = '\\';
    p += 1;
    buf[p] = 'x';
    p += 1;
    for (data) |b| {
        buf[p] = hex[b >> 4];
        buf[p + 1] = hex[b & 0x0f];
        p += 2;
    }
    buf[p] = '"';
    p += 1;
    return p;
}

const CivilDate = struct { y: i64, m: u32, d: u32 };

/// Howard Hinnant's civil-from-days: `z_in` = days since 1970-01-01 (proleptic
/// Gregorian). Returns (year, month[1-12], day[1-31]). Mirror of
/// pg_render.civilFromDays.
fn civilFromDays(z_in: i64) CivilDate {
    const z = z_in + 719468; // shift epoch to 0000-03-01
    const era = @divFloor(if (z >= 0) z else z - 146096, 146097);
    const doe: u64 = @intCast(z - era * 146097); // [0, 146096]
    const yoe: u64 = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365; // [0, 399]
    const y: i64 = @as(i64, @intCast(yoe)) + era * 400;
    const doy: u64 = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    const mp: u64 = (5 * doy + 2) / 153; // [0, 11]
    const d: u32 = @intCast(doy - (153 * mp + 2) / 5 + 1); // [1, 31]
    const m: u32 = @intCast(if (mp < 10) mp + 3 else mp - 9); // [1, 12]
    return .{ .y = y + @as(i64, @intFromBool(m <= 2)), .m = m, .d = d };
}

/// Append `.ffffff` (6-digit microseconds) iff usec != 0, matching isoformat().
fn writeIsoMicros(buf: []u8, pos: usize, usec: u32) usize {
    if (usec == 0) return pos;
    var p = pos;
    if (p + 7 > buf.len) return p;
    buf[p] = '.';
    p += 1;
    const s = std.fmt.bufPrint(buf[p..], "{d:0>6}", .{usec}) catch return pos;
    return p + s.len;
}

/// TIMESTAMP/TIMESTAMPTZ (µs since 2000-01-01) → `YYYY-MM-DDTHH:MM:SS[.ffffff]`
/// (naive, no UTC offset). Mirror of pg_render.writeIsoTimestamp.
fn isoTimestamp(buf: []u8, usec: i64) ?[]const u8 {
    const pg_epoch_offset: i64 = 946684800; // seconds Unix→PG epoch
    // @divFloor + @mod so a negative usec with a fraction floors correctly.
    const total_sec = @divFloor(usec, 1_000_000) + pg_epoch_offset;
    const rem_usec: u32 = @intCast(@mod(usec, 1_000_000));
    const days = @divFloor(total_sec, 86400);
    const sod = total_sec - days * 86400; // seconds of day [0, 86399]
    const civ = civilFromDays(days);
    if (civ.y < 1 or civ.y > 9999) return null;
    var pos: usize = 0;
    const head = std.fmt.bufPrint(buf[pos..], "{d:0>4}-{d:0>2}-{d:0>2}T{d:0>2}:{d:0>2}:{d:0>2}", .{
        @as(u64, @intCast(civ.y)), civ.m, civ.d,
        @as(u32, @intCast(@divTrunc(sod, 3600))),
        @as(u32, @intCast(@divTrunc(@mod(sod, 3600), 60))),
        @as(u32, @intCast(@mod(sod, 60))),
    }) catch return null;
    pos += head.len;
    pos = writeIsoMicros(buf, pos, rem_usec);
    return buf[0..pos];
}

/// DATE (days since 2000-01-01) → `YYYY-MM-DD`. Mirror of pg_render.writeIsoDate.
fn isoDate(buf: []u8, days: i32) ?[]const u8 {
    const civ = civilFromDays(@as(i64, days) + 10957); // 2000-01-01 is unix day 10957
    if (civ.y < 1 or civ.y > 9999) return null;
    return std.fmt.bufPrint(buf, "{d:0>4}-{d:0>2}-{d:0>2}", .{ @as(u64, @intCast(civ.y)), civ.m, civ.d }) catch null;
}

/// TIME (µs since midnight) → `HH:MM:SS[.ffffff]`. Mirror of pg_render.writeIsoTime.
fn isoTime(buf: []u8, usec: i64) ?[]const u8 {
    const total_sec = @divTrunc(usec, 1_000_000);
    const rem_usec: u32 = @intCast(@mod(usec, 1_000_000));
    var pos: usize = 0;
    const head = std.fmt.bufPrint(buf[pos..], "{d:0>2}:{d:0>2}:{d:0>2}", .{
        @as(u32, @intCast(@divTrunc(total_sec, 3600))),
        @as(u32, @intCast(@divTrunc(@mod(total_sec, 3600), 60))),
        @as(u32, @intCast(@mod(total_sec, 60))),
    }) catch return null;
    pos += head.len;
    pos = writeIsoMicros(buf, pos, rem_usec);
    return buf[0..pos];
}

/// UUID (16 bytes) → canonical lowercase hyphenated string. Mirror of
/// pg_render.pgUuidToStr.
fn uuidToStr(data: []const u8, buf: []u8) ?[]const u8 {
    if (data.len < 16 or buf.len < 36) return null;
    const hex = "0123456789abcdef";
    var pos: usize = 0;
    for (0..16) |i| {
        if (i == 4 or i == 6 or i == 8 or i == 10) {
            buf[pos] = '-';
            pos += 1;
        }
        buf[pos] = hex[data[i] >> 4];
        buf[pos + 1] = hex[data[i] & 0x0f];
        pos += 2;
    }
    return buf[0..pos];
}

fn isSlice(comptime T: type) ?type {
    switch (@typeInfo(T)) {
        .pointer => |ptr| {
            if (ptr.size != .slice) {
                compileHaltGetError(T);
            }
            return if (ptr.child == u8) null else ptr.child;
        },
        .optional => |opt| return isSlice(opt.child),
        else => return null,
    }
}

fn mapValue(comptime T: type, value: T, allocator: Allocator) !T {
    switch (@typeInfo(T)) {
        .optional => |opt| {
            if (value) |v| {
                return try mapValue(opt.child, v, allocator);
            }
            return null;
        },
        else => {},
    }

    if (T == []u8 or T == []const u8) {
        return try allocator.dupe(u8, value);
    }

    if (std.meta.hasFn(T, "pgzMoveOwner")) {
        return value.pgzMoveOwner(allocator);
    }

    return value;
}

pub fn Mapper(comptime T: type) type {
    return struct {
        result: *Result,
        allocator: ?Allocator,
        column_indexes: [std.meta.fields(T).len]?usize,

        const Self = @This();

        pub fn next(self: *const Self) !?T {
            const row = (try self.result.next()) orelse return null;

            var value: T = undefined;

            const allocator = self.allocator;
            inline for (std.meta.fields(T), self.column_indexes) |field, optional_column_index| {
                @field(value, field.name) = try row.mapColumn(field, optional_column_index, allocator);
            }
            return value;
        }
    };
}

pub const QueryRow = QueryRowT(.safe);
pub const QueryRowUnsafe = QueryRowT(.unsafe);

pub fn QueryRowT(comptime fail_mode: lib.FailMode) type {
    return struct {
        row: RowT(fail_mode),
        result: *Result,

        const Self = @This();

        pub fn get(self: *const Self, comptime T: type, col: usize) if (fail_mode == .safe) lib.TypeError!T else T {
            return self.row.get(T, col);
        }

        pub fn getCol(self: *const Self, comptime T: type, name: []const u8) if (fail_mode == .safe) lib.TypeError!T else T {
            return self.row.getCol(T, name);
        }

        pub fn iterator(self: *const Self, comptime T: type, col: usize) if (fail_mode == .safe) lib.TypeError!Iterator(T) else IteratorUnsafe(T) {
            return self.row.iterator(T, col);
        }

        pub fn iteratorCol(self: *const Self, comptime T: type, name: []const u8) if (fail_mode == .safe) lib.TypeError!Iterator(T) else IteratorUnsafe(T) {
            return self.row.iteratorCol(T, name);
        }

        pub fn record(self: *const Self, col: usize) RecordT(fail_mode) {
            return self.row.record(col);
        }

        pub fn recordCol(self: *const Self, name: []const u8) if (fail_mode == .safe) lib.TypeError!Record else RecordUnsafe {
            return self.row.recordCol(name);
        }
        pub fn to(self: *const Self, T: type, opts: Row.ToOpts) !T {
            return self.row.to(T, opts);
        }

        pub fn deinit(self: *Self) !void {
            // this is unfortunate
            try self.result.drain();
            self.result.deinit();
        }
    };
}

pub fn Iterator(comptime T: type) type {
    return IteratorT(.safe, T);
}
pub fn IteratorUnsafe(comptime T: type) type {
    return IteratorT(.unsafe, T);
}
pub fn IteratorT(comptime fail_mode: lib.FailMode, comptime T: type) type {
    return struct {
        is_null: bool,
        _len: usize,
        _pos: usize,
        _data: []const u8,
        _decoder: *const fn (data: []const u8) ItemType(),

        fn ItemType() type {
            return switch (@typeInfo(T)) {
                .optional => |opt| opt.child,
                else => T,
            };
        }

        const Self = @This();

        pub fn len(self: Self) usize {
            return self._len;
        }

        fn asNull() Self {
            return .{
                .is_null = true,
                ._len = 0,
                ._pos = 0,
                ._data = &.{},
                ._decoder = struct {
                    fn noop(_: []const u8) ItemType() {
                        unreachable;
                    }
                }.noop,
            };
        }

        // used internally by row.get(Iterator(T))
        fn fromPgzRow(data: []const u8, oid: i32) !Self {
            const TT = switch (@typeInfo(T)) {
                .optional => |opt| opt.child,
                else => T,
            };

            const decoder = switch (TT) {
                u8 => blk: {
                    lib.verifyDecodeType(fail_mode, []u8, &.{types.CharArray.oid.decimal}, oid) catch |err| {
                        if (comptime fail_mode == .unsafe) unreachable;
                        return err;
                    };
                    break :blk &types.Char.decodeKnown;
                },
                i16 => blk: {
                    lib.verifyDecodeType(fail_mode, []i16, &.{types.Int16Array.oid.decimal}, oid) catch |err| {
                        if (comptime fail_mode == .unsafe) unreachable;
                        return err;
                    };
                    break :blk &types.Int16.decodeKnown;
                },
                i32 => blk: {
                    lib.verifyDecodeType(fail_mode, []i32, &.{types.Int32Array.oid.decimal}, oid) catch |err| {
                        if (comptime fail_mode == .unsafe) unreachable;
                        return err;
                    };
                    break :blk &types.Int32.decodeKnown;
                },
                i64 => switch (oid) {
                    types.TimestampArray.oid.decimal => &types.Timestamp.decodeKnown,
                    types.TimestampTzArray.oid.decimal => &types.Timestamp.decodeKnown,
                    types.Int64Array.oid.decimal => &types.Int64.decodeKnown,
                    else => std.debug.panic("{d} oid cannot target i64 iterator", .{oid}),
                },
                f32 => blk: {
                    lib.verifyDecodeType(fail_mode, []f32, &.{types.Float32Array.oid.decimal}, oid) catch |err| {
                        if (comptime fail_mode == .unsafe) unreachable;
                        return err;
                    };
                    break :blk &types.Float32.decodeKnown;
                },
                f64 => switch (oid) {
                    types.Float64Array.oid.decimal => &types.Float64.decodeKnown,
                    types.NumericArray.oid.decimal => &types.Numeric.decodeKnownToFloat,
                    else => std.debug.panic("{d} oid cannot target f64 iterator", .{oid}),
                },
                bool => blk: {
                    lib.verifyDecodeType(fail_mode, []bool, &.{types.BoolArray.oid.decimal}, oid) catch |err| {
                        if (comptime fail_mode == .unsafe) unreachable;
                        return err;
                    };
                    break :blk &types.Bool.decodeKnown;
                },
                []const u8 => switch (oid) {
                    types.JSONBArray.oid.decimal => &types.JSONB.decodeKnown,
                    else => &types.Bytea.decodeKnown,
                },
                []u8 => switch (oid) {
                    types.JSONBArray.oid.decimal => &types.JSONB.decodeKnownMutable,
                    else => &types.Bytea.decodeKnownMutable,
                },
                types.Numeric => blk: {
                    lib.verifyDecodeType(fail_mode, []f64, &.{types.NumericArray.oid.decimal}, oid) catch |err| {
                        if (comptime fail_mode == .unsafe) unreachable;
                        return err;
                    };
                    break :blk &types.Numeric.decodeKnown;
                },
                types.Cidr => blk: {
                    lib.verifyDecodeType(fail_mode, []types.Cidr, &.{ types.CidrArray.oid.decimal, types.CidrArray.inet_oid.decimal }, oid) catch |err| {
                        if (comptime fail_mode == .unsafe) unreachable;
                        return err;
                    };
                    break :blk &types.Cidr.decodeKnown;
                },
                else => switch (@typeInfo(TT)) {
                    .@"enum" => blk: {
                        lib.verifyDecodeType(fail_mode, []const u8, &.{types.StringArray.oid.decimal}, oid) catch |err| {
                            if (comptime fail_mode == .unsafe) unreachable;
                            return err;
                        };
                        break :blk &EnumDecoder(TT).decodeKnown;
                    },
                    else => compileHaltGetError(T),
                },
            };

            if (data.len == 12) {
                // we have an empty array
                return .{
                    .is_null = false,
                    ._len = 0,
                    ._pos = 0,
                    ._data = &[_]u8{},
                    ._decoder = decoder,
                };
            }

            // A real 1-D array header is 20 bytes. This is peer-controlled wire
            // data, so validate for real (the lib.asserts here were no-ops in
            // ReleaseFast, letting a short/hostile header slice OOB below).
            if (data.len < 20) return error.InvalidData;
            const dimensions = std.mem.readInt(i32, data[0..4], .big);
            // This decoder only understands 1-D arrays; anything else has a
            // different layout and must not be interpreted with this offset math.
            if (dimensions != 1) return error.InvalidData;

            const has_nulls = std.mem.readInt(i32, data[4..8][0..4], .big);
            // Informational only: NULL elements are rejected/stopped in the
            // decode path (next/fillAlloc), so this stays a debug-only invariant.
            lib.assert(has_nulls == 0 or @typeInfo(T) == .optional);

            // const oid = std.mem.readInt(i32, data[8..12][0..4], .big);
            const l = std.mem.readInt(i32, data[12..16][0..4], .big);
            // const lower_bound = std.mem.readInt(i32, data[16..20][0..4], .big);

            // A negative element count would wrap when @intCast to usize and
            // drive a bogus allocation/loop bound in alloc()/fillAlloc().
            if (l < 0) return error.InvalidData;

            return .{
                .is_null = false,
                ._len = @intCast(l),
                ._pos = 0,
                ._data = data[20..],
                ._decoder = decoder,
            };
        }

        pub fn pgzMoveOwner(self: Self, allocator: Allocator) !Self {
            return .{
                .is_null = false,
                ._len = self._len,
                ._pos = self._pos,
                ._data = try allocator.dupe(u8, self._data),
                ._decoder = self._decoder,
            };
        }

        // Should only be called if the Iterator was created with row.to(...)
        // or a result mapper AND an explicit allocator was given
        pub fn deinit(self: *const Self, allocator: Allocator) void {
            allocator.free(self._data);
        }

        pub fn next(self: *Self) ?T {
            const pos = self._pos;
            const data = self._data;
            if (pos >= data.len) {
                return null;
            }

            // NOTE: PostgreSQL always sends the 4-byte length prefix, even for fixed-width types.
            // For nullable columns, we must read it to detect NULL (-1). The read is cheap
            // (single i32 decode from already-buffered data) so skipping it has negligible benefit.
            //
            // The prefix and payload are peer-controlled. The lib.assert below was
            // a no-op in ReleaseFast, so a hostile value_len (huge, or negative →
            // wrapping @intCast) produced an OOB slice. Validate for real and stop
            // iterating on any inconsistency (a NULL element, value_len == -1, also
            // stops here — next() has never yielded NULLs; the alloc/fill path does).
            if (pos + 4 > data.len) {
                return null;
            }
            const len_end = pos + 4;
            const value_len = std.mem.readInt(i32, data[pos..len_end][0..4], .big);
            if (value_len < 0) {
                return null;
            }

            const data_end = len_end + @as(usize, @intCast(value_len));
            if (data_end > data.len) {
                return null;
            }

            self._pos = data_end;
            return self._decoder(data[len_end..data_end]);
        }

        pub fn alloc(self: *const Self, allocator: Allocator) ![]T {
            const into = try allocator.alloc(T, self._len);
            // fillAlloc can now fail on malformed wire data; don't leak `into`.
            errdefer allocator.free(into);
            try self.fillAlloc(true, into, allocator);
            return into;
        }

        pub fn fill(self: *const Self, into: []T) void {
            // fillAlloc can now fail on malformed wire data. `fill` has no error
            // channel, so swallow it (partial fill) — a `catch unreachable` here
            // would be undefined behaviour in ReleaseFast on hostile input.
            self.fillAlloc(false, into, undefined) catch {};
        }

        fn fillAlloc(self: *const Self, comptime should_dupe: bool, into: []T, allocator: Allocator) !void {
            const data = self._data;
            const decoder = self._decoder;

            var pos: usize = 0;
            const limit = @min(into.len, self._len);
            for (0..limit) |i| {
                // NOTE: PostgreSQL always sends the 4-byte length prefix, even for fixed-width types.
                // For nullable columns, we must read it to detect NULL (-1). The read is cheap
                // (single i32 decode from already-buffered data) so skipping it has negligible benefit.
                //
                // Every length/offset below is peer-controlled: validate the
                // prefix fits, reject a negative (non-NULL) length before the
                // wrapping @intCast, and confirm the payload stays in bounds.
                if (pos + 4 > data.len) return error.InvalidData;
                const len_end = pos + 4;
                const data_len = std.mem.readInt(i32, data[pos..len_end][0..4], .big);

                if ((comptime @typeInfo(T) == .optional) and data_len == -1) {
                    pos = len_end;
                    into[i] = null;
                } else {
                    if (data_len < 0) return error.InvalidData;
                    const end = len_end + @as(usize, @intCast(data_len));
                    if (end > data.len) return error.InvalidData;
                    pos = end;
                    if (comptime should_dupe and (T == []u8 or T == []const u8)) {
                        into[i] = try allocator.dupe(u8, decoder(data[len_end..end]));
                    } else {
                        into[i] = decoder(data[len_end..end]);
                    }
                }
            }
        }
    };
}

fn EnumDecoder(comptime T: type) type {
    return struct {
        pub fn decodeKnown(data: []const u8) T {
            return std.meta.stringToEnum(T, data).?;
        }
    };
}

fn compileHaltGetError(comptime T: type) noreturn {
    @compileError("cannot get value of type " ++ @typeName(T));
}

pub const Record = RecordT(.safe);
pub const RecordUnsafe = RecordT(.unsafe);

pub fn RecordT(comptime fail_mode: lib.FailMode) type {
    return struct {
        data: []const u8,
        number_of_columns: usize,

        const Self = @This();

        pub fn next(self: *Self, comptime T: type) if (fail_mode == .safe) lib.TypeError!T else T {
            const data0 = self.data;

            // Composite element header = 4-byte type oid + 4-byte length. This is
            // peer-controlled wire data; the old `lib.assert(data.len >= 8)` was a
            // no-op in ReleaseFast and let a truncated record over-read. Validate
            // for real (matching the DataRow inner-length fix in _next).
            if (data0.len < 8) {
                if (comptime fail_mode == .safe) return error.InvalidData;
                // unsafe has no error channel: stop advancing, yield a null/sentinel.
                self.data = &.{};
                if (comptime @typeInfo(T) == .optional) return null;
                return getScalar(fail_mode, T, &.{}, 0);
            }

            const oid = std.mem.readInt(i32, data0[0..4], .big);
            const data = data0[4..];
            const len = std.mem.readInt(i32, data[0..4], .big);

            const TT = switch (@typeInfo(T)) {
                .optional => |opt| blk: {
                    if (len == -1) {
                        // NULL element: no payload — advance past the 8-byte header.
                        self.data = data[4..];
                        return null;
                    }
                    break :blk opt.child;
                },
                else => T,
            };

            // A negative (non-NULL) length would wrap the @intCast; a payload that
            // runs past the buffer is likewise malformed.
            if (len < 0) {
                if (comptime fail_mode == .safe) return error.InvalidData;
                self.data = &.{};
                return getScalar(fail_mode, TT, &.{}, oid);
            }

            // end of the data for this "column" (relative to `data`, which already
            // skipped the oid): 4-byte length prefix + payload.
            const end = @as(usize, @intCast(len)) + 4;
            if (end > data.len) {
                if (comptime fail_mode == .safe) return error.InvalidData;
                self.data = &.{};
                return getScalar(fail_mode, TT, data[4..], oid);
            }

            // the rest of the data
            self.data = data[end..];

            // start at 4 to skip the length which we already read
            return getScalar(fail_mode, TT, data[4..end], oid);
        }
    };
}

fn getScalar(comptime fail_mode: lib.FailMode, comptime T: type, data: []const u8, oid: i32) if (fail_mode == .safe) lib.TypeError!T else T {
    switch (T) {
        u8 => return types.Char.decode(fail_mode, data, oid),
        i16 => return types.Int16.decode(fail_mode, data, oid),
        i32 => return types.Int32.decode(fail_mode, data, oid),
        i64 => return types.Int64.decode(fail_mode, data, oid),
        f32 => return types.Float32.decode(fail_mode, data, oid),
        f64 => return types.Float64.decode(fail_mode, data, oid),
        bool => return types.Bool.decode(fail_mode, data, oid),
        []const u8 => return types.Bytea.decode(data, oid),
        []u8 => return @constCast(types.Bytea.decode(data, oid)),
        types.Numeric => return types.Numeric.decode(fail_mode, data, oid),
        types.Cidr => return types.Cidr.decode(fail_mode, data, oid),
        else => switch (@typeInfo(T)) {
            .@"enum" => {
                const str = types.Bytea.decode(data, oid);
                return std.meta.stringToEnum(T, str).?;
            },
            else => compileHaltGetError(T),
        },
    }
}

const t = lib.testing;

// Composite-decode bounds (no DB): RecordT.next() must not over-read a truncated
// composite. Safe mode returns error.InvalidData on a short/negative-length
// header; a well-formed element decodes normally.
test "Record: next() bounds on truncated composite data" {
    // Header shorter than 8 bytes (oid+len) → InvalidData, not an over-read.
    {
        var rec = Record{ .data = &[_]u8{ 0, 0, 0 }, .number_of_columns = 1 };
        try std.testing.expectError(error.InvalidData, rec.next(i32));
    }
    // Full 8-byte header but the declared payload length runs past the buffer.
    {
        // oid=23 (int4), len=4, but only 2 payload bytes present.
        var rec = Record{ .data = &[_]u8{ 0, 0, 0, 23, 0, 0, 0, 4, 0, 0 }, .number_of_columns = 1 };
        try std.testing.expectError(error.InvalidData, rec.next(i32));
    }
    // Negative (non-NULL) length.
    {
        var rec = Record{ .data = &[_]u8{ 0, 0, 0, 23, 0xff, 0xff, 0xff, 0xfe }, .number_of_columns = 1 };
        try std.testing.expectError(error.InvalidData, rec.next(i32));
    }
    // Well-formed: oid=23 (int4), len=4, payload = 9001 (0x00002329).
    {
        var rec = Record{ .data = &[_]u8{ 0, 0, 0, 23, 0, 0, 0, 4, 0, 0, 0x23, 0x29 }, .number_of_columns = 1 };
        try std.testing.expectEqual(@as(i32, 9001), try rec.next(i32));
    }
}

test "Result: ints" {
    var c = t.connect(.{});
    defer c.deinit();
    const sql = "select $1::smallint, $2::int, $3::bigint";

    {
        // int max
        var result = try c.query(sql, .{ @as(i16, 32767), @as(i32, 2147483647), @as(i64, 9223372036854775807) });
        defer result.deinit();
        const row = (try result.nextUnsafe()).?;
        try t.expectEqual(32767, row.get(i16, 0));
        try t.expectEqual(2147483647, row.get(i32, 1));
        try t.expectEqual(9223372036854775807, row.get(i64, 2));

        try t.expectEqual(32767, row.get(?i16, 0));
        try t.expectEqual(2147483647, row.get(?i32, 1));
        try t.expectEqual(9223372036854775807, row.get(?i64, 2));

        try t.expectEqual(null, result.next());
    }

    {
        // int min
        var result = try c.query(sql, .{ @as(i16, -32768), @as(i32, -2147483648), @as(i64, -9223372036854775808) });
        defer result.deinit();
        const row = (try result.nextUnsafe()).?;
        try t.expectEqual(-32768, row.get(i16, 0));
        try t.expectEqual(-2147483648, row.get(i32, 1));
        try t.expectEqual(-9223372036854775808, row.get(i64, 2));
        try result.drain();
    }

    {
        // int null
        var result = try c.query(sql, .{ null, null, null });
        defer result.deinit();
        defer result.drain() catch unreachable;
        const row = (try result.nextUnsafe()).?;
        try t.expectEqual(null, row.get(?i16, 0));
        try t.expectEqual(null, row.get(?i32, 1));
        try t.expectEqual(null, row.get(?i64, 2));
    }

    {
        // uint within limit
        var result = try c.query(sql, .{ @as(u16, 32767), @as(u32, 2147483647), @as(u64, 9223372036854775807) });
        defer result.deinit();
        const row = (try result.nextUnsafe()).?;
        try t.expectEqual(32767, row.get(i16, 0));
        try t.expectEqual(2147483647, row.get(i32, 1));
        try t.expectEqual(9223372036854775807, row.get(i64, 2));

        try t.expectEqual(32767, row.get(?i16, 0));
        try t.expectEqual(2147483647, row.get(?i32, 1));
        try t.expectEqual(9223372036854775807, row.get(?i64, 2));
        try result.drain();
    }

    {
        // u16 outside of limit
        try t.expectError(error.IntWontFit, c.query(sql, .{ @as(u16, 32768), @as(u32, 0), @as(u64, 0) }));
        // u32 outside of limit
        try t.expectError(error.IntWontFit, c.query(sql, .{ @as(u16, 0), @as(u32, 2147483648), @as(u64, 0) }));
        // u64 outside of limit
        try t.expectError(error.IntWontFit, c.query(sql, .{ @as(u16, 0), @as(u32, 0), @as(u64, 9223372036854775808) }));
    }
}

test "Result: floats" {
    var c = t.connect(.{});
    defer c.deinit();
    const sql = "select $1::float4, $2::float8";

    {
        // positive float
        var result = try c.query(sql, .{ @as(f32, 1.23456), @as(f64, 1093.229183) });
        defer result.deinit();
        const row = (try result.nextUnsafe()).?;
        try t.expectEqual(1.23456, row.get(f32, 0));
        try t.expectEqual(1093.229183, row.get(f64, 1));

        try t.expectEqual(1.23456, row.get(?f32, 0));
        try t.expectEqual(1093.229183, row.get(?f64, 1));

        try t.expectEqual(null, result.next());
    }

    {
        // negative float
        var result = try c.query(sql, .{ @as(f32, -392.31), @as(f64, -99991.99992) });
        defer result.deinit();
        const row = (try result.nextUnsafe()).?;
        try t.expectEqual(-392.31, row.get(f32, 0));
        try t.expectEqual(-99991.99992, row.get(f64, 1));
        try t.expectEqual(null, result.next());
    }

    {
        // null float
        var result = try c.query(sql, .{ null, null });
        defer result.deinit();
        const row = (try result.nextUnsafe()).?;
        try t.expectEqual(null, row.get(?f32, 0));
        try t.expectEqual(null, row.get(?f64, 1));
        try t.expectEqual(null, result.next());
    }
}

test "Result: bool" {
    var c = t.connect(.{});
    defer c.deinit();
    const sql = "select $1::bool";

    {
        // true
        var result = try c.query(sql, .{true});
        defer result.deinit();
        defer result.drain() catch unreachable;
        const row = (try result.nextUnsafe()).?;
        try t.expectEqual(true, row.get(bool, 0));
        try t.expectEqual(true, row.get(?bool, 0));
        try t.expectEqual(null, result.next());
    }

    {
        // false
        var result = try c.query(sql, .{false});
        defer result.deinit();
        defer result.drain() catch unreachable;
        const row = (try result.nextUnsafe()).?;
        try t.expectEqual(false, row.get(bool, 0));
        try t.expectEqual(false, row.get(?bool, 0));
        try t.expectEqual(null, result.next());
    }

    {
        // null
        var result = try c.query(sql, .{null});
        defer result.deinit();
        defer result.drain() catch unreachable;
        const row = (try result.nextUnsafe()).?;
        try t.expectEqual(null, row.get(?bool, 0));
        try t.expectEqual(null, result.next());
    }
}

test "Result: text and bytea" {
    var c = t.connect(.{});
    defer c.deinit();
    const sql = "select $1::text, $2::bytea";

    {
        // empty
        var result = try c.query(sql, .{ "", "" });
        defer result.deinit();
        const row = (try result.nextUnsafe()).?;
        try t.expectString("", row.get([]u8, 0));
        try t.expectString("", row.get(?[]u8, 0).?);
        try t.expectString("", row.get([]u8, 1));
        try t.expectString("", row.get(?[]u8, 1).?);
        try result.drain();
    }

    {
        // not empty
        var result = try c.query(sql, .{ "it's over 9000!!!", "i will Not fear" });
        defer result.deinit();
        const row = (try result.nextUnsafe()).?;
        try t.expectString("it's over 9000!!!", row.get([]u8, 0));
        try t.expectString("it's over 9000!!!", row.get(?[]const u8, 0).?);
        try t.expectString("i will Not fear", row.get([]const u8, 1));
        try t.expectString("i will Not fear", row.get(?[]u8, 1).?);
        try result.drain();
    }

    {
        // as an array
        var result = try c.query(sql, .{ [_]u8{ 'a', 'c', 'b' }, [_]u8{ 'z', 'z', '3' } });
        defer result.deinit();
        const row = (try result.nextUnsafe()).?;
        try t.expectString("acb", row.get([]const u8, 0));
        try t.expectString("acb", row.get(?[]u8, 0).?);
        try t.expectString("zz3", row.get([]const u8, 1));
        try t.expectString("zz3", row.get(?[]u8, 1).?);
        try result.drain();
    }

    {
        // as a slice
        const s1 = try t.allocator.alloc(u8, 4);
        defer t.allocator.free(s1);
        @memcpy(s1, "Leto");

        var result = try c.query(sql, .{ s1, constString() });
        defer result.deinit();
        const row = (try result.nextUnsafe()).?;
        try t.expectString("Leto", row.get([]u8, 0));
        try t.expectString("Leto", row.get(?[]u8, 0).?);
        try t.expectString("Ghanima", row.get([]u8, 1));
        try t.expectString("Ghanima", row.get(?[]u8, 1).?);
        try result.drain();
    }

    {
        // null
        var result = try c.query(sql, .{ null, null });
        defer result.deinit();
        const row = (try result.nextUnsafe()).?;
        try t.expectEqual(null, row.get(?[]u8, 0));
        try t.expectEqual(null, row.get(?[]u8, 1));
        try result.drain();
    }
}

fn constString() []const u8 {
    return "Ghanima";
}

test "Result: optional" {
    var c = t.connect(.{});
    defer c.deinit();
    const sql = "select $1::int, $2::int";

    {
        // int max
        var result = try c.query(sql, .{ @as(?i32, 321), @as(?i32, null) });
        defer result.deinit();
        const row = (try result.nextUnsafe()).?;
        try t.expectEqual(321, row.get(i32, 0));

        try t.expectEqual(321, row.get(?i32, 0));
        try t.expectEqual(null, row.get(?i32, 1));
        try t.expectEqual(null, result.next());
    }
}

test "Result: iterator" {
    var c = t.connect(.{});
    defer c.deinit();

    {
        // empty row.iterator()
        var result = try c.query("select $1::int[]", .{[_]i32{}});
        defer result.deinit();
        var row = (try result.nextUnsafe()).?;

        var iterator = row.iterator(i32, 0);
        try t.expectEqual(0, iterator.len());

        try t.expectEqual(null, iterator.next());
        try t.expectEqual(null, iterator.next());

        const a = try iterator.alloc(t.allocator);
        try t.expectEqual(0, a.len);
        try result.drain();
    }

    {
        // empty row.get()
        var result = try c.query("select $1::int[]", .{[_]i32{}});
        defer result.deinit();
        var row = (try result.nextUnsafe()).?;

        var iterator = row.get(Iterator(i32), 0);
        try t.expectEqual(0, iterator.len());

        try t.expectEqual(null, iterator.next());
        try t.expectEqual(null, iterator.next());

        const a = try iterator.alloc(t.allocator);
        try t.expectEqual(0, a.len);
        try result.drain();
    }

    {
        // one: row.iterator
        var result = try c.query("select $1::int[]", .{[_]i32{9}});
        defer result.deinit();
        var row = (try result.nextUnsafe()).?;

        var iterator = row.iterator(i32, 0);
        try t.expectEqual(1, iterator.len());

        try t.expectEqual(9, iterator.next());
        try t.expectEqual(null, iterator.next());

        const arr = try iterator.alloc(t.allocator);
        defer t.allocator.free(arr);
        try t.expectEqual(1, arr.len);
        try t.expectSlice(i32, &.{9}, arr);
        try result.drain();
    }

    {
        // one: row.get
        var result = try c.query("select $1::int[]", .{[_]i32{9}});
        defer result.deinit();
        var row = (try result.nextUnsafe()).?;

        var iterator = row.get(Iterator(i32), 0);
        try t.expectEqual(1, iterator.len());

        try t.expectEqual(9, iterator.next());
        try t.expectEqual(null, iterator.next());

        const arr = try iterator.alloc(t.allocator);
        defer t.allocator.free(arr);
        try t.expectEqual(1, arr.len);
        try t.expectSlice(i32, &.{9}, arr);
        try result.drain();
    }

    {
        // fill
        var result = try c.query("select $1::int[]", .{[_]i32{ 0, -19 }});
        defer result.deinit();
        var row = (try result.nextUnsafe()).?;

        var iterator = row.iterator(i32, 0);
        try t.expectEqual(2, iterator.len());

        try t.expectEqual(0, iterator.next());
        try t.expectEqual(-19, iterator.next());
        try t.expectEqual(null, iterator.next());

        var arr1: [2]i32 = undefined;
        iterator.fill(&arr1);
        try t.expectSlice(i32, &.{ 0, -19 }, &arr1);
        try result.drain();

        // smaller
        var arr2: [1]i32 = undefined;
        iterator.fill(&arr2);
        try t.expectSlice(i32, &.{0}, &arr2);
        try result.drain();
    }
}

test "Result: null iterator" {
    var c = t.connect(.{});
    defer c.deinit();

    {
        // null int
        var result = try c.query("select $1::int[]", .{null});
        defer result.deinit();

        var row = (try result.nextUnsafe()).?;

        var iterator = row.iterator(i32, 0);
        try t.expectEqual(true, iterator.is_null);
        try t.expectEqual(null, iterator.next());
        try result.drain();
    }

    {
        // null text
        var result = try c.query("select $1::text[]", .{null});
        defer result.deinit();

        var row = (try result.nextUnsafe()).?;

        var iterator = row.iterator([]u8, 0);
        try t.expectEqual(true, iterator.is_null);
        try t.expectEqual(null, iterator.next());
        try result.drain();
    }
}

test "Result: int[]" {
    var c = t.connect(.{});
    defer c.deinit();
    const sql = "select $1::smallint[], $2::int[], $3::bigint[]";

    var result = try c.query(sql, .{ [_]i16{ -303, 9449, 2 }, [_]i32{ -3003, 49493229, 0 }, [_]i64{ 944949338498392, -2 } });
    defer result.deinit();

    var row = (try result.nextUnsafe()).?;

    const v1 = try row.iterator(i16, 0).alloc(t.allocator);
    defer t.allocator.free(v1);
    try t.expectSlice(i16, &.{ -303, 9449, 2 }, v1);

    const v2 = try row.iterator(i32, 1).alloc(t.allocator);
    defer t.allocator.free(v2);
    try t.expectSlice(i32, &.{ -3003, 49493229, 0 }, v2);

    const v3 = try row.iterator(i64, 2).alloc(t.allocator);
    defer t.allocator.free(v3);
    try t.expectSlice(i64, &.{ 944949338498392, -2 }, v3);
}

test "Result: float[]" {
    var c = t.connect(.{});
    defer c.deinit();
    const sql = "select $1::float4[], $2::float8[]";

    var result = try c.query(sql, .{ [_]f32{ 1.1, 0, -384.2 }, [_]f64{ -888585.123322, 0.001 } });
    defer result.deinit();

    var row = (try result.nextUnsafe()).?;

    const v1 = try row.iterator(f32, 0).alloc(t.allocator);
    defer t.allocator.free(v1);
    try t.expectSlice(f32, &.{ 1.1, 0, -384.2 }, v1);

    const v2 = try row.iterator(f64, 1).alloc(t.allocator);
    defer t.allocator.free(v2);
    try t.expectSlice(f64, &.{ -888585.123322, 0.001 }, v2);
}

test "Result: bool[]" {
    var c = t.connect(.{});
    defer c.deinit();
    const sql = "select $1::bool[]";

    var result = try c.query(sql, .{[_]bool{ true, false, false }});
    defer result.deinit();

    var row = (try result.nextUnsafe()).?;

    const v1 = try row.iterator(bool, 0).alloc(t.allocator);
    defer t.allocator.free(v1);
    try t.expectSlice(bool, &.{ true, false, false }, v1);
}

test "Result: text[] & bytea[]" {
    var c = t.connect(.{});
    defer c.deinit();
    const sql = "select $1::text[], $2::bytea[]";

    var arr1 = [_]u8{ 0, 1, 2 };
    var arr2 = [_]u8{255};
    var result = try c.query(sql, .{ [_][]const u8{ "over", "9000" }, [_][]u8{ &arr1, &arr2 } });
    defer result.deinit();

    var row = (try result.nextUnsafe()).?;

    const v1 = try row.iterator([]u8, 0).alloc(t.allocator);
    defer {
        t.allocator.free(v1[0]);
        t.allocator.free(v1[1]);
        t.allocator.free(v1);
    }
    try t.expectString("over", v1[0]);
    try t.expectString("9000", v1[1]);
    try t.expectEqual(2, v1.len);

    const v2 = try row.iterator([]const u8, 1).alloc(t.allocator);
    defer {
        t.allocator.free(v2[0]);
        t.allocator.free(v2[1]);
        t.allocator.free(v2);
    }
    try t.expectString(&arr1, v2[0]);
    try t.expectString(&arr2, v2[1]);
    try t.expectEqual(2, v2.len);
}

test "Result: text[] alloc dupes" {
    var c = t.connect(.{});
    defer c.deinit();

    var arr1: [][]const u8 = undefined;
    var arr2: [][]const u8 = undefined;
    defer {
        for (arr1) |str| {
            t.allocator.free(str);
        }
        t.allocator.free(arr1);

        for (arr2) |str| {
            t.allocator.free(str);
        }
        t.allocator.free(arr2);
    }

    {
        var row = (try c.rowUnsafe("select array['Leto', 'Test']::text[]", .{})) orelse unreachable;
        defer row.deinit() catch {};
        arr1 = try row.iterator([]const u8, 0).alloc(t.allocator);
    }

    {
        var row = (try c.rowUnsafe("select array['Ghanima', 'Goku']::text[]", .{})) orelse unreachable;
        defer row.deinit() catch {};
        arr2 = try row.iterator([]const u8, 0).alloc(t.allocator);
    }

    try t.expectStringSlice(&.{ "Leto", "Test" }, arr1);
    try t.expectStringSlice(&.{ "Ghanima", "Goku" }, arr2);
}

test "Result: UUID" {
    var c = t.connect(.{});
    defer c.deinit();
    const sql = "select $1::uuid, $2::uuid";
    var result = try c.query(sql, .{ "fcbebf0f-b996-43b9-9818-672bc689cda8", &[_]u8{ 174, 47, 71, 95, 128, 112, 65, 183, 186, 51, 134, 187, 168, 137, 123, 222 } });
    defer result.deinit();

    const row = (try result.nextUnsafe()).?;
    try t.expectSlice(u8, &.{ 252, 190, 191, 15, 185, 150, 67, 185, 152, 24, 103, 43, 198, 137, 205, 168 }, row.get([]u8, 0));
    try t.expectSlice(u8, &.{ 174, 47, 71, 95, 128, 112, 65, 183, 186, 51, 134, 187, 168, 137, 123, 222 }, row.get([]u8, 1));
}

test "Result: lsn" {
    var c = t.connect(.{});
    defer c.deinit();
    const sql = "select $1::pg_lsn + 1";
    var result = try c.query(sql, .{32788447688});
    defer result.deinit();

    const row = (try result.nextUnsafe()).?;
    try t.expectEqual(32788447689, row.get(i64, 0));
}

test "Row: column names" {
    var c = t.connect(.{});
    defer c.deinit();
    const sql = "select 923 as id, 'Leto' as name";
    var row = (try c.rowUnsafeOpts(sql, .{}, .{ .column_names = true })).?;
    defer row.deinit() catch {};

    try t.expectEqual(923, row.getCol(i32, "id"));
    try t.expectString("Leto", row.getCol([]u8, "name"));
}

test "Result: mutable []u8" {
    var c = t.connect(.{});
    defer c.deinit();
    const sql = "select 'Leto'";
    var row = (try c.rowUnsafe(sql, .{})).?;
    defer row.deinit() catch {};

    var name = row.get([]u8, 0);
    name[3] = '!';
    try t.expectString("Let!", name);
}

test "Result: mutable [][]u8" {
    var c = t.connect(.{});
    defer c.deinit();
    const sql = "select array['Leto', 'Test']::text[]";
    var row = (try c.rowUnsafe(sql, .{})).?;
    defer row.deinit() catch {};

    var values = try row.iterator([]u8, 0).alloc(t.allocator);
    defer {
        t.allocator.free(values[0]);
        t.allocator.free(values[1]);
        t.allocator.free(values);
    }
    values[0][0] = 'n';
    try t.expectString("neto", values[0]);
    try t.expectString("Test", values[1]);
}

test "Row.to: ordinal" {
    const User = struct {
        id: i32,
        active: bool,
        name: []const u8,
        note: ?[]const u8,
        choice: Choice,

        const Choice = enum {
            blue,
            green,
            red,
        };
    };

    var c = t.connect(.{});
    defer c.deinit();

    {
        // null, no dupe
        var row = (try c.rowUnsafe("select 1::integer, true, 'teg', null::text, 'blue'", .{})).?;
        defer row.deinit() catch {};

        const user = try row.to(User, .{});
        try t.expectEqual(1, user.id);
        try t.expectEqual(true, user.active);
        try t.expectString("teg", user.name);
        try t.expectEqual(null, user.note);
        try t.expectEqual(.blue, user.choice);
    }

    {
        // not null, no dupe
        var row = (try c.rowUnsafe("select 2::integer, false, 'ghanima', 'n1', 'red'", .{})).?;
        defer row.deinit() catch {};

        const user = try row.to(User, .{});
        try t.expectEqual(2, user.id);
        try t.expectEqual(false, user.active);
        try t.expectString("ghanima", user.name);
        try t.expectString("n1", user.note.?);
        try t.expectEqual(.red, user.choice);
    }

    {
        // null, dupe with internal arena
        var row = (try c.rowUnsafe("select 1::integer, true, 'teg', null::text, 'red'", .{})).?;
        defer row.deinit() catch {};

        const user = try row.to(User, .{ .dupe = true });
        try t.expectEqual(1, user.id);
        try t.expectEqual(true, user.active);
        try t.expectString("teg", user.name);
        try t.expectEqual(null, user.note);
        try t.expectEqual(.red, user.choice);
    }

    {
        // not null, dupe with internal arena
        var row = (try c.rowUnsafe("select 2::integer, false, 'ghanima', 'n1', 'red'", .{})).?;
        const user = try row.to(User, .{ .dupe = true });
        defer row.deinit() catch {};

        try t.expectEqual(2, user.id);
        try t.expectEqual(false, user.active);
        try t.expectString("ghanima", user.name);
        try t.expectString("n1", user.note.?);
        try t.expectEqual(.red, user.choice);
    }

    {
        // null, dupe with explicit allocator
        var row = (try c.rowUnsafe("select 1::integer, true, 'teg', null::text, 'red'", .{})).?;
        const user = try row.to(User, .{ .allocator = t.allocator });
        row.deinit() catch {};

        defer t.allocator.free(user.name);
        try t.expectEqual(1, user.id);
        try t.expectEqual(true, user.active);
        try t.expectString("teg", user.name);
        try t.expectEqual(null, user.note);
        try t.expectEqual(.red, user.choice);
    }

    {
        // not null, dupe with explicit allocator
        var row = (try c.rowUnsafe("select 2::integer, false, 'ghanima', 'n1', 'red'", .{})).?;

        const user = try row.to(User, .{ .allocator = t.allocator });
        row.deinit() catch {};

        defer t.allocator.free(user.name);
        defer t.allocator.free(user.note.?);

        try t.expectEqual(2, user.id);
        try t.expectEqual(false, user.active);
        try t.expectString("ghanima", user.name);
        try t.expectString("n1", user.note.?);
        try t.expectEqual(.red, user.choice);
    }
}

test "Row.to: name no map" {
    const User = struct {
        id: i32 = 9876,
        active: bool,
        name: []const u8,
        note: ?[]const u8 = null,
    };

    var c = t.connect(.{});
    defer c.deinit();

    {
        // null, no dupe
        var row = (try c.rowUnsafeOpts("select 1 as id, true as active, 'teg' as name, null as note", .{}, .{ .column_names = true })).?;
        defer row.deinit() catch {};

        const user = try row.to(User, .{ .map = .name });
        try t.expectEqual(1, user.id);
        try t.expectEqual(true, user.active);
        try t.expectString("teg", user.name);
        try t.expectEqual(null, user.note);
    }

    {
        // default values are used if no colum
        // and extra columns are ignored
        var row = (try c.rowUnsafeOpts("select 2 as id, false as active, 'ghanima' as name, 'x123' as other", .{}, .{ .column_names = true })).?;
        defer row.deinit() catch {};

        const user = try row.to(User, .{ .map = .name });
        try t.expectEqual(2, user.id);
        try t.expectEqual(false, user.active);
        try t.expectString("ghanima", user.name);
        try t.expectEqual(null, user.note);
    }

    {
        // nullable fields are nulled if no column
        // and extra columns are ignored
        var row = (try c.rowUnsafeOpts("select false as active, 'ghanima' as name, 'x123' as other", .{}, .{ .column_names = true })).?;
        defer row.deinit() catch {};

        const user = try row.to(User, .{ .map = .name });
        try t.expectEqual(9876, user.id);
        try t.expectEqual(false, user.active);
        try t.expectString("ghanima", user.name);
        try t.expectEqual(null, user.note);
    }

    {
        // error on missing column with non-default value
        var row = (try c.rowUnsafeOpts("select 1 as id", .{}, .{ .column_names = true })).?;
        defer row.deinit() catch {};

        try t.expectError(error.FieldColumnMismatch, row.to(User, .{ .map = .name }));
    }

    {
        // not null, no dupe
        var row = (try c.rowUnsafeOpts("select 2::integer as id, false as active, 'ghanima' as name, 'n1' as note", .{}, .{ .column_names = true })).?;
        defer row.deinit() catch {};

        const user = try row.to(User, .{ .map = .name });
        try t.expectEqual(2, user.id);
        try t.expectEqual(false, user.active);
        try t.expectString("ghanima", user.name);
        try t.expectString("n1", user.note.?);
    }

    {
        // null, dupe with internal arena
        var row = (try c.rowUnsafeOpts("select 1::integer as id, true as active, 'teg' as name, null::text as note", .{}, .{ .column_names = true })).?;
        defer row.deinit() catch {};

        const user = try row.to(User, .{ .dupe = true, .map = .name });
        try t.expectEqual(1, user.id);
        try t.expectEqual(true, user.active);
        try t.expectString("teg", user.name);
        try t.expectEqual(null, user.note);
    }

    {
        // not null, dupe with internal arena
        var row = (try c.rowUnsafeOpts("select 2::integer as id, false as active, 'ghanima' as name, 'n1' as note", .{}, .{ .column_names = true })).?;
        defer row.deinit() catch {};

        const user = try row.to(User, .{ .dupe = true, .map = .name });
        try t.expectEqual(2, user.id);
        try t.expectEqual(false, user.active);
        try t.expectString("ghanima", user.name);
        try t.expectString("n1", user.note.?);
    }

    {
        // null, dupe with explicit allocator
        var row = (try c.rowUnsafeOpts("select 1::integer as id, true as active, 'teg' as name, null::text as note", .{}, .{ .column_names = true })).?;
        defer row.deinit() catch {};

        const user = try row.to(User, .{ .allocator = t.allocator, .map = .name });
        defer t.allocator.free(user.name);
        try t.expectEqual(1, user.id);
        try t.expectEqual(true, user.active);
        try t.expectString("teg", user.name);
        try t.expectEqual(null, user.note);
    }

    {
        // not null, dupe with explicit allocator
        var row = (try c.rowUnsafeOpts("select 5::integer as id, false as active, 'ghanima' as name, 'n1' as note", .{}, .{ .column_names = true })).?;
        defer row.deinit() catch {};

        const user = try row.to(User, .{ .allocator = t.allocator, .map = .name });
        defer t.allocator.free(user.name);
        defer t.allocator.free(user.note.?);

        try t.expectEqual(5, user.id);
        try t.expectEqual(false, user.active);
        try t.expectString("ghanima", user.name);
        try t.expectString("n1", user.note.?);
    }
}

test "Result.Mapper" {
    var c = t.connect(.{});
    defer c.deinit();

    {
        // mapper with missing column and non-default field
        var result = try c.queryOpts("select 1", .{}, .{ .column_names = true });
        defer result.deinit();
        const mapper = result.mapper(struct { id: i32 }, .{});
        try t.expectError(error.FieldColumnMismatch, mapper.next());
        try result.drain();
    }

    // null, no dupe
    try expectResultMapper(&c, "select 1 as id, true as active, 'teg' as name, null as note", .{
        .id = 1,
        .active = true,
        .name = "teg",
        .note = null,
    }, .{});

    // default values are used if no colum
    // and extra columns are ignored
    try expectResultMapper(&c, "select 2 as id, false as active, 'ghanima' as name, 'x123' as other", .{
        .id = 2,
        .active = false,
        .name = "ghanima",
        .note = null,
    }, .{});

    // nullable fields are nulled if no column
    // and extra columns are ignored
    try expectResultMapper(&c, "select false as active, 'ghanima' as name, 'x123' as other", .{
        .id = 9876,
        .active = false,
        .name = "ghanima",
        .note = null,
    }, .{});

    // not null, no dupe
    try expectResultMapper(&c, "select 2::integer as id, false as active, 'ghanima' as name, 'n1' as note", .{
        .id = 2,
        .active = false,
        .name = "ghanima",
        .note = "n1",
    }, .{});

    // null, dupe with internal arena
    try expectResultMapper(&c, "select 1::integer as id, true as active, 'teg' as name, null::text as note", .{
        .id = 1,
        .active = true,
        .name = "teg",
        .note = null,
    }, .{ .dupe = true });

    // not null, dupe with internal arena
    try expectResultMapper(&c, "select 3::integer as id, false as active, 'ghanima' as name, 'n1' as note", .{
        .id = 3,
        .active = false,
        .name = "ghanima",
        .note = "n1",
    }, .{ .dupe = true });

    // null, dupe with explicit allocator
    try expectResultMapper(&c, "select 4::integer as id, true as active, 'teg' as name, null::text as note", .{
        .id = 4,
        .active = true,
        .name = "teg",
        .note = null,
    }, .{ .allocator = t.allocator });

    // not null, dupe with explicit allocator
    try expectResultMapper(&c, "select 5::integer as id, false as active, 'ghanima' as name, 'n1' as note", .{
        .id = 5,
        .active = false,
        .name = "ghanima",
        .note = "n1",
    }, .{ .allocator = t.allocator });
}

test "Row.to: iterator" {
    const User = struct {
        parents: Iterator(i32),
        tags: ?Iterator([]const u8),
    };

    defer t.reset();
    var c = t.connect(.{});
    defer c.deinit();

    {
        var row = (try c.rowUnsafe("select array[1, 99]::integer[], null", .{})).?;
        defer row.deinit() catch {};

        const user = try row.to(User, .{});
        try t.expectSlice(i32, &.{ 1, 99 }, try user.parents.alloc(t.arena.allocator()));
        try t.expectEqual(null, user.tags);
    }

    {
        var row = (try c.rowUnsafe("select array[0]::integer[], array['over', '9000']::text[]", .{})).?;
        const user = try row.to(User, .{ .allocator = t.allocator });
        row.deinit() catch {};

        defer user.parents.deinit(t.allocator);
        defer user.tags.?.deinit(t.allocator);

        try t.expectSlice(i32, &.{0}, try user.parents.alloc(t.arena.allocator()));
        try t.expectStringSlice(&.{ "over", "9000" }, try user.tags.?.alloc(t.arena.allocator()));
    }

    {
        // dupe with result arena
        var result = try c.query(
            \\ select array[0]::integer[], array['over']::text[]
            \\ union all
            \\ select array[1]::integer[], array['9000']::text[]
        , .{});

        const user1 = try (try result.nextUnsafe()).?.to(User, .{ .dupe = true });
        const user2 = try (try result.nextUnsafe()).?.to(User, .{ .dupe = true });
        try t.expectEqual(null, try result.nextUnsafe());
        defer result.deinit();

        try t.expectSlice(i32, &.{0}, try user1.parents.alloc(t.arena.allocator()));
        try t.expectStringSlice(&.{"over"}, try user1.tags.?.alloc(t.arena.allocator()));

        try t.expectSlice(i32, &.{1}, try user2.parents.alloc(t.arena.allocator()));
        try t.expectStringSlice(&.{"9000"}, try user2.tags.?.alloc(t.arena.allocator()));
    }

    {
        // dupe with explicit arena
        var result = try c.query(
            \\ select array[0]::integer[], array['over']::text[]
            \\ union all
            \\ select array[1]::integer[], array['9000']::text[]
        , .{});

        const user1 = try (try result.nextUnsafe()).?.to(User, .{ .allocator = t.allocator });
        const user2 = try (try result.nextUnsafe()).?.to(User, .{ .allocator = t.allocator });
        try t.expectEqual(null, try result.nextUnsafe());
        result.deinit();

        defer user1.tags.?.deinit(t.allocator);
        defer user1.parents.deinit(t.allocator);
        defer user2.tags.?.deinit(t.allocator);
        defer user2.parents.deinit(t.allocator);

        try t.expectSlice(i32, &.{0}, try user1.parents.alloc(t.arena.allocator()));
        try t.expectStringSlice(&.{"over"}, try user1.tags.?.alloc(t.arena.allocator()));

        try t.expectSlice(i32, &.{1}, try user2.parents.alloc(t.arena.allocator()));
        try t.expectStringSlice(&.{"9000"}, try user2.tags.?.alloc(t.arena.allocator()));
    }
}

test "Row.to: array" {
    const User = struct {
        parents: []i32,
        tags: ?[][]const u8,
        choices: ?[]Choice,

        const Choice = enum {
            red,
            blue,
            green,
        };
    };

    defer t.reset();
    var c = t.connect(.{});
    defer c.deinit();

    {
        var row = (try c.rowUnsafe("select array[1, 99]::integer[], array['over', '9000']::text[], array['red', 'green']::text[]", .{})).?;
        const user = try row.to(User, .{ .allocator = t.allocator });
        row.deinit() catch {};

        defer {
            t.allocator.free(user.tags.?[0]);
            t.allocator.free(user.tags.?[1]);
            t.allocator.free(user.tags.?);
            t.allocator.free(user.parents);
            t.allocator.free(user.choices.?);
        }
        try t.expectSlice(i32, &.{ 1, 99 }, user.parents);
        try t.expectStringSlice(&.{ "over", "9000" }, user.tags.?);
        try t.expectSlice(User.Choice, &.{ .red, .green }, user.choices.?);
    }

    {
        var row = (try c.rowUnsafe("select array[1, 99]::integer[], null::text[], null::text[]", .{})).?;
        const user = try row.to(User, .{ .allocator = t.allocator });
        row.deinit() catch {};

        defer {
            t.allocator.free(user.parents);
        }
        try t.expectSlice(i32, &.{ 1, 99 }, user.parents);
        try t.expectEqual(null, user.tags);
        try t.expectEqual(null, user.choices);
    }
}

test "Result: safe" {
    var c = t.connect(.{});
    defer c.deinit();
    const sql = "select $1::int, $2::int";

    {
        var result = try c.query(sql, .{ @as(?i32, 321), @as(?i32, null) });
        defer result.deinit();
        const row = (try result.next()).?;
        try t.expectEqual(321, try row.get(i32, 0));
        try t.expectEqual(error.InvalidType, row.get(bool, 0));

        try t.expectEqual(321, try row.get(?i32, 0));
        try t.expectEqual(null, try row.get(?i32, 1));
        try t.expectEqual(null, result.next());
    }
}

fn expectResultMapper(conn: *Conn, sql: []const u8, expected: anytype, opts: Result.MapperOpts) !void {
    const User = struct {
        id: i32 = 9876,
        active: bool,
        name: []const u8,
        note: ?[]const u8 = null,
    };

    var result = try conn.queryOpts(sql, .{}, .{ .column_names = true });
    defer result.deinit();
    var mapper = result.mapper(User, opts);

    const user = (try mapper.next()) orelse unreachable;
    try t.expectEqual(expected.id, user.id);
    try t.expectEqual(expected.active, user.active);
    try t.expectString(expected.name, user.name);
    if (opts.allocator) |a| {
        a.free(user.name);
    }
    if (@TypeOf(expected.note) == @TypeOf(null)) {
        try t.expectEqual(null, user.note);
    } else {
        try t.expectString(expected.note, user.note.?);
        if (opts.allocator) |a| {
            a.free(user.note.?);
        }
    }

    try t.expectEqual(null, mapper.next());
}

// ── Wire-fuzz seeds — memory safety of inner-length validation ───────────────
// These exercise the field-length / array-header decoders directly with
// truncated, oversized and negative-length fixtures (no DB connection). They are
// the seed corpus for a broader wire-fuzz harness.

// Build a minimal binary 1-D array header: ndim, has_nulls flag, elem oid,
// dim length, lower bound. Element bytes (each a 4-byte length + payload) follow.
fn testArrayHeader(buf: []u8, has_nulls: i32, elem_oid: i32, dim_len: i32) void {
    std.mem.writeInt(i32, buf[0..4], 1, .big); // ndim
    std.mem.writeInt(i32, buf[4..8], has_nulls, .big);
    std.mem.writeInt(i32, buf[8..12], elem_oid, .big);
    std.mem.writeInt(i32, buf[12..16], dim_len, .big);
    std.mem.writeInt(i32, buf[16..20], 1, .big); // lower bound
}

test "writeIntArrayJson: truncated header renders as []" {
    var out: [64]u8 = undefined;
    // 12..19 bytes used to reach data[12..16] OOB; must bail to "[]"
    const short = [_]u8{0} ** 16;
    try t.expectString("[]", out[0..writeIntArrayJson(&short, 1007, &out)]);
    // an empty array (12 bytes, ndim would be 0) also renders as "[]"
    const empty = [_]u8{0} ** 12;
    try t.expectString("[]", out[0..writeIntArrayJson(&empty, 1007, &out)]);
}

test "writeIntArrayJson: valid int4 array" {
    var data: [20 + 8 + 8]u8 = undefined;
    testArrayHeader(&data, 0, 23, 2);
    std.mem.writeInt(i32, data[20..24], 4, .big);
    std.mem.writeInt(i32, data[24..28], 7, .big);
    std.mem.writeInt(i32, data[28..32], 4, .big);
    std.mem.writeInt(i32, data[32..36], 9, .big);
    var out: [64]u8 = undefined;
    try t.expectString("[7,9]", out[0..writeIntArrayJson(&data, 1007, &out)]);
}

test "writeIntArrayJson: element length overruns payload stops cleanly" {
    // dim_len says 2 elements but only 1 (truncated) element of bytes follow.
    var data: [20 + 6]u8 = undefined;
    testArrayHeader(&data, 0, 23, 2);
    std.mem.writeInt(i32, data[20..24], 4, .big); // claims 4-byte value...
    data[24] = 0;
    data[25] = 0; // ...but only 2 bytes present
    var out: [64]u8 = undefined;
    // must not read past `data`; loop ends when offset+4 > data.len
    const n = writeIntArrayJson(&data, 1007, &out);
    try t.expectEqual(true, n >= 1 and out[0] == '[');
}

test "writeIntArrayJson: negative element length is rejected" {
    // Header + a 4-byte length prefix of -5, with NO element payload after it,
    // so the size branches (which need trailing bytes) fall through to the skip
    // path where the negative-length guard fires.
    var data: [20 + 4]u8 = undefined;
    testArrayHeader(&data, 0, 23, 1);
    std.mem.writeInt(i32, data[20..24], -5, .big); // negative, not -1
    var out: [64]u8 = undefined;
    // -5 must not @bitCast into a giant skip; decoder returns 0 (malformed)
    try t.expectEqual(0, writeIntArrayJson(&data, 1007, &out));
}

test "writeIntArrayJson: tiny output buffer returns 0" {
    var data: [20 + 8]u8 = undefined;
    testArrayHeader(&data, 0, 23, 1);
    std.mem.writeInt(i32, data[20..24], 4, .big);
    std.mem.writeInt(i32, data[24..28], 7, .big);
    var out: [1]u8 = undefined; // can't even hold "[]"
    try t.expectEqual(0, writeIntArrayJson(&data, 1007, &out));
}

test "writeTextArrayJson: truncated header and negative length" {
    var out: [64]u8 = undefined;
    const short = [_]u8{0} ** 18;
    try t.expectString("[]", out[0..writeTextArrayJson(&short, &out)]);

    var data: [20 + 4]u8 = undefined;
    testArrayHeader(&data, 0, 25, 1);
    std.mem.writeInt(i32, data[20..24], -9, .big); // negative element length
    try t.expectEqual(0, writeTextArrayJson(&data, &out));
}

test "writeTextArrayJson: valid + element overruns payload" {
    // valid two-string array ["ab","c"]
    var data: [20 + (4 + 2) + (4 + 1)]u8 = undefined;
    testArrayHeader(&data, 0, 25, 2);
    std.mem.writeInt(i32, data[20..24], 2, .big);
    data[24] = 'a';
    data[25] = 'b';
    std.mem.writeInt(i32, data[26..30], 1, .big);
    data[30] = 'c';
    var out: [64]u8 = undefined;
    try t.expectString("[\"ab\",\"c\"]", out[0..writeTextArrayJson(&data, &out)]);

    // now a header claiming a 100-byte string with no payload — must `break`,
    // never read past `data`.
    var trunc: [20 + 4]u8 = undefined;
    testArrayHeader(&trunc, 0, 25, 1);
    std.mem.writeInt(i32, trunc[20..24], 100, .big);
    const n = writeTextArrayJson(&trunc, &out);
    try t.expectEqual(true, n >= 1 and out[0] == '[');
}

test "simdJsonEscape: control chars escaped (not dropped), overflow signalled" {
    var out: [64]u8 = undefined;
    // \b \t \n \f \r short forms + \u00XX for other control chars, + " and \.
    const s = simdJsonEscape("a\x00b\x01\t\n\"\\\x1f", &out).?;
    try t.expectString("a\\u0000b\\u0001\\t\\n\\\"\\\\\\u001f", out[0..s]);

    // No escape needed → verbatim (exercises the SIMD fast path ≥16 bytes).
    const plain = "0123456789abcdefABCDEF";
    const n = simdJsonEscape(plain, &out).?;
    try t.expectString(plain, out[0..n]);

    // Overflow → null so the caller triggers grow-and-retry.
    var tiny: [2]u8 = undefined;
    try t.expectEqual(null, simdJsonEscape("\x01\x01", &tiny)); // needs 12 bytes
}

test "writeJsonHex: `\\x`-prefixed lowercase hex JSON string" {
    var out: [32]u8 = undefined;
    const n = writeJsonHex(&out, &[_]u8{ 0xde, 0xad, 0xbe, 0xef });
    try t.expectString("\"\\\\xdeadbeef\"", out[0..n]);
    // empty bytea → `"\x"`
    const e = writeJsonHex(&out, &[_]u8{});
    try t.expectString("\"\\\\x\"", out[0..e]);
    // too small → 0 (grow-and-retry)
    var tiny: [4]u8 = undefined;
    try t.expectEqual(0, writeJsonHex(&tiny, &[_]u8{ 1, 2 }));
}

test "isoDate/isoTime/isoTimestamp/uuidToStr: canonical strings" {
    var buf: [40]u8 = undefined;
    // DATE: day 0 = 2000-01-01; leap day 2000-02-29 is day 59.
    try t.expectString("2000-01-01", isoDate(&buf, 0).?);
    try t.expectString("2000-02-29", isoDate(&buf, 59).?);

    // TIME: fraction omitted when zero, 6-digit when present.
    try t.expectString("00:00:00", isoTime(&buf, 0).?);
    try t.expectString("09:05:00", isoTime(&buf, (9 * 3600 + 5 * 60) * 1_000_000).?);
    try t.expectString("09:05:00.001000", isoTime(&buf, (9 * 3600 + 5 * 60) * 1_000_000 + 1000).?);

    // TIMESTAMP: naive ISO (no 'Z'); sub-second preserved; pre-2000 floors right.
    try t.expectString("2000-01-01T00:00:00", isoTimestamp(&buf, 0).?);
    try t.expectString("2000-01-01T00:00:00.123456", isoTimestamp(&buf, 123456).?);
    try t.expectString("1999-12-31T23:59:59.500000", isoTimestamp(&buf, -500000).?);

    // UUID: 16 bytes → canonical lowercase hyphenated (same vector as types.zig).
    const uuid_bytes = [_]u8{ 183, 204, 40, 47, 236, 67, 73, 190, 142, 9, 170, 250, 176, 16, 73, 21 };
    try t.expectString("b7cc282f-ec43-49be-8e09-aafab0104915", uuidToStr(&uuid_bytes, &buf).?);
    try t.expectEqual(null, uuidToStr(&[_]u8{ 1, 2, 3 }, &buf)); // too short
}

test "writeJsonValue: binary scalar/array types render as valid JSON" {
    var out: [128]u8 = undefined;
    const S = struct {
        fn render(oid: i32, data: []const u8, buf: []u8) []const u8 {
            var vals = [_]Result.State.Value{.{ .is_null = false, .data = data }};
            var oids = [_]i32{oid};
            // writeJsonValue only reads .values/.oids — _result is never touched.
            const row = Row{ ._result = undefined, .oids = &oids, .values = &vals };
            return buf[0..row.writeJsonValue(0, buf)];
        }
    };

    // UUID (2950): 16 binary bytes → canonical quoted string (was corrupt raw bytes).
    const uuid_bytes = [_]u8{ 183, 204, 40, 47, 236, 67, 73, 190, 142, 9, 170, 250, 176, 16, 73, 21 };
    try t.expectString("\"b7cc282f-ec43-49be-8e09-aafab0104915\"", S.render(2950, &uuid_bytes, &out));

    // DATE (1082) day 0 → 2000-01-01; TIME (1083) 0 → 00:00:00.
    try t.expectString("\"2000-01-01\"", S.render(1082, &[_]u8{ 0, 0, 0, 0 }, &out));
    try t.expectString("\"00:00:00\"", S.render(1083, &([_]u8{0} ** 8), &out));

    // TIMESTAMP (1114): sub-second preserved, real ISO-8601 (was bare epoch seconds).
    var ts: [8]u8 = undefined;
    std.mem.writeInt(i64, &ts, 123456, .big);
    try t.expectString("\"2000-01-01T00:00:00.123456\"", S.render(1114, &ts, &out));

    // BYTEA (17): `\x`hex JSON string (was raw binary → invalid JSON).
    try t.expectString("\"\\\\xdead\"", S.render(17, &[_]u8{ 0xde, 0xad }, &out));

    // Unknown oid: valid UTF-8 → escaped string; invalid UTF-8 → hex (never raw).
    try t.expectString("\"h\\u0001i\"", S.render(99999, "h\x01i", &out));
    try t.expectString("\"\\\\xfffe\"", S.render(99999, &[_]u8{ 0xff, 0xfe }, &out));

    // text[] (1009) with an embedded control char escapes to valid JSON.
    var arr: [20 + 4 + 3]u8 = undefined;
    testArrayHeader(&arr, 0, 25, 1);
    std.mem.writeInt(i32, arr[20..24], 3, .big);
    arr[24] = 'a';
    arr[25] = '\t';
    arr[26] = 'b';
    try t.expectString("[\"a\\tb\"]", S.render(1009, &arr, &out));
}

test "writeJsonValue: non-finite floats + NUMERIC render losslessly, never null/invalid" {
    var out: [128]u8 = undefined;
    const S = struct {
        fn render(oid: i32, data: []const u8, buf: []u8) []const u8 {
            var vals = [_]Result.State.Value{.{ .is_null = false, .data = data }};
            var oids = [_]i32{oid};
            const row = Row{ ._result = undefined, .oids = &oids, .values = &vals };
            return buf[0..row.writeJsonValue(0, buf)];
        }
    };
    const N = struct {
        // Build a NUMERIC binary payload: header (ndigits, weight, sign, dscale)
        // + base-10000 digit groups. Returns the slice of `buf` that was written.
        fn wire(buf: []u8, digits: []const i16, weight: i16, sign: u16, dscale: i16) []const u8 {
            std.mem.writeInt(i16, buf[0..2], @intCast(digits.len), .big);
            std.mem.writeInt(i16, buf[2..4], weight, .big);
            std.mem.writeInt(u16, buf[4..6], sign, .big);
            std.mem.writeInt(i16, buf[6..8], dscale, .big);
            for (digits, 0..) |d, i| std.mem.writeInt(i16, buf[8 + i * 2 ..][0..2], d, .big);
            return buf[0 .. 8 + digits.len * 2];
        }
    };

    // float8 (701): NaN / ±Inf → LOSSLESS quoted tokens (was invalid `nan`/`inf`,
    // and the earlier `null` fix silently HID the value). float()-parseable.
    var f8: [8]u8 = undefined;
    std.mem.writeInt(u64, &f8, @bitCast(std.math.nan(f64)), .big);
    try t.expectString("\"NaN\"", S.render(701, &f8, &out));
    std.mem.writeInt(u64, &f8, @bitCast(std.math.inf(f64)), .big);
    try t.expectString("\"Infinity\"", S.render(701, &f8, &out));
    std.mem.writeInt(u64, &f8, @bitCast(-std.math.inf(f64)), .big);
    try t.expectString("\"-Infinity\"", S.render(701, &f8, &out));
    // a finite float8 still renders as a bare JSON number.
    std.mem.writeInt(u64, &f8, @bitCast(@as(f64, 1.5)), .big);
    try t.expectString("1.5", S.render(701, &f8, &out));

    // float4 (700): NaN → quoted token (mirrors float8).
    var f4: [4]u8 = undefined;
    std.mem.writeInt(u32, &f4, @bitCast(std.math.nan(f32)), .big);
    try t.expectString("\"NaN\"", S.render(700, &f4, &out));

    // NUMERIC (1700): exact decimal STRING — no float precision loss, matches
    // db.zig's serializer + str(Decimal). Emitted as a quoted JSON string.
    var w: [64]u8 = undefined;
    // integer 123 (ndigits=1, weight=0, sign=+, dscale=0).
    try t.expectString("\"123\"", S.render(1700, N.wire(&w, &.{123}, 0, 0x0000, 0), &out));
    // 1.5 (digits {1, 5000}, weight=0, dscale=1).
    try t.expectString("\"1.5\"", S.render(1700, N.wire(&w, &.{ 1, 5000 }, 0, 0x0000, 1), &out));
    // 18-significant-digit value a float64 could NOT represent exactly:
    // 123456789012345678, base-10000 = [12, 3456, 7890, 1234, 5678], weight=4
    // (5 integer groups → weight = ndigits-1). Proves the exact-string path.
    try t.expectString(
        "\"123456789012345678\"",
        S.render(1700, N.wire(&w, &.{ 12, 3456, 7890, 1234, 5678 }, 4, 0x0000, 0), &out),
    );
    // -0.5 (negative, digit 5000 at weight -1, dscale 1).
    try t.expectString("\"-0.5\"", S.render(1700, N.wire(&w, &.{5000}, -1, 0x4000, 1), &out));
    // 100.0000 — integer 100 padded to scale 4 (round-4 scale-padding guard).
    try t.expectString("\"100.0000\"", S.render(1700, N.wire(&w, &.{100}, 0, 0x0000, 4), &out));
    // Zero coefficient with dscale >= 7 → scientific "0E-{dscale}" (matches
    // str(Decimal), whose adjusted exponent drops below -6).
    try t.expectString("\"0E-7\"", S.render(1700, N.wire(&w, &.{}, 0, 0x0000, 7), &out));
    // Zero with a small scale → "0.0000"; with scale 0 → "0".
    try t.expectString("\"0.0000\"", S.render(1700, N.wire(&w, &.{}, 0, 0x0000, 4), &out));
    try t.expectString("\"0\"", S.render(1700, N.wire(&w, &.{}, 0, 0x0000, 0), &out));
    // NaN / Infinity / -Infinity specials (encoded in the sign field only).
    try t.expectString("\"NaN\"", S.render(1700, N.wire(&w, &.{}, 0, 0xC000, 0), &out));
    try t.expectString("\"Infinity\"", S.render(1700, N.wire(&w, &.{}, 0, 0xD000, 0), &out));
    try t.expectString("\"-Infinity\"", S.render(1700, N.wire(&w, &.{}, 0, 0xF000, 0), &out));
}

test "writeTextArrayJson: control char in element escaped to valid JSON" {
    // one-element text[] whose element is "a\tb" (embedded TAB) → must escape.
    var data: [20 + 4 + 3]u8 = undefined;
    testArrayHeader(&data, 0, 25, 1);
    std.mem.writeInt(i32, data[20..24], 3, .big);
    data[24] = 'a';
    data[25] = '\t';
    data[26] = 'b';
    var out: [64]u8 = undefined;
    try t.expectString("[\"a\\tb\"]", out[0..writeTextArrayJson(&data, &out)]);
}

test "Iterator.fromPgzRow: rejects malformed 1-D array headers" {
    const It = IteratorT(.safe, i32);
    const oid = types.Int32Array.oid.decimal;

    // header shorter than 20 bytes (but not the empty-array 12) — was a no-op
    // lib.assert, now a real error
    var short: [15]u8 = undefined;
    testArrayHeaderPartial(&short);
    try t.expectError(error.InvalidData, It.fromPgzRow(&short, oid));

    // ndim != 1
    var multidim: [20]u8 = undefined;
    testArrayHeader(&multidim, 0, 23, 0);
    std.mem.writeInt(i32, multidim[0..4], 2, .big);
    try t.expectError(error.InvalidData, It.fromPgzRow(&multidim, oid));

    // negative declared element count
    var negcount: [20]u8 = undefined;
    testArrayHeader(&negcount, 0, 23, -1);
    try t.expectError(error.InvalidData, It.fromPgzRow(&negcount, oid));
}

fn testArrayHeaderPartial(buf: []u8) void {
    std.mem.writeInt(i32, buf[0..4], 1, .big);
    for (buf[4..]) |*b| b.* = 0;
}

test "Iterator.next: hostile element length stops instead of reading OOB" {
    const It = IteratorT(.safe, i32);
    const oid = types.Int32Array.oid.decimal;

    // _len claims 1 element; the element declares a 255-byte value with only
    // 2 payload bytes present. next() must return null (data_end > data.len).
    var data: [20 + 4 + 2]u8 = undefined;
    testArrayHeader(&data, 0, 23, 1);
    std.mem.writeInt(i32, data[20..24], 255, .big);
    data[24] = 0;
    data[25] = 0;
    var it = try It.fromPgzRow(&data, oid);
    try t.expectEqual(null, it.next());
}

test "Iterator.alloc: valid array and truncation error" {
    const It = IteratorT(.safe, i32);
    const oid = types.Int32Array.oid.decimal;

    // valid [7, 9]
    var data: [20 + 8 + 8]u8 = undefined;
    testArrayHeader(&data, 0, 23, 2);
    std.mem.writeInt(i32, data[20..24], 4, .big);
    std.mem.writeInt(i32, data[24..28], 7, .big);
    std.mem.writeInt(i32, data[28..32], 4, .big);
    std.mem.writeInt(i32, data[32..36], 9, .big);
    var it = try It.fromPgzRow(&data, oid);
    const arr = try it.alloc(t.allocator);
    defer t.allocator.free(arr);
    try t.expectSlice(i32, &.{ 7, 9 }, arr);

    // _len says 2 but only 1 element present → fillAlloc errors, no leak
    var trunc: [20 + 8]u8 = undefined;
    testArrayHeader(&trunc, 0, 23, 2);
    std.mem.writeInt(i32, trunc[20..24], 4, .big);
    std.mem.writeInt(i32, trunc[24..28], 7, .big);
    var it2 = try It.fromPgzRow(&trunc, oid);
    try t.expectError(error.InvalidData, it2.alloc(t.allocator));
}
