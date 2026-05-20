/** @file h3simd.h
 *  @brief Internal SIMD dispatch interface.
 *
 *  Callers include only this header. The h3SimdXxxBatch entry points resolve
 *  at compile time (see src/h3lib/lib/simd/dispatch.c) to the kernel selected
 *  for the build:
 *
 *    - H3_HAS_AVX2 defined  → AVX2 + FMA kernel (built with -mavx2 -mfma)
 *    - H3_HAS_NEON defined  → NEON kernel for pointInPoly; scalar elsewhere
 *    - neither              → scalar fallback for everything
 *
 *  These macros come from cmake/H3Simd.cmake's check_c_compiler_flag and
 *  check_c_source_compiles probes — they are configure-time test scripts,
 *  not runtime CPU detection. A library compiled with H3_HAS_AVX2 expects
 *  the host CPU to support AVX2+FMA at execution time.
 *
 *  Adding a new kernel:
 *    1. Add a `h3SimdFooBatch(...)` declaration here.
 *    2. Add an implementation under src/h3lib/lib/simd/scalar/foo_scalar.c.
 *    3. Optionally add ISA-specialized variants under
 *       src/h3lib/lib/simd/{avx2,neon}/foo_<isa>.c, guarded by
 *       `#if defined(H3_HAS_AVX2)` / `#if defined(H3_HAS_NEON)`.
 *    4. Wire the public entry point in dispatch.c with a static `#if`.
 *    5. Add the new TUs to cmake/H3Simd.cmake's source lists.
 */

#ifndef H3SIMD_H
#define H3SIMD_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "bbox.h"
#include "latLng.h"

/* ---- Batch API surface ---- */

/**
 * Test n points against a single bounding box. Each output byte is 1 if
 * the corresponding point is contained, 0 otherwise. Inputs are SoA: lats
 * and lngs in separate arrays. n=0 is a no-op.
 *
 * Equivalence policy: bitwise identical to repeated bboxContains() on each
 * point, including transmeridian handling. Pure compares + AND/OR with no
 * floating-point arithmetic, so SIMD and scalar paths agree at every bit.
 */
void h3SimdBboxContainsBatch(const BBox *bbox, size_t n, const double *lats,
                             const double *lngs, uint8_t *out);

/**
 * Test n points against a polygon loop (vertex array) using the same
 * ray-cast algorithm as pointInsideGeoLoop(). Each output byte is 1 if the
 * point is inside. The bbox is used only to detect transmeridian state.
 *
 * Loop is passed as raw vertex array to keep this header free of polygon.h
 * dependencies. Callers can pass `loop->numVerts, loop->verts` from the
 * existing GeoLoop type.
 *
 * Equivalence policy: matches the scalar pointInsideGeoLoop reference at
 * every assertion in src/apps/testapps/testSimdPointInPolyBatchInternal.c
 * across triangle, large polygon, transmeridian, and randomized fuzz inputs.
 * The DBL_EPSILON nudge that the scalar path applies for vertices coincident
 * with a query point is applied per-lane in the SIMD path; the resulting
 * inside/outside bit is identical to scalar except for points that lie on
 * a polygon edge to within ~1e-15 rad, where ULP-level differences in the
 * intersection longitude can flip the bit. No production caller should rely
 * on bit-exact behavior in that regime.
 */
void h3SimdPointInsideGeoLoopBatch(int numVerts, const LatLng *verts,
                                   const BBox *bbox, size_t n,
                                   const double *lats, const double *lngs,
                                   uint8_t *out);

/**
 * Vec3 batch ops. SoA inputs (separate x/y/z arrays). Output arrays may
 * alias inputs.
 *
 * Equivalence policy:
 *   - vec3LinCombBatch, vec3DotBatch: ULP-bounded (≤ 4 ULP) — FMA where
 *     available rearranges a*x + b*y vs (a*x)+(b*y). The 4 ULP bound is
 *     enforced by tests with a 1e-15 absolute floor for catastrophic-
 *     cancellation cases near zero.
 *   - latLngToVec3Batch: bitwise identical to repeated latLngToVec3() —
 *     both paths call libm sin/cos in the same order. Vector trig is not
 *     substituted today; if it ever is, the bound becomes ≤ 4 ULP.
 */
void h3SimdVec3LinCombBatch(double a, const double *x1, const double *y1,
                            const double *z1, double b, const double *x2,
                            const double *y2, const double *z2, size_t n,
                            double *xo, double *yo, double *zo);
void h3SimdVec3DotBatch(const double *x1, const double *y1, const double *z1,
                        const double *x2, const double *y2, const double *z2,
                        size_t n, double *out);
void h3SimdLatLngToVec3Batch(const double *lats, const double *lngs, size_t n,
                             double *xo, double *yo, double *zo);

#endif /* H3SIMD_H */
