/** @file dispatch.c
 *  @brief SIMD kernel dispatch — purely compile-time.
 *
 *  There is NO runtime CPU detection. The choice between scalar / AVX2 / NEON
 *  is baked at configure time by cmake/H3Simd.cmake (which test-compiles
 *  the relevant intrinsics) and surfaces here as the macros H3_HAS_AVX2 and
 *  H3_HAS_NEON. Each public entry-point is a thin static `#if` that forwards
 *  to the right kernel:
 *
 *    | kernel              | aarch64 (NEON build) | x86_64 (AVX2 build) | other
 * |
 *    |---------------------|----------------------|---------------------|--------|
 *    | bboxContainsBatch   | scalar*              | avx2                |
 * scalar | | pointInPolyBatch    | NEON                 | avx2                |
 * scalar | | vec3LinCombBatch    | scalar*              | avx2                |
 * scalar | | vec3DotBatch        | scalar*              | avx2                |
 * scalar | | latLngToVec3Batch   | scalar*              | avx2                |
 * scalar |
 *
 *    * the scalar batch path is what the AArch64 audit (bench_results/ab_*)
 *      selected: the compiler's autovec of the inlined SoA loop matched or
 *      beat hand-rolled NEON for these kernels, so manual NEON is not shipped.
 *
 *  Consequences of compile-time-only:
 *    - A library compiled with H3_HAS_AVX2 will SIGILL on a CPU without AVX2.
 *      The user opts in to that contract by enabling H3_ENABLE_SIMD on a
 *      build target whose ISA flag the compiler accepts.
 *    - There is no global initialization, no function-pointer table, no
 *      lock-free racy first-call init, no atomic stores. Every public entry
 *      point is a direct call resolved at link time.
 *    - Single-threaded callers, multi-threaded callers, and signal-handler
 *      callers all see the same kernel: the one chosen at compile time.
 *
 *  Adding a kernel: add its scalar variant in src/h3lib/lib/simd/scalar/, add
 *  optional ISA-specific variants in {avx2,neon}/, declare them in the extern
 *  blocks below, and add a thin #if dispatch as a public entry point at the
 *  bottom whose per-arch routing reflects the audit verdict.
 */

#include "h3simd.h"

/* ---- Scalar reference kernels (always available) ---- */
extern void h3SimdBboxContainsBatchScalar(const BBox *bbox, size_t n,
                                          const double *lats,
                                          const double *lngs, uint8_t *out);
extern void h3SimdPointInsideGeoLoopBatchScalar(
    int numVerts, const LatLng *verts, const BBox *bbox, size_t n,
    const double *lats, const double *lngs, uint8_t *out);
extern void h3SimdVec3LinCombBatchScalar(double a, const double *x1,
                                         const double *y1, const double *z1,
                                         double b, const double *x2,
                                         const double *y2, const double *z2,
                                         size_t n, double *xo, double *yo,
                                         double *zo);
extern void h3SimdVec3DotBatchScalar(const double *x1, const double *y1,
                                     const double *z1, const double *x2,
                                     const double *y2, const double *z2,
                                     size_t n, double *out);
extern void h3SimdLatLngToVec3BatchScalar(const double *lats,
                                          const double *lngs, size_t n,
                                          double *xo, double *yo, double *zo);

/* ---- AArch64 / NEON kernels (compiled in only when H3_HAS_NEON) ---- */
#if defined(H3_HAS_NEON)
extern void h3SimdPointInsideGeoLoopBatchNeon(int numVerts, const LatLng *verts,
                                              const BBox *bbox, size_t n,
                                              const double *lats,
                                              const double *lngs, uint8_t *out);
#endif

/* ---- x86_64 / AVX2 kernels (compiled in only when H3_HAS_AVX2) ---- */
#if defined(H3_HAS_AVX2)
extern void h3SimdBboxContainsBatchAvx2(const BBox *bbox, size_t n,
                                        const double *lats, const double *lngs,
                                        uint8_t *out);
extern void h3SimdPointInsideGeoLoopBatchAvx2(int numVerts, const LatLng *verts,
                                              const BBox *bbox, size_t n,
                                              const double *lats,
                                              const double *lngs, uint8_t *out);
extern void h3SimdVec3LinCombBatchAvx2(double a, const double *x1,
                                       const double *y1, const double *z1,
                                       double b, const double *x2,
                                       const double *y2, const double *z2,
                                       size_t n, double *xo, double *yo,
                                       double *zo);
extern void h3SimdVec3DotBatchAvx2(const double *x1, const double *y1,
                                   const double *z1, const double *x2,
                                   const double *y2, const double *z2, size_t n,
                                   double *out);
extern void h3SimdLatLngToVec3BatchAvx2(const double *lats, const double *lngs,
                                        size_t n, double *xo, double *yo,
                                        double *zo);
#endif

/* ---- x86_64 / AVX-512 kernels (compiled in only when H3_HAS_AVX512) ---- */
#if defined(H3_HAS_AVX512)
extern void h3SimdVec3LinCombBatchAvx512(double a, const double *x1,
                                         const double *y1, const double *z1,
                                         double b, const double *x2,
                                         const double *y2, const double *z2,
                                         size_t n, double *xo, double *yo,
                                         double *zo);
extern void h3SimdVec3DotBatchAvx512(const double *x1, const double *y1,
                                     const double *z1, const double *x2,
                                     const double *y2, const double *z2,
                                     size_t n, double *out);
extern void h3SimdLatLngToVec3BatchAvx512(const double *lats,
                                          const double *lngs, size_t n,
                                          double *xo, double *yo, double *zo);
#endif

/* ---- Public entry points ---- */

void h3SimdBboxContainsBatch(const BBox *bbox, size_t n, const double *lats,
                             const double *lngs, uint8_t *out) {
#if defined(H3_HAS_AVX2)
    h3SimdBboxContainsBatchAvx2(bbox, n, lats, lngs, out);
#else
    /* AArch64 audit: manual NEON measured 0.69-0.91x vs autovec'd scalar.
     * Route to scalar; the compiler vectorizes the inlined SoA loop. */
    h3SimdBboxContainsBatchScalar(bbox, n, lats, lngs, out);
#endif
}

void h3SimdPointInsideGeoLoopBatch(int numVerts, const LatLng *verts,
                                   const BBox *bbox, size_t n,
                                   const double *lats, const double *lngs,
                                   uint8_t *out) {
#if defined(H3_HAS_AVX2)
    h3SimdPointInsideGeoLoopBatchAvx2(numVerts, verts, bbox, n, lats, lngs,
                                      out);
#elif defined(H3_HAS_NEON)
    h3SimdPointInsideGeoLoopBatchNeon(numVerts, verts, bbox, n, lats, lngs,
                                      out);
#else
    h3SimdPointInsideGeoLoopBatchScalar(numVerts, verts, bbox, n, lats, lngs,
                                        out);
#endif
}

void h3SimdVec3LinCombBatch(double a, const double *x1, const double *y1,
                            const double *z1, double b, const double *x2,
                            const double *y2, const double *z2, size_t n,
                            double *xo, double *yo, double *zo) {
#if defined(H3_HAS_AVX512)
    h3SimdVec3LinCombBatchAvx512(a, x1, y1, z1, b, x2, y2, z2, n, xo, yo, zo);
#elif defined(H3_HAS_AVX2)
    h3SimdVec3LinCombBatchAvx2(a, x1, y1, z1, b, x2, y2, z2, n, xo, yo, zo);
#else
    /* AArch64 audit: NEON 0.81-1.60x (mixed). Scalar+autovec ties on every
     * non-pathological size and avoids ULP divergence from FMA reordering. */
    h3SimdVec3LinCombBatchScalar(a, x1, y1, z1, b, x2, y2, z2, n, xo, yo, zo);
#endif
}

void h3SimdVec3DotBatch(const double *x1, const double *y1, const double *z1,
                        const double *x2, const double *y2, const double *z2,
                        size_t n, double *out) {
#if defined(H3_HAS_AVX512)
    h3SimdVec3DotBatchAvx512(x1, y1, z1, x2, y2, z2, n, out);
#elif defined(H3_HAS_AVX2)
    h3SimdVec3DotBatchAvx2(x1, y1, z1, x2, y2, z2, n, out);
#else
    /* AArch64 audit: NEON 0.98-1.00x (no win). Route to scalar+autovec. */
    h3SimdVec3DotBatchScalar(x1, y1, z1, x2, y2, z2, n, out);
#endif
}

void h3SimdLatLngToVec3Batch(const double *lats, const double *lngs, size_t n,
                             double *xo, double *yo, double *zo) {
#if defined(H3_HAS_AVX512)
    h3SimdLatLngToVec3BatchAvx512(lats, lngs, n, xo, yo, zo);
#elif defined(H3_HAS_AVX2)
    h3SimdLatLngToVec3BatchAvx2(lats, lngs, n, xo, yo, zo);
#else
    /* AArch64 audit: NEON 1.00-1.01x (libm trig is the bottleneck on both
     * paths). Route to scalar; vector trig substitution would be a separate
     * decision documented in dev-docs/simd.md. */
    h3SimdLatLngToVec3BatchScalar(lats, lngs, n, xo, yo, zo);
#endif
}
