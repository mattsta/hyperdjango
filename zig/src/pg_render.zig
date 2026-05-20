//! Pure PostgreSQL binary → canonical-string renderers.
//!
//! These convert PG binary wire values (NUMERIC / TIMESTAMP / DATE / TIME /
//! UUID) into the exact text form the Python object path serializes to, so the
//! query_json fast path (db.zig) stays byte-identical to
//! `query() + fast_json_dumps`:
//!   * datetime/date/time → `isoformat()`  (naive — no UTC offset)
//!   * Decimal            → `str(Decimal)`  (scale-padded decimal string)
//!   * UUID               → `str(UUID)`     (lowercase hyphenated)
//!
//! They are the SINGLE SOURCE OF TRUTH shared by both the Python-object
//! converters (which wrap these strings in Decimal()/UUID()/datetime) and the
//! JSON writer. Kept dependency-free (only `std`) so they can be unit-tested
//! standalone with `zig test src/pg_render.zig` — the whole-extension test
//! artifact currently can't build under this Zig due to unrelated fuzz-API
//! churn in server.zig.

const std = @import("std");

// ── PostgreSQL type OIDs handled here ───────────────────────────────────────
pub const OID_DATE: i32 = 1082;
pub const OID_TIME: i32 = 1083;
pub const OID_TIMESTAMP: i32 = 1114;
pub const OID_TIMESTAMPTZ: i32 = 1184;
pub const OID_NUMERIC: i32 = 1700;
pub const OID_UUID: i32 = 2950;

// ── Calendar arithmetic ─────────────────────────────────────────────────────

pub const Civil = struct { y: i64, m: u32, d: u32 };

/// Howard Hinnant's civil-from-days: `z` = days since 1970-01-01 (proleptic
/// Gregorian). Returns (year, month[1-12], day[1-31]). Valid across the full
/// representable range.
pub fn civilFromDays(z_in: i64) Civil {
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

/// Append `.ffffff` (6-digit microseconds) iff usec != 0 — matches how
/// isoformat() only emits a fractional part for non-zero microseconds.
fn writeIsoMicros(buf: []u8, pos: usize, usec: u32) usize {
    if (usec == 0) return pos;
    var p = pos;
    if (p + 7 > buf.len) return p;
    buf[p] = '.';
    p += 1;
    const s = std.fmt.bufPrint(buf[p..], "{d:0>6}", .{usec}) catch return pos;
    return p + s.len;
}

/// TIMESTAMP/TIMESTAMPTZ (µs since 2000-01-01) → `YYYY-MM-DDTHH:MM:SS[.ffffff]`.
/// NAIVE — no UTC offset — matching the naive datetime the converter builds.
pub fn writeIsoTimestamp(buf: []u8, usec: i64) ?[]const u8 {
    const pg_epoch_offset: i64 = 946684800; // seconds Unix→PG epoch
    // @divFloor (not @divTrunc) so total_sec pairs correctly with @mod, which
    // always floors (rem_usec ∈ [0, 1e6)). @divTrunc rounds toward zero, which
    // for a negative usec with a nonzero fraction leaves total_sec one second
    // too high — e.g. -500000 µs → (0 s, 500000 µs) instead of (-1 s, 500000 µs),
    // rendering '1999-12-31 23:59:59.5' as '2000-01-01 00:00:00.5'.
    const total_sec = @divFloor(usec, 1_000_000) + pg_epoch_offset;
    const rem_usec: u32 = @intCast(@mod(usec, 1_000_000));
    const days = @divFloor(total_sec, 86400);
    const sod = total_sec - days * 86400; // seconds of day [0, 86399]
    const civ = civilFromDays(days);
    // Python's datetime only spans years 1..9999. A BC/overflow year would also
    // trap the `@intCast(civ.y)` to unsigned below (silent wrap in ReleaseFast).
    // Return null so the caller falls back to the Python object path.
    if (civ.y < 1 or civ.y > 9999) return null;
    var pos: usize = 0;
    // Year cast to unsigned for formatting — {d:0>4} on a signed int emits '+'.
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

/// DATE (days since 2000-01-01) → `YYYY-MM-DD` (matches date.isoformat()).
pub fn writeIsoDate(buf: []u8, days: i32) ?[]const u8 {
    // 2000-01-01 is unix day 10957.
    const civ = civilFromDays(@as(i64, days) + 10957);
    // date only spans years 1..9999; a BC/overflow year would trap the unsigned
    // cast below. Null → caller falls back to the Python object path.
    if (civ.y < 1 or civ.y > 9999) return null;
    return std.fmt.bufPrint(buf, "{d:0>4}-{d:0>2}-{d:0>2}", .{ @as(u64, @intCast(civ.y)), civ.m, civ.d }) catch null;
}

/// TIME (µs since midnight) → `HH:MM:SS[.ffffff]` (matches time.isoformat()).
pub fn writeIsoTime(buf: []u8, usec: i64) ?[]const u8 {
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

// ── NUMERIC ─────────────────────────────────────────────────────────────────

/// Render a PostgreSQL NUMERIC binary value into its canonical decimal string
/// (the exact text `Decimal(...)` is built from, and thus what `str(Decimal)`
/// reproduces — including trailing zeros padded out to `dscale`). Returns the
/// written slice, or null on a malformed/oversized value. `buf` must cover the
/// worst case: 64 base-10000 groups = 256 decimal digits, plus a sign and a
/// decimal point = 258 bytes; use >= 288 for scientific-exponent slack.
pub fn pgNumericToStr(data: []const u8, buf: []u8) ?[]const u8 {
    // Layout: ndigits(2), weight(2), sign(2), dscale(2), then ndigits base-10000 digits.
    if (data.len < 8) return null;
    const ndigits = std.mem.readInt(i16, data[0..2], .big);
    const weight = std.mem.readInt(i16, data[2..4], .big);
    const sign = std.mem.readInt(u16, data[4..6], .big);
    const dscale = std.mem.readInt(i16, data[6..8], .big);

    // Special values are encoded purely in the sign field (ndigits is 0). They
    // MUST be branched on BEFORE the ndigits==0 zero path, which would otherwise
    // render them as "0"/"0.0000". Python's Decimal accepts all three spellings,
    // so this string both builds the Decimal (object path) and is emitted as a
    // JSON string (query_json) — keeping the two paths byte-identical.
    switch (sign) {
        0xC000 => return copyLiteral(buf, "NaN"), // NUMERIC_NAN
        0xD000 => return copyLiteral(buf, "Infinity"), // NUMERIC_PINF
        0xF000 => return copyLiteral(buf, "-Infinity"), // NUMERIC_NINF
        else => {},
    }

    if (ndigits == 0) {
        // Zero coefficient, but dscale still dictates the rendered scale so the
        // string round-trips through Decimal unchanged. str(Decimal) gives:
        //   dscale 0      → "0"
        //   dscale 1..6   → "0." + dscale zeros           (e.g. "0.0000")
        //   dscale >= 7   → "0E-{dscale}"                 (scientific; adjusted
        //                    exponent -dscale drops below -6, e.g. "0E-7")
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

    var digits: [64]i16 = undefined;
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

    // Integer part: base-10000 digits with a non-negative exponent, i.e. indices
    // 0..weight (place value 10000^(weight-i)). When weight < 0 there are none.
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

    // Fractional part
    if (dscale > 0) {
        if (pos >= buf.len) return buf[0..pos];
        buf[pos] = '.';
        pos += 1;
        const udscale: usize = @intCast(@max(0, dscale));
        var frac_written: usize = 0;

        // Leading zero groups implied by weight < -1 (|value| < 0.0001). The first
        // STORED fractional digit (index int_digits, which is 0 here) has decimal
        // exponent -(-weight) base-10000 groups past the point — everything before
        // it is zero. Emitting these was the bug: a value like 0.00001234
        // (weight=-2) was rendered 0.12340000 (10^4× too large) because the first
        // stored group was placed immediately after the point.
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

        // Stored fractional digit groups begin at index int_digits. A while-loop
        // (not `for (int_digits..nd)`) so int_digits > nd — e.g. 10000.00, where
        // int_digits=2 and nd=1 — cannot form a reversed range (safety-panic /
        // UB in ReleaseFast).
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
        // dscale can exceed the significant fractional digits — pad with zeros
        // so str(Decimal) shows the full scale (matches Decimal(str) round-trip).
        while (frac_written < udscale and pos < buf.len) {
            buf[pos] = '0';
            pos += 1;
            frac_written += 1;
        }
    }

    // The plain string above is the correct value, but Python's Decimal.__str__
    // switches to scientific notation once the adjusted exponent drops below -6
    // (e.g. 0.000000005 → "5E-9"). pgNumericToStr's whole contract is to equal
    // str(Decimal), and this string both builds the Decimal (pgNumericToPyDecimal)
    // AND is emitted verbatim by the query_json fast path — so both must match
    // the object path byte-for-byte. Canonicalize the sub-1e-6 case.
    return canonicalizeDecimal(buf, pos, sign == 0x4000);
}

/// Reformat the plain decimal magnitude in `buf[0..pos]` into the exact form
/// Python's Decimal.__str__ produces. Only pure-fractional values whose first
/// significant digit sits more than 6 places past the decimal point (adjusted
/// exponent < -6) change — those render in scientific notation. Every other
/// value is already canonical and is returned unchanged. Scientific form is
/// always shorter than the plain form it replaces, so it fits in `buf`.
fn canonicalizeDecimal(buf: []u8, pos: usize, negative: bool) []const u8 {
    const sign_len: usize = if (negative) 1 else 0;
    if (pos < sign_len + 2) return buf[0..pos];
    const mag = buf[sign_len..pos];
    // Only "0.xxxx" magnitudes can have an adjusted exponent < -6.
    if (mag[0] != '0' or mag[1] != '.') return buf[0..pos];
    const frac = mag[2..];
    // Locate the first significant (non-zero) fractional digit.
    var first: usize = 0;
    while (first < frac.len and frac[first] == '0') first += 1;
    if (first >= frac.len) return buf[0..pos]; // all zeros → "0.000…" stays plain
    const k = first + 1; // 1-based position after the point == -(adjusted exponent)
    if (k <= 6) return buf[0..pos]; // adjusted >= -6 → plain form is already canonical

    // Scientific: coefficient = frac[first..] (trailing zeros preserved), exp = -k.
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

/// Copy a fixed literal into `buf`, returning the written slice (or null if it
/// does not fit). Used for the NUMERIC special values.
fn copyLiteral(buf: []u8, literal: []const u8) ?[]const u8 {
    if (buf.len < literal.len) return null;
    @memcpy(buf[0..literal.len], literal);
    return buf[0..literal.len];
}

// ── MONEY ───────────────────────────────────────────────────────────────────

/// Render a PostgreSQL MONEY value (int64 in the smallest currency unit — cents
/// for the default 2-fraction-digit locale) as its canonical decimal string,
/// e.g. -50 → "-0.50", 12345 → "123.45", 0 → "0.00". This equals str(Decimal)
/// for the Decimal the object path builds, so the object and query_json paths
/// stay byte-identical. The sign is derived from `cents < 0` and applied as an
/// explicit '-' prefix on the absolute value — deriving it from @divTrunc(cents,
/// 100) drops the sign for -1..-99 cents (|value| < $1), e.g. -50 → "0.50".
pub fn pgMoneyToStr(cents: i64, buf: []u8) ?[]const u8 {
    const neg = cents < 0;
    const abs_cents: u64 = @abs(cents);
    return std.fmt.bufPrint(buf, "{s}{d}.{d:0>2}", .{
        if (neg) "-" else "",
        abs_cents / 100,
        abs_cents % 100,
    }) catch null;
}

// ── MACADDR / MACADDR8 ──────────────────────────────────────────────────────

/// Render a PostgreSQL MACADDR (6 bytes) or MACADDR8 (8 bytes) as its canonical
/// lowercase colon-separated hex string, e.g. `08:00:2b:01:02:03`. This matches
/// PostgreSQL's own ::text output and psycopg's str form. Returns null on a
/// length that is neither 6 nor 8.
pub fn pgMacaddrToStr(data: []const u8, buf: []u8) ?[]const u8 {
    if (data.len != 6 and data.len != 8) return null;
    // Each byte → 2 hex + a separator; last has no trailing ':'.
    const needed = data.len * 3 - 1;
    if (buf.len < needed) return null;
    const hex = "0123456789abcdef";
    var pos: usize = 0;
    for (data, 0..) |b, i| {
        if (i != 0) {
            buf[pos] = ':';
            pos += 1;
        }
        buf[pos] = hex[b >> 4];
        buf[pos + 1] = hex[b & 0x0f];
        pos += 2;
    }
    return buf[0..pos];
}

// ── UUID ────────────────────────────────────────────────────────────────────

/// Render a PostgreSQL UUID (16 bytes) as its canonical lowercase hyphenated
/// string (`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` — exactly `str(UUID)`).
/// `buf` must be >= 36 bytes; returns the slice.
pub fn pgUuidToStr(data: []const u8, buf: []u8) ?[]const u8 {
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

// ── JSON whitespace compaction ──────────────────────────────────────────────

/// Copy a JSON document into `out`, dropping insignificant whitespace (that
/// which is NOT inside a string literal). PostgreSQL's canonical JSONB text
/// uses `": "` / `", "` spacing; removing it yields the compact form
/// fast_json_dumps (separators `(",", ":")`) emits after parsing JSONB into a
/// Python object — keeping the native and Python JSON paths byte-identical.
/// Escape-aware so whitespace inside string values is preserved. Returns the
/// number of bytes written (may be <= data.len; out should be >= data.len).
pub fn compactJson(data: []const u8, out: []u8) usize {
    var in_str = false;
    var escaped = false;
    var n: usize = 0;
    for (data) |ch| {
        if (in_str) {
            if (n < out.len) {
                out[n] = ch;
                n += 1;
            }
            if (escaped) {
                escaped = false;
            } else if (ch == '\\') {
                escaped = true;
            } else if (ch == '"') {
                in_str = false;
            }
        } else switch (ch) {
            ' ', '\t', '\n', '\r' => {}, // insignificant whitespace → drop
            else => {
                if (ch == '"') in_str = true;
                if (n < out.len) {
                    out[n] = ch;
                    n += 1;
                }
            },
        }
    }
    return n;
}

// ── Unit tests (fixtures) ───────────────────────────────────────────────────

test "civilFromDays: epoch, PG epoch, boundary" {
    const a = civilFromDays(0); // 1970-01-01
    try std.testing.expectEqual(@as(i64, 1970), a.y);
    try std.testing.expectEqual(@as(u32, 1), a.m);
    try std.testing.expectEqual(@as(u32, 1), a.d);
    const b = civilFromDays(10957); // 2000-01-01
    try std.testing.expectEqual(@as(i64, 2000), b.y);
    try std.testing.expectEqual(@as(u32, 1), b.m);
    try std.testing.expectEqual(@as(u32, 1), b.d);
    const c = civilFromDays(10957 - 1); // 1999-12-31
    try std.testing.expectEqual(@as(i64, 1999), c.y);
    try std.testing.expectEqual(@as(u32, 12), c.m);
    try std.testing.expectEqual(@as(u32, 31), c.d);
}

test "writeIsoTimestamp: naive ISO 8601, µs only when non-zero" {
    var buf: [40]u8 = undefined;
    try std.testing.expectEqualStrings("2000-01-01T00:00:00", writeIsoTimestamp(&buf, 0).?);
    try std.testing.expectEqualStrings("2000-01-01T00:00:01", writeIsoTimestamp(&buf, 1_000_000).?);
    try std.testing.expectEqualStrings("2000-01-02T00:00:00", writeIsoTimestamp(&buf, 86_400_000_000).?);
    try std.testing.expectEqualStrings("2000-01-01T00:00:00.123456", writeIsoTimestamp(&buf, 123_456).?);
    // 2024-01-15T10:30:45.123456 — µs since 2000-01-01 (verified via Python).
    try std.testing.expectEqualStrings("2024-01-15T10:30:45.123456", writeIsoTimestamp(&buf, 758_629_845_123_456).?);
}

test "writeIsoTimestamp: pre-2000 negative offsets with fractional seconds (divFloor)" {
    var buf: [40]u8 = undefined;
    // All values below are µs since 2000-01-01 computed from Python datetime, and
    // the expected strings are datetime.isoformat(). @divTrunc rendered every
    // fractional-negative case one second too late (e.g. the first as
    // "2000-01-01T00:00:00.500000"); @divFloor fixes it.
    try std.testing.expectEqualStrings("1999-12-31T23:59:59.500000", writeIsoTimestamp(&buf, -500000).?);
    try std.testing.expectEqualStrings("1969-07-20T20:17:40.123456", writeIsoTimestamp(&buf, -960867739876544).?);
    try std.testing.expectEqualStrings("1950-01-01T00:00:00.123456", writeIsoTimestamp(&buf, -1577836799876544).?);
    try std.testing.expectEqualStrings("1900-01-01T12:30:15.000007", writeIsoTimestamp(&buf, -3155628584999993).?);
    // Zero-fraction negative: no ".ffffff", and must stay exact (not shifted).
    try std.testing.expectEqualStrings("1999-12-31T23:59:59", writeIsoTimestamp(&buf, -1000000).?);
    // Positive path unchanged.
    try std.testing.expectEqualStrings("2024-01-15T10:30:45.123456", writeIsoTimestamp(&buf, 758629845123456).?);
}

test "writeIsoDate: days since 2000-01-01" {
    var buf: [16]u8 = undefined;
    try std.testing.expectEqualStrings("2000-01-01", writeIsoDate(&buf, 0).?);
    try std.testing.expectEqualStrings("2000-02-01", writeIsoDate(&buf, 31).?);
    try std.testing.expectEqualStrings("1999-12-31", writeIsoDate(&buf, -1).?);
    try std.testing.expectEqualStrings("2001-01-01", writeIsoDate(&buf, 366).?); // 2000 is leap
}

test "writeIsoTime: HH:MM:SS[.ffffff]" {
    var buf: [20]u8 = undefined;
    try std.testing.expectEqualStrings("00:00:00", writeIsoTime(&buf, 0).?);
    try std.testing.expectEqualStrings("01:01:01", writeIsoTime(&buf, 3_661_000_000).?);
    try std.testing.expectEqualStrings("10:30:45.123456", writeIsoTime(&buf, 37_845_123_456).?);
}

test "pgNumericToStr: decimal string with scale padding" {
    var buf: [128]u8 = undefined;
    // 123.456 → ndigits=2 weight=0 sign=0 dscale=3 digits=[123, 4560]
    const n1 = [_]u8{ 0, 2, 0, 0, 0, 0, 0, 3, 0, 123, 0x11, 0xD0 };
    try std.testing.expectEqualStrings("123.456", pgNumericToStr(&n1, &buf).?);
    // 0 → ndigits=0
    const zero = [_]u8{ 0, 0, 0, 0, 0, 0, 0, 0 };
    try std.testing.expectEqualStrings("0", pgNumericToStr(&zero, &buf).?);
    // -99.99 → ndigits=2 weight=0 sign=0x4000 dscale=2 digits=[99, 9900]
    const neg = [_]u8{ 0, 2, 0, 0, 0x40, 0, 0, 2, 0, 99, 0x26, 0xAC };
    try std.testing.expectEqualStrings("-99.99", pgNumericToStr(&neg, &buf).?);
    // 100 at scale 4 → ndigits=1 weight=0 sign=0 dscale=4 digits=[100] → "100.0000"
    const padded = [_]u8{ 0, 1, 0, 0, 0, 0, 0, 4, 0, 100 };
    try std.testing.expectEqualStrings("100.0000", pgNumericToStr(&padded, &buf).?);
}

test "pgNumericToStr: zero coefficient honors dscale (str(Decimal) fidelity)" {
    var buf: [128]u8 = undefined;
    // ndigits=0 with varying dscale. str(Decimal("0"))=="0",
    // str(Decimal("0.0000"))=="0.0000", str(Decimal("0.0000000"))=="0E-7".
    const z0 = [_]u8{ 0, 0, 0, 0, 0, 0, 0, 0 }; // dscale 0
    try std.testing.expectEqualStrings("0", pgNumericToStr(&z0, &buf).?);
    const z4 = [_]u8{ 0, 0, 0, 0, 0, 0, 0, 4 }; // dscale 4
    try std.testing.expectEqualStrings("0.0000", pgNumericToStr(&z4, &buf).?);
    const z6 = [_]u8{ 0, 0, 0, 0, 0, 0, 0, 6 }; // dscale 6 — last plain scale
    try std.testing.expectEqualStrings("0.000000", pgNumericToStr(&z6, &buf).?);
    const z7 = [_]u8{ 0, 0, 0, 0, 0, 0, 0, 7 }; // dscale 7 — scientific
    try std.testing.expectEqualStrings("0E-7", pgNumericToStr(&z7, &buf).?);
    const z10 = [_]u8{ 0, 0, 0, 0, 0, 0, 0, 10 }; // dscale 10 — scientific
    try std.testing.expectEqualStrings("0E-10", pgNumericToStr(&z10, &buf).?);
}

test "pgNumericToStr: 64-group (256-digit) NUMERIC round-trips without truncation" {
    var buf: [288]u8 = undefined;
    // 64 base-10000 integer groups, negative (weight=63, dscale=0). At the old
    // [256] buffer the last decimal digit was truncated. Bytes + expected string
    // generated from Python; str(Decimal(expected)) == expected.
    const big = [_]u8{ 0x00, 0x40, 0x00, 0x3f, 0x40, 0x00, 0x00, 0x00, 0x03, 0xe8, 0x04, 0x71, 0x04, 0xfa, 0x05, 0x83, 0x06, 0x0c, 0x06, 0x95, 0x07, 0x1e, 0x07, 0xa7, 0x08, 0x30, 0x08, 0xb9, 0x09, 0x42, 0x09, 0xcb, 0x0a, 0x54, 0x0a, 0xdd, 0x0b, 0x66, 0x0b, 0xef, 0x0c, 0x78, 0x0d, 0x01, 0x0d, 0x8a, 0x0e, 0x13, 0x0e, 0x9c, 0x0f, 0x25, 0x0f, 0xae, 0x10, 0x37, 0x10, 0xc0, 0x11, 0x49, 0x11, 0xd2, 0x12, 0x5b, 0x12, 0xe4, 0x13, 0x6d, 0x13, 0xf6, 0x14, 0x7f, 0x15, 0x08, 0x15, 0x91, 0x16, 0x1a, 0x16, 0xa3, 0x17, 0x2c, 0x17, 0xb5, 0x18, 0x3e, 0x18, 0xc7, 0x19, 0x50, 0x19, 0xd9, 0x1a, 0x62, 0x1a, 0xeb, 0x1b, 0x74, 0x1b, 0xfd, 0x1c, 0x86, 0x1d, 0x0f, 0x1d, 0x98, 0x1e, 0x21, 0x1e, 0xaa, 0x1f, 0x33, 0x1f, 0xbc, 0x20, 0x45, 0x20, 0xce, 0x21, 0x57, 0x21, 0xe0, 0x22, 0x69, 0x22, 0xf2, 0x23, 0x7b, 0x24, 0x04, 0x24, 0x8d, 0x25, 0x16, 0x25, 0x9f };
    const expected = "-1000113712741411154816851822195920962233237025072644278129183055319233293466360337403877401441514288442545624699483649735110524753845521565857955932606962066343648066176754689170287165730274397576771378507987812482618398853586728809894690839220935794949631";
    try std.testing.expectEqualStrings(expected, pgNumericToStr(&big, &buf).?);
}

test "pgNumericToStr: weight <= -2 leading zero groups (data-corruption regression)" {
    var buf: [128]u8 = undefined;
    // 0.00001234 = 1234 * 10000^-2 → ndigits=1 weight=-2 sign=0 dscale=8 digits=[1234].
    // Bug rendered this "0.12340000" (10^4× too large). Adjusted exp -5 ≥ -6 →
    // Decimal.__str__ keeps plain form "0.00001234".
    const n1 = [_]u8{ 0, 1, 0xFF, 0xFE, 0, 0, 0, 8, 0x04, 0xD2 };
    try std.testing.expectEqualStrings("0.00001234", pgNumericToStr(&n1, &buf).?);
    // 0.000000005 = 5000 * 10000^-3 → ndigits=1 weight=-3 sign=0 dscale=9 digits=[5000].
    // Bug rendered "0.500000000". Value is 5e-9; adjusted exp -9 < -6 → str(Decimal)
    // is scientific "5E-9".
    const n2 = [_]u8{ 0, 1, 0xFF, 0xFD, 0, 0, 0, 9, 0x13, 0x88 };
    try std.testing.expectEqualStrings("5E-9", pgNumericToStr(&n2, &buf).?);
    // 0.0001 = 1 * 10000^-1 → weight=-1, adjusted -4 → plain "0.0001".
    const n3 = [_]u8{ 0, 1, 0xFF, 0xFF, 0, 0, 0, 4, 0, 1 };
    try std.testing.expectEqualStrings("0.0001", pgNumericToStr(&n3, &buf).?);
    // 0.00000001 = 1e-8 = 1 * 10000^-2 → dscale=8, adjusted -8 < -6 → "1E-8".
    const n4 = [_]u8{ 0, 1, 0xFF, 0xFE, 0, 0, 0, 8, 0, 1 };
    try std.testing.expectEqualStrings("1E-8", pgNumericToStr(&n4, &buf).?);
    // 0.000012 = 1200 * 10000^-2 → weight=-2 dscale=6, adjusted -5 → plain "0.000012".
    const n5 = [_]u8{ 0, 1, 0xFF, 0xFE, 0, 0, 0, 6, 0x04, 0xB0 };
    try std.testing.expectEqualStrings("0.000012", pgNumericToStr(&n5, &buf).?);
    // 0.0000000050 = 5000 * 10000^-3 rendered at dscale=10 → the trailing zero is
    // significant, so str(Decimal) is "5.0E-9".
    const n6 = [_]u8{ 0, 1, 0xFF, 0xFD, 0, 0, 0, 10, 0x13, 0x88 };
    try std.testing.expectEqualStrings("5.0E-9", pgNumericToStr(&n6, &buf).?);
}

test "pgNumericToStr: int_digits > nd underflow guard (10000.00)" {
    var buf: [128]u8 = undefined;
    // 10000.00 = 1 * 10000^1 → ndigits=1 weight=1 sign=0 dscale=2 digits=[1].
    // int_digits (=2) > nd (=1): the old `for (int_digits..nd)` reversed range
    // safety-panicked. Must render "10000.00".
    const n1 = [_]u8{ 0, 1, 0, 1, 0, 0, 0, 2, 0, 1 };
    try std.testing.expectEqualStrings("10000.00", pgNumericToStr(&n1, &buf).?);
    // 32 integer digits + fractional: 12345678901234567890123456789012.50
    //   = 8 base-10000 integer groups + one fractional group, weight=7 dscale=2.
    const big = [_]u8{
        0, 9, // ndigits=9
        0, 7, // weight=7 → 8 integer groups
        0, 0, // sign=+
        0, 2, // dscale=2
        0x04, 0xD2, // 1234
        0x16, 0x2E, // 5678
        0x23, 0x34, // 9012
        0x0D, 0x80, // 3456
        0x1E, 0xD2, // 7890
        0x04, 0xD2, // 1234
        0x16, 0x2E, // 5678
        0x23, 0x34, // 9012
        0x13, 0x88, // 5000 (fractional group → ".50")
    };
    try std.testing.expectEqualStrings("12345678901234567890123456789012.50", pgNumericToStr(&big, &buf).?);
}

test "writeIsoTimestamp: rejects out-of-range (BC) years" {
    var buf: [40]u8 = undefined;
    // A large-negative µs offset lands before year 1 (proleptic Gregorian BC).
    // PG can store it; Python datetime cannot → must return null (caller falls back).
    try std.testing.expectEqual(@as(?[]const u8, null), writeIsoTimestamp(&buf, -100_000_000_000_000_000));
    // Year 0000 (a BC year in proleptic terms) also rejected.
    // 2000-01-01 minus 2000 years worth of days · 86400 · 1e6 lands < year 1.
    try std.testing.expectEqual(@as(?[]const u8, null), writeIsoTimestamp(&buf, -63_100_000_000_000_000));
}

test "pgNumericToStr: NaN / Infinity / -Infinity special values" {
    var buf: [16]u8 = undefined;
    // Special values: ndigits=0, weight=0, dscale=0, only the sign word varies.
    const nan = [_]u8{ 0, 0, 0, 0, 0xC0, 0x00, 0, 0 };
    try std.testing.expectEqualStrings("NaN", pgNumericToStr(&nan, &buf).?);
    const pinf = [_]u8{ 0, 0, 0, 0, 0xD0, 0x00, 0, 0 };
    try std.testing.expectEqualStrings("Infinity", pgNumericToStr(&pinf, &buf).?);
    const ninf = [_]u8{ 0, 0, 0, 0, 0xF0, 0x00, 0, 0 };
    try std.testing.expectEqualStrings("-Infinity", pgNumericToStr(&ninf, &buf).?);
}

test "pgMoneyToStr: sign preserved for |value| < $1" {
    var buf: [32]u8 = undefined;
    try std.testing.expectEqualStrings("-0.50", pgMoneyToStr(-50, &buf).?);
    try std.testing.expectEqualStrings("-0.01", pgMoneyToStr(-1, &buf).?);
    try std.testing.expectEqualStrings("-0.99", pgMoneyToStr(-99, &buf).?);
    try std.testing.expectEqualStrings("0.50", pgMoneyToStr(50, &buf).?);
    try std.testing.expectEqualStrings("0.00", pgMoneyToStr(0, &buf).?);
    try std.testing.expectEqualStrings("123.45", pgMoneyToStr(12345, &buf).?);
    try std.testing.expectEqualStrings("-123.45", pgMoneyToStr(-12345, &buf).?);
    try std.testing.expectEqualStrings("-1.00", pgMoneyToStr(-100, &buf).?);
}

test "pgMacaddrToStr: 6-byte and 8-byte colon-hex" {
    var buf: [24]u8 = undefined;
    const mac6 = [_]u8{ 0x08, 0x00, 0x2b, 0x01, 0x02, 0x03 };
    try std.testing.expectEqualStrings("08:00:2b:01:02:03", pgMacaddrToStr(&mac6, &buf).?);
    const mac8 = [_]u8{ 0x08, 0x00, 0x2b, 0x01, 0x02, 0x03, 0x04, 0x05 };
    try std.testing.expectEqualStrings("08:00:2b:01:02:03:04:05", pgMacaddrToStr(&mac8, &buf).?);
    const bad = [_]u8{ 0x08, 0x00, 0x2b };
    try std.testing.expectEqual(@as(?[]const u8, null), pgMacaddrToStr(&bad, &buf));
}

test "pgUuidToStr: canonical lowercase hyphenated" {
    var buf: [36]u8 = undefined;
    const bytes = [_]u8{ 0x55, 0x0e, 0x84, 0x00, 0xe2, 0x9b, 0x41, 0xd4, 0xa7, 0x16, 0x44, 0x66, 0x55, 0x44, 0x00, 0x00 };
    try std.testing.expectEqualStrings("550e8400-e29b-41d4-a716-446655440000", pgUuidToStr(&bytes, &buf).?);
    const zero = [_]u8{0} ** 16;
    try std.testing.expectEqualStrings("00000000-0000-0000-0000-000000000000", pgUuidToStr(&zero, &buf).?);
}

test "compactJson: strips insignificant whitespace, preserves in-string" {
    var out: [128]u8 = undefined;
    const a = "{\"a\": 1, \"b\": [2, 3]}";
    try std.testing.expectEqualStrings("{\"a\":1,\"b\":[2,3]}", out[0..compactJson(a, &out)]);
    // whitespace inside a string value is preserved
    const b = "{\"name\": \"John  Doe\"}";
    try std.testing.expectEqualStrings("{\"name\":\"John  Doe\"}", out[0..compactJson(b, &out)]);
    // escaped quote inside string doesn't end the string early
    const c = "{\"q\": \"a\\\" b\"}";
    try std.testing.expectEqualStrings("{\"q\":\"a\\\" b\"}", out[0..compactJson(c, &out)]);
}
