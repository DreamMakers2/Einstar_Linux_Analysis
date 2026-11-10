// Metric depth and 3-D points from a rectified disparity map.
//
// The reconstructed points are expressed in the LEFT IR CAMERA frame (mm,
// OpenCV axes: x right, y down, z forward).  The stereo pipeline works in the
// rectified frame, so points are rotated back with R1^T.  Every depth pixel
// carries an uncertainty derived from disparity noise and the calibration
// scale; the scale term is reported separately because the plate pitch is a
// pure gauge (the unresolved ~1% factory-vs-nominal difference).
#pragma once

#include <opencv2/core.hpp>

#include "einstar/recon/frame_set.hpp"
#include "einstar/recon/profile.hpp"
#include "einstar/recon/rectify.hpp"
#include "einstar/recon/stereo.hpp"

namespace einstar::recon {

struct DepthOptions {
  double disparity_sigma_px = 0.2;  // 1-sigma disparity noise (scaled pixels)
  double scale_rel_sigma = 0.01;    // 1% plate-pitch / calibration scale term
  bool color = true;                // sample the colour camera when valid
  float min_confidence = 0.0f;      // below this, depth is marked invalid
};

struct DepthStats {
  std::int64_t valid_pixels = 0;
  double valid_fraction = 0;
  double depth_p05_mm = 0, depth_p50_mm = 0, depth_p95_mm = 0;
  double sigma_p50_mm = 0, sigma_p95_mm = 0;
  double color_fraction = 0;        // share of valid pixels with colour
  double seconds = 0;
};

struct DepthResult {
  cv::Mat z_mm;        // CV_32FC1, 0 = invalid, left IR camera frame
  cv::Mat sigma_mm;    // CV_32FC1, 1-sigma depth uncertainty (random part)
  cv::Mat sigma_total_mm;  // including the scale/systematic term
  cv::Mat confidence;  // CV_32FC1
  cv::Mat valid;       // CV_8UC1
  cv::Mat xyz_mm;      // CV_32FC3, left IR camera frame
  cv::Mat bgr;         // CV_8UC3, colour sampled (empty when disabled/invalid)
  bool color_used = false;
  std::string color_status;  // "ok" | "disabled" | "profile-inconsistent" | ...
  DepthStats stats;
  bool ok = false;
  std::string error;
};

DepthResult compute_depth(const FrameSet& frames, const CalibrationProfile& profile,
                          const RectifiedPair& pair, const StereoResult& stereo,
                          const DepthOptions& options = {});

}  // namespace einstar::recon
