/** @file bbox_avx2.c
 *  @brief AVX2 implementation of h3SimdBboxContainsBatch.
 *
 *  Four doubles per vector. Compares + AND/OR — no FP arithmetic, so the
 *  result is bitwise identical to the scalar reference.
 */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "bbox.h"
#include "h3simd.h"

#if defined(H3_HAS_AVX2)
#include <immintrin.h>

void h3SimdBboxContainsBatchAvx2(const BBox *bbox, size_t n, const double *lats,
                                 const double *lngs, uint8_t *out) {
    bool isTM = bbox->east < bbox->west;
    __m256d vN = _mm256_set1_pd(bbox->north);
    __m256d vS = _mm256_set1_pd(bbox->south);
    __m256d vE = _mm256_set1_pd(bbox->east);
    __m256d vW = _mm256_set1_pd(bbox->west);

    size_t i = 0;
    for (; i + 4 <= n; i += 4) {
        __m256d lat = _mm256_loadu_pd(lats + i);
        __m256d lng = _mm256_loadu_pd(lngs + i);
        __m256d latOk = _mm256_and_pd(_mm256_cmp_pd(lat, vS, _CMP_GE_OQ),
                                      _mm256_cmp_pd(lat, vN, _CMP_LE_OQ));
        __m256d lngOk;
        if (isTM) {
            lngOk = _mm256_or_pd(_mm256_cmp_pd(lng, vW, _CMP_GE_OQ),
                                 _mm256_cmp_pd(lng, vE, _CMP_LE_OQ));
        } else {
            lngOk = _mm256_and_pd(_mm256_cmp_pd(lng, vW, _CMP_GE_OQ),
                                  _mm256_cmp_pd(lng, vE, _CMP_LE_OQ));
        }
        __m256d inside = _mm256_and_pd(latOk, lngOk);
        int bits = _mm256_movemask_pd(inside);
        out[i + 0] = (uint8_t)((bits >> 0) & 1);
        out[i + 1] = (uint8_t)((bits >> 1) & 1);
        out[i + 2] = (uint8_t)((bits >> 2) & 1);
        out[i + 3] = (uint8_t)((bits >> 3) & 1);
    }
    /* Scalar tail (at most 3). */
    for (; i < n; i++) {
        double lat = lats[i], lng = lngs[i];
        bool ok = (lat >= bbox->south && lat <= bbox->north);
        if (isTM) {
            ok = ok && (lng >= bbox->west || lng <= bbox->east);
        } else {
            ok = ok && (lng >= bbox->west && lng <= bbox->east);
        }
        out[i] = (uint8_t)ok;
    }
}

#endif /* H3_HAS_AVX2 */
