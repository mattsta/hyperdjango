// HyperDjango Native Extension
// Zig-native HTTP server, SIMD validation, PostgreSQL driver — all in one .so
//
// Vendored from: turboAPI (server, router, db), dhi (validation), pg.zig (Postgres)
// Adapted for hyperdjango with module name _hyperdjango_native

const std = @import("std");
pub const py = @import("py.zig");
const c = py.c;
const response = @import("response.zig");
const server = @import("server.zig");
pub const router = @import("router.zig");
const db = @import("db.zig");
const json_parser = @import("json_parser.zig");
const router_bridge = @import("router_bridge.zig");
const hashring = @import("hashring.zig");
const log_helpers = @import("log_helpers.zig");
const string_ops = @import("string_ops.zig");
const ws = @import("websocket_server.zig");
const multipart = @import("multipart.zig");
const validator = @import("validator");
const json_validator = @import("json_validator");
const model_validator = @import("model_validator.zig");
const batch_validator = @import("batch_validator.zig");
const file_watcher = @import("file_watcher.zig");
const profiler = @import("profiler.zig");
const template_engine = @import("template_engine.zig");
const static_helpers = @import("static_helpers.zig");
const guard_eval = @import("guard_eval.zig");
const where_compiler = @import("where_compiler.zig");
const metrics_py = @import("metrics_py.zig");
const h3 = @import("h3.zig");
const test_locks = @import("test_locks.zig");

// ── Method table ────────────────────────────────────────────────────────────

fn hello(_: ?*c.PyObject, _: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    return py.newString("hyperdjango-native is alive! (Zig HTTP + pg.zig + SIMD validation)");
}

var methods = [_]py.PyMethodDef{
    // Smoke test
    .{ .ml_name = "hello", .ml_meth = @ptrCast(&hello), .ml_flags = c.METH_NOARGS, .ml_doc = "Smoke test" },

    // ResponseView functions
    .{ .ml_name = "_rv_new", .ml_meth = @ptrCast(&response.response_new), .ml_flags = c.METH_VARARGS, .ml_doc = null },
    .{ .ml_name = "_rv_set_header", .ml_meth = @ptrCast(&response.response_set_header), .ml_flags = c.METH_VARARGS, .ml_doc = null },
    .{ .ml_name = "_rv_get_header", .ml_meth = @ptrCast(&response.response_get_header), .ml_flags = c.METH_VARARGS, .ml_doc = null },
    .{ .ml_name = "_rv_set_body", .ml_meth = @ptrCast(&response.response_set_body), .ml_flags = c.METH_VARARGS, .ml_doc = null },
    .{ .ml_name = "_rv_set_body_bytes", .ml_meth = @ptrCast(&response.response_set_body_bytes), .ml_flags = c.METH_VARARGS, .ml_doc = null },
    .{ .ml_name = "_rv_json", .ml_meth = @ptrCast(&response.response_json), .ml_flags = c.METH_VARARGS, .ml_doc = null },
    .{ .ml_name = "_rv_text", .ml_meth = @ptrCast(&response.response_text), .ml_flags = c.METH_VARARGS, .ml_doc = null },

    // Server functions
    .{ .ml_name = "_server_new", .ml_meth = @ptrCast(&server.server_new), .ml_flags = c.METH_VARARGS, .ml_doc = null },
    .{ .ml_name = "_server_add_route", .ml_meth = @ptrCast(&server.server_add_route), .ml_flags = c.METH_VARARGS, .ml_doc = null },
    .{ .ml_name = "_server_add_route_typed", .ml_meth = @ptrCast(&server.server_add_route_typed), .ml_flags = c.METH_VARARGS, .ml_doc = "Enhanced route with Zig-native param type coercion" },
    .{ .ml_name = "_server_add_route_fast", .ml_meth = @ptrCast(&server.server_add_route_fast), .ml_flags = c.METH_VARARGS, .ml_doc = null },
    .{ .ml_name = "_server_add_route_model", .ml_meth = @ptrCast(&server.server_add_route_model), .ml_flags = c.METH_VARARGS, .ml_doc = null },
    .{ .ml_name = "_server_add_route_async_fast", .ml_meth = @ptrCast(&server.server_add_route_async_fast), .ml_flags = c.METH_VARARGS, .ml_doc = null },
    .{ .ml_name = "_server_add_route_model_validated", .ml_meth = @ptrCast(&server.server_add_route_model_validated), .ml_flags = c.METH_VARARGS, .ml_doc = null },
    .{ .ml_name = "_server_add_native_route", .ml_meth = @ptrCast(&server.server_add_native_route), .ml_flags = c.METH_VARARGS, .ml_doc = null },
    .{ .ml_name = "_server_add_static_route", .ml_meth = @ptrCast(&server.server_add_static_route), .ml_flags = c.METH_VARARGS, .ml_doc = null },
    .{ .ml_name = "_server_add_file_route", .ml_meth = @ptrCast(&server.server_add_file_route), .ml_flags = c.METH_VARARGS, .ml_doc = "Serve file from disk as static route" },
    .{ .ml_name = "_server_add_middleware", .ml_meth = @ptrCast(&server.server_add_middleware), .ml_flags = c.METH_VARARGS, .ml_doc = null },
    .{ .ml_name = "_server_run", .ml_meth = @ptrCast(&server.server_run), .ml_flags = c.METH_NOARGS, .ml_doc = null },
    .{ .ml_name = "_server_shutdown", .ml_meth = @ptrCast(&server.server_shutdown), .ml_flags = c.METH_NOARGS, .ml_doc = null },
    .{ .ml_name = "_read_body_chunk", .ml_meth = @ptrCast(&server.read_body_chunk), .ml_flags = c.METH_VARARGS, .ml_doc = null },
    .{ .ml_name = "_server_configure_cors", .ml_meth = @ptrCast(&server.server_configure_cors), .ml_flags = c.METH_VARARGS, .ml_doc = null },
    .{ .ml_name = "_server_configure_security_headers", .ml_meth = @ptrCast(&server.server_configure_security_headers), .ml_flags = c.METH_VARARGS, .ml_doc = null },
    .{ .ml_name = "_server_enable_response_cache", .ml_meth = @ptrCast(&server.server_enable_response_cache), .ml_flags = c.METH_NOARGS, .ml_doc = null },
    .{ .ml_name = "_server_set_django_handler", .ml_meth = @ptrCast(&server.server_set_django_handler), .ml_flags = c.METH_VARARGS, .ml_doc = "Register Django WSGI handler as catch-all" },

    // DB functions (pg.zig native PostgreSQL)
    .{ .ml_name = "_db_configure", .ml_meth = @ptrCast(&db.db_configure), .ml_flags = c.METH_VARARGS, .ml_doc = null },
    .{ .ml_name = "_db_add_route", .ml_meth = @ptrCast(&db.db_add_route), .ml_flags = c.METH_VARARGS, .ml_doc = null },
    .{ .ml_name = "_db_query", .ml_meth = @ptrCast(&db.db_query), .ml_flags = c.METH_VARARGS, .ml_doc = "Execute SQL query, return list of tuples" },
    .{ .ml_name = "_db_query_dicts", .ml_meth = @ptrCast(&db.db_query_dicts), .ml_flags = c.METH_VARARGS, .ml_doc = "Execute SQL query, return list of dicts (native dict building)" },
    .{ .ml_name = "_db_register_query", .ml_meth = @ptrCast(&db.db_register_query), .ml_flags = c.METH_VARARGS, .ml_doc = "Register SQL for a lock-free query handle (returns int)" },
    .{ .ml_name = "_db_query_json", .ml_meth = @ptrCast(&db.db_query_json), .ml_flags = c.METH_VARARGS, .ml_doc = "Execute SQL query, return JSON bytes (zero Python overhead)" },
    .{ .ml_name = "_db_execute", .ml_meth = @ptrCast(&db.db_execute), .ml_flags = c.METH_VARARGS, .ml_doc = "Execute SQL statement, return rowcount" },
    .{ .ml_name = "_db_exec_many", .ml_meth = @ptrCast(&db.db_exec_many), .ml_flags = c.METH_VARARGS, .ml_doc = "Batch execute: one SQL, many param sets" },
    .{ .ml_name = "_db_get_last_columns", .ml_meth = @ptrCast(&db.db_get_last_columns), .ml_flags = c.METH_NOARGS, .ml_doc = "Get column names from last _db_query" },

    // Transaction support — pinned connections for atomic() blocks
    .{ .ml_name = "_db_conn_acquire", .ml_meth = @ptrCast(&db.db_conn_acquire), .ml_flags = c.METH_VARARGS, .ml_doc = "Acquire pinned connection from pool, return handle" },
    .{ .ml_name = "_db_conn_release", .ml_meth = @ptrCast(&db.db_conn_release), .ml_flags = c.METH_VARARGS, .ml_doc = "Release pinned connection" },
    .{ .ml_name = "_db_conn_execute", .ml_meth = @ptrCast(&db.db_conn_execute), .ml_flags = c.METH_VARARGS, .ml_doc = "Execute on pinned connection" },
    .{ .ml_name = "_db_set_active_pinned", .ml_meth = @ptrCast(&db.db_set_active_pinned), .ml_flags = c.METH_VARARGS, .ml_doc = "Route all queries through pinned conn" },
    .{ .ml_name = "_db_clear_active_pinned", .ml_meth = @ptrCast(&db.db_clear_active_pinned), .ml_flags = c.METH_NOARGS, .ml_doc = "Route queries back to pool" },
    .{ .ml_name = "_db_set_active_handle", .ml_meth = @ptrCast(&db.db_set_active_handle), .ml_flags = c.METH_VARARGS, .ml_doc = "Set active pool handle for Zig server" },
    .{ .ml_name = "_db_close_pool", .ml_meth = @ptrCast(&db.db_close_pool), .ml_flags = c.METH_VARARGS, .ml_doc = "Close a specific pool by handle" },
    .{ .ml_name = "_db_release_thread_conn", .ml_meth = @ptrCast(&db.db_release_thread_conn), .ml_flags = c.METH_VARARGS, .ml_doc = "Release thread-owned connection back to pool" },
    .{ .ml_name = "_db_mark_offload_worker", .ml_meth = @ptrCast(&db.db_mark_offload_worker), .ml_flags = c.METH_NOARGS, .ml_doc = "Mark this thread as a DB-offload worker (acquire/release per op, never pin)" },
    .{ .ml_name = "_db_clear_stmt_cache", .ml_meth = @ptrCast(&db.db_clear_stmt_cache), .ml_flags = c.METH_NOARGS, .ml_doc = "Clear prepared statement and column caches after DDL" },
    .{ .ml_name = "_db_stmt_cache_stats", .ml_meth = @ptrCast(&db.db_stmt_cache_stats), .ml_flags = c.METH_NOARGS, .ml_doc = "Get prepared statement cache stats: {hits, misses, evictions, entries, max_entries}" },
    .{ .ml_name = "_db_reset_stmt_cache_stats", .ml_meth = @ptrCast(&db.db_reset_stmt_cache_stats), .ml_flags = c.METH_NOARGS, .ml_doc = "Reset prepared statement cache stats counters" },
    .{ .ml_name = "_db_set_active_pool", .ml_meth = @ptrCast(&db.db_set_active_pool), .ml_flags = c.METH_VARARGS, .ml_doc = "Set active pool handle" },
    .{ .ml_name = "_db_copy_to", .ml_meth = @ptrCast(&db.db_copy_to), .ml_flags = c.METH_VARARGS, .ml_doc = "COPY TO STDOUT — returns list of row strings" },
    .{ .ml_name = "_db_copy_from", .ml_meth = @ptrCast(&db.db_copy_from), .ml_flags = c.METH_VARARGS, .ml_doc = "COPY FROM STDIN — accepts list of row strings, returns count" },
    .{ .ml_name = "_db_listen", .ml_meth = @ptrCast(&db.db_listen), .ml_flags = c.METH_VARARGS, .ml_doc = "LISTEN on channel — spawns background thread, calls callback(channel, payload)" },
    .{ .ml_name = "_db_register_hstore", .ml_meth = @ptrCast(&db.db_register_hstore), .ml_flags = c.METH_VARARGS, .ml_doc = "Query pg_type for hstore OID and register for native dict conversion" },
    .{ .ml_name = "_db_register_vector", .ml_meth = @ptrCast(&db.db_register_vector), .ml_flags = c.METH_VARARGS, .ml_doc = "Query pg_type for pgvector OID and register for native SIMD vector decoding" },
    .{ .ml_name = "_db_register_enum", .ml_meth = @ptrCast(&db.db_register_enum), .ml_flags = c.METH_VARARGS, .ml_doc = "Register custom enum type: (pool_handle, type_name) -> OID" },
    .{ .ml_name = "_db_list_enums", .ml_meth = @ptrCast(&db.db_list_enums), .ml_flags = c.METH_VARARGS, .ml_doc = "Discover all enum types: (pool_handle) -> {name: [labels]}" },
    .{ .ml_name = "_db_pipeline", .ml_meth = @ptrCast(&db.db_pipeline), .ml_flags = c.METH_VARARGS, .ml_doc = "Execute N queries in single pipeline: (pool_handle, [sql1, sql2, ...]) -> [results1, results2, ...]" },
    .{ .ml_name = "_db_pool_stats", .ml_meth = @ptrCast(&db.db_pool_stats), .ml_flags = c.METH_VARARGS, .ml_doc = "Get pool stats: (pool_handle) -> {total, available, in_use, ...}" },
    .{ .ml_name = "_db_warmup_statements", .ml_meth = @ptrCast(&db.db_warmup_statements), .ml_flags = c.METH_VARARGS, .ml_doc = "Pre-parse SQL statements to prime cache: (pool_handle, [sql]) -> count" },

    // H3 geospatial primitives (the mattsta/h3 fork, v4, compiled in).
    // Minimal recall surface; every wrapper raises ValueError / returns None on
    // H3Error and guards the uint64->int64 BIGINT boundary (never a fabricated
    // cell). See zig/src/h3.zig and server/src/server/geo.py.
    .{ .ml_name = "_h3_lat_lng_to_cell", .ml_meth = @ptrCast(&h3.h3_lat_lng_to_cell), .ml_flags = c.METH_VARARGS, .ml_doc = "H3 cell for a lat/lng (degrees) at res: (lat_deg, lng_deg, res) -> int" },
    .{ .ml_name = "_h3_grid_disk", .ml_meth = @ptrCast(&h3.h3_grid_disk), .ml_flags = c.METH_VARARGS, .ml_doc = "All cells within grid distance k: (origin_cell, k) -> list[int]" },
    .{ .ml_name = "_h3_grid_disk_distances", .ml_meth = @ptrCast(&h3.h3_grid_disk_distances), .ml_flags = c.METH_VARARGS, .ml_doc = "Cells within k paired with ring distance: (origin_cell, k) -> list[(cell, dist)]" },
    .{ .ml_name = "_h3_grid_distance", .ml_meth = @ptrCast(&h3.h3_grid_distance), .ml_flags = c.METH_VARARGS, .ml_doc = "Grid distance between two cells: (a, b) -> int | None" },
    .{ .ml_name = "_h3_cell_to_parent", .ml_meth = @ptrCast(&h3.h3_cell_to_parent), .ml_flags = c.METH_VARARGS, .ml_doc = "Coarser parent cell (shard key): (cell, parent_res) -> int" },
    .{ .ml_name = "_h3_get_resolution", .ml_meth = @ptrCast(&h3.h3_get_resolution), .ml_flags = c.METH_VARARGS, .ml_doc = "Resolution (0..15) of a cell: (cell) -> int" },

    // Validation functions (SIMD-accelerated via dhi)
    .{ .ml_name = "validate_email", .ml_meth = @ptrCast(&py_validate_email), .ml_flags = c.METH_VARARGS, .ml_doc = "Validate email using SIMD" },
    .{ .ml_name = "validate_int_range", .ml_meth = @ptrCast(&py_validate_int_range), .ml_flags = c.METH_VARARGS, .ml_doc = "Validate int in range using SIMD" },
    .{ .ml_name = "validate_string_length", .ml_meth = @ptrCast(&py_validate_string_length), .ml_flags = c.METH_VARARGS, .ml_doc = "Validate string length" },

    // JSON functions (native serialization + parsing)
    .{ .ml_name = "json_dumps_native", .ml_meth = @ptrCast(&py_json_dumps), .ml_flags = c.METH_VARARGS, .ml_doc = "Serialize Python object to JSON bytes" },
    .{ .ml_name = "json_loads_native", .ml_meth = @ptrCast(&json_parser.json_loads_native), .ml_flags = c.METH_VARARGS, .ml_doc = "Parse JSON string into Python objects" },

    // String operations (SIMD-accelerated)
    .{ .ml_name = "html_escape_native", .ml_meth = @ptrCast(&string_ops.html_escape), .ml_flags = c.METH_VARARGS, .ml_doc = "SIMD HTML escape" },
    .{ .ml_name = "url_encode_native", .ml_meth = @ptrCast(&string_ops.url_encode), .ml_flags = c.METH_VARARGS, .ml_doc = "SIMD URL encode" },
    .{ .ml_name = "url_decode_native", .ml_meth = @ptrCast(&string_ops.url_decode), .ml_flags = c.METH_VARARGS, .ml_doc = "SIMD URL decode" },
    .{ .ml_name = "parse_query_string_native", .ml_meth = @ptrCast(&string_ops.parse_query_string), .ml_flags = c.METH_VARARGS, .ml_doc = "SIMD query string parse" },
    .{ .ml_name = "parse_cookies_native", .ml_meth = @ptrCast(&string_ops.parse_cookies), .ml_flags = c.METH_VARARGS, .ml_doc = "Native cookie header parser" },
    .{ .ml_name = "base_encode_native", .ml_meth = @ptrCast(&string_ops.base_encode), .ml_flags = c.METH_VARARGS, .ml_doc = "Arbitrary-base encode int to string" },
    .{ .ml_name = "base_decode_native", .ml_meth = @ptrCast(&string_ops.base_decode), .ml_flags = c.METH_VARARGS, .ml_doc = "Arbitrary-base decode string to int" },
    .{ .ml_name = "xor_bytes_native", .ml_meth = @ptrCast(&string_ops.xor_bytes), .ml_flags = c.METH_VARARGS, .ml_doc = "SIMD XOR bytes with repeating mask" },

    // WebSocket
    .{ .ml_name = "_server_add_ws_route", .ml_meth = @ptrCast(&ws.server_add_ws_route), .ml_flags = c.METH_VARARGS, .ml_doc = "Register WebSocket handler for path" },
    .{ .ml_name = "_server_set_ws_config", .ml_meth = @ptrCast(&ws.server_set_ws_config), .ml_flags = c.METH_VARARGS, .ml_doc = "Configure WS: (max_msg_size, ping_interval_s, pong_timeout_s)" },
    .{ .ml_name = "_server_get_ws_config", .ml_meth = @ptrCast(&ws.server_get_ws_config), .ml_flags = c.METH_NOARGS, .ml_doc = "Get WS config: -> (max_msg_size, ping_interval_s, pong_timeout_s)" },
    .{ .ml_name = "_ws_send", .ml_meth = @ptrCast(&ws.ws_send), .ml_flags = c.METH_VARARGS, .ml_doc = "Send text frame: (conn_id, text)" },
    .{ .ml_name = "_ws_send_bytes", .ml_meth = @ptrCast(&ws.ws_send_bytes), .ml_flags = c.METH_VARARGS, .ml_doc = "Send binary frame: (conn_id, bytes)" },
    .{ .ml_name = "_ws_send_text_bytes", .ml_meth = @ptrCast(&ws.ws_send_text_bytes), .ml_flags = c.METH_VARARGS, .ml_doc = "Send text frame from UTF-8 bytes: (conn_id, bytes)" },
    .{ .ml_name = "_ws_try_send", .ml_meth = @ptrCast(&ws.ws_try_send), .ml_flags = c.METH_VARARGS, .ml_doc = "Non-blocking send: (conn_id, opcode, bytes) -> int (0 sent,1 would_block,2 shed,3 closed)" },
    .{ .ml_name = "_ws_flush_send", .ml_meth = @ptrCast(&ws.ws_flush_send), .ml_flags = c.METH_VARARGS, .ml_doc = "Flush buffered outbound (add_writer callback): (conn_id) -> int" },
    .{ .ml_name = "_ws_send_ping", .ml_meth = @ptrCast(&ws.ws_send_ping), .ml_flags = c.METH_VARARGS, .ml_doc = "Send keepalive ping via non-blocking path: (conn_id, bytes) -> int" },
    .{ .ml_name = "_ws_pong_age", .ml_meth = @ptrCast(&ws.ws_pong_age), .ml_flags = c.METH_VARARGS, .ml_doc = "Seconds since last inbound frame: (conn_id) -> float|None" },
    .{ .ml_name = "_ws_recv", .ml_meth = @ptrCast(&ws.ws_recv), .ml_flags = c.METH_VARARGS, .ml_doc = "Recv frame: (conn_id) -> str|bytes|None" },
    .{ .ml_name = "_ws_try_recv", .ml_meth = @ptrCast(&ws.ws_try_recv), .ml_flags = c.METH_VARARGS, .ml_doc = "Non-blocking recv attempt: (conn_id) -> str|bytes|False|None" },
    .{ .ml_name = "_ws_get_fd", .ml_meth = @ptrCast(&ws.ws_get_fd), .ml_flags = c.METH_VARARGS, .ml_doc = "Raw socket fd for asyncio add_reader: (conn_id) -> int|None" },
    .{ .ml_name = "_ws_close", .ml_meth = @ptrCast(&ws.ws_close), .ml_flags = c.METH_VARARGS, .ml_doc = "Close WS: (conn_id, code, reason)" },
    .{ .ml_name = "_ws_release", .ml_meth = @ptrCast(&ws.ws_release), .ml_flags = c.METH_VARARGS, .ml_doc = "Close socket + free connection, once: (conn_id)" },

    // Multipart parser
    .{ .ml_name = "parse_multipart_native", .ml_meth = @ptrCast(&multipart.parse_multipart), .ml_flags = c.METH_VARARGS, .ml_doc = "Parse multipart/form-data body" },

    // Logging helpers (hot path acceleration)
    .{ .ml_name = "_log_timestamp_iso", .ml_meth = @ptrCast(&log_helpers.log_timestamp_iso), .ml_flags = c.METH_NOARGS, .ml_doc = "UTC ISO 8601 timestamp as bytes (no datetime overhead)" },
    .{ .ml_name = "_log_basename", .ml_meth = @ptrCast(&log_helpers.log_basename), .ml_flags = c.METH_VARARGS, .ml_doc = "Extract basename from path (native)" },
    .{ .ml_name = "_log_module_name", .ml_meth = @ptrCast(&log_helpers.log_module_name), .ml_flags = c.METH_VARARGS, .ml_doc = "Extract module name from basename (strip .py)" },

    // Hash ring (native consistent hashing)
    .{ .ml_name = "_hashring_new", .ml_meth = @ptrCast(&hashring.hashring_new), .ml_flags = c.METH_VARARGS, .ml_doc = "Create hash ring: (replicas, vnodes) -> handle" },
    .{ .ml_name = "_hashring_free", .ml_meth = @ptrCast(&hashring.hashring_free), .ml_flags = c.METH_VARARGS, .ml_doc = "Free hash ring handle" },
    .{ .ml_name = "_hashring_add_node", .ml_meth = @ptrCast(&hashring.hashring_add_node), .ml_flags = c.METH_VARARGS, .ml_doc = "Add node: (handle, name, weight, vnodes, instance)" },
    .{ .ml_name = "_hashring_remove_node", .ml_meth = @ptrCast(&hashring.hashring_remove_node), .ml_flags = c.METH_VARARGS, .ml_doc = "Remove node: (handle, name) -> bool" },
    .{ .ml_name = "_hashring_build", .ml_meth = @ptrCast(&hashring.hashring_build), .ml_flags = c.METH_VARARGS, .ml_doc = "Build ring: (handle) -> point_count" },
    .{ .ml_name = "_hashring_get_node", .ml_meth = @ptrCast(&hashring.hashring_get_node), .ml_flags = c.METH_VARARGS, .ml_doc = "Get node name: (handle, key) -> str" },
    .{ .ml_name = "_hashring_get_node_instance", .ml_meth = @ptrCast(&hashring.hashring_get_node_instance), .ml_flags = c.METH_VARARGS, .ml_doc = "Get node instance: (handle, key) -> object" },
    .{ .ml_name = "_hashring_get_stats", .ml_meth = @ptrCast(&hashring.hashring_get_stats), .ml_flags = c.METH_VARARGS, .ml_doc = "Get ring stats: (handle) -> dict" },
    .{ .ml_name = "_hashring_hash_key", .ml_meth = @ptrCast(&hashring.hashring_hash_key), .ml_flags = c.METH_VARARGS, .ml_doc = "Hash key: (key) -> uint32" },

    // Router functions (native radix trie)
    .{ .ml_name = "_router_new", .ml_meth = @ptrCast(&router_bridge.router_new), .ml_flags = c.METH_NOARGS, .ml_doc = "Create a new radix trie router, return handle" },
    .{ .ml_name = "_router_add", .ml_meth = @ptrCast(&router_bridge.router_add), .ml_flags = c.METH_VARARGS, .ml_doc = "Add route: (handle, method, pattern, key)" },
    .{ .ml_name = "_router_resolve", .ml_meth = @ptrCast(&router_bridge.router_resolve), .ml_flags = c.METH_VARARGS, .ml_doc = "Resolve: (handle, method, path) → (key, params) or None" },
    .{ .ml_name = "_router_finalize", .ml_meth = @ptrCast(&router_bridge.router_finalize), .ml_flags = c.METH_VARARGS, .ml_doc = "Optimize router: compress paths, sort children" },
    .{ .ml_name = "_router_free", .ml_meth = @ptrCast(&router_bridge.router_free), .ml_flags = c.METH_VARARGS, .ml_doc = "Free router handle" },

    // Model validation (compile specs at class def, validate entire model in one call)
    .{ .ml_name = "compile_model_specs", .ml_meth = @ptrCast(&model_validator.py_compile_model_specs), .ml_flags = c.METH_VARARGS, .ml_doc = "Pre-compile field specs into native structs: (specs_tuple) -> PyCapsule" },
    .{ .ml_name = "init_model_full", .ml_meth = @ptrCast(&model_validator.py_init_model_full), .ml_flags = c.METH_FASTCALL, .ml_doc = "Full native init: (self, kwargs, capsule, extra_mode) -> None or errors" },
    .{ .ml_name = "validate_field", .ml_meth = @ptrCast(&model_validator.py_validate_field), .ml_flags = c.METH_VARARGS, .ml_doc = "Validate single field: (value, name, constraints) -> validated value" },
    .{ .ml_name = "dump_model_compiled", .ml_meth = @ptrCast(&model_validator.py_dump_model_compiled), .ml_flags = c.METH_VARARGS, .ml_doc = "Ultra-fast model_dump: (self, capsule) -> dict" },
    .{ .ml_name = "dump_model_to_json", .ml_meth = @ptrCast(&py_dump_model_to_json), .ml_flags = c.METH_VARARGS, .ml_doc = "Model → JSON bytes in single call: (self, capsule) -> bytes" },
    .{ .ml_name = "json_loads_model", .ml_meth = @ptrCast(&model_validator.py_json_loads_model), .ml_flags = c.METH_FASTCALL, .ml_doc = "JSON → Model in single pass: (json_bytes, self, capsule, extra_mode) -> None or errors" },

    // Batch validation (SIMD-parallel)
    .{ .ml_name = "validate_int_batch_simd", .ml_meth = @ptrCast(&batch_validator.py_validate_int_batch_simd), .ml_flags = c.METH_VARARGS, .ml_doc = "SIMD batch int range: (list, min, max) -> (results, count)" },
    .{ .ml_name = "validate_string_length_batch", .ml_meth = @ptrCast(&batch_validator.py_validate_string_batch), .ml_flags = c.METH_VARARGS, .ml_doc = "Batch string length: (list, min, max) -> (results, count)" },
    .{ .ml_name = "validate_email_batch", .ml_meth = @ptrCast(&batch_validator.py_validate_email_batch), .ml_flags = c.METH_VARARGS, .ml_doc = "Batch email: (list) -> (results, count)" },
    .{ .ml_name = "validate_batch_direct", .ml_meth = @ptrCast(&batch_validator.py_validate_batch_direct), .ml_flags = c.METH_VARARGS, .ml_doc = "Batch dict validation: (list, specs) -> (results, count)" },
    .{ .ml_name = "validate_model_batch", .ml_meth = @ptrCast(&batch_validator.py_validate_model_batch), .ml_flags = c.METH_VARARGS, .ml_doc = "Batch model validation: (list, capsule) -> list of errors" },

    // File watcher (kqueue/inotify — no polling)
    .{ .ml_name = "_file_watcher_start", .ml_meth = @ptrCast(&file_watcher.py_file_watcher_start), .ml_flags = c.METH_VARARGS, .ml_doc = "Start native file watcher: (dirs, exts, callback) -> handle" },
    .{ .ml_name = "_file_watcher_stop", .ml_meth = @ptrCast(&file_watcher.py_file_watcher_stop), .ml_flags = c.METH_VARARGS, .ml_doc = "Stop file watcher by handle" },

    // Template engine (native Zig compilation + rendering)
    .{ .ml_name = "_template_compile", .ml_meth = @ptrCast(&template_engine.py_template_compile), .ml_flags = c.METH_VARARGS, .ml_doc = "Compile template: (source, path) -> capsule" },
    .{ .ml_name = "_template_render", .ml_meth = @ptrCast(&template_engine.py_template_render), .ml_flags = c.METH_VARARGS, .ml_doc = "Render template: (capsule, context_dict) -> bytes" },
    .{ .ml_name = "_template_register_filter", .ml_meth = @ptrCast(&template_engine.py_template_register_filter), .ml_flags = c.METH_VARARGS, .ml_doc = "Register filter: (capsule, name, callable)" },
    .{ .ml_name = "_template_set_loader", .ml_meth = @ptrCast(&template_engine.py_template_set_loader), .ml_flags = c.METH_VARARGS, .ml_doc = "Set template loader: (callable)" },
    .{ .ml_name = "_template_set_undefined_mode", .ml_meth = @ptrCast(&template_engine.py_template_set_undefined_mode), .ml_flags = c.METH_VARARGS, .ml_doc = "Set undefined mode: 0=silent, 1=strict, 2=debug" },
    .{ .ml_name = "_template_set_delimiters", .ml_meth = @ptrCast(&template_engine.py_template_set_delimiters), .ml_flags = c.METH_VARARGS, .ml_doc = "Set template delimiters: (block_start, block_end, var_start, var_end, comment_start, comment_end)" },
    .{ .ml_name = "_template_set_sandbox", .ml_meth = @ptrCast(&template_engine.py_template_set_sandbox), .ml_flags = c.METH_VARARGS, .ml_doc = "Enable/disable template sandbox mode: (0 or 1)" },
    .{ .ml_name = "_template_set_autoescape", .ml_meth = @ptrCast(&template_engine.py_template_set_autoescape), .ml_flags = c.METH_VARARGS, .ml_doc = "Set engine-level autoescape default: (0 or 1)" },
    .{ .ml_name = "_template_set_safety_limits", .ml_meth = @ptrCast(&template_engine.py_template_set_safety_limits), .ml_flags = c.METH_VARARGS, .ml_doc = "Set safety limits: (max_string_len, max_array_count, max_expr_depth). 0 = default." },
    .{ .ml_name = "_template_set_i18n_callback", .ml_meth = @ptrCast(&template_engine.py_template_set_i18n_callback), .ml_flags = c.METH_VARARGS, .ml_doc = "Set i18n translation callback for {% trans %} blocks: (callable_or_none)" },
    .{ .ml_name = "_template_serialize", .ml_meth = @ptrCast(&template_engine.py_template_serialize), .ml_flags = c.METH_VARARGS, .ml_doc = "Serialize compiled template: (capsule, source) -> bytes" },
    .{ .ml_name = "_template_deserialize", .ml_meth = @ptrCast(&template_engine.py_template_deserialize), .ml_flags = c.METH_VARARGS, .ml_doc = "Deserialize template: (bytes, source_hash) -> capsule or None" },

    // Profiler (nanosecond precision)
    .{ .ml_name = "_profiler_nanos", .ml_meth = @ptrCast(&profiler.py_profiler_nanos), .ml_flags = c.METH_NOARGS, .ml_doc = "Current nanosecond timestamp" },
    .{ .ml_name = "_profiler_diff_nanos", .ml_meth = @ptrCast(&profiler.py_profiler_diff_nanos), .ml_flags = c.METH_VARARGS, .ml_doc = "Elapsed nanoseconds since start" },

    // Static file helpers (native file I/O + MD5 + gzip)
    .{ .ml_name = "_hash_file_md5", .ml_meth = @ptrCast(&static_helpers.py_hash_file_md5), .ml_flags = c.METH_VARARGS, .ml_doc = "MD5 hash a file: (path) -> 32-char hex digest" },
    .{ .ml_name = "_file_read_with_hash", .ml_meth = @ptrCast(&static_helpers.py_file_read_with_hash), .ml_flags = c.METH_VARARGS, .ml_doc = "Read file + MD5: (path) -> (bytes, hex_digest)" },

    // Build mode query
    .{ .ml_name = "_is_release_build", .ml_meth = @ptrCast(&isReleaseBuild), .ml_flags = c.METH_NOARGS, .ml_doc = "True if compiled with ReleaseFast/ReleaseSafe" },

    // HyperGuard bytecode evaluator
    .{ .ml_name = "_guard_evaluate", .ml_meth = @ptrCast(&guard_eval.py_guard_evaluate), .ml_flags = c.METH_VARARGS, .ml_doc = "Evaluate guard bytecode: (bytecode, user_dict, resource_dict, field_names, constants) -> bool" },

    // WhereNode acceleration (compiled query cache)
    .{ .ml_name = "_where_cache_key", .ml_meth = @ptrCast(&where_compiler.where_cache_key), .ml_flags = c.METH_FASTCALL, .ml_doc = "Compute 64-bit FNV-1a hash from filter keys + value shapes" },
    .{ .ml_name = "_where_compile", .ml_meth = @ptrCast(&where_compiler.where_compile), .ml_flags = c.METH_FASTCALL, .ml_doc = "Compile WhereNode tree to (sql, params, next_idx)" },

    // ── Telemetry — native metric primitives (v0.14.19+, task #213-#219) ─────
    // Runtime-dynamic metric registry for Python FFI. Handle-based,
    // atomic-RMW hot paths, zero locks on inc/observe/set. See
    // zig/src/metrics_py.zig and docs/TelemetryArchitecturePlan.md.
    .{ .ml_name = "_metric_counter_register", .ml_meth = @ptrCast(&metrics_py.py_metric_counter_register), .ml_flags = c.METH_VARARGS, .ml_doc = "Register a counter: (name, help) -> handle" },
    .{ .ml_name = "_metric_counter_inc", .ml_meth = @ptrCast(&metrics_py.py_metric_counter_inc), .ml_flags = c.METH_VARARGS, .ml_doc = "Increment counter: (handle, amount)" },
    .{ .ml_name = "_metric_counter_read", .ml_meth = @ptrCast(&metrics_py.py_metric_counter_read), .ml_flags = c.METH_VARARGS, .ml_doc = "Read counter value: (handle) -> int (test helper)" },
    .{ .ml_name = "_metric_gauge_register", .ml_meth = @ptrCast(&metrics_py.py_metric_gauge_register), .ml_flags = c.METH_VARARGS, .ml_doc = "Register a gauge: (name, help) -> handle" },
    .{ .ml_name = "_metric_gauge_set", .ml_meth = @ptrCast(&metrics_py.py_metric_gauge_set), .ml_flags = c.METH_VARARGS, .ml_doc = "Set gauge: (handle, value)" },
    .{ .ml_name = "_metric_gauge_add", .ml_meth = @ptrCast(&metrics_py.py_metric_gauge_add), .ml_flags = c.METH_VARARGS, .ml_doc = "Add to gauge: (handle, delta)" },
    .{ .ml_name = "_metric_gauge_read", .ml_meth = @ptrCast(&metrics_py.py_metric_gauge_read), .ml_flags = c.METH_VARARGS, .ml_doc = "Read gauge value: (handle) -> int" },
    .{ .ml_name = "_metric_histogram_register", .ml_meth = @ptrCast(&metrics_py.py_metric_histogram_register), .ml_flags = c.METH_VARARGS, .ml_doc = "Register a histogram: (name, help, buckets) -> handle" },
    .{ .ml_name = "_metric_histogram_observe", .ml_meth = @ptrCast(&metrics_py.py_metric_histogram_observe), .ml_flags = c.METH_VARARGS, .ml_doc = "Observe a histogram value: (handle, value)" },
    .{ .ml_name = "_metric_counter_vec_register", .ml_meth = @ptrCast(&metrics_py.py_metric_counter_vec_register), .ml_flags = c.METH_VARARGS, .ml_doc = "Register labeled counter: (name, help, label_names) -> handle" },
    .{ .ml_name = "_metric_counter_vec_inc", .ml_meth = @ptrCast(&metrics_py.py_metric_counter_vec_inc), .ml_flags = c.METH_VARARGS, .ml_doc = "Increment labeled counter: (handle, label_values, amount)" },
    .{ .ml_name = "_metric_histogram_vec_register", .ml_meth = @ptrCast(&metrics_py.py_metric_histogram_vec_register), .ml_flags = c.METH_VARARGS, .ml_doc = "Register labeled histogram: (name, help, label_names, buckets) -> handle" },
    .{ .ml_name = "_metric_histogram_vec_observe", .ml_meth = @ptrCast(&metrics_py.py_metric_histogram_vec_observe), .ml_flags = c.METH_VARARGS, .ml_doc = "Observe labeled histogram: (handle, label_values, value)" },
    .{ .ml_name = "_metric_registry_write_prometheus", .ml_meth = @ptrCast(&metrics_py.py_metric_registry_write_prometheus), .ml_flags = c.METH_NOARGS, .ml_doc = "Export all metrics as Prometheus text exposition -> bytes" },
    .{ .ml_name = "_metric_registry_reset", .ml_meth = @ptrCast(&metrics_py.py_metric_registry_reset), .ml_flags = c.METH_NOARGS, .ml_doc = "Reset the metric registry (test helper — leaks memory)" },
    .{ .ml_name = "_metric_registry_size", .ml_meth = @ptrCast(&metrics_py.py_metric_registry_size), .ml_flags = c.METH_NOARGS, .ml_doc = "Return the number of registered metrics" },

    // ── Telemetry — native span ring (Phase 3, task #226) ────────────────────
    .{ .ml_name = "_span_start", .ml_meth = @ptrCast(&metrics_py.py_span_start), .ml_flags = c.METH_VARARGS, .ml_doc = "Start a span: (trace_high, trace_low, parent_id, name, sampled) -> handle" },
    .{ .ml_name = "_span_set_attr_str", .ml_meth = @ptrCast(&metrics_py.py_span_set_attr_str), .ml_flags = c.METH_VARARGS, .ml_doc = "Set span string attr: (handle, key, value)" },
    .{ .ml_name = "_span_set_attr_int", .ml_meth = @ptrCast(&metrics_py.py_span_set_attr_int), .ml_flags = c.METH_VARARGS, .ml_doc = "Set span int attr: (handle, key, value)" },
    .{ .ml_name = "_span_set_attr_float", .ml_meth = @ptrCast(&metrics_py.py_span_set_attr_float), .ml_flags = c.METH_VARARGS, .ml_doc = "Set span float attr: (handle, key, value)" },
    .{ .ml_name = "_span_set_status", .ml_meth = @ptrCast(&metrics_py.py_span_set_status), .ml_flags = c.METH_VARARGS, .ml_doc = "Set span status code: (handle, code: 0=unset 1=ok 2=error)" },
    .{ .ml_name = "_span_end", .ml_meth = @ptrCast(&metrics_py.py_span_end), .ml_flags = c.METH_VARARGS, .ml_doc = "End a span: (handle) — writes end_ns, transitions slot to complete" },
    .{ .ml_name = "_span_add_event", .ml_meth = @ptrCast(&metrics_py.py_span_add_event), .ml_flags = c.METH_VARARGS, .ml_doc = "Add timestamped event: (handle, name) — packed into slot event arena" },
    .{ .ml_name = "_span_drain", .ml_meth = @ptrCast(&metrics_py.py_span_drain), .ml_flags = c.METH_NOARGS, .ml_doc = "Drain completed spans -> list[dict]. Called by background drain thread." },
    .{ .ml_name = "_span_dropped_count", .ml_meth = @ptrCast(&metrics_py.py_span_dropped_count), .ml_flags = c.METH_NOARGS, .ml_doc = "Total dropped spans (ring overflow + unsampled)" },
    .{ .ml_name = "_span_configure", .ml_meth = @ptrCast(&metrics_py.py_span_configure), .ml_flags = c.METH_VARARGS, .ml_doc = "Set span ring capacity (power of 2, before init)" },
    .{ .ml_name = "_span_capacity", .ml_meth = @ptrCast(&metrics_py.py_span_capacity), .ml_flags = c.METH_NOARGS, .ml_doc = "Read live or configured span ring capacity" },
    .{ .ml_name = "_span_is_operational", .ml_meth = @ptrCast(&metrics_py.py_span_is_operational), .ml_flags = c.METH_NOARGS, .ml_doc = "True if ring is allocated and recording (False before init or after failed init)" },
    .{ .ml_name = "_span_reset_for_tests", .ml_meth = @ptrCast(&metrics_py.py_span_reset_for_tests), .ml_flags = c.METH_NOARGS, .ml_doc = "Reset span ring state (test helper)" },

    // ── Test-only lock-correctness stressors (gate: no-op lock primitive) ────
    // Prove py.RwLock / py.Mutex are real mutual-exclusion primitives, not
    // silently-degraded no-ops (the macOS pthread_rwlock sig-clobber bug). Each
    // spins real Zig threads doing lock()+non-atomic-increment+unlock(); a
    // correct lock returns exactly n_threads*iters, a no-op loses updates.
    // Driven by tests/test_freethread_lock_correctness.py.
    .{ .ml_name = "_test_rwlock_stress", .ml_meth = @ptrCast(&test_locks.test_rwlock_stress), .ml_flags = c.METH_VARARGS, .ml_doc = "TEST-ONLY: (n_threads, iters) -> counter; == n*iters iff RwLock is real" },
    .{ .ml_name = "_test_mutex_stress", .ml_meth = @ptrCast(&test_locks.test_mutex_stress), .ml_flags = c.METH_VARARGS, .ml_doc = "TEST-ONLY: (n_threads, iters) -> counter; == n*iters iff Mutex is real" },

    // sentinel
    .{ .ml_name = null, .ml_meth = null, .ml_flags = 0, .ml_doc = null },
};

// ── Validation wrappers (call dhi SIMD validators from Python) ──────────────

fn py_validate_email(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var email_ptr: [*c]const u8 = undefined;
    var email_len: c.Py_ssize_t = undefined;
    if (c.PyArg_ParseTuple(args, "s#", &email_ptr, &email_len) == 0) return null;

    const email_slice = email_ptr[0..@intCast(email_len)];
    // Use dhi's Email validator
    if (validator.Email.init(email_slice)) |_| {
        return py.pyTrue();
    } else |_| {
        return py.pyFalse();
    }
}

fn py_validate_int_range(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var value: c_long = undefined;
    var min_val: c_long = undefined;
    var max_val: c_long = undefined;
    if (c.PyArg_ParseTuple(args, "lll", &value, &min_val, &max_val) == 0) return null;
    return if (value >= min_val and value <= max_val) py.pyTrue() else py.pyFalse();
}

fn py_validate_string_length(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var str_ptr: [*c]const u8 = undefined;
    var str_len: c.Py_ssize_t = undefined;
    var min_len: c.Py_ssize_t = undefined;
    var max_len: c.Py_ssize_t = undefined;
    if (c.PyArg_ParseTuple(args, "s#nn", &str_ptr, &str_len, &min_len, &max_len) == 0) return null;
    return if (str_len >= min_len and str_len <= max_len) py.pyTrue() else py.pyFalse();
}

/// Native JSON serializer: walks Python object tree via C API,
/// writes JSON directly to a buffer with SIMD string escape scanning.
/// No Python json module involved — pure Zig serialization.
/// Per-thread serialization scratch, retained between calls.
///
/// Serializing a large body used to malloc AND free one oversized block per
/// call. Under the native server that is once per request from every worker
/// thread simultaneously, so the cost showed up as throughput that DEGRADED as
/// worker count grew — allocator contention, not a constant per-request cost.
/// Each thread now keeps its grown buffer and reuses it, so the steady state
/// performs zero allocator work no matter how many workers are running.
///
/// Retention is capped: a one-off huge payload is freed rather than pinned for
/// the thread's lifetime (same discipline as the server's `retain_with_limit`
/// request arena). `busy` guards re-entrancy — a `__json__`/`__str__` hook can
/// call back into json_dumps on this same thread, and the nested call must not
/// scribble on the outer call's buffer, so it falls back to a private one.
const JSON_SCRATCH_RETAIN_MAX: usize = 1 << 20; // 1 MiB
threadlocal var json_scratch: []u8 = &.{};
threadlocal var json_scratch_busy: bool = false;

fn py_json_dumps(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var obj: ?*c.PyObject = null;
    if (c.PyArg_ParseTuple(args, "O", &obj) == 0) return null;

    // Start with this thread's retained scratch (or a 4KB stack buffer on the
    // first call / a re-entrant one), grow to heap if needed.
    var stack_buf: [4096]u8 = undefined;
    var heap_buf: ?[]u8 = null;
    defer if (heap_buf) |hb| std.heap.c_allocator.free(hb);

    const owns_scratch = !json_scratch_busy;
    if (owns_scratch) json_scratch_busy = true;
    defer if (owns_scratch) {
        json_scratch_busy = false;
    };

    var buf: []u8 = if (owns_scratch and json_scratch.len >= stack_buf.len) json_scratch else &stack_buf;
    var pos: usize = 0;

    const ok = jsonSerialize(obj, &buf, &pos, &heap_buf, 0);

    // Adopt whatever the serializer grew into as this thread's scratch, so the
    // next call of the same shape starts pre-sized and allocates nothing. Only
    // the owner may adopt (a nested call must leave the outer buffer alone).
    if (owns_scratch) {
        if (heap_buf) |grown| {
            if (grown.len <= JSON_SCRATCH_RETAIN_MAX) {
                if (json_scratch.len > 0) std.heap.c_allocator.free(json_scratch);
                json_scratch = grown;
                heap_buf = null; // now owned by the thread, not this call
            }
        }
    }

    if (ok) {
        return py.newBytes(buf[0..pos]);
    } else {
        // Fallback to Python json.dumps on error
        return jsonDumpsFallback(obj);
    }
}

/// Recursively serialize a Python object to JSON in buf[pos..].
/// Returns false if buffer operations fail critically.
fn jsonSerialize(obj: ?*c.PyObject, buf: *[]u8, pos: *usize, heap_buf: *?[]u8, depth: usize) bool {
    if (depth > 64) return false; // Guard against infinite recursion
    const o = obj orelse {
        return jsonWrite(buf, pos, heap_buf, "null");
    };

    // None
    if (o == @as(*c.PyObject, @ptrCast(&c._Py_NoneStruct))) {
        return jsonWrite(buf, pos, heap_buf, "null");
    }
    // Bool (check before int — bool is subclass of int in Python)
    if (c.PyBool_Check(o) != 0) {
        return if (o == py.pyTrue()) jsonWrite(buf, pos, heap_buf, "true") else jsonWrite(buf, pos, heap_buf, "false");
    }
    // Int
    if (c.PyLong_Check(o) != 0) {
        const val = c.PyLong_AsLongLong(o);
        if (val == -1 and c.PyErr_Occurred() != null) {
            // Overflow: integer too large for i64, fall back to str()
            c.PyErr_Clear();
            const str_obj = c.PyObject_Str(o) orelse return false;
            defer c.Py_DecRef(str_obj);
            var slen: c.Py_ssize_t = 0;
            const sptr = c.PyUnicode_AsUTF8AndSize(str_obj, &slen) orelse return false;
            // Write raw numeric string (no quotes — it's still a number)
            return jsonWrite(buf, pos, heap_buf, sptr[0..@intCast(slen)]);
        }
        var num_buf: [24]u8 = undefined;
        const s = std.fmt.bufPrint(&num_buf, "{d}", .{val}) catch return false;
        return jsonWrite(buf, pos, heap_buf, s);
    }
    // Float — use Python repr() for shortest exact roundtrip (Ryu algorithm)
    if (c.PyFloat_Check(o) != 0) {
        const val = c.PyFloat_AsDouble(o);
        // Non-finite doubles have no JSON literal. Emit the LOSSLESS quoted
        // strings that round-trip through float() — byte-identical to the
        // PG→JSON path (db.zig query_json / native auto-CRUD) — rather than
        // silently dropping the value to null and hiding data from callers.
        if (std.math.isNan(val)) return jsonWriteString(buf, pos, heap_buf, "NaN");
        if (val == std.math.inf(f64)) return jsonWriteString(buf, pos, heap_buf, "Infinity");
        if (val == -std.math.inf(f64)) return jsonWriteString(buf, pos, heap_buf, "-Infinity");
        // Use Python repr for exact roundtrip precision
        const repr_obj = c.PyObject_Repr(o) orelse return false;
        defer c.Py_DecRef(repr_obj);
        var repr_len: c.Py_ssize_t = 0;
        const repr_ptr = c.PyUnicode_AsUTF8AndSize(repr_obj, &repr_len) orelse return false;
        return jsonWrite(buf, pos, heap_buf, repr_ptr[0..@intCast(repr_len)]);
    }
    // String
    if (c.PyUnicode_Check(o) != 0) {
        var str_len: c.Py_ssize_t = 0;
        const str_ptr = c.PyUnicode_AsUTF8AndSize(o, &str_len) orelse return false;
        const str_data = str_ptr[0..@intCast(str_len)];
        return jsonWriteString(buf, pos, heap_buf, str_data);
    }
    // Bytes
    if (c.PyBytes_Check(o) != 0) {
        var str_len: c.Py_ssize_t = 0;
        var str_ptr: [*c]u8 = undefined;
        if (c.PyBytes_AsStringAndSize(o, @ptrCast(&str_ptr), &str_len) < 0) return false;
        const str_data = str_ptr[0..@intCast(str_len)];
        return jsonWriteString(buf, pos, heap_buf, str_data);
    }
    // Dict
    if (c.PyDict_Check(o) != 0) {
        if (!jsonWriteByte(buf, pos, heap_buf, '{')) return false;
        var dict_pos: c.Py_ssize_t = 0;
        var key: ?*c.PyObject = null;
        var value: ?*c.PyObject = null;
        var first = true;
        while (c.PyDict_Next(o, &dict_pos, &key, &value) != 0) {
            if (!first) {
                if (!jsonWriteByte(buf, pos, heap_buf, ',')) return false;
            }
            first = false;
            // Key must be string
            if (c.PyUnicode_Check(key.?) != 0) {
                var klen: c.Py_ssize_t = 0;
                const kptr = c.PyUnicode_AsUTF8AndSize(key.?, &klen) orelse return false;
                if (!jsonWriteString(buf, pos, heap_buf, kptr[0..@intCast(klen)])) return false;
            } else {
                // Non-string key — convert to string
                const str_key = c.PyObject_Str(key.?) orelse return false;
                defer c.Py_DecRef(str_key);
                var klen: c.Py_ssize_t = 0;
                const kptr = c.PyUnicode_AsUTF8AndSize(str_key, &klen) orelse return false;
                if (!jsonWriteString(buf, pos, heap_buf, kptr[0..@intCast(klen)])) return false;
            }
            if (!jsonWriteByte(buf, pos, heap_buf, ':')) return false;
            if (!jsonSerialize(value, buf, pos, heap_buf, depth + 1)) return false;
        }
        return jsonWriteByte(buf, pos, heap_buf, '}');
    }
    // List/Tuple
    if (c.PyList_Check(o) != 0 or c.PyTuple_Check(o) != 0) {
        if (!jsonWriteByte(buf, pos, heap_buf, '[')) return false;
        const size: usize = @intCast(c.PySequence_Size(o));
        for (0..size) |i| {
            if (i > 0) {
                if (!jsonWriteByte(buf, pos, heap_buf, ',')) return false;
            }
            const item = c.PySequence_GetItem(o, @intCast(i));
            defer if (item) |it| c.Py_DecRef(it);
            if (!jsonSerialize(item, buf, pos, heap_buf, depth + 1)) return false;
        }
        return jsonWriteByte(buf, pos, heap_buf, ']');
    }
    // datetime / date / time → ISO 8601. isoformat() uses the 'T' separator
    // and omits the fractional-seconds part when the microsecond is zero,
    // unlike str() which uses a space separator. Full microsecond precision is
    // preserved (no truncation / data loss).
    if (c.PyObject_HasAttrString(o, "isoformat") != 0) {
        const iso_method = c.PyObject_GetAttrString(o, "isoformat") orelse {
            c.PyErr_Clear();
            return jsonSerializeFallback(o, buf, pos, heap_buf);
        };
        defer c.Py_DecRef(iso_method);
        const iso_obj = c.PyObject_CallNoArgs(iso_method) orelse {
            c.PyErr_Clear();
            return jsonSerializeFallback(o, buf, pos, heap_buf);
        };
        defer c.Py_DecRef(iso_obj);
        if (c.PyUnicode_Check(iso_obj) != 0) {
            var iso_len: c.Py_ssize_t = 0;
            const iso_ptr = c.PyUnicode_AsUTF8AndSize(iso_obj, &iso_len) orelse return false;
            return jsonWriteString(buf, pos, heap_buf, iso_ptr[0..@intCast(iso_len)]);
        }
        return jsonSerializeFallback(o, buf, pos, heap_buf);
    }
    // Enum instance detection: enum.Enum subclasses expose `_value_` as an
    // instance attribute holding the underlying scalar (str/int/etc.).
    // Without this, Color.RED would fall through to PyObject_Str and
    // serialize as "Color.RED" instead of the intended "red" value.
    // Framework correctness: every API returning a model with an enum
    // field now JSON-serializes the enum's value correctly. Check via
    // PyObject_GetAttrString so we walk the MRO, matching isinstance.
    if (c.PyObject_HasAttrString(o, "_value_") != 0) {
        const val_obj = c.PyObject_GetAttrString(o, "_value_") orelse {
            c.PyErr_Clear();
            // Fall through to str() fallback below
            const str_fallback = c.PyObject_Str(o) orelse return false;
            defer c.Py_DecRef(str_fallback);
            var slen_fb: c.Py_ssize_t = 0;
            const sptr_fb = c.PyUnicode_AsUTF8AndSize(str_fallback, &slen_fb) orelse return false;
            return jsonWriteString(buf, pos, heap_buf, sptr_fb[0..@intCast(slen_fb)]);
        };
        defer c.Py_DecRef(val_obj);
        return jsonSerialize(val_obj, buf, pos, heap_buf, depth + 1);
    }
    // model_dump() protocol: dataclasses, SessionUser, Pydantic models, etc.
    // Call model_dump() → dict, then recursively serialize the result.
    if (c.PyObject_HasAttrString(o, "model_dump") != 0) {
        const method = c.PyObject_GetAttrString(o, "model_dump") orelse {
            c.PyErr_Clear();
            // Fall through to __dict__ / str() fallback below
            return jsonSerializeFallback(o, buf, pos, heap_buf);
        };
        defer c.Py_DecRef(method);
        const result = c.PyObject_CallNoArgs(method) orelse {
            c.PyErr_Clear();
            return jsonSerializeFallback(o, buf, pos, heap_buf);
        };
        defer c.Py_DecRef(result);
        return jsonSerialize(result, buf, pos, heap_buf, depth + 1);
    }
    // __dict__ fallback: plain objects with instance attributes (e.g. slotless classes).
    // Slots-based dataclasses won't have __dict__, so this only catches non-slotted objects.
    if (c.PyObject_HasAttrString(o, "__dict__") != 0) {
        const dict_obj = c.PyObject_GetAttrString(o, "__dict__") orelse {
            c.PyErr_Clear();
            return jsonSerializeFallback(o, buf, pos, heap_buf);
        };
        defer c.Py_DecRef(dict_obj);
        if (c.PyDict_Check(dict_obj) != 0) {
            return jsonSerialize(dict_obj, buf, pos, heap_buf, depth + 1);
        }
    }
    // Final fallback: call str() and serialize as string
    return jsonSerializeFallback(o, buf, pos, heap_buf);
}

/// Fallback: call str() on an object and serialize as a JSON string.
fn jsonSerializeFallback(o: *c.PyObject, buf: *[]u8, pos: *usize, heap_buf: *?[]u8) bool {
    const str_obj = c.PyObject_Str(o) orelse return false;
    defer c.Py_DecRef(str_obj);
    var slen: c.Py_ssize_t = 0;
    const sptr = c.PyUnicode_AsUTF8AndSize(str_obj, &slen) orelse return false;
    return jsonWriteString(buf, pos, heap_buf, sptr[0..@intCast(slen)]);
}

/// Write one escaped byte at `buf[pos]`. The caller guarantees 6 bytes of room.
inline fn jsonWriteEscaped(buf: []u8, pos: *usize, ch: u8) void {
    switch (ch) {
        '"', '\\' => {
            buf[pos.*] = '\\';
            buf[pos.* + 1] = ch;
            pos.* += 2;
        },
        '\n' => {
            buf[pos.*] = '\\';
            buf[pos.* + 1] = 'n';
            pos.* += 2;
        },
        '\r' => {
            buf[pos.*] = '\\';
            buf[pos.* + 1] = 'r';
            pos.* += 2;
        },
        '\t' => {
            buf[pos.*] = '\\';
            buf[pos.* + 1] = 't';
            pos.* += 2;
        },
        else => {
            if (ch >= 0x20) {
                buf[pos.*] = ch;
                pos.* += 1;
            } else {
                // Control chars U+0000-U+001F → \uXXXX (JSON spec)
                const hex = "0123456789abcdef";
                buf[pos.*] = '\\';
                buf[pos.* + 1] = 'u';
                buf[pos.* + 2] = '0';
                buf[pos.* + 3] = '0';
                buf[pos.* + 4] = hex[ch >> 4];
                buf[pos.* + 5] = hex[ch & 0x0f];
                pos.* += 6;
            }
        },
    }
}

/// Write a JSON-escaped string with surrounding quotes to the buffer.
///
/// Reservation is the UNESCAPED size (`len + 2`), not the `\uXXXX` worst case.
/// The old `len * 6 + 2` reservation meant a 64 KiB body demanded a ~394 KB
/// buffer to produce ~65 KB of JSON — a 6x oversized large-block allocation on
/// the process allocator, once per request, from every worker thread at once.
/// Escapes are handled by growing lazily inside the loop, which for the
/// overwhelming majority of payloads (plain text, no control characters) never
/// fires. A separate escape PRE-scan was measured and rejected: the extra
/// read pass over the payload costs more than the oversized allocation saves
/// (5.67 → 7.31 µs per 64 KiB call), so the scan stays fused with the copy.
///
/// Loop invariant, established by the initial reserve and restored by the
/// escape branch: `buf.len - pos >= (bytes of `str` still unwritten) + 1`,
/// i.e. the remainder always fits unescaped plus the closing quote.
fn jsonWriteString(buf: *[]u8, pos: *usize, heap_buf: *?[]u8, str: []const u8) bool {
    if (!jsonEnsureSpace(buf, pos, heap_buf, str.len + 2)) return false;

    buf.*[pos.*] = '"';
    pos.* += 1;

    const simd_width = 16;
    var i: usize = 0;

    // SIMD fast path: check 16 bytes at a time. The invariant guarantees room
    // for a clean 16-byte copy, so no per-iteration capacity check is needed.
    while (i + simd_width <= str.len) : (i += simd_width) {
        const chunk: @Vector(simd_width, u8) = str[i..][0..simd_width].*;
        const ctrl_mask = chunk < @as(@Vector(simd_width, u8), @splat(0x20));
        const quote_mask = chunk == @as(@Vector(simd_width, u8), @splat('"'));
        const bslash_mask = chunk == @as(@Vector(simd_width, u8), @splat('\\'));
        const any_special = @reduce(.Or, ctrl_mask) or @reduce(.Or, quote_mask) or @reduce(.Or, bslash_mask);

        if (!any_special) {
            @memcpy(buf.*[pos.*..][0..simd_width], str[i..][0..simd_width]);
            pos.* += simd_width;
        } else {
            // This chunk can expand up to 6x. Reserve that plus the untouched
            // remainder and the closing quote, restoring the invariant.
            const rest = str.len - i - simd_width;
            if (!jsonEnsureSpace(buf, pos, heap_buf, simd_width * 6 + rest + 1)) return false;
            for (str[i..][0..simd_width]) |ch| jsonWriteEscaped(buf.*, pos, ch);
        }
    }

    // Scalar remainder
    while (i < str.len) : (i += 1) {
        const ch = str[i];
        if (ch >= 0x20 and ch != '"' and ch != '\\') {
            buf.*[pos.*] = ch;
            pos.* += 1;
        } else {
            if (!jsonEnsureSpace(buf, pos, heap_buf, 6 + (str.len - i - 1) + 1)) return false;
            jsonWriteEscaped(buf.*, pos, ch);
        }
    }

    buf.*[pos.*] = '"';
    pos.* += 1;
    return true;
}

fn jsonWrite(buf: *[]u8, pos: *usize, heap_buf: *?[]u8, data: []const u8) bool {
    if (!jsonEnsureSpace(buf, pos, heap_buf, data.len)) return false;
    @memcpy(buf.*[pos.*..][0..data.len], data);
    pos.* += data.len;
    return true;
}

fn jsonWriteByte(buf: *[]u8, pos: *usize, heap_buf: *?[]u8, byte: u8) bool {
    if (!jsonEnsureSpace(buf, pos, heap_buf, 1)) return false;
    buf.*[pos.*] = byte;
    pos.* += 1;
    return true;
}

fn jsonEnsureSpace(buf: *[]u8, pos: *usize, heap_buf: *?[]u8, needed: usize) bool {
    if (pos.* + needed <= buf.*.len) return true;
    // Grow buffer
    const new_size = @max(buf.*.len * 2, pos.* + needed + 1024);
    const new_buf = std.heap.c_allocator.alloc(u8, new_size) catch return false;
    @memcpy(new_buf[0..pos.*], buf.*[0..pos.*]);
    if (heap_buf.*) |old| std.heap.c_allocator.free(old);
    heap_buf.* = new_buf;
    buf.* = new_buf;
    return true;
}

fn jsonDumpsFallback(obj: ?*c.PyObject) ?*c.PyObject {
    const json_mod = c.PyImport_ImportModule("json") orelse return null;
    defer c.Py_DecRef(json_mod);
    const dumps_fn = c.PyObject_GetAttrString(json_mod, "dumps") orelse return null;
    defer c.Py_DecRef(dumps_fn);
    const call_args = c.PyTuple_Pack(1, obj) orelse return null;
    defer c.Py_DecRef(call_args);
    const result = c.PyObject_Call(dumps_fn, call_args, null) orelse return null;
    defer c.Py_DecRef(result);
    const encode_fn = c.PyObject_GetAttrString(result, "encode") orelse return null;
    defer c.Py_DecRef(encode_fn);
    // Capture the "utf-8" str so its ref is dropped — PyTuple_Pack INCREFs its
    // items rather than stealing, so passing newString() inline would leak it.
    const enc = py.newString("utf-8") orelse return null;
    defer c.Py_DecRef(enc);
    const encode_args = c.PyTuple_Pack(1, enc) orelse return null;
    defer c.Py_DecRef(encode_args);
    return c.PyObject_Call(encode_fn, encode_args, null);
}

/// dump_model_to_json(model_instance, compiled_capsule) → JSON bytes
/// Single Zig call: walks compiled field specs, reads field values from model __dict__,
/// writes JSON directly to buffer. Skips intermediate Python dict entirely.
/// 3-5x faster than model_dump() + json_dumps().
fn py_dump_model_to_json(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var model_self: ?*c.PyObject = null;
    var capsule: ?*c.PyObject = null;
    if (c.PyArg_ParseTuple(args, "OO", &model_self, &capsule) == 0) return null;

    const ms_ptr = c.PyCapsule_GetPointer(capsule.?, "hyperdjango.compiled_specs") orelse {
        // Fallback: dump_model_compiled → json_dumps. py_json_dumps parses its
        // args tuple (PyArg_ParseTuple) without stealing it, so the packed tuple
        // must be decref'd here — mirror the sibling fallback below.
        const dict = model_validator.py_dump_model_compiled(null, args) orelse return null;
        defer c.Py_DecRef(dict);
        const fa = c.PyTuple_Pack(1, dict) orelse return null;
        defer c.Py_DecRef(fa);
        return py_json_dumps(null, fa);
    };
    const ms: *model_validator.CompiledModelSpecs = @ptrCast(@alignCast(ms_ptr));

    const obj_dict = c.PyObject_GenericGetDict(model_self.?, null) orelse return null;
    defer c.Py_DECREF(obj_dict);

    var stack_buf: [4096]u8 = undefined;
    var buf: []u8 = &stack_buf;
    var heap_buf: ?[]u8 = null;
    defer if (heap_buf) |hb| std.heap.c_allocator.free(hb);
    var pos: usize = 0;

    if (modelToJson(obj_dict, ms, &buf, &pos, &heap_buf, 0)) {
        return py.newBytes(buf[0..pos]);
    } else {
        // Fallback on any error
        const dict = model_validator.py_dump_model_compiled(null, args) orelse return null;
        defer c.Py_DecRef(dict);
        const fa = c.PyTuple_Pack(1, dict) orelse return null;
        defer c.Py_DecRef(fa);
        return py_json_dumps(null, fa);
    }
}

/// Walk compiled model specs and write JSON directly to buffer.
fn modelToJson(obj_dict: *c.PyObject, ms: *model_validator.CompiledModelSpecs, buf: *[]u8, pos: *usize, heap_buf: *?[]u8, depth: usize) bool {
    if (depth > 32) return false;
    if (!jsonWriteByte(buf, pos, heap_buf, '{')) return false;

    var first = true;
    var i: usize = 0;
    while (i < ms.n_fields) : (i += 1) {
        const fs = &ms.specs[i];
        const value = c.PyDict_GetItem(obj_dict, fs.name_obj) orelse continue;

        if (!first) {
            if (!jsonWriteByte(buf, pos, heap_buf, ',')) return false;
        }
        first = false;

        // Write field name as JSON key
        var klen: c.Py_ssize_t = 0;
        const kptr = c.PyUnicode_AsUTF8AndSize(fs.name_obj, &klen) orelse return false;
        if (!jsonWriteString(buf, pos, heap_buf, kptr[0..@intCast(klen)])) return false;
        if (!jsonWriteByte(buf, pos, heap_buf, ':')) return false;

        // Write value — use type_code for fast dispatch
        switch (fs.type_code) {
            6 => {
                // Nested model — check for compiled specs and recurse
                if (!serializeModelOrValue(value, buf, pos, heap_buf, depth + 1)) return false;
            },
            7 => {
                // List of models
                if (c.PyList_Check(value) != 0) {
                    if (!jsonWriteByte(buf, pos, heap_buf, '[')) return false;
                    const len: usize = @intCast(c.PyList_Size(value));
                    for (0..len) |j| {
                        if (j > 0) {
                            if (!jsonWriteByte(buf, pos, heap_buf, ',')) return false;
                        }
                        const item = c.PyList_GetItem(value, @intCast(j)).?;
                        if (!serializeModelOrValue(item, buf, pos, heap_buf, depth + 1)) return false;
                    }
                    if (!jsonWriteByte(buf, pos, heap_buf, ']')) return false;
                } else {
                    if (!jsonSerialize(value, buf, pos, heap_buf, depth + 1)) return false;
                }
            },
            else => {
                // Primitive or unknown — use generic jsonSerialize
                if (!jsonSerialize(value, buf, pos, heap_buf, depth + 1)) return false;
            },
        }
    }

    return jsonWriteByte(buf, pos, heap_buf, '}');
}

/// Serialize a value that might be a model (has __dhi_compiled_specs__) or a plain value.
fn serializeModelOrValue(value: *c.PyObject, buf: *[]u8, pos: *usize, heap_buf: *?[]u8, depth: usize) bool {
    // Check if it's a model with compiled specs
    const compiled_attr = c.PyObject_GetAttrString(
        @as(*c.PyObject, @ptrCast(c.Py_TYPE(value))),
        "__dhi_compiled_specs__",
    );
    if (compiled_attr != null and compiled_attr != @as(?*c.PyObject, @ptrCast(&c._Py_NoneStruct))) {
        defer c.Py_DECREF(compiled_attr.?);
        const nested_ptr = c.PyCapsule_GetPointer(compiled_attr.?, "hyperdjango.compiled_specs");
        if (nested_ptr) |np| {
            const nested_ms: *model_validator.CompiledModelSpecs = @ptrCast(@alignCast(np));
            const nested_dict = c.PyObject_GenericGetDict(value, null) orelse return jsonSerialize(value, buf, pos, heap_buf, depth);
            defer c.Py_DECREF(nested_dict);
            return modelToJson(nested_dict, nested_ms, buf, pos, heap_buf, depth);
        }
    }
    if (compiled_attr) |ca| c.Py_DECREF(ca);
    // Not a model — use generic serialization
    return jsonSerialize(value, buf, pos, heap_buf, depth);
}

// ── Module definition ───────────────────────────────────────────────────────

fn module_free(_: ?*anyopaque) callconv(.c) void {
    // Abandon Python object references before interpreter shutdown.
    // During finalization, Python may have already freed objects — calling
    // Py_DecRef on them would SIGABRT. Instead, null out all references
    // and let process exit reclaim the memory.
    template_engine.module_cleanup();
    db.module_cleanup();
}

fn isReleaseBuild(_: ?*c.PyObject, _: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    const is_release = @import("builtin").mode != .Debug;
    return if (is_release) py.pyTrue() else py.pyFalse();
}

// Multi-phase init slot table. Required by Python 3.14 free-threaded
// builds (3.14.5+ enforces this — single-phase init via PyModule_Create
// segfaults inside PyModule_Create2 on macOS/3.14.5). Py_mod_gil declares
// this extension is free-threading-safe and skips the GIL re-enable.
var module_slots = [_]c.PyModuleDef_Slot{
    .{ .slot = c.Py_mod_exec, .value = @ptrCast(@constCast(&module_exec)) },
    .{ .slot = if (@hasDecl(c, "Py_mod_gil")) c.Py_mod_gil else 0, .value = if (@hasDecl(c, "Py_MOD_GIL_NOT_USED")) c.Py_MOD_GIL_NOT_USED else null },
    .{ .slot = 0, .value = null },
};

var module_def = c.PyModuleDef{
    .m_base = std.mem.zeroes(c.PyModuleDef_Base),
    .m_name = "_hyperdjango_native",
    .m_doc = "HyperDjango Native — Zig HTTP server + pg.zig + SIMD validation",
    .m_size = 0,
    .m_methods = @ptrCast(&methods),
    .m_slots = &module_slots,
    .m_traverse = null,
    .m_clear = null,
    .m_free = @ptrCast(&module_free),
};

// Bootstrap Python wrapper classes directly into the module
const bootstrap_code: [*:0]const u8 =
    \\class ResponseView:
    \\    def __init__(self, status_code=None):
    \\        self._state = _m._rv_new(status_code if status_code is not None else 200)
    \\        self.status_code = status_code if status_code is not None else 200
    \\    def set_header(self, name, value):
    \\        _m._rv_set_header(self._state, name, value)
    \\    def get_header(self, name):
    \\        return _m._rv_get_header(self._state, name)
    \\    def set_body(self, body):
    \\        _m._rv_set_body(self._state, body)
    \\    def set_body_bytes(self, body):
    \\        _m._rv_set_body_bytes(self._state, body)
    \\    def get_body_str(self):
    \\        b = self._state.get('body', b'')
    \\        return b.decode('utf-8') if isinstance(b, bytes) else str(b)
    \\    def get_body_bytes(self):
    \\        return self._state.get('body', b'')
    \\    def json(self, data):
    \\        _m._rv_json(self._state, data)
    \\    def text(self, data):
    \\        _m._rv_text(self._state, data)
    \\
    \\class HyperServer:
    \\    def __init__(self, host=None, port=None, max_body_size=None):
    \\        args = [host or "127.0.0.1", port or 8000, max_body_size or 10485760]
    \\        self._state = _m._server_new(*args)
    \\    def add_route(self, method, path, handler):
    \\        _m._server_add_route(method, path, handler)
    \\    def add_route_typed(self, method, path, handler, param_types_json):
    \\        _m._server_add_route_typed(method, path, handler, param_types_json)
    \\    def add_route_fast(self, method, path, handler, handler_type, param_types_json, original_handler):
    \\        _m._server_add_route_fast(method, path, handler, handler_type, param_types_json, original_handler)
    \\    def add_route_model(self, method, path, handler, param_name, model_class, original_handler):
    \\        _m._server_add_route_model(method, path, handler, param_name, model_class, original_handler)
    \\    def add_route_model_validated(self, method, path, handler, param_name, model_class, original_handler, schema_json):
    \\        _m._server_add_route_model_validated(method, path, handler, param_name, model_class, original_handler, schema_json)
    \\    def add_route_async_fast(self, method, path, handler, handler_type, param_types_json, original_handler):
    \\        _m._server_add_route_async_fast(method, path, handler, handler_type, param_types_json, original_handler)
    \\    def add_native_route(self, method, path, lib_path, symbol_name):
    \\        _m._server_add_native_route(method, path, lib_path, symbol_name)
    \\    def add_static_route(self, method, path, status, content_type, body):
    \\        _m._server_add_static_route(method, path, status, content_type, body)
    \\    def add_middleware(self, middleware):
    \\        _m._server_add_middleware(middleware)
    \\    def run(self):
    \\        _m._server_run()
    \\    def configure_cors(self, origins="*", methods="GET, POST, PUT, DELETE, OPTIONS, PATCH, HEAD", headers="*", max_age=600, credentials=0):
    \\        _m._server_configure_cors(origins, methods, headers, max_age, int(credentials))
    \\    def configure_security_headers(self, block):
    \\        _m._server_configure_security_headers(block)
    \\    def enable_response_cache(self):
    \\        _m._server_enable_response_cache()
    \\    def configure_db(self, conn_string, pool_size=16):
    \\        _m._db_configure(conn_string, pool_size)
    \\    def configure_db_handle(self, handle):
    \\        _m._db_set_active_handle(handle)
    \\    def add_db_route(self, method, path, op, table, pk_column, pk_param, columns):
    \\        _m._db_add_route(method, path, op, table, pk_column or "", pk_param or "", columns or "")
    \\
    \\_m.ResponseView = ResponseView
    \\_m.HyperServer = HyperServer
;

// Module-exec slot: runs after the module object exists. Returns 0 on
// success, -1 on failure (Python prints the pending exception).
fn module_exec(m: ?*c.PyObject) callconv(.c) c_int {
    const mod = m orelse return -1;

    const globals = c.PyDict_New() orelse return -1;
    defer c.Py_DecRef(globals);

    _ = c.PyDict_SetItemString(globals, "_m", mod);
    _ = c.PyDict_SetItemString(globals, "__builtins__", c.PyEval_GetBuiltins());

    const result = c.PyRun_String(bootstrap_code, c.Py_file_input, globals, globals) orelse return -1;
    c.Py_DecRef(result);

    return 0;
}

// Initialize PyModuleDef.m_base at runtime — the equivalent of the
// C `PyModuleDef_HEAD_INIT` macro. Required by the docs for any static
// PyModuleDef: zero-init leaves ob_flags=0 and ob_ref_local=0, so the
// def fails the _Py_IsImmortal check and a later Py_DECREF on it (which
// CPython does in _PyImport_RunModInitFunc) crashes in _Py_DecRefShared.
// Done at runtime (rather than a comptime const initializer) so the
// compiler can't fold the def into a read-only data section — CPython
// also writes ob_type via Py_SET_TYPE inside PyModuleDef_Init and that
// write must succeed.
fn ensureModuleBaseInit() void {
    const base = &module_def.m_base.ob_base;
    if (@hasField(c.PyObject, "ob_flags")) {
        // Free-threaded (Py_GIL_DISABLED): split refcount + ob_flags.
        base.ob_flags = c._Py_STATICALLY_ALLOCATED_FLAG;
        base.ob_ref_local = c._Py_IMMORTAL_REFCNT_LOCAL;
    } else if (@hasField(c.PyObject, "ob_refcnt_full")) {
        base.ob_refcnt_full = c._Py_STATIC_IMMORTAL_INITIAL_REFCNT;
    } else {
        base.ob_refcnt = c._Py_STATIC_IMMORTAL_INITIAL_REFCNT;
    }
}

export fn PyInit__hyperdjango_native() ?*c.PyObject {
    ensureModuleBaseInit();
    return c.PyModuleDef_Init(&module_def);
}

test "sanity" {
    var r = router.Router.init(std.testing.allocator);
    defer r.deinit();
}

test "router parameterized match" {
    var r = router.Router.init(std.testing.allocator);
    defer r.deinit();

    try r.addRoute("GET", "/users/{id}/posts/{post_id}", "GET /users/{id}/posts/{post_id}");

    var m = r.findRoute("GET", "/users/42/posts/7").?;
    defer m.deinit();

    try std.testing.expectEqualStrings("42", m.params.get("id").?);
    try std.testing.expectEqualStrings("7", m.params.get("post_id").?);
}

// Pull imported modules' `test` blocks (e.g. db.zig's parseListenerDsn tests)
// into this `b.addTest`-rooted-at-main.zig suite. Zig only runs tests from a
// non-root file when that file's decls are referenced in a test context.
test {
    std.testing.refAllDecls(db);
}

// Pull json_parser's recursion-depth-guard tests into the suite (over-deep
// array/object nesting must error, not SIGSEGV the worker). See ws23-json-depth.
test {
    _ = @import("json_parser.zig");
}

// Pull the HTTP reactor's unit tests into the suite (kqueue/epoll readiness,
// wakeup, register/remove). See docs/design/http-connection-reactor.md.
test {
    _ = @import("reactor.zig");
}
