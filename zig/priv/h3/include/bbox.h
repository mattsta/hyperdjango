/*
 * Copyright 2016-2017, 2020-2021, 2026 Uber Technologies, Inc.
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
/** @file bbox.h
 * @brief   Geographic bounding box functions
 */

#ifndef BBOX_H
#define BBOX_H

#include <stdbool.h>

#include "constants.h" /* M_2PI */
#include "h3api.h"
#include "latLng.h"

/** @struct BBox
 *  @brief  Geographic bounding box with coordinates defined in radians
 */
typedef struct {
    double north; ///< north latitude
    double south; ///< south latitude
    double east;  ///< east longitude
    double west;  ///< west longitude
} BBox;

/* === Inline helpers ===
 *
 * Small bbox helpers are `static inline` in this header so the polyfill
 * iterators, gridDisk inner loops, and SIMD batch dispatch can fold them
 * into one or two instructions per call. They aren't individually huge
 * but the call overhead per use is close to the actual work. */

/** Whether the given bounding box crosses the antimeridian. */
static inline bool bboxIsTransmeridian(const BBox *bbox) {
    return bbox->east < bbox->west;
}

/** Height of the bounding box, in rads. */
static inline double bboxHeightRads(const BBox *bbox) {
    return bbox->north - bbox->south;
}

/** Width of the bounding box, in rads (handles transmeridian). */
static inline double bboxWidthRads(const BBox *bbox) {
    return bboxIsTransmeridian(bbox) ? bbox->east - bbox->west + (double)M_2PI
                                     : bbox->east - bbox->west;
}

/** Whether two bounding boxes are strictly equal. */
static inline bool bboxEquals(const BBox *b1, const BBox *b2) {
    return b1->north == b2->north && b1->south == b2->south &&
           b1->east == b2->east && b1->west == b2->west;
}

/** Get the center of a bounding box. */
static inline void bboxCenter(const BBox *bbox, LatLng *center) {
    center->lat = (bbox->north + bbox->south) * 0.5;
    /* If the bbox crosses the antimeridian, shift east 360 degrees. */
    double east =
        bboxIsTransmeridian(bbox) ? bbox->east + (double)M_2PI : bbox->east;
    center->lng = constrainLng((east + bbox->west) * 0.5);
}

/** Whether the bounding box contains a given point. */
static inline bool bboxContains(const BBox *bbox, const LatLng *point) {
    return point->lat >= bbox->south && point->lat <= bbox->north &&
           (bboxIsTransmeridian(bbox) ? /* transmeridian */
                (point->lng >= bbox->west || point->lng <= bbox->east)
                                      : /* standard */
                (point->lng >= bbox->west && point->lng <= bbox->east));
}

/** Determine the longitude normalization scheme for two bounding boxes. */
static inline void bboxNormalization(const BBox *a, const BBox *b,
                                     LongitudeNormalization *aNormalization,
                                     LongitudeNormalization *bNormalization) {
    bool aIsTransmeridian = bboxIsTransmeridian(a);
    bool bIsTransmeridian = bboxIsTransmeridian(b);
    bool aToBTrendsEast = a->west - b->east < b->west - a->east;
    *aNormalization = !aIsTransmeridian  ? NORMALIZE_NONE
                      : bIsTransmeridian ? NORMALIZE_EAST
                      : aToBTrendsEast   ? NORMALIZE_EAST
                                         : NORMALIZE_WEST;
    *bNormalization = !bIsTransmeridian  ? NORMALIZE_NONE
                      : aIsTransmeridian ? NORMALIZE_EAST
                      : aToBTrendsEast   ? NORMALIZE_WEST
                                         : NORMALIZE_EAST;
}

/** Whether two bounding boxes overlap. */
static inline bool bboxOverlapsBBox(const BBox *a, const BBox *b) {
    /* Latitude overlap (early-out for the common no-overlap case). */
    if (a->north < b->south || a->south > b->north) {
        return false;
    }
    LongitudeNormalization aNorm;
    LongitudeNormalization bNorm;
    bboxNormalization(a, b, &aNorm, &bNorm);
    if (normalizeLng(a->east, aNorm) < normalizeLng(b->west, bNorm) ||
        normalizeLng(a->west, aNorm) > normalizeLng(b->east, bNorm)) {
        return false;
    }
    return true;
}

/** Whether one bounding box contains another. */
static inline bool bboxContainsBBox(const BBox *a, const BBox *b) {
    if (a->north < b->north || a->south > b->south) {
        return false;
    }
    LongitudeNormalization aNorm;
    LongitudeNormalization bNorm;
    bboxNormalization(a, b, &aNorm, &bNorm);
    return normalizeLng(a->west, aNorm) <= normalizeLng(b->west, bNorm) &&
           normalizeLng(a->east, aNorm) >= normalizeLng(b->east, bNorm);
}

/** Scale a bounding box around its center (in-place). Mirror of the
 * original out-of-line implementation; preserves the +/- 2*PI single-
 * step wrap so semantics on extreme inputs match exactly. */
static inline void scaleBBox(BBox *bbox, double scale) {
    double width = bboxWidthRads(bbox);
    double height = bboxHeightRads(bbox);
    double widthBuffer = (width * scale - width) * 0.5;
    double heightBuffer = (height * scale - height) * 0.5;
    /* Scale north/south, clamp to latitude domain. */
    bbox->north += heightBuffer;
    if (bbox->north > M_PI_2) {
        bbox->north = M_PI_2;
    }
    bbox->south -= heightBuffer;
    if (bbox->south < -M_PI_2) {
        bbox->south = -M_PI_2;
    }
    /* Scale east/west, single +/- 2*PI wrap into longitude domain. */
    bbox->east += widthBuffer;
    if (bbox->east > M_PI) {
        bbox->east -= (double)M_2PI;
    }
    if (bbox->east < -M_PI) {
        bbox->east += (double)M_2PI;
    }
    bbox->west -= widthBuffer;
    if (bbox->west > M_PI) {
        bbox->west -= (double)M_2PI;
    }
    if (bbox->west < -M_PI) {
        bbox->west += (double)M_2PI;
    }
}

/* === Out-of-line declarations (bigger or rarely-called helpers) === */
CellBoundary bboxToCellBoundary(const BBox *bbox);
H3Error bboxHexEstimate(const BBox *bbox, int res, int64_t *out);
H3Error lineHexEstimate(const LatLng *origin, const LatLng *destination,
                        int res, int64_t *out);

#endif
