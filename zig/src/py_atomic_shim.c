/*
 * Shim for Python 3.14t free-threaded build.
 *
 * Zig's @cImport translate-C cannot translate CPython's static inline
 * functions that use GCC __atomic_* builtins and inline assembly.
 * The C compiler sees these as static inline and compiles them fine,
 * but Zig drops them — so at link time the symbols are missing.
 *
 * Solution: compile this C file with the same Python headers, but
 * give the functions external linkage instead of static inline.
 * We do this by including Python.h (which defines them as static inline)
 * and then defining non-static wrapper functions that call through.
 *
 * Source references (CPython 3.14.3 free-threaded):
 *   - _Py_ThreadId:                     object.h:186
 *   - _Py_IsOwnedByCurrentThread:       object.h:251
 *   - _Py_atomic_load_uint32_relaxed:    cpython/pyatomic_gcc.h:366
 *   - _Py_atomic_store_uint32_relaxed:   cpython/pyatomic_gcc.h:492
 *   - _Py_atomic_load_uint64_relaxed:    cpython/pyatomic_gcc.h:390
 *   - _Py_atomic_load_ssize_relaxed:     cpython/pyatomic_gcc.h:382
 *   - _Py_atomic_load_uintptr_relaxed:   cpython/pyatomic_gcc.h:374
 *   - _Py_atomic_add_ssize:             cpython/pyatomic_gcc.h:62
 */

#include <stdint.h>
#include <stddef.h>

/* ── Thread ID (object.h:186) ─────────────────────────────────────────────── */
/* On aarch64 Apple: reads tpidrro_el0 register (read-only thread pointer) */

uintptr_t _Py_ThreadId(void) {
    uintptr_t tid;
#if defined(__aarch64__) && defined(__APPLE__)
    __asm__ ("mrs %0, tpidrro_el0" : "=r" (tid));
#elif defined(__aarch64__)
    __asm__ ("mrs %0, tpidr_el0" : "=r" (tid));
#elif defined(__MACH__) && defined(__x86_64__)
    __asm__("movq %%gs:0, %0" : "=r" (tid));
#elif defined(__x86_64__)
    __asm__("movq %%fs:0, %0" : "=r" (tid));
#elif defined(__i386__)
    __asm__("movl %%gs:0, %0" : "=r" (tid));
#else
    #error "Unsupported platform for _Py_ThreadId"
#endif
    return tid;
}

/* ── Atomic loads (pyatomic_gcc.h — __ATOMIC_RELAXED) ─────────────────────── */

uint32_t _Py_atomic_load_uint32_relaxed(const uint32_t *obj) {
    return __atomic_load_n(obj, __ATOMIC_RELAXED);
}

uint64_t _Py_atomic_load_uint64_relaxed(const uint64_t *obj) {
    return __atomic_load_n(obj, __ATOMIC_RELAXED);
}

ptrdiff_t _Py_atomic_load_ssize_relaxed(const ptrdiff_t *obj) {
    return __atomic_load_n(obj, __ATOMIC_RELAXED);
}

uintptr_t _Py_atomic_load_uintptr_relaxed(const uintptr_t *obj) {
    return __atomic_load_n(obj, __ATOMIC_RELAXED);
}

/* ── Atomic stores (pyatomic_gcc.h — __ATOMIC_RELAXED) ────────────────────── */

void _Py_atomic_store_uint32_relaxed(uint32_t *obj, uint32_t value) {
    __atomic_store_n(obj, value, __ATOMIC_RELAXED);
}

/* ── Atomic add (pyatomic_gcc.h:62 — __ATOMIC_SEQ_CST) ───────────────────── */

ptrdiff_t _Py_atomic_add_ssize(ptrdiff_t *obj, ptrdiff_t value) {
    return __atomic_fetch_add(obj, value, __ATOMIC_SEQ_CST);
}
