// Point-cloud assembly and PLY export with provenance.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include <opencv2/core.hpp>

#include "einstar/recon/depth.hpp"

namespace einstar::recon {

struct PointCloudOptions {
  bool with_color = true;
  float min_confidence = 0.0f;
  bool ascii = false;
};

struct PointCloud {
  std::vector<cv::Point3f> points;   // left IR camera frame, mm
  std::vector<cv::Vec3b> colors;     // BGR, may be empty
  std::vector<float> confidence;
  std::vector<float> sigma_mm;
};

struct PlyMetadata {
  std::string profile_id;
  std::string profile_kind;
  std::string source;
  std::string frame;
  std::string frame_name = "ir_left";
  double plate_pitch_mm = 28.0;
  bool has_color = false;
  std::string tool = "einstar_recon";
  std::string extra;                 // free-form single-line comment
};

PointCloud make_cloud(const DepthResult& depth, const PointCloudOptions& options = {});

// Writes a PLY 1.0 file.  The header records units (mm), the coordinate frame,
// the calibration id/kind and the source; ASCII or binary little-endian.
// Throws std::runtime_error on I/O failure.  Returns the vertex count.
std::size_t write_ply(const std::string& path, const PointCloud& cloud,
                      const PlyMetadata& metadata);

// Reads back the vertex count and header comments (tests / diagnostics).
struct PlyInfo {
  std::size_t vertex_count = 0;
  bool ascii = false;
  bool has_color = false;
  std::vector<std::string> comments;
};
PlyInfo read_ply_info(const std::string& path);

}  // namespace einstar::recon
