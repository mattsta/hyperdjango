/** @file vec3_scalar.c
 *  @brief Scalar reference for vec3*Batch and latLngToVec3Batch.
 *
 *  Plain straight-line C; the compiler is free to (and does) auto-vectorize
 *  these. The point of having explicit batch entries even at the scalar
 *  level is that callers don't pay function-call overhead per element.
 */

#include <math.h>
#include <stddef.h>
#include <stdint.h>

#include "approx_libm.h"
#include "h3simd.h"
#include "latLng.h"

void h3SimdVec3LinCombBatchScalar(double a, const double *x1, const double *y1,
                                  const double *z1, double b, const double *x2,
                                  const double *y2, const double *z2, size_t n,
                                  double *xo, double *yo, double *zo) {
    for (size_t i = 0; i < n; i++) {
        xo[i] = a * x1[i] + b * x2[i];
        yo[i] = a * y1[i] + b * y2[i];
        zo[i] = a * z1[i] + b * z2[i];
    }
}

void h3SimdVec3DotBatchScalar(const double *x1, const double *y1,
                              const double *z1, const double *x2,
                              const double *y2, const double *z2, size_t n,
                              double *out) {
    for (size_t i = 0; i < n; i++) {
        out[i] = x1[i] * x2[i] + y1[i] * y2[i] + z1[i] * z2[i];
    }
}

void h3SimdLatLngToVec3BatchScalar(const double *lats, const double *lngs,
                                   size_t n, double *xo, double *yo,
                                   double *zo) {
    /* Uses the branchless _unchecked variant: the API contract on lats/lngs
     * is "valid lat/lng radians" (|x| ≤ ~6.3) so the safety fallback in
     * approx_sincos never fires. The branchless form lets the
     * autovectorizer hoist the polynomial into a packed SIMD chain at
     * large N, which the safety branch was defeating. */
    for (size_t i = 0; i < n; i++) {
        double sLat, cLat, sLng, cLng;
        approx_sincos_unchecked(lats[i], &sLat, &cLat);
        approx_sincos_unchecked(lngs[i], &sLng, &cLng);
        xo[i] = cLng * cLat;
        yo[i] = sLng * cLat;
        zo[i] = sLat;
    }
}
