/** @file h3omp.h
 *  @brief Internal OpenMP integration macros for bulk APIs.
 *
 *  When the library is configured with `-DH3_ENABLE_OPENMP=ON` and the
 *  toolchain's CMake `find_package(OpenMP)` succeeded, the `H3_OPENMP`
 *  preprocessor macro is defined on every TU. The macros below expand to
 *  `_Pragma("omp ...")` annotations in that case, and to nothing otherwise.
 *
 *  The threshold below the parallel-for kicks in keeps the OMP fork/join
 *  overhead amortized — for tiny N the serial path is faster. The
 *  threshold is conservative; real-world bulk decoders typically operate
 *  at N >> 1024 (whole map tiles, query result sets) where the parallel
 *  speedup is near-linear with core count.
 *
 *  Bulk APIs that wrap their per-cell scalar pre-pass with
 *  `H3_OMP_PARALLEL_FOR_LARGE`:
 *    - `cellsToLatLngs` (Stage 1: scalar `_h3ToFaceIjk` per cell)
 *    - `cellsToBoundaries` (Stages A, B, D: per-cell scalar work + post)
 *    - `latLngsToCells` (Stage 2: per-cell `vec3ToCell`)
 *
 *  These cells are fully independent across iterations so parallelization
 *  is data-race-free without any locking. The SIMD trig batches (Stage C
 *  in cellsToBoundaries, Stage 2 in cellsToLatLngs) are not parallelized
 *  here — the SIMD primitives already keep the FPU busy and OMP fork
 *  overhead would dwarf the gain for the relatively short trig pass.
 */

#ifndef H3OMP_H
#define H3OMP_H

/** Crossover point below which OMP fork/join overhead exceeds the parallel
 *  speedup. Calibrated on Apple Silicon at n ≈ 1024 cells (per-cell scalar
 *  work is tens of nanoseconds; a 2-thread fork/join is ~5–10 µs).
 *  Conservative — pushing this too low penalizes small-batch callers. */
#define H3_OMP_PARALLEL_THRESHOLD 1024

#if defined(H3_OPENMP)
#include <omp.h>

/* `_Pragma` takes a string literal but doesn't substitute macro parameters
 * inside its argument; the indirection through `H3_DO_PRAGMA(x)` uses `#x`
 * stringification so that `n_var` (the loop-count expression passed by the
 * caller) and the threshold macro both expand to tokens before the string
 * is built. */
#define H3_DO_PRAGMA(x) _Pragma(#x)

/* Mark a `for` loop for OpenMP parallel execution when n exceeds the
 * threshold. The `if(...)` clause does the runtime check inside OpenMP
 * itself — for small n the runtime collapses the parallel construct to a
 * serial loop with negligible overhead. */
#define H3_OMP_PARALLEL_FOR_LARGE(n_var)                                       \
    H3_DO_PRAGMA(omp parallel for if(n_var >= H3_OMP_PARALLEL_THRESHOLD))

/* Annotate the next assignment as an atomic write so concurrent threads
 * inside a parallel-for can race to set a shared error flag without
 * undefined behavior. The first writer wins (we don't need ordering). */
#define H3_OMP_ATOMIC_WRITE _Pragma("omp atomic write")

#else /* H3_OPENMP not defined */
#define H3_OMP_PARALLEL_FOR_LARGE(n_var)
#define H3_OMP_ATOMIC_WRITE
#endif

#endif /* H3OMP_H */
