//! Test aggregator for the vendored pg.zig driver module.
//!
//! `zig build test` roots at src/main.zig, whose module imports `pg` as a
//! separate compilation unit — so the pg module's own ~110 unit tests are never
//! collected. This root pulls every test-bearing pg file into one test binary so
//! `zig build test-pg` runs them (against a live PostgreSQL; see t.zig for the
//! env-driven connection + fixture schema).
test {
    _ = @import("lib.zig");
    _ = @import("conn.zig");
    _ = @import("result.zig");
    _ = @import("reader.zig");
    _ = @import("pool.zig");
    _ = @import("types.zig");
    _ = @import("auth.zig");
    _ = @import("listener.zig");
    _ = @import("pg.zig");
}
