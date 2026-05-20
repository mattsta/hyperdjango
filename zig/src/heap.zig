//! Shared allocator authority.
//!
//! Production builds use the raw libc allocator (fast, no bookkeeping).
//! `-Dheap-safety=true` swaps in Zig's safety-checking DebugAllocator, which
//! detects double-free, use-after-free, and buffer overflow at free() time
//! (panicking with a stack trace) — turning the remote glibc "double free or
//! corruption" aborts into precise, local failures.
//!
//! Note: leak reporting requires an explicit `debug_instance.deinit()` at
//! shutdown, which the Python extension never performs (the interpreter tears
//! down first), so in practice only the corruption detectors (double-free /
//! use-after-free / out-of-bounds) fire. The DebugAllocator is thread-safe so
//! it is sound under the free-threaded (no-GIL) runtime.
const std = @import("std");
const build_options = @import("build_options");

pub var debug_instance: std.heap.DebugAllocator(.{
    .thread_safe = true,
    .safety = true,
}) = .init;

pub const gpa: std.mem.Allocator = if (build_options.heap_safety)
    debug_instance.allocator()
else
    std.heap.c_allocator;
