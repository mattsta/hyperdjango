/** @file bbox_scalar.c
 *  @brief Scalar reference for h3SimdBboxContainsBatch.
 *
 *  Always compiled. Used directly when SIMD is disabled or unsupported,
 *  and as the equivalence-target for ISA-specific kernels in tests.
 */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "bbox.h"
#include "h3simd.h"

void h3SimdBboxContainsBatchScalar(const BBox *bbox, size_t n,
                                   const double *lats, const double *lngs,
                                   uint8_t *out) {
    /* Hoist transmeridian state once; the original bboxContains() recomputed
     * it per-call. */
    bool isTM = bbox->east < bbox->west;
    double north = bbox->north, south = bbox->south;
    double east = bbox->east, west = bbox->west;
    if (isTM) {
        for (size_t i = 0; i < n; i++) {
            double lat = lats[i], lng = lngs[i];
            out[i] = (uint8_t)(lat >= south && lat <= north &&
                               (lng >= west || lng <= east));
        }
    } else {
        for (size_t i = 0; i < n; i++) {
            double lat = lats[i], lng = lngs[i];
            out[i] = (uint8_t)(lat >= south && lat <= north && lng >= west &&
                               lng <= east);
        }
    }
}
