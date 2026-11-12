// Stereo correspondence for the Einstar IR pair.
//
// Baseline is OpenCV SGBM on a rectified, optionally contrast-normalised pair,
// with a disparity search window derived from the metric depth range.  The
// matcher must handle the real hardware pair: the two IR images can differ in
// exposure by more than 2x, the projector texture is fine speckle, and the
// verged geometry means the disparity range is hundreds of pixels (the pair is
// NOT rectified in hardware).  Sub-pixel disparity, left-right consistency,
// occlusion labelling and a [0,1] confidence are part of the contract.
#pragma once

#include <opencv2/core.hpp>

#include "einstar/recon/rectify.hpp"

namespace einstar::recon {

struct StereoOptions {
  double scale = 0.5;               // matching resolution (0.5 is the default)
  int block_size = 5;
  int p1_factor = 8;
  int p2_factor = 96;
  int uniqueness_ratio = 10;
  int speckle_window = 100;
  int speckle_range = 2;
  bool equalize = false;            // CLAHE on the rectified pair
  int min_disparity_alignment = 16; // SGBM requires a multiple of 16
  double min_depth_mm = 150.0;
  double max_depth_mm = 800.0;
  double lr_tolerance_px = 1.0;     // left-right consistency threshold
  bool left_right_check = true;     // false skips the mirrored second pass
                                    // (half the matching cost, no occlusion
                                    // rejection -- measured, opt-in only)
  bool compute_confidence = true;
  bool subpixel = true;             // SGBM already returns 1/16 px
};

struct StereoStats {
  double valid_fraction = 0;
  double occluded_fraction = 0;
  double median_disparity = 0;
  double p05_disparity = 0, p95_disparity = 0;
  int min_disparity = 0;
  int num_disparities = 0;
  double seconds = 0;
  int width = 0, height = 0;
};

struct StereoResult {
  // CV_32FC1, scaled rectified pixels.  d = physical_shift + min_disparity,
  // so the *physical* pixel shift between the rectified images is
  // (d - min_disparity); geometry.disp_offset = min_disparity + P_offset makes
  // depth_at(d) = f*B/(d - disp_offset) exact.  The search covers the physical
  // range starting at min_disparity, so map values start at 2*min_disparity.
  // Invalid pixels are <= 0.
  cv::Mat disparity;
  cv::Mat confidence;    // CV_32FC1 in [0,1]
  cv::Mat valid;         // CV_8UC1, 255 = usable
  cv::Mat occluded;      // CV_8UC1, 255 = left-occluded / inconsistent
  StereoGeometry geometry;
  StereoStats stats;
  bool ok = false;
  std::string error;
};

StereoResult compute_stereo(const RectifiedPair& pair,
                            const StereoOptions& options = {});

}  // namespace einstar::recon
