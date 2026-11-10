#include "einstar/recon/pointcloud.hpp"

#include <cstring>
#include <fstream>
#include <sstream>
#include <stdexcept>

namespace einstar::recon {

PointCloud make_cloud(const DepthResult& depth, const PointCloudOptions& options) {
  PointCloud cloud;
  if (!depth.ok || depth.xyz_mm.empty()) return cloud;
  const bool use_color = options.with_color && !depth.bgr.empty();
  for (int v = 0; v < depth.xyz_mm.rows; ++v)
    for (int u = 0; u < depth.xyz_mm.cols; ++u) {
      if (!depth.valid.at<uchar>(v, u)) continue;
      const float conf = depth.confidence.empty() ? 1.0f : depth.confidence.at<float>(v, u);
      if (conf < options.min_confidence) continue;
      const cv::Vec3f& p = depth.xyz_mm.at<cv::Vec3f>(v, u);
      cloud.points.emplace_back(p[0], p[1], p[2]);
      cloud.confidence.push_back(conf);
      cloud.sigma_mm.push_back(depth.sigma_total_mm.empty()
                                   ? 0.0f
                                   : depth.sigma_total_mm.at<float>(v, u));
      if (use_color) cloud.colors.push_back(depth.bgr.at<cv::Vec3b>(v, u));
    }
  return cloud;
}

namespace {
void put_f32(std::ostream& o, float v) { o.write(reinterpret_cast<const char*>(&v), 4); }
}  // namespace

std::size_t write_ply(const std::string& path, const PointCloud& cloud,
                      const PlyMetadata& meta) {
  std::ofstream o(path, std::ios::binary);
  if (!o) throw std::runtime_error("cannot write " + path);
  const bool ascii = meta.extra.find("ascii") != std::string::npos;  // overridden below
  (void)ascii;
  const std::size_t n = cloud.points.size();
  const bool color = meta.has_color && cloud.colors.size() == n;
  o << "ply\n";
  o << "format " << (meta.extra.rfind("format=ascii", 0) == 0 ? "ascii" : "binary_little_endian")
    << " 1.0\n";
  o << "comment tool=" << meta.tool << "\n";
  o << "comment calibration_id=" << meta.profile_id << "\n";
  o << "comment calibration_kind=" << meta.profile_kind << "\n";
  o << "comment source=" << meta.source << "\n";
  o << "comment frame=" << meta.frame << "\n";
  o << "comment frame_name=" << meta.frame_name << "\n";
  o << "comment units=mm\n";
  o << "comment plate_pitch_mm=" << meta.plate_pitch_mm << "\n";
  if (!meta.extra.empty()) o << "comment " << meta.extra << "\n";
  o << "element vertex " << n << "\n";
  o << "property float x\nproperty float y\nproperty float z\n";
  if (color) o << "property uchar red\nproperty uchar green\nproperty uchar blue\n";
  o << "property float confidence\nproperty float sigma_mm\n";
  o << "end_header\n";
  const bool ascii_out = meta.extra.rfind("format=ascii", 0) == 0;
  for (std::size_t i = 0; i < n; ++i) {
    const auto& p = cloud.points[i];
    if (ascii_out) {
      o << p.x << ' ' << p.y << ' ' << p.z;
      if (color) o << ' ' << int(cloud.colors[i][2]) << ' ' << int(cloud.colors[i][1]) << ' '
                    << int(cloud.colors[i][0]);
      o << ' ' << cloud.confidence[i] << ' ' << cloud.sigma_mm[i] << '\n';
    } else {
      put_f32(o, p.x); put_f32(o, p.y); put_f32(o, p.z);
      if (color) {
        const char c[3] = {char(cloud.colors[i][2]), char(cloud.colors[i][1]),
                           char(cloud.colors[i][0])};
        o.write(c, 3);
      }
      put_f32(o, cloud.confidence[i]);
      put_f32(o, cloud.sigma_mm[i]);
    }
  }
  if (!o) throw std::runtime_error("write failed: " + path);
  return n;
}

PlyInfo read_ply_info(const std::string& path) {
  std::ifstream in(path, std::ios::binary);
  if (!in) throw std::runtime_error("cannot read " + path);
  PlyInfo info;
  std::string line;
  std::getline(in, line);
  if (line.rfind("ply", 0) != 0) throw std::runtime_error(path + ": not a PLY file");
  while (std::getline(in, line)) {
    if (line.rfind("format ascii", 0) == 0) info.ascii = true;
    if (line.rfind("comment ", 0) == 0) info.comments.push_back(line.substr(8));
    if (line.rfind("element vertex ", 0) == 0)
      info.vertex_count = std::stoull(line.substr(std::strlen("element vertex ")));
    if (line.rfind("property uchar red", 0) == 0) info.has_color = true;
    if (line == "end_header") break;
  }
  return info;
}
}  // namespace einstar::recon
