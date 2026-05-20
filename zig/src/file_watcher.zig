// Native file watcher — kqueue (macOS) / inotify (Linux)
//
// Uses kernel-level file system notifications instead of polling.
// Spawns a background thread that monitors directories and calls
// a Python callback when changes are detected.
//
// API:
//   _file_watcher_start(directories_list, extensions_list, callback) -> handle
//   _file_watcher_stop(handle)

const std = @import("std");
pub const py = @import("py.zig");
const c = py.c;
const builtin = @import("builtin");

const Allocator = std.heap.c_allocator;

// ── Platform-specific file watching ──────────────────────────────────────────

const WatcherState = struct {
    running: std.atomic.Value(bool),
    thread: ?std.Thread,
    callback: ?*c.PyObject,
    watch_dirs: std.ArrayListUnmanaged([]const u8),
    extensions: std.ArrayListUnmanaged([]const u8),

    // kqueue/inotify fd
    watch_fd: std.posix.fd_t,

    // Directory fd tracking (kqueue needs open fds)
    dir_fds: std.ArrayListUnmanaged(DirEntry),

    const DirEntry = struct {
        path: []const u8,
        fd: std.posix.fd_t,
    };

    fn deinit(self: *WatcherState) void {
        // Close dir fds
        for (self.dir_fds.items) |entry| {
            _ = std.posix.system.close(entry.fd);
            Allocator.free(entry.path);
        }
        self.dir_fds.deinit(Allocator);

        // Close watch fd
        if (self.watch_fd != -1) {
            _ = std.posix.system.close(self.watch_fd);
        }

        // Free strings
        for (self.watch_dirs.items) |d| Allocator.free(d);
        self.watch_dirs.deinit(Allocator);
        for (self.extensions.items) |e| Allocator.free(e);
        self.extensions.deinit(Allocator);

        // Release Python callback
        if (self.callback) |cb| {
            const state = c.PyGILState_Ensure();
            c.Py_DECREF(cb);
            c.PyGILState_Release(state);
        }
    }
};

// Global watcher registry (supports multiple watchers)
var g_watchers: std.ArrayListUnmanaged(?*WatcherState) = .empty;

// ── py_file_watcher_start ────────────────────────────────────────────────────
// Start watching directories for file changes.
// Args: (directories_list, extensions_list, callback)
// Returns: watcher handle (int)

pub fn py_file_watcher_start(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var dirs_obj: ?*c.PyObject = null;
    var exts_obj: ?*c.PyObject = null;
    var callback_obj: ?*c.PyObject = null;
    if (c.PyArg_ParseTuple(args, "O!O!O", &c.PyList_Type, &dirs_obj, &c.PyList_Type, &exts_obj, &callback_obj) == 0)
        return null;

    if (c.PyCallable_Check(callback_obj) == 0) {
        c.PyErr_SetString(c.PyExc_TypeError, "callback must be callable");
        return null;
    }

    // Create watcher state
    const state = Allocator.create(WatcherState) catch {
        _ = c.PyErr_NoMemory();
        return null;
    };
    state.* = .{
        .running = std.atomic.Value(bool).init(true),
        .thread = null,
        .callback = callback_obj.?,
        .watch_dirs = .empty,
        .extensions = .empty,
        .watch_fd = -1,
        .dir_fds = .empty,
    };
    c.Py_INCREF(callback_obj.?);

    // Extract directory paths
    const n_dirs = c.PyList_Size(dirs_obj.?);
    var i: c.Py_ssize_t = 0;
    while (i < n_dirs) : (i += 1) {
        const item = c.PyList_GetItem(dirs_obj.?, i).?;
        const str_ptr = c.PyUnicode_AsUTF8(item) orelse continue;
        const s = std.mem.span(str_ptr);
        const copy = Allocator.alloc(u8, s.len) catch continue;
        @memcpy(copy, s);
        state.watch_dirs.append(Allocator, copy) catch {
            Allocator.free(copy);
        };
    }

    // Extract extensions
    const n_exts = c.PyList_Size(exts_obj.?);
    i = 0;
    while (i < n_exts) : (i += 1) {
        const item = c.PyList_GetItem(exts_obj.?, i).?;
        const str_ptr = c.PyUnicode_AsUTF8(item) orelse continue;
        const s = std.mem.span(str_ptr);
        const copy = Allocator.alloc(u8, s.len) catch continue;
        @memcpy(copy, s);
        state.extensions.append(Allocator, copy) catch {
            Allocator.free(copy);
        };
    }

    // Initialize platform-specific watcher
    if (comptime builtin.os.tag == .macos) {
        state.watch_fd = initKqueue(state) catch {
            state.deinit();
            Allocator.destroy(state);
            c.PyErr_SetString(c.PyExc_OSError, "Failed to create kqueue");
            return null;
        };
    } else if (comptime builtin.os.tag == .linux) {
        state.watch_fd = initInotify(state) catch {
            state.deinit();
            Allocator.destroy(state);
            c.PyErr_SetString(c.PyExc_OSError, "Failed to create inotify");
            return null;
        };
    } else {
        state.deinit();
        Allocator.destroy(state);
        c.PyErr_SetString(c.PyExc_OSError, "File watching not supported on this platform");
        return null;
    }

    // Spawn watcher thread
    state.thread = std.Thread.spawn(.{}, watcherThread, .{state}) catch {
        state.deinit();
        Allocator.destroy(state);
        c.PyErr_SetString(c.PyExc_OSError, "Failed to spawn watcher thread");
        return null;
    };

    // Register in global registry
    const handle: c.Py_ssize_t = @intCast(g_watchers.items.len);
    g_watchers.append(Allocator, state) catch {
        // The watcher thread was already spawned above and is reading `state`.
        // Signal it AND join before deinit/destroy — otherwise deinit() closes
        // fds and frees state (incl. the callback) out from under a live thread
        // (use-after-free). Mirror the ordered teardown in py_file_watcher_stop.
        state.running.store(false, .release);
        if (state.thread) |thr| thr.join();
        state.deinit();
        Allocator.destroy(state);
        _ = c.PyErr_NoMemory();
        return null;
    };

    return c.PyLong_FromSsize_t(handle);
}

// ── py_file_watcher_stop ─────────────────────────────────────────────────────

pub fn py_file_watcher_stop(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    var handle: c.Py_ssize_t = undefined;
    if (c.PyArg_ParseTuple(args, "n", &handle) == 0) return null;

    const idx: usize = @intCast(handle);
    if (idx >= g_watchers.items.len) {
        c.PyErr_SetString(c.PyExc_ValueError, "Invalid watcher handle");
        return null;
    }

    if (g_watchers.items[idx]) |state| {
        // Signal thread to stop — it checks running every 1s (kevent timeout)
        state.running.store(false, .release);

        // Wait for thread to exit (at most 1 second for kevent timeout)
        if (state.thread) |t| {
            t.join();
        }

        // Thread has exited — safe to close all fds
        state.deinit();
        Allocator.destroy(state);
        g_watchers.items[idx] = null;
    }

    return py.pyNone();
}

// ── kqueue (macOS/BSD) ───────────────────────────────────────────────────────
// kqueue requires an open fd per watched entity. To detect file CONTENT changes
// (not just directory-level create/delete), we must open every matching file
// and register NOTE.WRITE on each file's fd individually. Directories are also
// watched for NOTE.WRITE (new files created) so we can dynamically add them.

fn initKqueue(state: *WatcherState) !std.posix.fd_t {
    const kq_rc = std.posix.system.kqueue();
    if (kq_rc < 0) return error.KqueueFailed;
    const kq: std.posix.fd_t = @intCast(kq_rc);

    // Recursively watch each directory + all matching files within
    for (state.watch_dirs.items) |dir_path| {
        addDirTreeToKqueue(state, kq, dir_path) catch continue;
    }

    return kq;
}

fn registerFdWithKqueue(kq: std.posix.fd_t, fd: std.posix.fd_t) void {
    var changelist = [_]std.posix.Kevent{.{
        .ident = @intCast(fd),
        .filter = std.posix.system.EVFILT.VNODE,
        .flags = std.posix.system.EV.ADD | std.posix.system.EV.CLEAR,
        .fflags = std.posix.system.NOTE.WRITE | std.posix.system.NOTE.DELETE | std.posix.system.NOTE.RENAME | std.posix.system.NOTE.ATTRIB,
        .data = 0,
        .udata = 0,
    }};
    var no_events: [0]std.c.Kevent = .{};
    _ = std.c.kevent(kq, &changelist, 1, &no_events, 0, null);
}

fn hasMatchingExtension(name: []const u8, extensions: []const []const u8) bool {
    for (extensions) |ext| {
        if (name.len >= ext.len and std.mem.eql(u8, name[name.len - ext.len ..], ext)) {
            return true;
        }
    }
    return false;
}

fn addDirTreeToKqueue(state: *WatcherState, kq: std.posix.fd_t, dir_path: []const u8) !void {
    // Watch the directory itself (for new file creation events)
    const dir_z = try Allocator.dupeZ(u8, dir_path);
    defer Allocator.free(dir_z);

    const dir_fd = std.c.open(dir_z, std.c.O{}, @as(std.c.mode_t, 0));
    if (dir_fd < 0) return;
    registerFdWithKqueue(kq, dir_fd);

    const dir_path_copy = try Allocator.alloc(u8, dir_path.len);
    @memcpy(dir_path_copy, dir_path);
    try state.dir_fds.append(Allocator, .{ .path = dir_path_copy, .fd = dir_fd });

    // Open and watch every matching FILE in this directory
    var dir = py.openDirAbsolute(dir_path) catch return;
    defer dir.close();

    while (dir.next()) |entry| {
        if (entry.kind == .directory) {
            if (isIgnoredDir(entry.name)) continue;
            // Recurse into subdirectory
            var sub_buf: [py.max_path_bytes]u8 = undefined;
            const sub_path = std.fmt.bufPrint(&sub_buf, "{s}/{s}", .{ dir_path, entry.name }) catch continue;
            const sub_copy = Allocator.alloc(u8, sub_path.len) catch continue;
            @memcpy(sub_copy, sub_path);
            // The recursive call dupes whatever it retains (its own dir_fds
            // entries), so sub_copy is only borrowed for the call. It leaked on
            // the success path before — free it unconditionally afterward.
            defer Allocator.free(sub_copy);
            addDirTreeToKqueue(state, kq, sub_copy) catch {};
        } else if (entry.kind == .file) {
            // Check if this file matches our watched extensions
            if (!hasMatchingExtension(entry.name, state.extensions.items)) continue;

            // Open the FILE and register it with kqueue for content changes
            var file_buf: [py.max_path_bytes]u8 = undefined;
            const file_path = std.fmt.bufPrint(&file_buf, "{s}/{s}", .{ dir_path, entry.name }) catch continue;
            const file_z = Allocator.dupeZ(u8, file_path) catch continue;
            defer Allocator.free(file_z);

            const file_fd = blk: {
                const fd = std.c.open(file_z, std.c.O{}, @as(std.c.mode_t, 0));
                if (fd < 0) continue;
                break :blk fd;
            };
            registerFdWithKqueue(kq, file_fd);

            const file_path_copy = Allocator.alloc(u8, file_path.len) catch {
                _ = std.posix.system.close(file_fd);
                continue;
            };
            @memcpy(file_path_copy, file_path);
            state.dir_fds.append(Allocator, .{ .path = file_path_copy, .fd = file_fd }) catch {
                _ = std.posix.system.close(file_fd);
                Allocator.free(file_path_copy);
            };
        }
    }
}

// ── inotify (Linux) ──────────────────────────────────────────────────────────

// Direct extern declarations — Zig 0.16 removed std.posix.inotify_init1 /
// inotify_add_watch and the std.posix.system.IN.* constants from the Linux
// platform module. Bind to libc symbols and define the kernel ABI constants
// + struct ourselves so we don't depend on std.posix.system at all here.
extern fn inotify_init1(flags: c_int) c_int;
extern fn inotify_add_watch(fd: c_int, pathname: [*:0]const u8, mask: u32) c_int;

const IN_NONBLOCK: c_int = 0o4000; // 0x800
const IN_MODIFY: u32 = 0x00000002;
const IN_CREATE: u32 = 0x00000100;
const IN_DELETE: u32 = 0x00000200;
const IN_MOVED_TO: u32 = 0x00000080;

const InotifyEvent = extern struct {
    wd: i32,
    mask: u32,
    cookie: u32,
    len: u32,
    // name follows: char[len], null-terminated
};

fn initInotify(state: *WatcherState) !std.posix.fd_t {
    const ifd = inotify_init1(IN_NONBLOCK);
    if (ifd < 0) return error.InotifyInitFailed;

    for (state.watch_dirs.items) |dir_path| {
        addDirToInotify(state, ifd, dir_path) catch continue;
    }

    return ifd;
}

fn addDirToInotify(state: *WatcherState, ifd: std.posix.fd_t, dir_path: []const u8) !void {
    const dir_z = try Allocator.dupeZ(u8, dir_path);
    defer Allocator.free(dir_z);

    const mask: u32 = IN_MODIFY | IN_CREATE | IN_DELETE | IN_MOVED_TO;
    const wd = inotify_add_watch(ifd, dir_z.ptr, mask);
    if (wd < 0) return;

    const path_copy = try Allocator.alloc(u8, dir_path.len);
    @memcpy(path_copy, dir_path);
    try state.dir_fds.append(Allocator, .{ .path = path_copy, .fd = -1 }); // inotify doesn't need per-dir fd

    // Recurse into subdirectories
    var dir = py.openDirAbsolute(dir_path) catch return;
    defer dir.close();

    while (dir.next()) |entry| {
        if (entry.kind == .directory) {
            if (isIgnoredDir(entry.name)) continue;

            var sub_buf: [py.max_path_bytes]u8 = undefined;
            const sub_path = std.fmt.bufPrint(&sub_buf, "{s}/{s}", .{ dir_path, entry.name }) catch continue;
            const sub_copy = Allocator.alloc(u8, sub_path.len) catch continue;
            @memcpy(sub_copy, sub_path);
            // Recursive call only borrows sub_copy (it dupes what it keeps), so
            // free it unconditionally afterward — it leaked on success before.
            defer Allocator.free(sub_copy);
            addDirToInotify(state, ifd, sub_copy) catch {};
        }
    }
}

// ── Watcher thread ───────────────────────────────────────────────────────────

fn watcherThread(state: *WatcherState) void {
    if (comptime builtin.os.tag == .macos) {
        watcherThreadKqueue(state);
    } else if (comptime builtin.os.tag == .linux) {
        watcherThreadInotify(state);
    }
}

fn watcherThreadKqueue(state: *WatcherState) void {
    var events: [32]std.posix.Kevent = undefined;

    // Track which fds are directory fds vs file fds for dynamic registration
    // When a directory event fires, scan for new files and register them

    while (state.running.load(.acquire)) {
        const timeout = std.c.timespec{ .sec = 1, .nsec = 0 };
        const n = std.c.kevent(state.watch_fd, @ptrCast(&[0]std.c.Kevent{}), 0, &events, @intCast(events.len), &timeout);
        if (n < 0) break;
        const event_count: usize = @intCast(n);

        if (event_count > 0 and state.running.load(.acquire)) {
            // Check if any event is a directory change — if so, scan for new files
            for (events[0..event_count]) |ev| {
                const ev_fd: std.posix.fd_t = @intCast(ev.ident);
                // Check if this fd is a directory by seeing if it's in our dir list
                // and if the event is NOTE.WRITE (new file created in dir)
                if (ev.fflags & std.posix.system.NOTE.WRITE != 0) {
                    for (state.dir_fds.items) |entry| {
                        if (entry.fd == ev_fd) {
                            // This is a directory — scan for new files to register
                            scanAndRegisterNewFiles(state, state.watch_fd, entry.path);
                            break;
                        }
                    }
                }
            }

            notifyPython(state);
        }
    }
}

fn scanAndRegisterNewFiles(state: *WatcherState, kq: std.posix.fd_t, dir_path: []const u8) void {
    var dir = py.openDirAbsolute(dir_path) catch return;
    defer dir.close();

    while (dir.next()) |entry| {
        if (entry.kind != .file) continue;
        if (!hasMatchingExtension(entry.name, state.extensions.items)) continue;

        // Check if we already have this file registered
        var file_buf: [py.max_path_bytes]u8 = undefined;
        const file_path = std.fmt.bufPrint(&file_buf, "{s}/{s}", .{ dir_path, entry.name }) catch continue;

        var already_watched = false;
        for (state.dir_fds.items) |existing| {
            if (std.mem.eql(u8, existing.path, file_path)) {
                already_watched = true;
                break;
            }
        }
        if (already_watched) continue;

        // New file — open fd and register with kqueue
        const file_z = Allocator.dupeZ(u8, file_path) catch continue;
        defer Allocator.free(file_z);

        const file_fd = blk: {
            const fd = std.c.open(file_z, std.c.O{}, @as(std.c.mode_t, 0));
            if (fd < 0) continue;
            break :blk fd;
        };
        registerFdWithKqueue(kq, file_fd);

        const path_copy = Allocator.alloc(u8, file_path.len) catch {
            _ = std.posix.system.close(file_fd);
            continue;
        };
        @memcpy(path_copy, file_path);
        state.dir_fds.append(Allocator, .{ .path = path_copy, .fd = file_fd }) catch {
            _ = std.posix.system.close(file_fd);
            Allocator.free(path_copy);
        };
    }
}

fn watcherThreadInotify(state: *WatcherState) void {
    var buf: [4096]u8 align(@alignOf(InotifyEvent)) = undefined;
    var poll_fds = [_]std.posix.pollfd{.{
        .fd = state.watch_fd,
        .events = std.posix.POLL.IN,
        .revents = 0,
    }};

    while (state.running.load(.acquire)) {
        // Poll with 1 second timeout
        // posix-safe: dedicated watcher thread; poll's unreachable errnos
        // (EFAULT/EINVAL) can't arise from a stack array + the single owned
        // inotify/kqueue fd. `catch break` on any returned error.
        const n = std.posix.poll(&poll_fds, 1000) catch break;
        if (n > 0 and (poll_fds[0].revents & std.posix.POLL.IN) != 0) {
            // Read events to clear the fd
            // posix-safe: read into a valid stack buffer from the owned inotify
            // fd; read's only unreachable errnos are EFAULT/EINVAL (neither
            // reachable here) — EBADF/etc. are returned, caught by `break`.
            _ = std.posix.read(state.watch_fd, &buf) catch break;

            if (state.running.load(.acquire)) {
                notifyPython(state);
            }
        }
    }
}

fn notifyPython(state: *WatcherState) void {
    const callback = state.callback orelse return;

    // Acquire GIL, call Python callback, release GIL
    const gil_state = c.PyGILState_Ensure();
    defer c.PyGILState_Release(gil_state);

    const result = c.PyObject_CallNoArgs(callback);
    if (result) |r| {
        c.Py_DECREF(r);
    } else {
        // Clear any Python exception from the callback
        c.PyErr_Clear();
    }
}

// ── Helpers ──────────────────────────────────────────────────────────────────

fn isIgnoredDir(name: []const u8) bool {
    const ignored = [_][]const u8{
        "__pycache__",   ".git",        "node_modules",   ".venv",      "venv",
        ".tox",          "zig-cache",   "zig-out",        ".zig-cache", ".mypy_cache",
        ".pytest_cache", ".ruff_cache", "__pypackages__",
    };
    for (ignored) |ign| {
        if (std.mem.eql(u8, name, ign)) return true;
    }
    // Skip hidden directories
    if (name.len > 0 and name[0] == '.') return true;
    return false;
}
