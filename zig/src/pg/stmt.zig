const std = @import("std");
const lib = @import("lib.zig");
const Buffer = @import("buffer").Buffer;
const TRACE = @import("builtin").mode == .Debug;

const types = lib.types;
const Conn = lib.Conn;
const Result = lib.Result;
const Allocator = std.mem.Allocator;
const ArenaAllocator = std.heap.ArenaAllocator;

pub const Stmt = struct {
    buf: *Buffer,

    opts: Conn.QueryOpts,

    conn: *Conn,
    // Executing a stmt may or may not require allocations. It depends on the
    // number of columns, number of parameters, size of the SQL, size of the
    // serialized values and our configuration (e.g. how big
    // our write buffer is).
    arena: *ArenaAllocator,

    // Every call to stmt.bind increments this value. Important because the Bind
    // message contains all the parameter meta data first, then the serialized
    // values. So when we bind a parameter, we need to jump around our buf payload
    // based on the param_index * $some_offset.
    param_index: u16,

    // Number of parameters in the query.
    param_count: u16,

    // The type of each parameter, which postgresql tells us after we send it the
    // SQL and ask for a description. `param_oids.len` can be greater than
    // `param_count` because we initially use the conn._param_oids which is
    // globally configured.
    param_oids: []i32,

    // Number of colums in the result
    column_count: u16,

    // Information about the colums in the result, which postgresql tells us after
    // we send it the SQL and ask for a description. The slices in this structure
    // can be larger than `column_count` because we initially conn._result_state
    // which is globally configured.
    result_state: Result.State,

    // Name of the prepared statement. Empty == unnamed, so it won't be cached
    // by the server
    name: []const u8,

    // Offset where the current Bind message starts in the buffer.
    // Used by batch execution to patch the Bind length at the correct position.
    bind_start: usize = 0,

    pub fn init(conn: *Conn, opts: Conn.QueryOpts) !Stmt {
        const base_allocator = opts.allocator orelse conn._allocator;
        const arena = try base_allocator.create(ArenaAllocator);
        arena.* = ArenaAllocator.init(base_allocator);

        return .{
            .conn = conn,
            .opts = opts,
            .buf = &conn._buf,
            .arena = arena,
            .param_index = 0,
            .param_count = 0,
            .param_oids = conn._param_oids,
            .column_count = 0,
            .result_state = conn._result_state,
            .name = opts.cache_name orelse "",
        };
    }

    pub fn fromDescribe(conn: *Conn, describe: *Describe, opts: Conn.QueryOpts) !Stmt {
        const base_allocator = opts.allocator orelse conn._allocator;
        const arena = try base_allocator.create(ArenaAllocator);
        arena.* = ArenaAllocator.init(base_allocator);

        return .{
            .conn = conn,
            .opts = opts,
            .buf = &conn._buf,
            .arena = arena,
            .param_index = 0,
            .param_count = @intCast(describe.param_oids.len),
            .param_oids = describe.param_oids,
            .column_count = @intCast(describe.result_state.oids.len),
            .result_state = describe.result_state,
            .name = opts.cache_name.?,
        };
    }

    // Should only be called in an error case. In a normal case, where
    // stmt.execute() returns a result, stmt.deinit() must not be called (all
    // ownership is passed to the result).
    pub fn deinit(self: *Stmt) void {
        self.conn._reader.endFlow() catch {
            // this can only fail in extreme conditions (OOM) and it will only impact
            // the next query (and if the app is using the pool, the pool will try to
            // recover from this anyways)
            self.conn._state = .fail;
        };

        const arena = self.arena;
        const allocator = arena.child_allocator;
        arena.deinit();
        allocator.destroy(arena);
    }

    // When describe_allocator != null, we intend to cache the query information
    // (in conn.__prepared_statements).
    pub fn prepare(self: *Stmt, sql: []const u8, describe_allocator: ?Allocator) !void {
        var conn = self.conn;
        const opts = &self.opts;
        const statement_arena = self.arena.allocator();

        try conn._reader.startFlow(statement_arena, opts.timeout);

        var buf = self.buf;
        buf.reset();

        const name = self.name;

        // This function will issue Close* + Parse + Describe + Sync commands.
        // Close messages deallocate evicted prepared statements on the server.
        // Parse sends the SQL to PostgreSQL for preparation.
        // We need the response from Describe to build our Bind message.
        const num_pending_closes = conn._pending_deallocates.items.len;
        {
            // Calculate Close message sizes for pending deallocates.
            // Close format: 'C' + int32(len) + 'S' + name + '\0'
            // CloseComplete response: '3' + int32(4) — must be consumed.
            const pending = conn._pending_deallocates.items;
            var close_total_len: usize = 0;
            for (pending) |dealloc_name| {
                close_total_len += 1 + 4 + 1 + dealloc_name.len + 1; // type + len + 'S' + name + null
            }

            // Optional declared param type OIDs (see QueryOpts.param_oids).
            // Each declared OID adds 4 bytes to the Parse payload.
            const declared_oids: []const i32 = opts.param_oids orelse &.{};
            const parse_payload_len = 8 + sql.len + name.len + declared_oids.len * 4;
            const describe_payload_len = 6 + name.len;
            const sync_payload_len = 4;

            const total_length = close_total_len + 3 + parse_payload_len + describe_payload_len + sync_payload_len;

            try buf.ensureTotalCapacity(total_length);
            var view = buf.skip(total_length) catch unreachable;

            // CLOSE messages for evicted statements (batched, zero extra round-trips)
            for (pending) |dealloc_name| {
                const close_payload_len: u32 = @intCast(4 + 1 + dealloc_name.len + 1);
                view.writeByte('C');
                view.writeIntBig(u32, close_payload_len);
                view.writeByte('S'); // Close a prepared Statement
                view.write(dealloc_name);
                view.writeByte(0);
            }

            // PARSE
            view.writeByte('P');
            view.writeIntBig(u32, @intCast(parse_payload_len));
            view.write(name);
            view.writeByte(0);
            view.write(sql);
            view.writeByte(0); // null-terminate the SQL string
            // Parameter type declarations: int16 count, then that many int32
            // OIDs. Historically 0 (let PostgreSQL infer everything); when the
            // caller supplies inferred OIDs we declare them so shapes like
            // `col = ANY(ARRAY[$1,$2])` bind with the right element type.
            view.writeIntBig(u16, @intCast(declared_oids.len));
            for (declared_oids) |oid| {
                view.writeIntBig(i32, oid);
            }

            // DESCRIBE
            view.writeByte('D');
            view.writeIntBig(u32, @intCast(describe_payload_len));
            view.writeByte('S'); // Describe a prepared statement
            view.write(name);
            view.writeByte(0); // null terminate our name

            // SYNC
            view.write(&.{ 'S', 0, 0, 0, 4 });
            try conn.write(buf.string());

            // Free pending deallocate names and clear the list
            for (pending) |dealloc_name| {
                conn._allocator.free(dealloc_name);
            }
            conn._pending_deallocates.clearRetainingCapacity();
        }

        // no longer idle, we're now in a query
        conn._state = .query;

        // Consume CloseComplete ('3') responses for each pending deallocate,
        // then the ParseComplete ('1') for the new statement.
        {
            var got_parse_complete = false;
            var close_idx: usize = 0;
            while (close_idx < num_pending_closes) : (close_idx += 1) {
                const close_msg = conn.read() catch |err| {
                    if (TRACE) {
                        std.debug.print("[PG.STMT] prepare CLOSE response failed err={s}\n", .{@errorName(err)});
                    }
                    conn.readyForQuery() catch {};
                    return err;
                };
                if (close_msg.type == '3') continue; // CloseComplete — expected
                if (close_msg.type == '1') {
                    got_parse_complete = true;
                    break;
                }
                return conn.unexpectedDBMessage();
            }

            // Read ParseComplete if we haven't consumed it yet
            if (!got_parse_complete) {
                const msg = conn.read() catch |err| {
                    if (TRACE) {
                        std.debug.print("[PG.STMT] prepare PARSE FAILED err={s} name={s} sql={s}\n", .{
                            @errorName(err),
                            name,
                            sql[0..@min(sql.len, 80)],
                        });
                        if (conn.err) |pg_err| {
                            std.debug.print("[PG.STMT] PG ERROR during Parse: {s}\n", .{
                                pg_err.message[0..@min(pg_err.message.len, 200)],
                            });
                        }
                    }
                    conn.readyForQuery() catch {};
                    return err;
                };

                if (msg.type != '1') {
                    return conn.unexpectedDBMessage();
                }
            }
        }

        var param_count: u16 = 0;

        {
            // we expect a ParameterDescription message
            const msg = try conn.read();
            if (msg.type != 't') {
                return conn.unexpectedDBMessage();
            }

            var param_oids = self.param_oids;
            const data = msg.data;
            // ParameterDescription: int16 count, then count * int32 OIDs. The
            // count is peer-controlled — validate the payload actually holds the
            // 2-byte count and all count*4 OID bytes before the read loop below,
            // which had no such guard.
            if (data.len < 2) return conn.unexpectedDBMessage();
            param_count = std.mem.readInt(u16, data[0..2], .big);
            if (data.len < 2 + @as(usize, param_count) * 4) return conn.unexpectedDBMessage();
            if (describe_allocator) |da| {
                // If we plan on caching this prepared statement, then we need
                // to allocate a new param_oids list which will outlive this
                // statement
                param_oids = try da.alloc(i32, param_count);
                self.param_oids = param_oids;
            } else if (param_count > param_oids.len) {
                lib.metrics.allocParams(param_count);
                param_oids = try statement_arena.alloc(i32, param_count);
                self.param_oids = param_oids;
            }

            var pos: usize = 2;
            for (0..param_count) |i| {
                const end = pos + 4;
                param_oids[i] = std.mem.readInt(i32, data[pos..end][0..4], .big);
                pos = end;
            }
            self.param_count = param_count;
        }

        {
            // We now expect an answer to our describe message.
            // This is either going to be a RowDescription, or a NoData. NoData means
            // our statement doesn't return any data. Either way, we're going to use
            // this information when we generate our Bind message, next.
            const msg = try conn.read();
            switch (msg.type) {
                'n' => {}, // no data, column_count = 0
                'T' => {
                    var state = self.result_state;
                    const data = msg.data;
                    // RowDescription: int16 column count, then per-column fields.
                    // Guard the 2-byte count read; the per-column field walk in
                    // Result.State.from is already bounds-checked.
                    if (data.len < 2) return conn.unexpectedDBMessage();
                    const column_count = std.mem.readInt(u16, data[0..2], .big);
                    if (describe_allocator) |da| {
                        // If we plan on caching this prepared statement, then we need
                        // to allocate a new param_oids list which will outlive this
                        // statement
                        state = try Result.State.init(da, column_count);
                        self.result_state = state;
                    } else if (column_count > state.oids.len) {
                        lib.metrics.allocColumns(column_count);
                        // we have more columns than our self._result_state can handle, we
                        // need to create a new Result.State specifically for this
                        state = try Result.State.init(statement_arena, column_count);
                        self.result_state = state;
                    }
                    const a: ?Allocator = if (opts.column_names) (describe_allocator orelse statement_arena) else null;
                    try state.from(column_count, data, a);
                    self.column_count = column_count;
                },
                else => return conn.unexpectedDBMessage(),
            }
        }

        return self.prepareForBind(param_count);
    }

    // We need to call Bind for every value we're binding. Rather than having
    // to check "is this the first call to bind" each time, we make it the caller's
    // responsibility to "prepareForBind" upfront.
    pub fn prepareForBind(self: *Stmt, param_count: u16) !void {
        try self.conn.readyForQuery();

        var buf = self.buf;
        buf.resetRetainingCapacity();

        const name = self.name;

        // Bind command = 'B'
        // 4 byte length placeholder - 0, 0, 0, 0
        // portal name (empty string, length 0) - 0
        // prepared statement name  + null terminator
        try buf.ensureTotalCapacity(1 + 4 + 1 + name.len + 1 + 2);

        // length of buffer is guaranteed to be 128, so it's safe to use
        // writeAssumeCapacity (4 byte length placeholder, 1 byte empty portal)
        buf.writeAssumeCapacity(&.{ 'B', 0, 0, 0, 0, 0 });

        buf.writeAssumeCapacity(name);
        buf.writeByteAssumeCapacity(0);

        // number of parameters types we're sending a
        try buf.writeIntBig(u16, param_count);

        // the format (text or binary) of each parameter. We'll default to text
        // for now, and fill this in as we get the data.
        // Widen before the *2: param_count is u16 and PG allows up to 65535
        // params, so `param_count * 2` in u16 wraps for >= 32768 params.
        try buf.writeByteNTimes(0, @as(usize, param_count) * 2);

        // number of parameters we're sending a
        try buf.writeIntBig(u16, param_count);
    }

    pub fn bind(self: *Stmt, value: anytype) !void {
        const name = self.name;

        const param_index = self.param_index;
        lib.assert(param_index < self.param_count);

        // Format codes are at bind_start + 9 + name.len offset, 2 bytes each.
        // Widen param_index before the *2: it is u16 and wraps for >= 32768.
        const format_offset = self.bind_start + 9 + (@as(usize, param_index) * 2) + name.len;

        try types.bindValue(@TypeOf(value), self.param_oids[param_index], value, self.buf, format_offset);
        self.param_index = param_index + 1;
    }

    /// Finish the current Bind message and append Execute — WITHOUT Sync.
    /// Used for batch execution: call this for each row, then send Sync separately.
    /// Returns the number of bytes written (for batch flush decisions).
    pub fn finishBindExecuteNoSync(self: *Stmt) !usize {
        lib.assert(self.param_index == self.param_count);

        const buf = self.buf;
        const bind_start = self.bind_start;

        // Result encoding suffix for the Bind message
        try lib.types.resultEncoding(self.result_state.oids[0..self.column_count], buf);

        // Patch the Bind message length at bind_start+1
        // Length = total bytes from byte after 'B' to end of Bind message
        const bind_end = buf.len();
        std.mem.writeInt(u32, buf.buf[bind_start + 1 ..][0..4], @intCast(bind_end - bind_start - 1), .big);

        // Execute message (unnamed portal, no row limit) — NO Sync
        try buf.write(&.{
            'E',
            0, 0, 0, 9, // message length
            0, // unnamed portal
            0, 0, 0, 0, // no row limit
        });

        return buf.len();
    }

    /// Re-initialize the Bind message for a new row in batch mode.
    /// Preserves the prepared statement state, resets only the bind buffer.
    pub fn startNewBind(self: *Stmt) !void {
        var buf = self.buf;
        const name = self.name;
        const param_count = self.param_count;

        // Record where this Bind message starts
        self.bind_start = buf.len();

        try buf.ensureUnusedCapacity(1 + 4 + 1 + name.len + 1 + 2 + (@as(usize, param_count) * 2) + 2);

        buf.writeAssumeCapacity(&.{ 'B', 0, 0, 0, 0, 0 }); // Bind + length placeholder + empty portal
        buf.writeAssumeCapacity(name);
        buf.writeByteAssumeCapacity(0);

        // Parameter format codes
        try buf.writeIntBig(u16, param_count);
        try buf.writeByteNTimes(0, @as(usize, param_count) * 2);

        // Parameter count
        try buf.writeIntBig(u16, param_count);

        self.param_index = 0;
    }

    pub fn execute(self: *Stmt) !*Result {
        lib.assert(self.param_index == self.param_count);

        // We haven't sent our `bind` message yet. We need to finish it, and then
        // send it, along with our `Execute` and a final `Sync` message.

        const buf = self.buf;
        const conn = self.conn;

        // The last part of the bind message is telling PostgreSQL the format we
        // want to receive the result columns in.
        try lib.types.resultEncoding(self.result_state.oids[0..self.column_count], buf);

        // Patch the Bind message length at bind_start+1
        const bind_start = self.bind_start;
        std.mem.writeInt(u32, buf.buf[bind_start + 1 ..][0..4], @intCast(buf.len() - bind_start - 1), .big);

        try buf.write(&.{
            'E',
            // message length
            0,
            0,
            0,
            9,
            // unname portal
            0,
            // no row limit
            0,
            0,
            0,
            0,
            // sync
            'S',
            // message length
            0,
            0,
            0,
            4,
        });

        try conn.write(buf.string());

        {
            const msg = conn.read() catch |err| {
                conn.readyForQuery() catch {};
                return err;
            };
            if (msg.type != '2') {
                // expecting a BindComplete
                return conn.unexpectedDBMessage();
            }
        }

        try conn.peekForError();

        // our call to readyForQuery above changed the state, but as far as we're
        // concerned, we're still doing the query.
        conn._state = .query;

        lib.metrics.query();

        const opts = &self.opts;
        const state = self.result_state;
        const column_count = self.column_count;

        const arena = self.arena;

        // Put result on the heap largely for the QueryRow (created via the
        // conn.row(...) helper). This allows QueryRow.result and QueryRow.row._result
        // to reference the result, which isn't otherwise owned.
        const result = try arena.allocator().create(Result);
        result.* = .{
            ._conn = conn,
            ._arena = self.arena,
            ._release_conn = opts.release_conn,
            ._oids = state.oids[0..column_count],
            ._values = state.values[0..column_count],
            .column_names = if (opts.column_names) state.names[0..column_count] else &[_][]const u8{},
            .number_of_columns = column_count,
        };
        return result;
    }

    pub const Describe = struct {
        param_oids: []i32,
        arena: ArenaAllocator,
        result_state: Result.State,
    };
};
