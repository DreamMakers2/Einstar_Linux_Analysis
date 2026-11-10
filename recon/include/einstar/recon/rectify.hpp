// Stereo rectification of the IR pair (Bouguet / OpenCV convention).
//
// Input order is the calibration order: left IR = Camera::IrLeft, right IR =
// Camera::IrRight, with X_right = R X_left + T.  The output pair is rectified
// so that epipolar lines are horizontal and the right camera centre lies on
// +x.  Rectification is a pure function of the profile and the image size;
// the maps are reused for every frame of a session.
#pragma once

#include <opencv2/core.hpp>

#include "einstar/recon/frame_set.hpp"
#include "einstar/recon/profile.hpp"

namespace einstar::recon {

struct RectifyOptions {
  // OpenCV stereoRectify alpha: -1 keeps every source pixel (recommended; the
  // pair is verged by ~22 deg and alpha=0 would zoom to ~1.9x focal length).
  double alpha = -1.0;
  // Erode the valid masks by this many pixels (matching window safety).
  int border_erode = 5;
};

struct RectifiedPair {
  cv::Mat left, right;          // CV_8UC1, full resolution, rectified
  cv::Mat valid_left, valid_right;  // CV_8UC1 255 = sample inside source
  cv::Matx33d K;                // full-resolution rectified left intrinsics
  double f = 0, cx = 0, cy = 0;
  double baseline_mm = 0;
  double disp_offset_full = 0;  // P1[0,2] - P2[0,2] (constant offset)
  cv::Mat R1, R2, P1, P2, Q;    // CV_64F, as produced by stereoRectify
  cv::Size size;
  bool ok = false;
  std::string error;
};

// Rectifies one frame set.  Throws std::runtime_error only for unusable input
// (missing cameras, invalid profile); geometric failure is returned in
// RectifiedPair::ok/error.
RectifiedPair rectify(const FrameSet& frames, const CalibrationProfile& profile,
                      const RectifyOptions& options = {});

struct StereoGeometry {
  double scale = 1.0;
  double f = 0, cx = 0, cy = 0;     // scaled rectified left intrinsics
  double baseline_mm = 0;
  double disp_offset = 0;           // scaled constant disparity offset
  cv::Size size;
  // Disparity-map value of a point at depth z: f*baseline/z + disp_offset.
  // disp_offset includes the matcher's search-window minimum, so the physical
  // pixel shift between the rectified images is (disparity - disp_offset).
  double disparity_at(double depth_mm) const;
  double depth_at(double disparity) const;
};

StereoGeometry scaled_geometry(const RectifiedPair& pair, double scale);

}  // namespace einstar::recon
