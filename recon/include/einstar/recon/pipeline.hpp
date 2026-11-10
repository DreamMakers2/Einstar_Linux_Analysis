// One acquisition-to-reconstruction path for live and replay data.
//
//   FrameSet + CalibrationProfile
//     -> rectify() -> compute_stereo() -> compute_depth() -> make_cloud()
//     -> write_outputs() (PLY, maps, report, preview)
//
// The same function is used for recorded streams, the driver simulator and
// (when available) live frames; nothing in the geometry depends on where the
// pixels came from.  Every output embeds the calibration id and the input
// hashes, and reconstruct() is deterministic for identical inputs.
#pragma once

#include <string>

#include "einstar/recon/depth.hpp"
#include "einstar/recon/pointcloud.hpp"
#include "einstar/recon/rectify.hpp"
#include "einstar/recon/stereo.hpp"

namespace einstar::recon {

struct ReconOptions {
  RectifyOptions rectify;
  StereoOptions stereo;
  DepthOptions depth;
  PointCloudOptions cloud;
};

struct ReconStats {
  std::string profile_id;
  std::string profile_kind;
  std::string source;
  std::string frame;
  std::string frame_sha256;         // hash of the three raw stream images
  double rectify_seconds = 0;
  double stereo_seconds = 0;
  double depth_seconds = 0;
  double cloud_seconds = 0;
  double total_seconds = 0;
  std::int64_t points = 0;
  double depth_p05_mm = 0, depth_p50_mm = 0, depth_p95_mm = 0;
  double sigma_p50_mm = 0;
  double valid_fraction = 0;
  bool color = false;
};

struct ReconResult {
  CalibrationProfile profile;
  FrameSet frames;
  RectifiedPair rectified;
  StereoResult stereo;
  DepthResult depth;
  PointCloud cloud;
  ReconStats stats;
  bool ok = false;
  std::string error;
};

ReconResult reconstruct(const FrameSet& frames, const CalibrationProfile& profile,
                        const ReconOptions& options = {});

struct OutputOptions {
  std::string prefix = "recon";
  bool write_ply = true;
  bool write_maps = true;       // rectified pair, disparity, depth, confidence
  bool write_preview = true;    // shaded point-cloud preview PNG
  bool write_report = true;     // report.json
  bool ascii_ply = false;
};

// Writes outputs into `outdir` (created if needed) and returns the PLY path
// (empty when write_ply is false).  Throws std::runtime_error on I/O failure.
std::string write_outputs(const ReconResult& result, const std::string& outdir,
                          const OutputOptions& options = {});

}  // namespace einstar::recon
