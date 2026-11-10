// Synthetic ground truth for end-to-end validation.
//
// A textured fronto-parallel plane at a known depth in the left IR camera frame
// is rendered through the *full* camera model (intrinsics + Brown-Conrady
// distortion) into all three stream images, in raw stream orientation.  The
// reconstruction pipeline therefore exercises loading, rotation mapping,
// undistortion, rectification, matching, depth and PLY exactly as with real
// frames.  Ground truth per pixel is the plane depth.
#pragma once

#include <opencv2/core.hpp>

#include "einstar/recon/frame_set.hpp"
#include "einstar/recon/profile.hpp"

namespace einstar::recon {

struct SyntheticOptions {
  double plane_depth_mm = 350.0;   // left IR camera frame
  int width = 1280, height = 1024;
  std::uint64_t seed = 1;
  double texture_contrast = 60.0;
  double texture_offset = 110.0;
  int blur_sigma = 0;
  bool rotate_to_stream = true;    // produce raw stream orientation
};

// Renders one frame set.  The texture is deterministic from the seed.
FrameSet render_synthetic_frame_set(const CalibrationProfile& profile,
                                    const SyntheticOptions& options = {});
// Ground-truth depth map in the left IR camera frame (CV_32FC1, mm) for a
// fronto-parallel plane.  Outside the valid image it is 0.
cv::Mat synthetic_depth_truth(const CalibrationProfile& profile, double plane_depth_mm,
                              int width = 1280, int height = 1024);

}  // namespace einstar::recon
