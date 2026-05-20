/**
 * GIL release/restore shim for Zig code.
 *
 * Thin C wrappers around PyEval_SaveThread / PyEval_RestoreThread so Zig
 * can call them via `extern fn` without needing to cimport the opaque
 * PyThreadState struct. Enables GIL release during native I/O operations
 * (database queries, file I/O) so other Python threads can run concurrently.
 *
 * Usage from Zig:
 *   extern fn py_gil_save() ?*anyopaque;
 *   extern fn py_gil_restore(state: ?*anyopaque) void;
 *
 *   // Release GIL during I/O:
 *   const save = py_gil_save();
 *   // ... native I/O here (no Python API calls!) ...
 *   py_gil_restore(save);
 */

#include <Python.h>

void *py_gil_save(void) {
    return (void *)PyEval_SaveThread();
}

void py_gil_restore(void *state) {
    PyEval_RestoreThread((PyThreadState *)state);
}
