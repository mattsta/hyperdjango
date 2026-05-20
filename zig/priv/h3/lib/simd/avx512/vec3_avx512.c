/** @file vec3_avx512.c
 *  @brief AVX-512 implementations of vec3*Batch.
 *
 *  8 doubles per vector — 2× the lane count of AVX2. Used by Intel
 *  hosts that probe positive for AVX-512 F + DQ + FMA. Same precision
 *  guarantees as scalar/NEON/AVX2 (≤180 nm absolute error vs libm).
 */

#include <math.h>
#include <stddef.h>
#include <stdint.h>

#include "approx_libm.h"
#include "h3simd.h"
#include "latLng.h"

#if defined(H3_HAS_AVX512)
#include <immintrin.h>

void h3SimdVec3LinCombBatchAvx512(double a, const double *x1, const double *y1,
                                  const double *z1, double b, const double *x2,
                                  const double *y2, const double *z2, size_t n,
                                  double *xo, double *yo, double *zo) {
    __m512d va = _mm512_set1_pd(a);
    __m512d vb = _mm512_set1_pd(b);
    size_t i = 0;
    for (; i + 8 <= n; i += 8) {
        __m512d x1v = _mm512_loadu_pd(x1 + i), x2v = _mm512_loadu_pd(x2 + i);
        __m512d y1v = _mm512_loadu_pd(y1 + i), y2v = _mm512_loadu_pd(y2 + i);
        __m512d z1v = _mm512_loadu_pd(z1 + i), z2v = _mm512_loadu_pd(z2 + i);
        _mm512_storeu_pd(xo + i,
                         _mm512_fmadd_pd(vb, x2v, _mm512_mul_pd(va, x1v)));
        _mm512_storeu_pd(yo + i,
                         _mm512_fmadd_pd(vb, y2v, _mm512_mul_pd(va, y1v)));
        _mm512_storeu_pd(zo + i,
                         _mm512_fmadd_pd(vb, z2v, _mm512_mul_pd(va, z1v)));
    }
    for (; i < n; i++) {
        xo[i] = a * x1[i] + b * x2[i];
        yo[i] = a * y1[i] + b * y2[i];
        zo[i] = a * z1[i] + b * z2[i];
    }
}

void h3SimdVec3DotBatchAvx512(const double *x1, const double *y1,
                              const double *z1, const double *x2,
                              const double *y2, const double *z2, size_t n,
                              double *out) {
    size_t i = 0;
    for (; i + 8 <= n; i += 8) {
        __m512d x1v = _mm512_loadu_pd(x1 + i), x2v = _mm512_loadu_pd(x2 + i);
        __m512d y1v = _mm512_loadu_pd(y1 + i), y2v = _mm512_loadu_pd(y2 + i);
        __m512d z1v = _mm512_loadu_pd(z1 + i), z2v = _mm512_loadu_pd(z2 + i);
        __m512d acc = _mm512_mul_pd(x1v, x2v);
        acc = _mm512_fmadd_pd(y1v, y2v, acc);
        acc = _mm512_fmadd_pd(z1v, z2v, acc);
        _mm512_storeu_pd(out + i, acc);
    }
    for (; i < n; i++) {
        out[i] = x1[i] * x2[i] + y1[i] * y2[i] + z1[i] * z2[i];
    }
}

void h3SimdLatLngToVec3BatchAvx512(const double *lats, const double *lngs,
                                   size_t n, double *xo, double *yo,
                                   double *zo) {
    /* AVX-512 8-wide path using approx_sincos_avx512. Same precision
     * (≤180 nm absolute error vs libm) — same coefficients as
     * scalar/NEON/AVX2. */
    size_t i = 0;
    for (; i + 8 <= n; i += 8) {
        __m512d vLat = _mm512_loadu_pd(lats + i);
        __m512d vLng = _mm512_loadu_pd(lngs + i);
        __m512d sLat, cLat, sLng, cLng;
        approx_sincos_avx512(vLat, &sLat, &cLat);
        approx_sincos_avx512(vLng, &sLng, &cLng);
        _mm512_storeu_pd(xo + i, _mm512_mul_pd(cLng, cLat));
        _mm512_storeu_pd(yo + i, _mm512_mul_pd(sLng, cLat));
        _mm512_storeu_pd(zo + i, sLat);
    }
    /* Tail: scalar approx_sincos_unchecked for the last 0-7 elements. */
    for (; i < n; i++) {
        double sLat, cLat, sLng, cLng;
        approx_sincos_unchecked(lats[i], &sLat, &cLat);
        approx_sincos_unchecked(lngs[i], &sLng, &cLng);
        xo[i] = cLng * cLat;
        yo[i] = sLng * cLat;
        zo[i] = sLat;
    }
}

#endif /* H3_HAS_AVX512 */
