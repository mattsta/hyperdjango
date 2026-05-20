/** @file pointInPoly_avx2.c
 *  @brief AVX2 + FMA implementation of h3SimdPointInsideGeoLoopBatch.
 *
 *  4 doubles per vector. Same algorithm as the NEON kernel, wider lanes.
 */

#include <float.h>
#include <math.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>

#include "bbox.h"
#include "h3simd.h"
#include "latLng.h"

#if defined(H3_HAS_AVX2)
#include <immintrin.h>

#ifndef M_2PI
#define M_2PI 6.28318530717958647692528676655900576839433L
#endif

/* Small-batch stack scratch threshold — see pointInPoly_scalar.c. */
#define H3_PIP_STACK_SCRATCH 64

void h3SimdPointInsideGeoLoopBatchAvx2(int numVerts, const LatLng *verts,
                                       const BBox *bbox, size_t n,
                                       const double *lats, const double *lngs,
                                       uint8_t *out) {
    if (n == 0 || numVerts == 0) {
        for (size_t i = 0; i < n; i++) {
            out[i] = 0;
        }
        return;
    }
    bool isTM = bboxIsTransmeridian(bbox);

    double stackLat[H3_PIP_STACK_SCRATCH];
    double stackLng[H3_PIP_STACK_SCRATCH];
    double *plat;
    double *plng;
    if (n <= H3_PIP_STACK_SCRATCH) {
        plat = stackLat;
        plng = stackLng;
    } else {
        plat = malloc(n * sizeof(double));
        plng = malloc(n * sizeof(double));
        if (!plat || !plng) {
            free(plat);
            free(plng);
            for (size_t i = 0; i < n; i++) {
                out[i] = 0;
            }
            return;
        }
    }
    double m2pi = (double)M_2PI;
    for (size_t i = 0; i < n; i++) {
        plat[i] = lats[i];
        double l = lngs[i];
        plng[i] = (isTM && l < 0.0) ? (l + m2pi) : l;
        out[i] = 0;
    }

    __m256d veps = _mm256_set1_pd(DBL_EPSILON);
    __m256d vnegeps = _mm256_set1_pd(-DBL_EPSILON);
    __m256d v2pi = _mm256_set1_pd(m2pi);
    __m256d vzero = _mm256_setzero_pd();

    for (int e = 0; e < numVerts; e++) {
        LatLng a = verts[e];
        LatLng b = verts[(e + 1) % numVerts];
        if (a.lat > b.lat) {
            LatLng t = a;
            a = b;
            b = t;
        }
        double aLng = (isTM && a.lng < 0.0) ? a.lng + m2pi : a.lng;
        double bLng = (isTM && b.lng < 0.0) ? b.lng + m2pi : b.lng;

        __m256d vaLat = _mm256_set1_pd(a.lat);
        __m256d vbLat = _mm256_set1_pd(b.lat);
        __m256d vaLng = _mm256_set1_pd(aLng);
        __m256d vbLng = _mm256_set1_pd(bLng);
        __m256d vlatRange = _mm256_sub_pd(vbLat, vaLat);
        __m256d vlngDelta = _mm256_sub_pd(vbLng, vaLng);

        size_t i = 0;
        for (; i + 4 <= n; i += 4) {
            __m256d lat = _mm256_loadu_pd(plat + i);
            __m256d lng = _mm256_loadu_pd(plng + i);

            __m256d latEqA = _mm256_cmp_pd(lat, vaLat, _CMP_EQ_OQ);
            __m256d latEqB = _mm256_cmp_pd(lat, vbLat, _CMP_EQ_OQ);
            __m256d latNudge = _mm256_or_pd(latEqA, latEqB);
            __m256d latNudgeAmt = _mm256_and_pd(latNudge, veps);
            lat = _mm256_add_pd(lat, latNudgeAmt);
            _mm256_storeu_pd(plat + i, lat);

            __m256d skip = _mm256_or_pd(_mm256_cmp_pd(lat, vaLat, _CMP_LT_OQ),
                                        _mm256_cmp_pd(lat, vbLat, _CMP_GT_OQ));

            __m256d lngEqA = _mm256_cmp_pd(vaLng, lng, _CMP_EQ_OQ);
            __m256d lngEqB = _mm256_cmp_pd(vbLng, lng, _CMP_EQ_OQ);
            __m256d lngNudge = _mm256_or_pd(lngEqA, lngEqB);
            __m256d lngNudgeAmt = _mm256_and_pd(lngNudge, vnegeps);
            lng = _mm256_add_pd(lng, lngNudgeAmt);
            _mm256_storeu_pd(plng + i, lng);

            __m256d ratio = _mm256_div_pd(_mm256_sub_pd(lat, vaLat), vlatRange);
            __m256d testLng = _mm256_fmadd_pd(vlngDelta, ratio, vaLng);
            if (isTM) {
                __m256d neg = _mm256_cmp_pd(testLng, vzero, _CMP_LT_OQ);
                __m256d add = _mm256_and_pd(neg, v2pi);
                testLng = _mm256_add_pd(testLng, add);
            }
            __m256d cross = _mm256_cmp_pd(testLng, lng, _CMP_GT_OQ);
            __m256d toggle = _mm256_andnot_pd(skip, cross);
            int bits = _mm256_movemask_pd(toggle);
            out[i + 0] ^= (uint8_t)((bits >> 0) & 1);
            out[i + 1] ^= (uint8_t)((bits >> 1) & 1);
            out[i + 2] ^= (uint8_t)((bits >> 2) & 1);
            out[i + 3] ^= (uint8_t)((bits >> 3) & 1);
        }
        for (; i < n; i++) {
            double lat = plat[i];
            double lng = plng[i];
            if (lat == a.lat || lat == b.lat) {
                lat += DBL_EPSILON;
                plat[i] = lat;
            }
            if (lat < a.lat || lat > b.lat) {
                continue;
            }
            if (aLng == lng || bLng == lng) {
                lng -= DBL_EPSILON;
                plng[i] = lng;
            }
            double ratio = (lat - a.lat) / (b.lat - a.lat);
            double testLng = aLng + (bLng - aLng) * ratio;
            if (isTM && testLng < 0.0) {
                testLng += m2pi;
            }
            if (testLng > lng) {
                out[i] ^= 1;
            }
        }
    }
    if (n > H3_PIP_STACK_SCRATCH) {
        free(plat);
        free(plng);
    }
}

#endif /* H3_HAS_AVX2 */
