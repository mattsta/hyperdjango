/** @file pointInPoly_scalar.c
 *  @brief Scalar reference for h3SimdPointInsideGeoLoopBatch.
 *
 *  Mirrors the algorithm in polygonAlgos.h's pointInsideGeoLoop() but
 *  iterates polygon EDGES in the outer loop and POINTS in the inner loop.
 *  This inversion is what enables the ISA-specific kernels (NEON / AVX2)
 *  to vectorize across points; it doesn't change the math.
 */

#include <float.h>
#include <math.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>

#include "bbox.h"
#include "constants.h"
#include "h3simd.h"
#include "latLng.h"

/* M_2PI is used in NORMALIZE_LNG; pulled here to keep the kernel
 * self-contained without including polygonAlgos.h's macro state machine. */
#ifndef M_2PI
#define M_2PI 6.28318530717958647692528676655900576839433L
#endif

/* Small-batch stack scratch threshold. polygonToCells's ring batches are
 * size 7; benchmarks frequently use 16/64/256. 64 doubles (= 512 bytes)
 * per scratch buffer × 2 = 1KB on the stack, comfortably under any reasonable
 * stack size and zero allocator pressure for the hot caller. */
#define H3_PIP_STACK_SCRATCH 64

static inline double normLng(double lng, bool isTM) {
    return (isTM && lng < 0.0) ? (lng + M_2PI) : lng;
}

void h3SimdPointInsideGeoLoopBatchScalar(int numVerts, const LatLng *verts,
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

    /* Per-point mutable state, mirroring the scalar pointInsideGeoLoop.
     * Stack for small n, heap for larger — eliminates per-call malloc churn
     * for the polygonToCells hot caller (which calls us with n ≤ 7). */
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
    for (size_t i = 0; i < n; i++) {
        plat[i] = lats[i];
        plng[i] = normLng(lngs[i], isTM);
        out[i] = 0; /* contains accumulator (0/1) */
    }

    int v = numVerts;
    for (int e = 0; e < v; e++) {
        LatLng a = verts[e];
        LatLng b = verts[(e + 1) % v];
        if (a.lat > b.lat) {
            LatLng t = a;
            a = b;
            b = t;
        }
        double aLng = normLng(a.lng, isTM);
        double bLng = normLng(b.lng, isTM);

        for (size_t i = 0; i < n; i++) {
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
            double testLng = normLng(aLng + (bLng - aLng) * ratio, isTM);
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
