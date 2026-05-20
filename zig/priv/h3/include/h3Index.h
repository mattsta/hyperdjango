/*
 * Copyright 2016-2018, 2020, 2026 Uber Technologies, Inc.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *         http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
/** @file h3Index.h
 * @brief   H3Index functions.
 */

#ifndef H3INDEX_H
#define H3INDEX_H

#include "baseCells.h"
#include "faceijk.h"
#include "h3api.h"

// define's of constants and macros for bitwise manipulation of H3Index's.

/** The number of bits in an H3 index. */
#define H3_NUM_BITS 64

/** The bit offset of the max resolution digit in an H3 index. */
#define H3_MAX_OFFSET 63

/** The bit offset of the mode in an H3 index. */
#define H3_MODE_OFFSET 59

/** The bit offset of the base cell in an H3 index. */
#define H3_BC_OFFSET 45

/** The bit offset of the resolution in an H3 index. */
#define H3_RES_OFFSET 52

/** The bit offset of the reserved bits in an H3 index. */
#define H3_RESERVED_OFFSET 56

/** The number of bits in a single H3 resolution digit. */
#define H3_PER_DIGIT_OFFSET 3

/** 1 in the highest bit, 0's everywhere else. */
#define H3_HIGH_BIT_MASK ((uint64_t)(1) << H3_MAX_OFFSET)

/** 0 in the highest bit, 1's everywhere else. */
#define H3_HIGH_BIT_MASK_NEGATIVE (~H3_HIGH_BIT_MASK)

/** 1's in the 4 mode bits, 0's everywhere else. */
#define H3_MODE_MASK ((uint64_t)(15) << H3_MODE_OFFSET)

/** 0's in the 4 mode bits, 1's everywhere else. */
#define H3_MODE_MASK_NEGATIVE (~H3_MODE_MASK)

/** 1's in the 7 base cell bits, 0's everywhere else. */
#define H3_BC_MASK ((uint64_t)(127) << H3_BC_OFFSET)

/** 0's in the 7 base cell bits, 1's everywhere else. */
#define H3_BC_MASK_NEGATIVE (~H3_BC_MASK)

/** 1's in the 4 resolution bits, 0's everywhere else. */
#define H3_RES_MASK (UINT64_C(15) << H3_RES_OFFSET)

/** 0's in the 4 resolution bits, 1's everywhere else. */
#define H3_RES_MASK_NEGATIVE (~H3_RES_MASK)

/** 1's in the 3 reserved bits, 0's everywhere else. */
#define H3_RESERVED_MASK ((uint64_t)(7) << H3_RESERVED_OFFSET)

/** 0's in the 3 reserved bits, 1's everywhere else. */
#define H3_RESERVED_MASK_NEGATIVE (~H3_RESERVED_MASK)

/** 1's in the 3 bits of res 15 digit bits, 0's everywhere else. */
#define H3_DIGIT_MASK ((uint64_t)(7))

/** 0's in the 7 base cell bits, 1's everywhere else. */
#define H3_DIGIT_MASK_NEGATIVE (~H3_DIGIT_MASK)

/**
 * H3 index with mode 0, res 0, base cell 0, and 7 for all index digits.
 * Typically used to initialize the creation of an H3 cell index, which
 * expects all direction digits to be 7 beyond the cell's resolution.
 */
#define H3_INIT (UINT64_C(35184372088831))

/**
 * Gets the highest bit of the H3 index.
 */
#define H3_GET_HIGH_BIT(h3)                                                    \
    ((int)((((h3) & H3_HIGH_BIT_MASK) >> H3_MAX_OFFSET)))

/**
 * Sets the highest bit of the h3 to v.
 */
#define H3_SET_HIGH_BIT(h3, v)                                                 \
    (h3) = (((h3) & H3_HIGH_BIT_MASK_NEGATIVE) |                               \
            (((uint64_t)(v)) << H3_MAX_OFFSET))

/**
 * Gets the integer mode of h3.
 */
#define H3_GET_MODE(h3) ((int)((((h3) & H3_MODE_MASK) >> H3_MODE_OFFSET)))

/**
 * Sets the integer mode of h3 to v.
 */
#define H3_SET_MODE(h3, v)                                                     \
    (h3) =                                                                     \
        (((h3) & H3_MODE_MASK_NEGATIVE) | (((uint64_t)(v)) << H3_MODE_OFFSET))

/**
 * Gets the integer base cell of h3.
 */
#define H3_GET_BASE_CELL(h3) ((int)((((h3) & H3_BC_MASK) >> H3_BC_OFFSET)))

/**
 * Sets the integer base cell of h3 to bc.
 */
#define H3_SET_BASE_CELL(h3, bc)                                               \
    (h3) = (((h3) & H3_BC_MASK_NEGATIVE) | (((uint64_t)(bc)) << H3_BC_OFFSET))

/**
 * Gets the integer resolution of h3.
 */
#define H3_GET_RESOLUTION(h3) ((int)((((h3) & H3_RES_MASK) >> H3_RES_OFFSET)))

/**
 * Sets the integer resolution of h3.
 */
#define H3_SET_RESOLUTION(h3, res)                                             \
    (h3) =                                                                     \
        (((h3) & H3_RES_MASK_NEGATIVE) | (((uint64_t)(res)) << H3_RES_OFFSET))

/**
 * Gets the resolution res integer digit (0-7) of h3.
 */
#define H3_GET_INDEX_DIGIT(h3, res)                                            \
    ((Direction)((((h3) >> ((MAX_H3_RES - (res)) * H3_PER_DIGIT_OFFSET)) &     \
                  H3_DIGIT_MASK)))

/**
 * Sets a value in the reserved space. Setting to non-zero may produce invalid
 * indexes.
 */
#define H3_SET_RESERVED_BITS(h3, v)                                            \
    (h3) = (((h3) & H3_RESERVED_MASK_NEGATIVE) |                               \
            (((uint64_t)(v)) << H3_RESERVED_OFFSET))

/**
 * Gets a value in the reserved space. Should always be zero for valid indexes.
 */
#define H3_GET_RESERVED_BITS(h3)                                               \
    ((int)((((h3) & H3_RESERVED_MASK) >> H3_RESERVED_OFFSET)))

/**
 * Sets the resolution res digit of h3 to the integer digit (0-7)
 */
#define H3_SET_INDEX_DIGIT(h3, res, digit)                                     \
    (h3) = (((h3) & ~((H3_DIGIT_MASK                                           \
                       << ((MAX_H3_RES - (res)) * H3_PER_DIGIT_OFFSET)))) |    \
            (((uint64_t)(digit))                                               \
             << ((MAX_H3_RES - (res)) * H3_PER_DIGIT_OFFSET)))

void setH3Index(H3Index *h, int res, int baseCell, Direction initDigit);

/* `isResolutionClassIII` is a parity check on the resolution. It's called
 * thousands of times per call from inside hot loops in `_h3ToFaceIjk*`,
 * `_faceIjkTo*`, `_h3ToCellBoundary`, and `_faceIjkToCellBoundary`. The
 * suite-wide profile showed it consuming 492 self-time samples across 6
 * benches as an out-of-line function call. Inlining collapses each
 * caller to a single `tst`/`AND` instruction. */
static inline int isResolutionClassIII(int r) {
    return r & 1;
}

// Internal functions

int _h3ToFaceIjkWithInitializedFijk(H3Index h, FaceIJK *fijk);
H3Error _h3ToFaceIjk(H3Index h, FaceIJK *fijk);
H3Index _faceIjkToH3(const FaceIJK *fijk, int res);

/* `_h3LeadingNonZeroDigit` returns the highest-resolution non-zero digit in
 * an H3 index — i.e. the digit at the smallest r in [1..res] that is
 * non-zero, or CENTER_DIGIT (0) if all digits in that range are zero.
 *
 * The original implementation walked r=1..res calling H3_GET_INDEX_DIGIT,
 * which is O(res) (up to 15 iterations). This implementation does the
 * same job in constant time using `__builtin_clzll` to find the highest
 * set bit in the masked digit area.
 *
 * It's `static inline` in the header because:
 *   - the function is small and very hot (830+ samples / 4 benches in the
 *     suite-wide profile, called from 8+ sites in algos.c and h3Index.c),
 *   - the H3 layout it depends on is already known to all callers, so
 *     inlining is a free win across translation units.
 *
 * Bit layout reminder: digit at resolution r occupies bits
 * [(MAX_H3_RES - r) * 3, (MAX_H3_RES - r) * 3 + 2] of the H3Index. So
 * r=1 sits at bits 42..44, r=15 at bits 0..2. The "valid digit area" for
 * an index with resolution `res` is bits [(MAX_H3_RES - res) * 3, 44]. */
static inline Direction _h3LeadingNonZeroDigit(H3Index h) {
    int res = H3_GET_RESOLUTION(h);
    if (res == 0) {
        return CENTER_DIGIT;
    }
    /* Top of the digit area: bit 44 (the 3rd bit of r=1's field). */
    const uint64_t topMask = (UINT64_C(1) << 45) - UINT64_C(1);
    /* Bottom of the valid area: bit (MAX_H3_RES - res) * 3. */
    int lowBit = (MAX_H3_RES - res) * H3_PER_DIGIT_OFFSET;
    uint64_t bottomMask = ~((UINT64_C(1) << lowBit) - UINT64_C(1));
    uint64_t bits = h & topMask & bottomMask;
    if (bits == 0) {
        return CENTER_DIGIT;
    }
    /* Highest set bit's position is 63 - clzll(bits). The 3-bit digit
     * field containing it starts at the largest multiple-of-3 position
     * <= that. Since 44 / 3 == 14 (integer), this aligns naturally to
     * the per-digit field boundaries. */
    int hi = 63 - __builtin_clzll(bits);
    int fieldStart = (hi / H3_PER_DIGIT_OFFSET) * H3_PER_DIGIT_OFFSET;
    return (Direction)((h >> fieldStart) & H3_DIGIT_MASK);
}

/* `_h3IsPentagon` is the in-tree fast equivalent of the public
 * `H3_EXPORT(isPentagon)`. The public function survives unchanged for
 * ABI; this header-only inline lets internal callers (32 sites across
 * algos.c, vertex.c, polyfill.c, h3Index.c, etc.) collapse pentagon
 * checks to two already-inlined helpers without paying the cross-TU
 * function-call overhead.
 *
 * The body is identical to the exported function:
 *   _isBaseCellPentagon(base) && !_h3LeadingNonZeroDigit(h)
 * Both helpers are themselves `static inline`, so the whole check folds
 * into a couple of bit operations at every call site. */
static inline int _h3IsPentagon(H3Index h) {
    return _isBaseCellPentagon(H3_GET_BASE_CELL(h)) &&
           !_h3LeadingNonZeroDigit(h);
}

/* The four `_h3Rotate*` helpers are bounded loops of length [1..res] (max
 * 15 iterations) each. They're called from `h3NeighborRotations` and
 * from the `localij` and base-cell-overage paths. Inlining across TUs
 * lets the compiler fold the per-digit rotation into the surrounding
 * pentagon-adjustment switch in `h3NeighborRotations`.
 *
 * Order matters: `_h3RotatePent60c{cw,w}` reference both
 * `_h3LeadingNonZeroDigit` (defined above) and `_h3Rotate60c{cw,w}`, so
 * the non-pentagon variants must come first. */
static inline H3Index _h3Rotate60ccw(H3Index h) {
    for (int r = 1, res = H3_GET_RESOLUTION(h); r <= res; r++) {
        Direction oldDigit = H3_GET_INDEX_DIGIT(h, r);
        H3_SET_INDEX_DIGIT(h, r, _rotate60ccw(oldDigit));
    }
    return h;
}

static inline H3Index _h3Rotate60cw(H3Index h) {
    for (int r = 1, res = H3_GET_RESOLUTION(h); r <= res; r++) {
        H3_SET_INDEX_DIGIT(h, r, _rotate60cw(H3_GET_INDEX_DIGIT(h, r)));
    }
    return h;
}

/* Precomputed n-step rotation tables. The non-pentagon rotation cycle is
 * 1 -> 5 -> 4 -> 6 -> 2 -> 3 -> 1 (period 6); 0 (CENTER) and 7 (INVALID)
 * are identity. _rotate60ccw_n[n][d] = _rotate60ccw applied n times to
 * digit d, with n in [0..5]. Used to fuse the multi-pass rotation in
 * h3NeighborRotations into a single digit walk. */
static const Direction _rotate60ccw_n_tbl[6][8] = {
    {0, 1, 2, 3, 4, 5, 6, 7}, {0, 5, 3, 1, 6, 4, 2, 7},
    {0, 4, 1, 5, 2, 6, 3, 7}, {0, 6, 5, 4, 3, 2, 1, 7},
    {0, 2, 4, 6, 1, 3, 5, 7}, {0, 3, 6, 2, 5, 1, 4, 7},
};
static const Direction _rotate60cw_n_tbl[6][8] = {
    {0, 1, 2, 3, 4, 5, 6, 7}, {0, 3, 6, 2, 5, 1, 4, 7},
    {0, 2, 4, 6, 1, 3, 5, 7}, {0, 6, 5, 4, 3, 2, 1, 7},
    {0, 4, 1, 5, 2, 6, 3, 7}, {0, 5, 3, 1, 6, 4, 2, 7},
};

/* Apply _rotate60ccw n times to every digit of h in a single walk.
 * Matches the no-op semantics of `for (int i = 0; i < n; i++)`: any n <= 0
 * returns h unchanged. n is reduced mod 6 first. */
static inline H3Index _h3Rotate60ccw_n(H3Index h, int n) {
    if (n <= 0) {
        return h;
    }
    n = n % 6;
    if (n == 0) {
        return h;
    }
    const Direction *tbl = _rotate60ccw_n_tbl[n];
    for (int r = 1, res = H3_GET_RESOLUTION(h); r <= res; r++) {
        Direction d = H3_GET_INDEX_DIGIT(h, r);
        H3_SET_INDEX_DIGIT(h, r, tbl[d]);
    }
    return h;
}

static inline H3Index _h3Rotate60cw_n(H3Index h, int n) {
    if (n <= 0) {
        return h;
    }
    n = n % 6;
    if (n == 0) {
        return h;
    }
    const Direction *tbl = _rotate60cw_n_tbl[n];
    for (int r = 1, res = H3_GET_RESOLUTION(h); r <= res; r++) {
        Direction d = H3_GET_INDEX_DIGIT(h, r);
        H3_SET_INDEX_DIGIT(h, r, tbl[d]);
    }
    return h;
}

static inline H3Index _h3RotatePent60ccw(H3Index h) {
    /* rotate in place; skip leading 1 digits (k-axis) */
    int foundFirstNonZeroDigit = 0;
    for (int r = 1, res = H3_GET_RESOLUTION(h); r <= res; r++) {
        H3_SET_INDEX_DIGIT(h, r, _rotate60ccw(H3_GET_INDEX_DIGIT(h, r)));
        if (!foundFirstNonZeroDigit && H3_GET_INDEX_DIGIT(h, r) != 0) {
            foundFirstNonZeroDigit = 1;
            /* adjust for deleted k-axes sequence */
            if (_h3LeadingNonZeroDigit(h) == K_AXES_DIGIT) {
                h = _h3Rotate60ccw(h);
            }
        }
    }
    return h;
}

static inline H3Index _h3RotatePent60cw(H3Index h) {
    /* rotate in place; skip leading 1 digits (k-axis) */
    int foundFirstNonZeroDigit = 0;
    for (int r = 1, res = H3_GET_RESOLUTION(h); r <= res; r++) {
        H3_SET_INDEX_DIGIT(h, r, _rotate60cw(H3_GET_INDEX_DIGIT(h, r)));
        if (!foundFirstNonZeroDigit && H3_GET_INDEX_DIGIT(h, r) != 0) {
            foundFirstNonZeroDigit = 1;
            /* adjust for deleted k-axes sequence */
            if (_h3LeadingNonZeroDigit(h) == K_AXES_DIGIT) {
                h = _h3Rotate60cw(h);
            }
        }
    }
    return h;
}

/* Pentagon n-step rotation. Each pent iteration is "rotate every digit
 * once, then if leading-nonzero is K rotate every digit once more". For
 * ccw, that second rotation fires when the original leading-nonzero was
 * JK (since ccw(JK)=K); for cw, when it was IK (cw(IK)=K). Track the
 * leading-nonzero digit as a small state machine over n iterations,
 * count `extras` (K-firings), then collapse to a single digit walk via
 * _h3Rotate60c(c)w_n with total rotations = (n + extras) % 6. */
static inline H3Index _h3RotatePent60ccw_n(H3Index h, int n) {
    if (n <= 0) {
        return h;
    }
    n = n % 6;
    if (n == 0) {
        return h;
    }
    Direction lead = _h3LeadingNonZeroDigit(h);
    int extras = 0;
    for (int i = 0; i < n; i++) {
        if (lead == JK_AXES_DIGIT) {
            extras++;
        }
        lead = _rotate60ccw(lead);
        if (lead == K_AXES_DIGIT) {
            lead = _rotate60ccw(lead);
        }
    }
    return _h3Rotate60ccw_n(h, (n + extras) % 6);
}

static inline H3Index _h3RotatePent60cw_n(H3Index h, int n) {
    if (n <= 0) {
        return h;
    }
    n = n % 6;
    if (n == 0) {
        return h;
    }
    Direction lead = _h3LeadingNonZeroDigit(h);
    int extras = 0;
    for (int i = 0; i < n; i++) {
        if (lead == IK_AXES_DIGIT) {
            extras++;
        }
        lead = _rotate60cw(lead);
        if (lead == K_AXES_DIGIT) {
            lead = _rotate60cw(lead);
        }
    }
    return _h3Rotate60cw_n(h, (n + extras) % 6);
}

/* `_zeroIndexDigits` zeroes out index digits from `start` to `end`, inclusive
 * (no-op if start > end). It's called from `iterInitParent` (every child
 * iterator init) and `cellToCenterChild`. The body is pure bit math, so
 * inlining lets each caller fold the mask construction into surrounding
 * H3 macros. */
static inline H3Index _zeroIndexDigits(H3Index h, int start, int end) {
    if (start > end) {
        return h;
    }
    H3Index m = 0;
    m = ~m;
    m <<= H3_PER_DIGIT_OFFSET * (end - start + 1);
    m = ~m;
    m <<= H3_PER_DIGIT_OFFSET * (MAX_H3_RES - end);
    m = ~m;
    return h & m;
}

H3Error vec3ToCell(const Vec3d *v, int res, H3Index *out);
H3Error cellToVec3(H3Index h3, Vec3d *v);

#endif
