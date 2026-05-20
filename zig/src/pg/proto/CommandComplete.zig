const std = @import("std");
const proto = @import("_proto.zig");
const Reader = proto.Reader;

const CommandComplete = @This();

tag: []const u8,

pub fn parse(data: []const u8) !CommandComplete {
    var reader = Reader.init(data);
    return .{
        .tag = try reader.restAsString(),
    };
}

// Finds the last number in the tag of the command completed. If no number is
// found, than 0 rows were affected. Commands like "create table" or "create
// role" have a tag that's just "CREATE XYZ".
// But update/delete/select/... have something like "delete #"
// "insert" is a bit more complicated, but the rows inserted is the last number
// so this works for it too.
pub fn rowsAffected(self: CommandComplete) ?i64 {
    const tag = self.tag;
    if (tag.len == 0) {
        return null;
    }

    // The rows-affected number, if any, is the trailing run of digits. Scan
    // backwards from the last byte to the first non-digit. An explicit
    // `i > 0` guard avoids the usize underflow an `i >= 0` loop hits on an
    // all-digit tag (which would wrap past 0 and read out of bounds).
    var i: usize = tag.len - 1;

    // Tag ends in a non-digit -> no number.
    if (tag[i] < '0' or tag[i] > '9') {
        return null;
    }

    while (i > 0) {
        const b = tag[i - 1];
        if (b < '0' or b > '9') {
            break;
        }
        i -= 1;
    }

    // tag[i..] is the maximal trailing run of digits (i == 0 when the entire
    // tag is digits).
    return std.fmt.parseInt(i64, tag[i..], 10) catch unreachable;
}

const t = proto.testing;
test "CommandComplete: parse" {
    var buf = try proto.Buffer.init(t.allocator, 128);
    defer buf.deinit();

    {
        // not a string (not null terminated)
        try buf.write("123");
        try t.expectError(error.NotAString, CommandComplete.parse(buf.string()));
    }

    {
        // V2: zero-length remainder (pos == data.len) must not read r[SIZE_MAX];
        // restAsString returns error.NotAString instead of an OOB read.
        try t.expectError(error.NotAString, CommandComplete.parse(""));
    }

    {
        // success
        buf.reset();
        try buf.write("CREATE ROLE");
        try buf.writeByte(0);

        const c = try CommandComplete.parse(buf.string());
        try t.expectString("CREATE ROLE", c.tag);
    }
}

test "CommandComplete: rowsAffected" {
    {
        const c = CommandComplete{ .tag = "DROP ROLE" };
        try t.expectEqual(null, c.rowsAffected());
    }

    {
        const c = CommandComplete{ .tag = "INSERT 392 1" };
        try t.expectEqual(1, c.rowsAffected());
    }

    {
        const c = CommandComplete{ .tag = "DELETE 9392" };
        try t.expectEqual(9392, c.rowsAffected());
    }

    {
        // V1: empty tag must not underflow -> null (no rows).
        const c = CommandComplete{ .tag = "" };
        try t.expectEqual(null, c.rowsAffected());
    }

    {
        // V1: all-digit tag must not run the reverse scan past index 0.
        const c = CommandComplete{ .tag = "12345" };
        try t.expectEqual(12345, c.rowsAffected());
    }

    {
        // V1: single trailing digit.
        const c = CommandComplete{ .tag = "SELECT 1" };
        try t.expectEqual(1, c.rowsAffected());
    }
}
