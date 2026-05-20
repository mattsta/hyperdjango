/** @file pointInPoly_neon.c
 *  @brief NEON (AArch64) implementation of h3SimdPointInsideGeoLoopBatch.
 *
 *  2 doubles per vector. Iterates polygon EDGES in the outer loop, points in
 *  the inner SIMD loop. Per-point lat/lng/contains state lives in scratch
 *  arrays; updates are masked so each lane evolves independently.
 *
 *  Math is identical to the scalar reference (pointInPoly_scalar.c). The
 *  only divergence is the order of FMA operations, which is bitwise stable
 *  here because the inner per-point math has no horizontal reductions.
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

#if defined(H3_HAS_NEON)
#include <arm_neon.h>

#ifndef M_2PI
#define M_2PI 6.28318530717958647692528676655900576839433L
#endif

/* Small-batch stack scratch threshold — see pointInPoly_scalar.c. */
#define H3_PIP_STACK_SCRATCH 64

void h3SimdPointInsideGeoLoopBatchNeon(int numVerts, const LatLng *verts,
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

    /* Per-point mutable state: lat / lng evolve as edges nudge them; the
     * contains accumulator is XOR-ed each toggle. Stack for small n. */
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
    double zero = 0.0;
    /* Initialize state with longitude normalization for transmeridian. */
    for (size_t i = 0; i < n; i++) {
        plat[i] = lats[i];
        double l = lngs[i];
        plng[i] = (isTM && l < 0.0) ? (l + m2pi) : l;
        out[i] = 0;
    }

    float64x2_t veps = vdupq_n_f64(DBL_EPSILON);
    float64x2_t vnegeps = vdupq_n_f64(-DBL_EPSILON);
    float64x2_t v2pi = vdupq_n_f64(m2pi);
    float64x2_t vzero = vdupq_n_f64(zero);

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

        float64x2_t vaLat = vdupq_n_f64(a.lat);
        float64x2_t vbLat = vdupq_n_f64(b.lat);
        float64x2_t vaLng = vdupq_n_f64(aLng);
        float64x2_t vbLng = vdupq_n_f64(bLng);
        float64x2_t vlatRange = vsubq_f64(vbLat, vaLat);
        float64x2_t vlngDelta = vsubq_f64(vbLng, vaLng);

        size_t i = 0;
        for (; i + 2 <= n; i += 2) {
            float64x2_t lat = vld1q_f64(plat + i);
            float64x2_t lng = vld1q_f64(plng + i);

            /* ε-nudge lat if it equals either endpoint. */
            uint64x2_t latEqA = vceqq_f64(lat, vaLat);
            uint64x2_t latEqB = vceqq_f64(lat, vbLat);
            uint64x2_t latNudge = vorrq_u64(latEqA, latEqB);
            float64x2_t latNudgeAmt = vreinterpretq_f64_u64(
                vandq_u64(latNudge, vreinterpretq_u64_f64(veps)));
            lat = vaddq_f64(lat, latNudgeAmt);
            vst1q_f64(plat + i, lat);

            /* skip = (lat < a.lat) | (lat > b.lat) */
            uint64x2_t skip =
                vorrq_u64(vcltq_f64(lat, vaLat), vcgtq_f64(lat, vbLat));

            /* ε-nudge lng if it equals either endpoint longitude. */
            uint64x2_t lngEqA = vceqq_f64(vaLng, lng);
            uint64x2_t lngEqB = vceqq_f64(vbLng, lng);
            uint64x2_t lngNudge = vorrq_u64(lngEqA, lngEqB);
            float64x2_t lngNudgeAmt = vreinterpretq_f64_u64(
                vandq_u64(lngNudge, vreinterpretq_u64_f64(vnegeps)));
            lng = vaddq_f64(lng, lngNudgeAmt);
            vst1q_f64(plng + i, lng);

            /* ratio = (lat - aLat) / (bLat - aLat) */
            float64x2_t ratio = vdivq_f64(vsubq_f64(lat, vaLat), vlatRange);
            /* testLng = aLng + lngDelta * ratio */
            float64x2_t testLng = vfmaq_f64(vaLng, vlngDelta, ratio);
            if (isTM) {
                uint64x2_t neg = vcltq_f64(testLng, vzero);
                float64x2_t add = vreinterpretq_f64_u64(
                    vandq_u64(neg, vreinterpretq_u64_f64(v2pi)));
                testLng = vaddq_f64(testLng, add);
            }

            /* toggle = (testLng > lng) AND NOT skip */
            uint64x2_t cross = vcgtq_f64(testLng, lng);
            uint64x2_t toggle = vbicq_u64(cross, skip);
            /* XOR low bit of mask into out[i], out[i+1] */
            uint64_t m0 = vgetq_lane_u64(toggle, 0) & 1u;
            uint64_t m1 = vgetq_lane_u64(toggle, 1) & 1u;
            out[i + 0] ^= (uint8_t)m0;
            out[i + 1] ^= (uint8_t)m1;
        }
        /* Scalar tail (n%2 == 1). */
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

#endif /* H3_HAS_NEON */
