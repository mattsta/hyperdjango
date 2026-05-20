const std = @import("std");

pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    // Default to ReleaseFast — Debug is far too slow for the HTTP core and was
    // never what we want to ship. Unlike standardOptimizeOption (which only maps
    // --release to a preferred mode and otherwise stays Debug), this makes a bare
    // `zig build` release by default; pass -Doptimize=Debug to opt out.
    // Tip: add -Dcpu=native for a machine-tuned (non-portable) artifact;
    // shipped builds stay on the generic baseline.
    const optimize = b.option(
        std.builtin.OptimizeMode,
        "optimize",
        "Prioritize performance, safety, or binary size (default: ReleaseFast)",
    ) orelse .ReleaseFast;

    // ── Python configuration ──
    const py_version = b.option([]const u8, "python", "Python label: 3.13, 3.14, or 3.14t") orelse "3.13";
    const is_free_threaded = std.mem.eql(u8, py_version, "3.14t");

    const include_path = b.option([]const u8, "py-include", "Python include path") orelse
        @panic("pass -Dpy-include=<path> or use: python zig/build_hyperdjango.py");
    const lib_path = b.option([]const u8, "py-libdir", "Python lib path") orelse
        @panic("pass -Dpy-libdir=<path> or use: python zig/build_hyperdjango.py");

    const py_lib_name: []const u8 = if (is_free_threaded)
        "python3.14t"
    else if (std.mem.eql(u8, py_version, "3.14"))
        "python3.14"
    else
        "python3.13";

    // ── build options (importable as `build_options`) ──
    // heap-safety swaps the raw c_allocator for Zig's safety-checking
    // DebugAllocator on the pool/db path (double-free / UAF / OOB detection).
    // Default false: production keeps the fast, bookkeeping-free c_allocator.
    const heap_safety = b.option(
        bool,
        "heap-safety",
        "Use the safety-checking DebugAllocator on the pool/db path (default: false)",
    ) orelse false;
    // sanitize-thread compiles the Zig code with ThreadSanitizer. It requires a
    // ThreadSanitizer-instrumented CPython to actually RUN (the stock free-
    // threaded interpreter SIGSEGVs under TSan), so it is only ever set by the
    // deep-validation TSan lane, never by a production build. Default false.
    const sanitize_thread = b.option(
        bool,
        "sanitize-thread",
        "Compile with ThreadSanitizer (needs a --with-thread-sanitizer CPython; default: false)",
    ) orelse false;
    const build_options = b.addOptions();
    build_options.addOption(bool, "heap_safety", heap_safety);
    build_options.addOption(bool, "sanitize_thread", sanitize_thread);
    const build_options_mod = build_options.createModule();

    // ── Incorporated modules (all source lives in src/) ──

    // dhi SIMD validation — src/dhi/
    const validator_mod = b.createModule(.{
        .root_source_file = b.path("src/dhi/validator.zig"),
        .target = target,
        .optimize = optimize,
    });
    const json_validator_mod = b.createModule(.{
        .root_source_file = b.path("src/dhi/json_validator.zig"),
        .target = target,
        .optimize = optimize,
    });
    json_validator_mod.addImport("validator", validator_mod);

    // buffer.zig — src/buffer/
    const buffer_mod = b.createModule(.{
        .root_source_file = b.path("src/buffer/buffer.zig"),
        .target = target,
        .optimize = optimize,
    });

    // metrics.zig — src/metrics/
    const metrics_mod = b.createModule(.{
        .root_source_file = b.path("src/metrics/metrics.zig"),
        .target = target,
        .optimize = optimize,
        .link_libc = true,
        .sanitize_thread = sanitize_thread,
    });

    // pg.zig native PostgreSQL driver — src/pg/
    const pg_mod = b.createModule(.{
        .root_source_file = b.path("src/pg/pg.zig"),
        .target = target,
        .optimize = optimize,
        .link_libc = true,
        .sanitize_thread = sanitize_thread,
    });
    pg_mod.addImport("buffer", buffer_mod);
    pg_mod.addImport("metrics", metrics_mod);
    pg_mod.addImport("build_options", build_options_mod);

    // ── shared library (_hyperdjango_native) ──
    const lib = b.addLibrary(.{
        .name = "hyperdjango",
        .linkage = .dynamic,
        .root_module = b.createModule(.{
            .root_source_file = b.path("src/main.zig"),
            .target = target,
            .optimize = optimize,
            .link_libc = true,
            .sanitize_thread = sanitize_thread,
        }),
    });

    // LTO ONLY on the production artifact (ReleaseFast). ReleaseSafe and Debug
    // are our diagnostic builds — full LTO inlines across every module and
    // mangles the DWARF the panic handler walks, so a Zig safety panic on a
    // worker thread prints bare hex ("0x… in ??? (???)") instead of file:line,
    // which is exactly why the CI ReleaseSafe crashes were unsymbolizable.
    // Keeping LTO off there yields real, symbolized stack traces. (Still skip
    // even on ReleaseFast for the macOS free-threaded path, whose
    // `-undefined dynamic_lookup` link doesn't cooperate with LTO codegen.)
    if (optimize == .ReleaseFast and !(is_free_threaded and target.result.os.tag == .macos)) {
        lib.lto = .full;
    }
    // Preserve debug info in the non-production builds so panics symbolize.
    if (optimize != .ReleaseFast) {
        lib.root_module.strip = false;
    }

    lib.root_module.addImport("validator", validator_mod);
    lib.root_module.addImport("json_validator", json_validator_mod);
    lib.root_module.addImport("pg", pg_mod);
    lib.root_module.addImport("build_options", build_options_mod);

    lib.root_module.addIncludePath(.{ .cwd_relative = include_path });
    lib.root_module.addRPathSpecial("@loader_path");

    // ── Compile the H3 library into the native ext (per-ISA SIMD) ──
    addH3(b, lib.root_module, target);

    if (is_free_threaded) {
        // Free-threaded needs the atomic shim, but on macOS we MUST NOT
        // hard-link libpython into the .so — recording an LC_LOAD_DYLIB
        // for libpython (any path: absolute or @rpath) causes macOS dyld
        // to map a second copy of libpython distinct from the one already
        // owned by the python3.14t binary. The two copies then have two
        // distinct &PyModuleDef_Type symbols and PyModuleDef_Init writes
        // ob_type pointing into copy A while the runtime's
        // _PyImport_RunModInitFunc compares against copy B, producing
        // "did not return an extension module" SystemError on import.
        //
        // Match what setuptools/distutils does for CPython C extensions
        // on macOS: -undefined dynamic_lookup. Symbols resolve from the
        // already-loaded libpython at dlopen time. The atomic shim
        // itself doesn't pull in libpython symbols — it implements
        // _Py_atomic_* / _Py_ThreadId via raw compiler builtins.
        if (target.result.os.tag == .macos) {
            lib.linker_allow_shlib_undefined = true;
        } else {
            lib.root_module.addLibraryPath(.{ .cwd_relative = lib_path });
            lib.root_module.linkSystemLibrary(py_lib_name, .{});
        }
        lib.root_module.addCSourceFile(.{
            .file = b.path("src/py_atomic_shim.c"),
            .flags = &.{ "-I", include_path },
        });
    } else {
        // Standard: allow undefined (symbols resolve at import time)
        lib.linker_allow_shlib_undefined = true;
    }

    b.installArtifact(lib);

    // ── unit tests ──
    const tests = b.addTest(.{
        .root_module = b.createModule(.{
            .root_source_file = b.path("src/main.zig"),
            .target = target,
            .optimize = optimize,
            .link_libc = true,
            .sanitize_thread = sanitize_thread,
        }),
    });
    tests.root_module.addImport("validator", validator_mod);
    tests.root_module.addImport("json_validator", json_validator_mod);
    tests.root_module.addImport("pg", pg_mod);
    tests.root_module.addImport("build_options", build_options_mod);
    tests.root_module.addIncludePath(.{ .cwd_relative = include_path });
    tests.root_module.addLibraryPath(.{ .cwd_relative = lib_path });
    tests.root_module.linkSystemLibrary(py_lib_name, .{});
    if (is_free_threaded) {
        // The free-threaded runtime's out-of-line _Py_atomic_* helpers come
        // from our shim. The test binary links libpython directly (no macOS
        // dynamic_lookup), so it must compile the shim in too — mirrors the
        // library target above, otherwise linking fails with undefined
        // _Py_atomic_* symbols.
        tests.root_module.addCSourceFile(.{
            .file = b.path("src/py_atomic_shim.c"),
            .flags = &.{ "-I", include_path },
        });
    }
    addH3(b, tests.root_module, target);

    const run_tests = b.addRunArtifact(tests);
    const test_step = b.step("test", "Run unit tests");
    test_step.dependOn(&run_tests.step);

    // ── pg-module unit tests ──
    // The pg driver lives in its own module (imported as `pg` above), so the
    // `test` artifact rooted at main.zig never collects its ~110 tests. Wire a
    // dedicated artifact rooted at the pg test aggregator; it needs a live
    // PostgreSQL (env: PGHOST/PGPORT/PGUSER/PGDATABASE, see src/pg/t.zig).
    const pg_tests = b.addTest(.{
        .root_module = b.createModule(.{
            .root_source_file = b.path("src/pg/test_all.zig"),
            .target = target,
            .optimize = optimize,
            .link_libc = true,
        }),
    });
    pg_tests.root_module.addImport("buffer", buffer_mod);
    pg_tests.root_module.addImport("metrics", metrics_mod);
    const run_pg_tests = b.addRunArtifact(pg_tests);
    const test_pg_step = b.step("test-pg", "Run pg-driver unit tests (needs live PostgreSQL)");
    test_pg_step.dependOn(&run_pg_tests.step);
}

/// Compile the vendored H3 library into `mod`, with per-ISA SIMD acceleration.
///
/// The H3 sources live in-tree under `priv/h3/` (a vendored copy of the
/// mattsta/h3 v4 fork — see `priv/h3/README.md` for provenance and the
/// re-vendoring recipe). Nothing outside the repo is referenced.
///
/// SIMD selection is COMPILE-TIME (H3's `simd/dispatch.c` has no runtime CPU
/// probe): the kernels we compile must match the `H3_HAS_*` macros we define,
/// and both must match what the resolved *target* CPU can actually execute. We
/// key off Zig's target CPU features rather than the fork's CMake `check_c_*`
/// probes — this is exact for the resolved `-Dtarget` (default = the host) and
/// cannot SIGILL, because we never enable an ISA the target lacks.
///
///   * aarch64: NEON is baseline (always present), no `-m` flag. Per the fork's
///     A/B audit only the pointInPoly kernel ships hand-NEON; bbox/vec3/latLng
///     route to the compiler's autovec of the scalar TUs. → `-DH3_HAS_NEON=1`.
///   * x86_64: AVX2 (`-mavx2 -mfma`) and, where the target has it, AVX-512
///     (`-mavx512f -mavx512dq -mfma`) — gated on the target's feature set.
///   * anything else: the scalar TUs (which always compile) — dispatch.c becomes
///     a no-op forwarder to them.
///
/// Discipline: dispatch.c gates on `#if defined(H3_HAS_AVX2|AVX512|NEON)` —
/// DEFINEDNESS, not value. The macro is defined on EVERY H3 TU (so dispatch.c,
/// itself compiled WITHOUT the `-m` flags, statically routes to the kernel),
/// while the ISA `-m` flags apply ONLY to that ISA's kernel TU. Each kernel TU
/// is additionally `#if`-guarded on its own macro, so it is a harmless empty TU
/// if the macro is absent.
///
/// `priv/h3/include/` is a single flattened include dir holding the hand-written
/// headers AND the build-generated `h3api.h` (version/feature placeholders
/// filled — without it `@cImport`/`#include "h3api.h"` would see only the `.in`
/// template). H3 is pure C99 with no external deps; `H3_EXPORT` resolves to
/// UNPREFIXED symbols so they link directly against the Zig externs in src/h3.zig.
fn addH3(b: *std.Build, mod: *std.Build.Module, target: std.Build.ResolvedTarget) void {
    // Module include path applies to every C source compiled into `mod`.
    mod.addIncludePath(b.path("priv/h3/include"));

    const arch = target.result.cpu.arch;
    const feats = target.result.cpu.features;
    const has_avx2 = arch == .x86_64 and
        std.Target.x86.featureSetHas(feats, .avx2) and
        std.Target.x86.featureSetHas(feats, .fma);
    const has_avx512 = has_avx2 and
        std.Target.x86.featureSetHas(feats, .avx512f) and
        std.Target.x86.featureSetHas(feats, .avx512dq);

    // Base TUs: the 18 LIB_SOURCE_FILES (CMakeLists.txt:241-258) + dispatch.c +
    // the scalar kernels (always compiled). Their flags carry the ISA macro so
    // dispatch.c routes to the right kernel; they carry NO `-m` ISA flag.
    const base_sources = [_][]const u8{
        // LIB_SOURCE_FILES (.c)
        "h3Assert.c",       "algos.c",
        "bbox.c",           "polygon.c",
        "polyfill.c",       "h3Index.c",
        "vec2d.c",          "vertex.c",
        "linkedGeo.c",      "localij.c",
        "latLng.c",         "directedEdge.c",
        "mathExtensions.c", "iterators.c",
        "faceijk.c",        "baseCells.c",
        "area.c",           "cellsToMultiPoly.c",
        // Scalar SIMD (H3_SIMD_SCALAR_SOURCES) — always present; dispatch.c
        // forwards here for any kernel/arch without a hand-vectorized TU.
        "simd/dispatch.c",
        "simd/scalar/bbox_scalar.c",
        "simd/scalar/pointInPoly_scalar.c",
        "simd/scalar/vec3_scalar.c",
    };
    const base_flags: []const []const u8 = if (arch == .aarch64)
        &.{ "-std=gnu11", "-DH3_HAS_NEON=1" }
    else if (has_avx512)
        &.{ "-std=gnu11", "-DH3_HAS_AVX2=1", "-DH3_HAS_AVX512=1" }
    else if (has_avx2)
        &.{ "-std=gnu11", "-DH3_HAS_AVX2=1" }
    else
        &.{"-std=gnu11"};
    mod.addCSourceFiles(.{
        .root = b.path("priv/h3/lib"),
        .files = &base_sources,
        .flags = base_flags,
    });

    // Per-ISA kernel TUs — compiled with their `-m` flag, only for the ISAs the
    // resolved target supports.
    if (arch == .aarch64) {
        mod.addCSourceFiles(.{
            .root = b.path("priv/h3/lib"),
            .files = &.{"simd/neon/pointInPoly_neon.c"},
            .flags = &.{ "-std=gnu11", "-DH3_HAS_NEON=1" }, // NEON is baseline: no -m flag
        });
    }
    if (has_avx2) {
        mod.addCSourceFiles(.{
            .root = b.path("priv/h3/lib"),
            .files = &.{
                "simd/avx2/bbox_avx2.c",
                "simd/avx2/pointInPoly_avx2.c",
                "simd/avx2/vec3_avx2.c",
            },
            .flags = &.{ "-std=gnu11", "-mavx2", "-mfma", "-DH3_HAS_AVX2=1" },
        });
    }
    if (has_avx512) {
        mod.addCSourceFiles(.{
            .root = b.path("priv/h3/lib"),
            .files = &.{"simd/avx512/vec3_avx512.c"},
            .flags = &.{ "-std=gnu11", "-mavx512f", "-mavx512dq", "-mfma", "-DH3_HAS_AVX2=1", "-DH3_HAS_AVX512=1" },
        });
    }
}
