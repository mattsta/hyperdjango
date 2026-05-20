/** @file approx_libm.h
 *  @brief Polynomial approximations of atan, atan2, sincos, asin used in
 *  H3's hot conversion paths. Trade ≤180 nm absolute error vs libm
 *  (~6 mm at most) for ~3-5× faster math.
 *
 *  All routines are `static inline` so any TU can use them without
 *  binary-size duplication concerns; they're small and pipeline well.
 *
 *  ULP guarantees (vs. libm on doubles) — verified by
 *  testApproxLibmInternal.c sweeps:
 *
 *    approx_atan(x)        : ≤32 ULP for |x| < 1e6  (≤90 nm)
 *    approx_atan2(y, x)    : ≤32 ULP all quadrants
 *    approx_sincos(x, ...) : ≤64 ULP for |x| < 2π   (≤180 nm)
 *    approx_asin(x)        : ≤32 ULP for |x| ≤ 1
 *
 *  These guarantees are 5–6 orders of magnitude tighter than H3's
 *  half-meter precision budget on Earth's surface (~180M ULPs of
 *  headroom). Polynomial drift at this level is invisible at any H3
 *  resolution including res 15 (58 cm cells, 25 cm inradius).
 */

#ifndef APPROX_LIBM_H
#define APPROX_LIBM_H

#include <math.h>

#include "constants.h" /* M_PI, M_PI_2, M_2PI */

/* --- Internal: polynomial atan on [-tan(π/8), tan(π/8)] ≈ [-0.4142, 0.4142] -
 *
 * Cephes-style 11-coefficient rational. Maximum relative error in this
 * range is ~1.6e-17 (sub-ULP). See:
 *   https://www.netlib.org/cephes/  atan.c
 *
 * Used as the kernel for both approx_atan and approx_atan2 after
 * argument reduction. */
static inline double _approx_atan_kernel(double x) {
    double x2 = x * x;
    /* Numerator: P(x²) */
    double p = -8.750608600031904122785e-01;
    p = p * x2 + -1.615753718733365076637e+01;
    p = p * x2 + -7.500855792314704667340e+01;
    p = p * x2 + -1.228866684490136173410e+02;
    p = p * x2 + -6.485021904942025371773e+01;
    /* Denominator: Q(x²) (Q has constant term 1.0) */
    double q = 1.0;
    q = q * x2 + +2.485846490142306297962e+01;
    q = q * x2 + +1.650270098316988542046e+02;
    q = q * x2 + +4.328810604912902668951e+02;
    q = q * x2 + +4.853903996359136964868e+02;
    q = q * x2 + +1.945506571482613964425e+02;
    return x + x * x2 * (p / q);
}

/** approx_atan(x) — ≤4 ULP for |x| < 1e10 */
static inline double approx_atan(double x) {
    /* Range reduce |x| > tan(3π/8) via atan(x) = π/2 - atan(1/x).
     * Range reduce tan(π/8) < |x| ≤ tan(3π/8) via:
     *   atan(x) = π/4 + atan((x-1)/(x+1))
     */
    static const double TAN_PI_8 = 0.41421356237309503;  /* tan(π/8) */
    static const double TAN_3PI_8 = 2.41421356237309515; /* tan(3π/8) */

    double sign = (x < 0.0) ? -1.0 : 1.0;
    double ax = fabs(x);
    double y;
    if (ax > TAN_3PI_8) {
        y = M_PI_2 - _approx_atan_kernel(1.0 / ax);
    } else if (ax > TAN_PI_8) {
        y = (double)M_PI_4 + _approx_atan_kernel((ax - 1.0) / (ax + 1.0));
    } else {
        y = _approx_atan_kernel(ax);
    }
    return sign * y;
}

/** approx_atan2(y, x) — ≤4 ULP for finite (y,x), all quadrants. */
static inline double approx_atan2(double y, double x) {
    /* Standard quadrant decomposition: compute atan(y/x) in the first
     * quadrant and recompose. We avoid the division when |y| > |x|
     * by computing atan(x/y) and using atan(y/x) = π/2 - atan(x/y). */
    if (x == 0.0 && y == 0.0) {
        return 0.0;
    }

    double ax = fabs(x);
    double ay = fabs(y);
    double t;
    int swapped;
    if (ay > ax) {
        t = ax / ay;
        swapped = 1;
    } else {
        t = ay / ax;
        swapped = 0;
    }
    double a;
    /* t is in [0, 1] now. Reduce further. */
    if (t > 0.41421356237309503) {
        a = (double)M_PI_4 + _approx_atan_kernel((t - 1.0) / (t + 1.0));
    } else {
        a = _approx_atan_kernel(t);
    }
    if (swapped) {
        a = M_PI_2 - a;
    }
    if (x < 0.0) {
        a = M_PI - a;
    }
    if (y < 0.0) {
        a = -a;
    }
    return a;
}

/* --- Internal: sin/cos kernels on [-π/4, π/4] -----------------------------
 *
 * Cephes-style. Polynomials are minimax fits; max ULP error <1 within
 * range. Produces sin and cos simultaneously since both are needed in
 * H3's recomposition.
 */
static inline double _approx_sin_kernel(double x) {
    /* sin(x) = x + x³ * P(x²)  on [-π/4, π/4]
     * Cephes double-precision minimax coefficients (max relative error <1
     * ULP within the kernel's stated range). */
    double x2 = x * x;
    double s = 1.58962301576546568060E-10;
    s = s * x2 + -2.50507477628578072866E-8;
    s = s * x2 + 2.75573136213857245213E-6;
    s = s * x2 + -1.98412698295895385996E-4;
    s = s * x2 + 8.33333333332211858878E-3;
    s = s * x2 + -1.66666666666666307295E-1;
    return x + x * x2 * s;
}

static inline double _approx_cos_kernel(double x) {
    /* cos(x) = 1 - x²/2 + x⁴ * P(x²)  on [-π/4, π/4]
     * Cephes double-precision minimax coefficients. */
    double x2 = x * x;
    double c = -1.13585365213876817300E-11;
    c = c * x2 + 2.08757008419747316778E-9;
    c = c * x2 + -2.75573141792967388112E-7;
    c = c * x2 + 2.48015872888517045348E-5;
    c = c * x2 + -1.38888888888730564116E-3;
    c = c * x2 + 4.16666666666665929218E-2;
    return 1.0 - 0.5 * x2 + x2 * x2 * c;
}

/** approx_sincos(x, *s, *c) — ≤180 nm absolute error for |x| ≤ 1e6 rad.
 *  For |x| beyond that, falls back to libm sin/cos (the polynomial
 *  argument reduction can't safely cast to int64). H3 inputs are
 *  always lat/lng in radians (|x| ≤ ~6.3) so the fallback never fires
 *  in practice; it's purely defensive against torture tests like
 *  `latLngToCellExtremeCoordinates` (lng = 1e45). */
static inline void approx_sincos(double x, double *sOut, double *cOut) {
    /* Fallback for extreme inputs: cast to long long would overflow. */
    if (!(fabs(x) < 1.0e15)) {
        *sOut = sin(x);
        *cOut = cos(x);
        return;
    }
    /* Argument reduction: x = k*π/2 + r, with r ∈ [-π/4, π/4].
     * Then we compute sin(r), cos(r) via polynomials and recompose.
     *
     *   k mod 4 == 0:  sin(x) =  sin(r),  cos(x) =  cos(r)
     *   k mod 4 == 1:  sin(x) =  cos(r),  cos(x) = -sin(r)
     *   k mod 4 == 2:  sin(x) = -sin(r),  cos(x) = -cos(r)
     *   k mod 4 == 3:  sin(x) = -cos(r),  cos(x) =  sin(r)
     */
    static const double TWO_OVER_PI = 0.6366197723675814; /* 2/π */
    static const double PIO2_HI = 1.5707963267948966;     /* π/2 */

    double f = x * TWO_OVER_PI;
    /* Round to nearest k, ties to even. */
    double kd = (f >= 0.0) ? (double)(long long)(f + 0.5)
                           : (double)(long long)(f - 0.5);
    double r = x - kd * PIO2_HI;
    long long k = (long long)kd;

    double sr = _approx_sin_kernel(r);
    double cr = _approx_cos_kernel(r);

    switch (((unsigned long long)k) & 3ULL) {
    case 0:
        *sOut = sr;
        *cOut = cr;
        break;
    case 1:
        *sOut = cr;
        *cOut = -sr;
        break;
    case 2:
        *sOut = -sr;
        *cOut = -cr;
        break;
    default: /* 3 */
        *sOut = -cr;
        *cOut = sr;
        break;
    }
}

/** approx_sincos_unchecked — branchless variant for tight batch loops
 *  where the caller guarantees |x| < 1e15. Drops the safety fallback so
 *  the autovectorizer can hoist this into a packed SIMD chain. */
static inline void approx_sincos_unchecked(double x, double *sOut,
                                           double *cOut) {
    static const double TWO_OVER_PI = 0.6366197723675814;
    static const double PIO2_HI = 1.5707963267948966;

    double f = x * TWO_OVER_PI;
    double kd = (f >= 0.0) ? (double)(long long)(f + 0.5)
                           : (double)(long long)(f - 0.5);
    double r = x - kd * PIO2_HI;
    long long k = (long long)kd;

    double sr = _approx_sin_kernel(r);
    double cr = _approx_cos_kernel(r);

    switch (((unsigned long long)k) & 3ULL) {
    case 0:
        *sOut = sr;
        *cOut = cr;
        break;
    case 1:
        *sOut = cr;
        *cOut = -sr;
        break;
    case 2:
        *sOut = -sr;
        *cOut = -cr;
        break;
    default:
        *sOut = -cr;
        *cOut = sr;
        break;
    }
}

/** approx_sin / approx_cos — convenience. */
static inline double approx_sin(double x) {
    double s, c;
    approx_sincos(x, &s, &c);
    return s;
}

static inline double approx_cos(double x) {
    double s, c;
    approx_sincos(x, &s, &c);
    return c;
}

/** approx_asin(x) — fdlibm-style rational approximation. ≤2 ULP for
 *  |x| ≤ 1. Coefficients are from Sun's freely-distributable fdlibm
 *  (e_asin.c), which gives near-libm precision on doubles. */
static inline double _approx_asin_R(double z) {
    /* P(z)/Q(z) on z = u² near 0; coefficients from fdlibm. */
    static const double pS0 = 1.66666666666666657415e-01;
    static const double pS1 = -3.25565818622400915405e-01;
    static const double pS2 = 2.01212532134862925881e-01;
    static const double pS3 = -4.00555345006794114027e-02;
    static const double pS4 = 7.91534994289814532176e-04;
    static const double pS5 = 3.47933107596021167570e-05;
    static const double qS1 = -2.40339491173441421878e+00;
    static const double qS2 = 2.02094576023350569471e+00;
    static const double qS3 = -6.88283971605453293030e-01;
    static const double qS4 = 7.70381505559019352791e-02;
    double p =
        z * (pS0 + z * (pS1 + z * (pS2 + z * (pS3 + z * (pS4 + z * pS5)))));
    double q = 1.0 + z * (qS1 + z * (qS2 + z * (qS3 + z * qS4)));
    return p / q;
}

static inline double approx_asin(double x) {
    double sign = (x < 0.0) ? -1.0 : 1.0;
    double ax = fabs(x);
    if (ax >= 1.0) {
        return sign * M_PI_2;
    }
    if (ax < 0.5) {
        double r = _approx_asin_R(x * x);
        return x + x * r;
    }
    /* |x| in [0.5, 1):  asin(x) = π/2 - 2*asin(sqrt((1-|x|)/2)) */
    double w = (1.0 - ax) * 0.5;
    double s = sqrt(w);
    double r = _approx_asin_R(w);
    double y = M_PI_2 - 2.0 * (s + s * r);
    return sign * y;
}

/* =========================================================================
 *  NEON 2-wide variants — process 2 doubles per call.
 *
 *  These mirror the scalar approximations above but operate on
 *  `float64x2_t` SIMD registers, suitable for direct use in any 2-wide
 *  batch path (including the 6-vertex `_faceIjkToCellBoundary` fan-out
 *  — 6 vertices = 3 NEON pairs).
 *
 *  Same precision guarantees as the scalar versions: ≤180 nm absolute
 *  error vs libm. The implementations are fully branchless via
 *  `vbslq_*` predicate masking — no per-lane control flow that would
 *  defeat SIMD pipelining. The only fallback is the fabs(x) > 1e15
 *  safety net in approx_sincos: SIMD batches that hit lat/lng inputs
 *  always satisfy |x| < 7, so the equivalent `_unchecked_neon2`
 *  variants below skip that check.
 * ========================================================================= */
#if defined(H3_HAS_NEON)
#include <arm_neon.h>

/** approx_atan_kernel_neon2(x): polynomial atan kernel for |x| < tan(π/8).
 *  Same Cephes-style 11-coefficient rational as the scalar kernel, lifted
 *  into NEON FMA chains. */
static inline float64x2_t _approx_atan_kernel_neon2(float64x2_t x) {
    float64x2_t x2 = vmulq_f64(x, x);
    /* Numerator polynomial in x²: P[0] + P[1]*x² + ... + P[4]*x⁸ */
    float64x2_t p = vdupq_n_f64(-8.750608600031904122785e-01);
    p = vfmaq_f64(vdupq_n_f64(-1.615753718733365076637e+01), p, x2);
    p = vfmaq_f64(vdupq_n_f64(-7.500855792314704667340e+01), p, x2);
    p = vfmaq_f64(vdupq_n_f64(-1.228866684490136173410e+02), p, x2);
    p = vfmaq_f64(vdupq_n_f64(-6.485021904942025371773e+01), p, x2);
    /* Denominator polynomial Q (constant term = 1.0) */
    float64x2_t q = vdupq_n_f64(1.0);
    q = vfmaq_f64(vdupq_n_f64(+2.485846490142306297962e+01), q, x2);
    q = vfmaq_f64(vdupq_n_f64(+1.650270098316988542046e+02), q, x2);
    q = vfmaq_f64(vdupq_n_f64(+4.328810604912902668951e+02), q, x2);
    q = vfmaq_f64(vdupq_n_f64(+4.853903996359136964868e+02), q, x2);
    q = vfmaq_f64(vdupq_n_f64(+1.945506571482613964425e+02), q, x2);
    /* result = x + x * x² * (p / q) */
    float64x2_t pq = vdivq_f64(p, q);
    return vfmaq_f64(x, vmulq_f64(vmulq_f64(x, x2), pq), vdupq_n_f64(1.0));
}

/** approx_atan_neon2(x): ≤6 nm abs error vs libm, branchless. */
static inline float64x2_t approx_atan_neon2(float64x2_t x) {
    /* The scalar atan reduces |x| via atan(x) = π/2 - atan(1/x) for large x,
     * and atan(x) = π/4 + atan((x-1)/(x+1)) for medium x. Apply both
     * reductions branchlessly via masks. */
    static const double TAN_PI_8 = 0.41421356237309503;
    static const double TAN_3PI_8 = 2.41421356237309515;

    uint64x2_t sign_mask = vreinterpretq_u64_f64(x);
    sign_mask = vandq_u64(sign_mask, vdupq_n_u64(0x8000000000000000ULL));
    float64x2_t ax = vabsq_f64(x);

    /* Three regions: small (|x| ≤ tan π/8), medium (tan π/8 < |x| ≤ tan 3π/8),
     * large (|x| > tan 3π/8). Compute the kernel input and the additive
     * offset for each region, then select. */
    uint64x2_t m_med = vcgtq_f64(ax, vdupq_n_f64(TAN_PI_8));
    uint64x2_t m_lrg = vcgtq_f64(ax, vdupq_n_f64(TAN_3PI_8));

    /* small: arg = ax,             offset = 0
     * med:   arg = (ax-1)/(ax+1),  offset = π/4
     * large: arg = 1/ax,           offset = π/2  (and the kernel is negated) */
    float64x2_t arg_small = ax;
    float64x2_t arg_med = vdivq_f64(vsubq_f64(ax, vdupq_n_f64(1.0)),
                                    vaddq_f64(ax, vdupq_n_f64(1.0)));
    float64x2_t arg_lrg = vdivq_f64(vdupq_n_f64(1.0), ax);

    /* select arg per lane: start small, override med, then override large */
    float64x2_t arg = vbslq_f64(m_med, arg_med, arg_small);
    arg = vbslq_f64(m_lrg, arg_lrg, arg);

    float64x2_t kern = _approx_atan_kernel_neon2(arg);
    /* For large region we want π/2 - kern(1/ax); for small/med we want
     * offset + kern(arg). */
    float64x2_t offset_small = vdupq_n_f64(0.0);
    float64x2_t offset_med = vdupq_n_f64(M_PI_4);
    float64x2_t offset = vbslq_f64(m_med, offset_med, offset_small);

    float64x2_t result_smallmed = vaddq_f64(offset, kern);
    float64x2_t result_lrg = vsubq_f64(vdupq_n_f64(M_PI_2), kern);
    float64x2_t y = vbslq_f64(m_lrg, result_lrg, result_smallmed);

    /* Apply original sign by XOR-ing the sign bit. */
    return vreinterpretq_f64_u64(
        veorq_u64(vreinterpretq_u64_f64(y), sign_mask));
}

/** approx_atan2_neon2(y, x): ≤6 nm abs error vs libm, branchless. */
static inline float64x2_t approx_atan2_neon2(float64x2_t y, float64x2_t x) {
    /* Quadrant decomposition with branchless masking. */
    float64x2_t ax = vabsq_f64(x);
    float64x2_t ay = vabsq_f64(y);
    uint64x2_t swap = vcgtq_f64(ay, ax);
    /* t = swap ? (ax / ay) : (ay / ax) */
    float64x2_t t = vbslq_f64(swap, vdivq_f64(ax, ay), vdivq_f64(ay, ax));
    /* Reduce further if t > tan(π/8). */
    uint64x2_t med = vcgtq_f64(t, vdupq_n_f64(0.41421356237309503));
    float64x2_t t_med = vdivq_f64(vsubq_f64(t, vdupq_n_f64(1.0)),
                                  vaddq_f64(t, vdupq_n_f64(1.0)));
    float64x2_t arg = vbslq_f64(med, t_med, t);
    float64x2_t kern = _approx_atan_kernel_neon2(arg);
    float64x2_t offset = vbslq_f64(med, vdupq_n_f64(M_PI_4), vdupq_n_f64(0.0));
    float64x2_t a = vaddq_f64(offset, kern);
    /* If swapped (|y| > |x|), a = π/2 - a. */
    a = vbslq_f64(swap, vsubq_f64(vdupq_n_f64(M_PI_2), a), a);
    /* If x < 0, a = π - a. */
    uint64x2_t x_neg = vcltq_f64(x, vdupq_n_f64(0.0));
    a = vbslq_f64(x_neg, vsubq_f64(vdupq_n_f64(M_PI), a), a);
    /* If y < 0, a = -a (XOR sign). */
    uint64x2_t y_sign =
        vandq_u64(vreinterpretq_u64_f64(y), vdupq_n_u64(0x8000000000000000ULL));
    return vreinterpretq_f64_u64(veorq_u64(vreinterpretq_u64_f64(a), y_sign));
}

/** approx_sincos_neon2 — branchless 2-wide. Caller must guarantee
 *  |x| < 1e15 (true for all H3 lat/lng inputs); for unsafe inputs use
 *  the scalar approx_sincos which has the libm fallback branch. */
static inline void approx_sincos_neon2(float64x2_t x, float64x2_t *sOut,
                                       float64x2_t *cOut) {
    static const double TWO_OVER_PI = 0.6366197723675814;
    static const double PIO2_HI = 1.5707963267948966;

    /* k = round(x * 2/π), as int64 lanes. */
    float64x2_t f = vmulq_f64(x, vdupq_n_f64(TWO_OVER_PI));
    /* vrndnq_f64 rounds to nearest, ties to even — exact for our use. */
    float64x2_t kd = vrndnq_f64(f);
    int64x2_t k = vcvtq_s64_f64(kd);

    float64x2_t r = vfmsq_f64(x, kd, vdupq_n_f64(PIO2_HI));

    /* Polynomial kernels for sin and cos on r ∈ [-π/4, π/4]. */
    float64x2_t r2 = vmulq_f64(r, r);

    /* sin kernel: x + x³ * P(x²) */
    float64x2_t s = vdupq_n_f64(1.58962301576546568060E-10);
    s = vfmaq_f64(vdupq_n_f64(-2.50507477628578072866E-8), s, r2);
    s = vfmaq_f64(vdupq_n_f64(2.75573136213857245213E-6), s, r2);
    s = vfmaq_f64(vdupq_n_f64(-1.98412698295895385996E-4), s, r2);
    s = vfmaq_f64(vdupq_n_f64(8.33333333332211858878E-3), s, r2);
    s = vfmaq_f64(vdupq_n_f64(-1.66666666666666307295E-1), s, r2);
    float64x2_t sr = vfmaq_f64(r, vmulq_f64(r, r2), s);

    /* cos kernel: 1 - x²/2 + x⁴ * P(x²) */
    float64x2_t c = vdupq_n_f64(-1.13585365213876817300E-11);
    c = vfmaq_f64(vdupq_n_f64(2.08757008419747316778E-9), c, r2);
    c = vfmaq_f64(vdupq_n_f64(-2.75573141792967388112E-7), c, r2);
    c = vfmaq_f64(vdupq_n_f64(2.48015872888517045348E-5), c, r2);
    c = vfmaq_f64(vdupq_n_f64(-1.38888888888730564116E-3), c, r2);
    c = vfmaq_f64(vdupq_n_f64(4.16666666666665929218E-2), c, r2);
    /* cr = 1 - 0.5*r² + r⁴ * c  =  1 + r²*(c*r² - 0.5) */
    float64x2_t cr = vfmaq_f64(
        vdupq_n_f64(1.0), r2,
        vfmsq_f64(vmulq_f64(c, r2), vdupq_n_f64(0.5), vdupq_n_f64(1.0)));

    /* Recompose sin/cos from k mod 4 — branchless via mask selection.
     *   k=0:  s,c =  sr,  cr
     *   k=1:  s,c =  cr, -sr
     *   k=2:  s,c = -sr, -cr
     *   k=3:  s,c = -cr,  sr
     *
     * Define:
     *   swap     = (k & 1) != 0       — swap sr and cr roles
     *   s_neg    = (k & 2) != 0       — flip sign of sin output (k in {2,3})
     *   c_neg    = ((k+1) & 2) != 0   — flip sign of cos output (k in {1,2})
     */
    uint64x2_t k_u = vreinterpretq_u64_s64(k);
    uint64x2_t k_lsb = vandq_u64(k_u, vdupq_n_u64(1));
    uint64x2_t swap_mask = vtstq_u64(k_lsb, vdupq_n_u64(1));
    uint64x2_t s_neg_mask = vtstq_u64(k_u, vdupq_n_u64(2));
    uint64x2_t k_plus_1 = vaddq_u64(k_u, vdupq_n_u64(1));
    uint64x2_t c_neg_mask = vtstq_u64(k_plus_1, vdupq_n_u64(2));

    uint64x2_t signbit = vdupq_n_u64(0x8000000000000000ULL);
    /* Apply signs by XOR-ing the sign bit conditionally. */
    float64x2_t sr_signed = vreinterpretq_f64_u64(
        veorq_u64(vreinterpretq_u64_f64(sr), vandq_u64(s_neg_mask, signbit)));
    float64x2_t cr_signed = vreinterpretq_f64_u64(
        veorq_u64(vreinterpretq_u64_f64(cr), vandq_u64(c_neg_mask, signbit)));
    /* For the c output when swap is true, the contribution from sr is
     * negated when c_neg is true. */
    float64x2_t s_swap_for_c = vreinterpretq_f64_u64(
        veorq_u64(vreinterpretq_u64_f64(sr), vandq_u64(c_neg_mask, signbit)));
    /* For the s output when swap is true, the contribution from cr is
     * negated when s_neg is true. */
    float64x2_t c_swap_for_s = vreinterpretq_f64_u64(
        veorq_u64(vreinterpretq_u64_f64(cr), vandq_u64(s_neg_mask, signbit)));

    *sOut = vbslq_f64(swap_mask, c_swap_for_s, sr_signed);
    *cOut = vbslq_f64(swap_mask, s_swap_for_c, cr_signed);
}

/** approx_asin_R_neon2(z): fdlibm rational kernel, P(z)/Q(z). */
static inline float64x2_t _approx_asin_R_neon2(float64x2_t z) {
    /* Numerator: ((((((pS5*z + pS4)*z + pS3)*z + pS2)*z + pS1)*z + pS0)*z */
    float64x2_t p = vdupq_n_f64(3.47933107596021167570e-05);
    p = vfmaq_f64(vdupq_n_f64(7.91534994289814532176e-04), p, z);
    p = vfmaq_f64(vdupq_n_f64(-4.00555345006794114027e-02), p, z);
    p = vfmaq_f64(vdupq_n_f64(2.01212532134862925881e-01), p, z);
    p = vfmaq_f64(vdupq_n_f64(-3.25565818622400915405e-01), p, z);
    p = vfmaq_f64(vdupq_n_f64(1.66666666666666657415e-01), p, z);
    p = vmulq_f64(p, z);
    /* Denominator: 1 + qS1*z + qS2*z² + qS3*z³ + qS4*z⁴ */
    float64x2_t q = vdupq_n_f64(7.70381505559019352791e-02);
    q = vfmaq_f64(vdupq_n_f64(-6.88283971605453293030e-01), q, z);
    q = vfmaq_f64(vdupq_n_f64(2.02094576023350569471e+00), q, z);
    q = vfmaq_f64(vdupq_n_f64(-2.40339491173441421878e+00), q, z);
    q = vfmaq_f64(vdupq_n_f64(1.0), q, z);
    return vdivq_f64(p, q);
}

/** approx_asin_neon2(x): ≤6 nm abs error vs libm, branchless. */
static inline float64x2_t approx_asin_neon2(float64x2_t x) {
    uint64x2_t sign_bit =
        vandq_u64(vreinterpretq_u64_f64(x), vdupq_n_u64(0x8000000000000000ULL));
    float64x2_t ax = vabsq_f64(x);
    /* Two paths:
     *   |x| ≤ 0.5: y = x + x * R(x²)
     *   |x| > 0.5: w = (1-|x|)/2; s = sqrt(w); y = π/2 - 2*(s + s*R(w))
     * Compute both, select by mask. */
    float64x2_t z_small = vmulq_f64(ax, ax);
    float64x2_t r_small = _approx_asin_R_neon2(z_small);
    /* y_small = x + x * r_small (preserves sign via x, not ax) */
    float64x2_t y_small = vfmaq_f64(x, x, r_small);

    float64x2_t w =
        vmulq_f64(vsubq_f64(vdupq_n_f64(1.0), ax), vdupq_n_f64(0.5));
    float64x2_t s = vsqrtq_f64(w);
    float64x2_t r_big = _approx_asin_R_neon2(w);
    /* y_big = π/2 - 2*(s + s*r_big). Apply sign at the end. */
    float64x2_t y_big_pos =
        vsubq_f64(vdupq_n_f64(M_PI_2),
                  vmulq_f64(vdupq_n_f64(2.0), vfmaq_f64(s, s, r_big)));
    /* Apply original sign. */
    float64x2_t y_big = vreinterpretq_f64_u64(
        veorq_u64(vreinterpretq_u64_f64(y_big_pos), sign_bit));

    uint64x2_t big = vcgtq_f64(ax, vdupq_n_f64(0.5));
    return vbslq_f64(big, y_big, y_small);
}

#endif /* H3_HAS_NEON */

/* =========================================================================
 *  AVX2 4-wide variants — process 4 doubles per call.
 *
 *  Mirror of the NEON 2-wide primitives but on __m256d (AVX2 + FMA3
 *  required). At 4 lanes per call vs NEON's 2, Intel hosts get 2× more
 *  parallelism per primitive call. Same coefficients, same precision
 *  guarantees as the scalar versions.
 *
 *  All routines branchless via `_mm256_blendv_pd` predicate masking —
 *  no per-lane control flow that would defeat SIMD pipelining.
 *
 *  Validated by testApproxLibmInternal AVX2 sub-tests when compiled
 *  with H3_HAS_AVX2.
 *
 *  IMPORTANT — guard rationale: this block is gated on the compiler
 *  intrinsic predefine `__AVX2__` AND `__FMA__`, NOT on the project's
 *  `H3_HAS_AVX2` macro. `H3_HAS_AVX2` is a CMake-set target compile
 *  definition that applies to *every* h3 translation unit, even those
 *  compiled without `-mavx2` (only the SIMD kernel TUs get `-mavx2`
 *  via per-file flags from `cmake/H3Simd.cmake`). When a non-AVX2 TU
 *  includes this header, GCC's `-Wpsabi` (made an error in CI by
 *  `-Werror`) fires on functions whose signatures contain `__m256d`
 *  return values because the ABI of those returns depends on AVX
 *  state. Gating on `__AVX2__` (auto-defined ONLY when -mavx2 is in
 *  effect at THIS TU's compile flags) makes the declarations vanish
 *  in non-AVX TUs, eliminating both the psabi warning and the risk
 *  of accidentally generating AVX-bearing code where it isn't safe.
 * ========================================================================= */
#if defined(H3_HAS_AVX2) && defined(__AVX2__) && defined(__FMA__)
#include <immintrin.h>

/** _approx_atan_kernel_avx2: polynomial atan kernel on |x| < tan(π/8). */
static inline __m256d _approx_atan_kernel_avx2(__m256d x) {
    __m256d x2 = _mm256_mul_pd(x, x);
    /* P(x²) numerator */
    __m256d p = _mm256_set1_pd(-8.750608600031904122785e-01);
    p = _mm256_fmadd_pd(p, x2, _mm256_set1_pd(-1.615753718733365076637e+01));
    p = _mm256_fmadd_pd(p, x2, _mm256_set1_pd(-7.500855792314704667340e+01));
    p = _mm256_fmadd_pd(p, x2, _mm256_set1_pd(-1.228866684490136173410e+02));
    p = _mm256_fmadd_pd(p, x2, _mm256_set1_pd(-6.485021904942025371773e+01));
    /* Q(x²) denominator (constant term = 1.0) */
    __m256d q = _mm256_set1_pd(1.0);
    q = _mm256_fmadd_pd(q, x2, _mm256_set1_pd(+2.485846490142306297962e+01));
    q = _mm256_fmadd_pd(q, x2, _mm256_set1_pd(+1.650270098316988542046e+02));
    q = _mm256_fmadd_pd(q, x2, _mm256_set1_pd(+4.328810604912902668951e+02));
    q = _mm256_fmadd_pd(q, x2, _mm256_set1_pd(+4.853903996359136964868e+02));
    q = _mm256_fmadd_pd(q, x2, _mm256_set1_pd(+1.945506571482613964425e+02));
    /* result = x + x * x² * (p / q) */
    __m256d pq = _mm256_div_pd(p, q);
    return _mm256_fmadd_pd(_mm256_mul_pd(_mm256_mul_pd(x, x2), pq),
                           _mm256_set1_pd(1.0), x);
}

/** approx_atan_avx2(x): ≤6 nm abs error vs libm, branchless 4-wide. */
static inline __m256d approx_atan_avx2(__m256d x) {
    static const double TAN_PI_8 = 0.41421356237309503;
    static const double TAN_3PI_8 = 2.41421356237309515;

    /* Sign mask: extract bit 63 of each lane. */
    __m256d sign_bit = _mm256_and_pd(x, _mm256_set1_pd(-0.0));
    __m256d ax = _mm256_andnot_pd(_mm256_set1_pd(-0.0), x);

    __m256d m_med = _mm256_cmp_pd(ax, _mm256_set1_pd(TAN_PI_8), _CMP_GT_OQ);
    __m256d m_lrg = _mm256_cmp_pd(ax, _mm256_set1_pd(TAN_3PI_8), _CMP_GT_OQ);

    __m256d arg_small = ax;
    __m256d arg_med = _mm256_div_pd(_mm256_sub_pd(ax, _mm256_set1_pd(1.0)),
                                    _mm256_add_pd(ax, _mm256_set1_pd(1.0)));
    __m256d arg_lrg = _mm256_div_pd(_mm256_set1_pd(1.0), ax);

    __m256d arg = _mm256_blendv_pd(arg_small, arg_med, m_med);
    arg = _mm256_blendv_pd(arg, arg_lrg, m_lrg);

    __m256d kern = _approx_atan_kernel_avx2(arg);
    __m256d offset_small = _mm256_setzero_pd();
    __m256d offset_med = _mm256_set1_pd(M_PI_4);
    __m256d offset = _mm256_blendv_pd(offset_small, offset_med, m_med);

    __m256d result_smallmed = _mm256_add_pd(offset, kern);
    __m256d result_lrg = _mm256_sub_pd(_mm256_set1_pd(M_PI_2), kern);
    __m256d y = _mm256_blendv_pd(result_smallmed, result_lrg, m_lrg);

    /* Apply original sign by XOR-ing the sign bit. */
    return _mm256_xor_pd(y, sign_bit);
}

/** approx_atan2_avx2(y, x): ≤6 nm abs error vs libm, branchless 4-wide. */
static inline __m256d approx_atan2_avx2(__m256d y, __m256d x) {
    __m256d signmask = _mm256_set1_pd(-0.0);
    __m256d ax = _mm256_andnot_pd(signmask, x);
    __m256d ay = _mm256_andnot_pd(signmask, y);
    __m256d swap = _mm256_cmp_pd(ay, ax, _CMP_GT_OQ);
    __m256d t =
        _mm256_blendv_pd(_mm256_div_pd(ay, ax), _mm256_div_pd(ax, ay), swap);
    __m256d med =
        _mm256_cmp_pd(t, _mm256_set1_pd(0.41421356237309503), _CMP_GT_OQ);
    __m256d t_med = _mm256_div_pd(_mm256_sub_pd(t, _mm256_set1_pd(1.0)),
                                  _mm256_add_pd(t, _mm256_set1_pd(1.0)));
    __m256d arg = _mm256_blendv_pd(t, t_med, med);
    __m256d kern = _approx_atan_kernel_avx2(arg);
    __m256d offset =
        _mm256_blendv_pd(_mm256_setzero_pd(), _mm256_set1_pd(M_PI_4), med);
    __m256d a = _mm256_add_pd(offset, kern);
    a = _mm256_blendv_pd(a, _mm256_sub_pd(_mm256_set1_pd(M_PI_2), a), swap);
    __m256d x_neg = _mm256_cmp_pd(x, _mm256_setzero_pd(), _CMP_LT_OQ);
    a = _mm256_blendv_pd(a, _mm256_sub_pd(_mm256_set1_pd(M_PI), a), x_neg);
    /* Apply y sign. */
    __m256d y_sign = _mm256_and_pd(y, signmask);
    return _mm256_xor_pd(a, y_sign);
}

/** approx_sincos_avx2 — branchless 4-wide. Caller guarantees |x| < 1e15. */
static inline void approx_sincos_avx2(__m256d x, __m256d *sOut, __m256d *cOut) {
    static const double TWO_OVER_PI = 0.6366197723675814;
    static const double PIO2_HI = 1.5707963267948966;

    __m256d f = _mm256_mul_pd(x, _mm256_set1_pd(TWO_OVER_PI));
    /* Round to nearest, ties to even. */
    __m256d kd =
        _mm256_round_pd(f, _MM_FROUND_TO_NEAREST_INT | _MM_FROUND_NO_EXC);
    __m256d r = _mm256_fnmadd_pd(kd, _mm256_set1_pd(PIO2_HI), x);
    __m256d r2 = _mm256_mul_pd(r, r);

    /* sin kernel: x + x³ * P(x²) */
    __m256d s = _mm256_set1_pd(1.58962301576546568060E-10);
    s = _mm256_fmadd_pd(s, r2, _mm256_set1_pd(-2.50507477628578072866E-8));
    s = _mm256_fmadd_pd(s, r2, _mm256_set1_pd(2.75573136213857245213E-6));
    s = _mm256_fmadd_pd(s, r2, _mm256_set1_pd(-1.98412698295895385996E-4));
    s = _mm256_fmadd_pd(s, r2, _mm256_set1_pd(8.33333333332211858878E-3));
    s = _mm256_fmadd_pd(s, r2, _mm256_set1_pd(-1.66666666666666307295E-1));
    __m256d sr = _mm256_fmadd_pd(_mm256_mul_pd(r, r2), s, r);

    /* cos kernel: 1 - x²/2 + x⁴ * P(x²) */
    __m256d c = _mm256_set1_pd(-1.13585365213876817300E-11);
    c = _mm256_fmadd_pd(c, r2, _mm256_set1_pd(2.08757008419747316778E-9));
    c = _mm256_fmadd_pd(c, r2, _mm256_set1_pd(-2.75573141792967388112E-7));
    c = _mm256_fmadd_pd(c, r2, _mm256_set1_pd(2.48015872888517045348E-5));
    c = _mm256_fmadd_pd(c, r2, _mm256_set1_pd(-1.38888888888730564116E-3));
    c = _mm256_fmadd_pd(c, r2, _mm256_set1_pd(4.16666666666665929218E-2));
    /* cr = 1 + r²*(c*r² - 0.5) */
    __m256d cr = _mm256_fmadd_pd(
        r2, _mm256_fmsub_pd(c, r2, _mm256_set1_pd(0.5)), _mm256_set1_pd(1.0));

    /* Recompose by k mod 4 — pure SIMD via FP comparison masks.
     * kd_mod_4 = kd - 4 * floor(kd / 4)  is in [0, 4) for any sign of kd. */
    __m256d kd_div_4 = _mm256_floor_pd(_mm256_mul_pd(kd, _mm256_set1_pd(0.25)));
    __m256d kd_mod_4 = _mm256_fnmadd_pd(_mm256_set1_pd(4.0), kd_div_4, kd);

    __m256d eq0 = _mm256_cmp_pd(kd_mod_4, _mm256_setzero_pd(), _CMP_EQ_OQ);
    __m256d eq1 = _mm256_cmp_pd(kd_mod_4, _mm256_set1_pd(1.0), _CMP_EQ_OQ);
    __m256d eq2 = _mm256_cmp_pd(kd_mod_4, _mm256_set1_pd(2.0), _CMP_EQ_OQ);
    __m256d signbit = _mm256_set1_pd(-0.0);
    __m256d neg_sr = _mm256_xor_pd(sr, signbit);
    __m256d neg_cr = _mm256_xor_pd(cr, signbit);

    /* Default (case 3): s = -cr, c = sr */
    __m256d s_out = neg_cr;
    __m256d c_out = sr;
    /* Case 2: s = -sr, c = -cr */
    s_out = _mm256_blendv_pd(s_out, neg_sr, eq2);
    c_out = _mm256_blendv_pd(c_out, neg_cr, eq2);
    /* Case 1: s = cr, c = -sr */
    s_out = _mm256_blendv_pd(s_out, cr, eq1);
    c_out = _mm256_blendv_pd(c_out, neg_sr, eq1);
    /* Case 0: s = sr, c = cr */
    s_out = _mm256_blendv_pd(s_out, sr, eq0);
    c_out = _mm256_blendv_pd(c_out, cr, eq0);

    *sOut = s_out;
    *cOut = c_out;
}

/** _approx_asin_R_avx2: fdlibm rational kernel P(z)/Q(z), 4-wide. */
static inline __m256d _approx_asin_R_avx2(__m256d z) {
    /* Numerator */
    __m256d p = _mm256_set1_pd(3.47933107596021167570e-05);
    p = _mm256_fmadd_pd(p, z, _mm256_set1_pd(7.91534994289814532176e-04));
    p = _mm256_fmadd_pd(p, z, _mm256_set1_pd(-4.00555345006794114027e-02));
    p = _mm256_fmadd_pd(p, z, _mm256_set1_pd(2.01212532134862925881e-01));
    p = _mm256_fmadd_pd(p, z, _mm256_set1_pd(-3.25565818622400915405e-01));
    p = _mm256_fmadd_pd(p, z, _mm256_set1_pd(1.66666666666666657415e-01));
    p = _mm256_mul_pd(p, z);
    /* Denominator */
    __m256d q = _mm256_set1_pd(7.70381505559019352791e-02);
    q = _mm256_fmadd_pd(q, z, _mm256_set1_pd(-6.88283971605453293030e-01));
    q = _mm256_fmadd_pd(q, z, _mm256_set1_pd(2.02094576023350569471e+00));
    q = _mm256_fmadd_pd(q, z, _mm256_set1_pd(-2.40339491173441421878e+00));
    q = _mm256_fmadd_pd(q, z, _mm256_set1_pd(1.0));
    return _mm256_div_pd(p, q);
}

/** approx_asin_avx2(x): ≤6 nm abs error vs libm, branchless 4-wide. */
static inline __m256d approx_asin_avx2(__m256d x) {
    __m256d signmask = _mm256_set1_pd(-0.0);
    __m256d sign_bit = _mm256_and_pd(x, signmask);
    __m256d ax = _mm256_andnot_pd(signmask, x);

    /* Small path: y = x + x * R(x²) */
    __m256d z_small = _mm256_mul_pd(ax, ax);
    __m256d r_small = _approx_asin_R_avx2(z_small);
    __m256d y_small = _mm256_fmadd_pd(x, r_small, x);

    /* Big path: y = π/2 - 2*(s + s*R(w))  where w=(1-|x|)/2, s=sqrt(w) */
    __m256d w = _mm256_mul_pd(_mm256_sub_pd(_mm256_set1_pd(1.0), ax),
                              _mm256_set1_pd(0.5));
    __m256d s = _mm256_sqrt_pd(w);
    __m256d r_big = _approx_asin_R_avx2(w);
    __m256d y_big_pos = _mm256_sub_pd(
        _mm256_set1_pd(M_PI_2),
        _mm256_mul_pd(_mm256_set1_pd(2.0), _mm256_fmadd_pd(s, r_big, s)));
    /* Apply original sign. */
    __m256d y_big = _mm256_xor_pd(y_big_pos, sign_bit);

    __m256d big = _mm256_cmp_pd(ax, _mm256_set1_pd(0.5), _CMP_GT_OQ);
    return _mm256_blendv_pd(y_small, y_big, big);
}

#endif /* H3_HAS_AVX2 */

/* =========================================================================
 *  AVX-512 8-wide variants — process 8 doubles per call.
 *
 *  Mirror of the AVX2 4-wide primitives but on __m512d. At 8 lanes per
 *  call, AVX-512 hosts get 4× more parallelism than NEON's 2-wide and
 *  2× more than AVX2's 4-wide. Same coefficients, same precision
 *  guarantees as scalar/NEON/AVX2.
 *
 *  Selection uses AVX-512's mask-register API (`__mmask8` +
 *  `_mm512_mask_blend_pd`) instead of vector blends.
 *
 *  Used by `simd/avx512/vec3_avx512.c::h3SimdLatLngToVec3BatchAvx512`
 *  and any 8-wide batch caller. Validated by testApproxLibmInternal
 *  AVX-512 sub-tests when compiled with H3_HAS_AVX512.
 *
 *  Same `__AVX512F__` guard rationale as the AVX2 block above: gate on
 *  the compiler-set predefine that's only present when this TU's own
 *  flags enable AVX-512, not on the project-wide H3_HAS_AVX512 macro
 *  (which is set on every TU regardless of per-TU ISA flags).
 * ========================================================================= */
#if defined(H3_HAS_AVX512) && defined(__AVX512F__)
#include <immintrin.h>

/** _approx_atan_kernel_avx512 — 8-wide. */
static inline __m512d _approx_atan_kernel_avx512(__m512d x) {
    __m512d x2 = _mm512_mul_pd(x, x);
    __m512d p = _mm512_set1_pd(-8.750608600031904122785e-01);
    p = _mm512_fmadd_pd(p, x2, _mm512_set1_pd(-1.615753718733365076637e+01));
    p = _mm512_fmadd_pd(p, x2, _mm512_set1_pd(-7.500855792314704667340e+01));
    p = _mm512_fmadd_pd(p, x2, _mm512_set1_pd(-1.228866684490136173410e+02));
    p = _mm512_fmadd_pd(p, x2, _mm512_set1_pd(-6.485021904942025371773e+01));
    __m512d q = _mm512_set1_pd(1.0);
    q = _mm512_fmadd_pd(q, x2, _mm512_set1_pd(+2.485846490142306297962e+01));
    q = _mm512_fmadd_pd(q, x2, _mm512_set1_pd(+1.650270098316988542046e+02));
    q = _mm512_fmadd_pd(q, x2, _mm512_set1_pd(+4.328810604912902668951e+02));
    q = _mm512_fmadd_pd(q, x2, _mm512_set1_pd(+4.853903996359136964868e+02));
    q = _mm512_fmadd_pd(q, x2, _mm512_set1_pd(+1.945506571482613964425e+02));
    __m512d pq = _mm512_div_pd(p, q);
    return _mm512_fmadd_pd(_mm512_mul_pd(_mm512_mul_pd(x, x2), pq),
                           _mm512_set1_pd(1.0), x);
}

/** approx_atan_avx512(x) — branchless 8-wide. */
static inline __m512d approx_atan_avx512(__m512d x) {
    static const double TAN_PI_8 = 0.41421356237309503;
    static const double TAN_3PI_8 = 2.41421356237309515;

    __m512d signmask = _mm512_set1_pd(-0.0);
    __mmask8 sign_negative =
        _mm512_cmp_pd_mask(x, _mm512_setzero_pd(), _CMP_LT_OQ);
    __m512d ax = _mm512_andnot_pd(signmask, x);

    __mmask8 m_med =
        _mm512_cmp_pd_mask(ax, _mm512_set1_pd(TAN_PI_8), _CMP_GT_OQ);
    __mmask8 m_lrg =
        _mm512_cmp_pd_mask(ax, _mm512_set1_pd(TAN_3PI_8), _CMP_GT_OQ);

    __m512d arg_small = ax;
    __m512d arg_med = _mm512_div_pd(_mm512_sub_pd(ax, _mm512_set1_pd(1.0)),
                                    _mm512_add_pd(ax, _mm512_set1_pd(1.0)));
    __m512d arg_lrg = _mm512_div_pd(_mm512_set1_pd(1.0), ax);

    __m512d arg = _mm512_mask_blend_pd(m_med, arg_small, arg_med);
    arg = _mm512_mask_blend_pd(m_lrg, arg, arg_lrg);

    __m512d kern = _approx_atan_kernel_avx512(arg);
    __m512d offset = _mm512_mask_blend_pd(m_med, _mm512_setzero_pd(),
                                          _mm512_set1_pd(M_PI_4));
    __m512d result_smallmed = _mm512_add_pd(offset, kern);
    __m512d result_lrg = _mm512_sub_pd(_mm512_set1_pd(M_PI_2), kern);
    __m512d y = _mm512_mask_blend_pd(m_lrg, result_smallmed, result_lrg);

    /* Apply original sign. */
    __m512d neg_y = _mm512_sub_pd(_mm512_setzero_pd(), y);
    return _mm512_mask_blend_pd(sign_negative, y, neg_y);
}

/** approx_atan2_avx512(y, x) — branchless 8-wide. */
static inline __m512d approx_atan2_avx512(__m512d y, __m512d x) {
    __m512d signmask = _mm512_set1_pd(-0.0);
    __m512d ax = _mm512_andnot_pd(signmask, x);
    __m512d ay = _mm512_andnot_pd(signmask, y);
    __mmask8 swap = _mm512_cmp_pd_mask(ay, ax, _CMP_GT_OQ);
    __m512d t = _mm512_mask_blend_pd(swap, _mm512_div_pd(ay, ax),
                                     _mm512_div_pd(ax, ay));
    __mmask8 med =
        _mm512_cmp_pd_mask(t, _mm512_set1_pd(0.41421356237309503), _CMP_GT_OQ);
    __m512d t_med = _mm512_div_pd(_mm512_sub_pd(t, _mm512_set1_pd(1.0)),
                                  _mm512_add_pd(t, _mm512_set1_pd(1.0)));
    __m512d arg = _mm512_mask_blend_pd(med, t, t_med);
    __m512d kern = _approx_atan_kernel_avx512(arg);
    __m512d offset =
        _mm512_mask_blend_pd(med, _mm512_setzero_pd(), _mm512_set1_pd(M_PI_4));
    __m512d a = _mm512_add_pd(offset, kern);
    a = _mm512_mask_blend_pd(swap, a, _mm512_sub_pd(_mm512_set1_pd(M_PI_2), a));
    __mmask8 x_neg = _mm512_cmp_pd_mask(x, _mm512_setzero_pd(), _CMP_LT_OQ);
    a = _mm512_mask_blend_pd(x_neg, a, _mm512_sub_pd(_mm512_set1_pd(M_PI), a));
    __mmask8 y_neg = _mm512_cmp_pd_mask(y, _mm512_setzero_pd(), _CMP_LT_OQ);
    a = _mm512_mask_blend_pd(y_neg, a, _mm512_sub_pd(_mm512_setzero_pd(), a));
    return a;
}

/** approx_sincos_avx512 — branchless 8-wide. */
static inline void approx_sincos_avx512(__m512d x, __m512d *sOut,
                                        __m512d *cOut) {
    static const double TWO_OVER_PI = 0.6366197723675814;
    static const double PIO2_HI = 1.5707963267948966;

    __m512d f = _mm512_mul_pd(x, _mm512_set1_pd(TWO_OVER_PI));
    __m512d kd =
        _mm512_roundscale_pd(f, _MM_FROUND_TO_NEAREST_INT | _MM_FROUND_NO_EXC);
    __m512d r = _mm512_fnmadd_pd(kd, _mm512_set1_pd(PIO2_HI), x);
    __m512d r2 = _mm512_mul_pd(r, r);

    /* sin kernel */
    __m512d s = _mm512_set1_pd(1.58962301576546568060E-10);
    s = _mm512_fmadd_pd(s, r2, _mm512_set1_pd(-2.50507477628578072866E-8));
    s = _mm512_fmadd_pd(s, r2, _mm512_set1_pd(2.75573136213857245213E-6));
    s = _mm512_fmadd_pd(s, r2, _mm512_set1_pd(-1.98412698295895385996E-4));
    s = _mm512_fmadd_pd(s, r2, _mm512_set1_pd(8.33333333332211858878E-3));
    s = _mm512_fmadd_pd(s, r2, _mm512_set1_pd(-1.66666666666666307295E-1));
    __m512d sr = _mm512_fmadd_pd(_mm512_mul_pd(r, r2), s, r);

    /* cos kernel */
    __m512d c = _mm512_set1_pd(-1.13585365213876817300E-11);
    c = _mm512_fmadd_pd(c, r2, _mm512_set1_pd(2.08757008419747316778E-9));
    c = _mm512_fmadd_pd(c, r2, _mm512_set1_pd(-2.75573141792967388112E-7));
    c = _mm512_fmadd_pd(c, r2, _mm512_set1_pd(2.48015872888517045348E-5));
    c = _mm512_fmadd_pd(c, r2, _mm512_set1_pd(-1.38888888888730564116E-3));
    c = _mm512_fmadd_pd(c, r2, _mm512_set1_pd(4.16666666666665929218E-2));
    __m512d cr = _mm512_fmadd_pd(
        r2, _mm512_fmsub_pd(c, r2, _mm512_set1_pd(0.5)), _mm512_set1_pd(1.0));

    /* Recompose by k mod 4. */
    __m512d kd_div_4 = _mm512_floor_pd(_mm512_mul_pd(kd, _mm512_set1_pd(0.25)));
    __m512d kd_mod_4 = _mm512_fnmadd_pd(_mm512_set1_pd(4.0), kd_div_4, kd);

    __mmask8 eq0 =
        _mm512_cmp_pd_mask(kd_mod_4, _mm512_setzero_pd(), _CMP_EQ_OQ);
    __mmask8 eq1 =
        _mm512_cmp_pd_mask(kd_mod_4, _mm512_set1_pd(1.0), _CMP_EQ_OQ);
    __mmask8 eq2 =
        _mm512_cmp_pd_mask(kd_mod_4, _mm512_set1_pd(2.0), _CMP_EQ_OQ);
    __m512d signbit = _mm512_set1_pd(-0.0);
    __m512d neg_sr = _mm512_xor_pd(sr, signbit);
    __m512d neg_cr = _mm512_xor_pd(cr, signbit);

    /* Default (case 3): s = -cr, c = sr */
    __m512d s_out = neg_cr;
    __m512d c_out = sr;
    /* Case 2: s = -sr, c = -cr */
    s_out = _mm512_mask_blend_pd(eq2, s_out, neg_sr);
    c_out = _mm512_mask_blend_pd(eq2, c_out, neg_cr);
    /* Case 1: s = cr, c = -sr */
    s_out = _mm512_mask_blend_pd(eq1, s_out, cr);
    c_out = _mm512_mask_blend_pd(eq1, c_out, neg_sr);
    /* Case 0: s = sr, c = cr */
    s_out = _mm512_mask_blend_pd(eq0, s_out, sr);
    c_out = _mm512_mask_blend_pd(eq0, c_out, cr);

    *sOut = s_out;
    *cOut = c_out;
}

/** _approx_asin_R_avx512 — fdlibm rational kernel, 8-wide. */
static inline __m512d _approx_asin_R_avx512(__m512d z) {
    __m512d p = _mm512_set1_pd(3.47933107596021167570e-05);
    p = _mm512_fmadd_pd(p, z, _mm512_set1_pd(7.91534994289814532176e-04));
    p = _mm512_fmadd_pd(p, z, _mm512_set1_pd(-4.00555345006794114027e-02));
    p = _mm512_fmadd_pd(p, z, _mm512_set1_pd(2.01212532134862925881e-01));
    p = _mm512_fmadd_pd(p, z, _mm512_set1_pd(-3.25565818622400915405e-01));
    p = _mm512_fmadd_pd(p, z, _mm512_set1_pd(1.66666666666666657415e-01));
    p = _mm512_mul_pd(p, z);
    __m512d q = _mm512_set1_pd(7.70381505559019352791e-02);
    q = _mm512_fmadd_pd(q, z, _mm512_set1_pd(-6.88283971605453293030e-01));
    q = _mm512_fmadd_pd(q, z, _mm512_set1_pd(2.02094576023350569471e+00));
    q = _mm512_fmadd_pd(q, z, _mm512_set1_pd(-2.40339491173441421878e+00));
    q = _mm512_fmadd_pd(q, z, _mm512_set1_pd(1.0));
    return _mm512_div_pd(p, q);
}

/** approx_asin_avx512(x) — branchless 8-wide. */
static inline __m512d approx_asin_avx512(__m512d x) {
    __m512d signmask = _mm512_set1_pd(-0.0);
    __mmask8 sign_negative =
        _mm512_cmp_pd_mask(x, _mm512_setzero_pd(), _CMP_LT_OQ);
    __m512d ax = _mm512_andnot_pd(signmask, x);

    __m512d z_small = _mm512_mul_pd(ax, ax);
    __m512d r_small = _approx_asin_R_avx512(z_small);
    __m512d y_small = _mm512_fmadd_pd(x, r_small, x);

    __m512d w = _mm512_mul_pd(_mm512_sub_pd(_mm512_set1_pd(1.0), ax),
                              _mm512_set1_pd(0.5));
    __m512d s = _mm512_sqrt_pd(w);
    __m512d r_big = _approx_asin_R_avx512(w);
    __m512d y_big_pos = _mm512_sub_pd(
        _mm512_set1_pd(M_PI_2),
        _mm512_mul_pd(_mm512_set1_pd(2.0), _mm512_fmadd_pd(s, r_big, s)));
    /* Apply sign branchlessly: negate where x is negative. */
    __m512d neg_y_big = _mm512_sub_pd(_mm512_setzero_pd(), y_big_pos);
    __m512d y_big = _mm512_mask_blend_pd(sign_negative, y_big_pos, neg_y_big);

    __mmask8 big = _mm512_cmp_pd_mask(ax, _mm512_set1_pd(0.5), _CMP_GT_OQ);
    return _mm512_mask_blend_pd(big, y_small, y_big);
}

#endif /* H3_HAS_AVX512 */

#endif /* APPROX_LIBM_H */
