# Vendored H3 (mattsta/h3 fork)

This directory is a vendored copy of the H3 geospatial library from the
[mattsta/h3 fork](https://github.com/mattsta/h3), compiled directly into the
`_hyperdjango_native` extension (see
`zig/build.zig` → `addH3`). It backs the `_h3_*` Python surface used by MESH's
indexed candidate recall (ADR-0026).

## Provenance

- Source: the [mattsta/h3 fork](https://github.com/mattsta/h3).
- Version: `4.4.1` (`git describe`: `v4.4.1-41-g413f9983`, commit `413f9983`).
- License: Apache-2.0 — see `LICENSE`, `NOTICE`, `LICENSE-mattsta`.

## What's here

- `include/` — the hand-written H3 headers **plus** the build-generated
  `h3api.h` (version/feature placeholders resolved; upstream ships only the
  `h3api.h.in` template, which is kept here for reference). Flattened into one
  directory so the build needs a single include path.
- `lib/*.c` — the 18 `LIB_SOURCE_FILES` (`CMakeLists.txt`).
- `lib/simd/dispatch.c` + `lib/simd/scalar/*.c` — the compile-time dispatcher and
  the always-available scalar kernels.
- `lib/simd/{avx2,avx512,neon}/*.c` — the per-ISA SIMD kernels. `build.zig` →
  `addH3` compiles the ones the resolved target CPU supports (NEON on aarch64,
  AVX2/AVX-512 on x86_64), gated on the target's feature set so a build never
  enables an ISA the target can't run.

## SIMD selection

H3's `dispatch.c` picks a kernel at COMPILE TIME via the `H3_HAS_AVX2` /
`H3_HAS_AVX512` / `H3_HAS_NEON` macros — there is no runtime CPU probe. `addH3`
defines the right macro for the target arch and attaches the matching `-m` flags
to just that ISA's kernel TU (NEON needs none — it is baseline on aarch64). See
the `addH3` doc comment in `zig/build.zig` for the full discipline.

## Re-vendoring / updating

Re-copy from an H3 fork checkout: headers from `src/h3lib/include` + the
generated `build_native/src/h3lib/include/h3api.h`, the 18 `lib/*.c`
`LIB_SOURCE_FILES`, and all of `lib/simd/` (dispatch + scalar + the per-ISA
kernel dirs). No CMake/config step is needed to build — `addH3` compiles the C
directly.
