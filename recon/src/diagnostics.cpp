#include "einstar/recon/diagnostics.hpp"

#include <algorithm>
#include <cmath>
#include <limits>

#include <opencv2/core/persistence.hpp>
#include <opencv2/imgproc.hpp>

namespace einstar::recon {

cv::Mat colorize_scalar(const cv::Mat& values, const cv::Mat& valid, double lo, double hi,
                        int colormap) {
  cv::Mat gray(values.size(), CV_8UC1, cv::Scalar(0));
  const double span = std::max(hi - lo, 1e-12);
  for (int v = 0; v < values.rows; ++v) {
    const float* val = values.ptr<float>(v);
    const uchar* ok = valid.empty() ? nullptr : valid.ptr<uchar>(v);
    uchar* g = gray.ptr<uchar>(v);
    for (int u = 0; u < values.cols; ++u) {
      if (ok && !ok[u]) continue;
      if (!std::isfinite(val[u]) || val[u] <= 0) continue;
      g[u] = cv::saturate_cast<uchar>(255.0 * (val[u] - lo) / span);
    }
  }
  cv::Mat color;
  cv::applyColorMap(gray, color, colormap);
  if (!valid.empty()) color.setTo(cv::Scalar(0, 0, 0), valid == 0);
  return color;
}

cv::Mat colorize_confidence(const cv::Mat& confidence) {
  cv::Mat g(confidence.size(), CV_8UC1, cv::Scalar(0));
  for (int v = 0; v < confidence.rows; ++v)
    for (int u = 0; u < confidence.cols; ++u)
      g.at<uchar>(v, u) = cv::saturate_cast<uchar>(255.0f * confidence.at<float>(v, u));
  cv::Mat color;
  cv::applyColorMap(g, color, cv::COLORMAP_TURBO);
  return color;
}

cv::Mat render_preview(const PointCloud& cloud, cv::Size size, float yaw_deg, float pitch_deg) {
  cv::Mat img(size, CV_8UC3, cv::Scalar(8, 8, 10));
  if (cloud.points.empty()) return img;
  const double yaw = yaw_deg * CV_PI / 180.0, pitch = pitch_deg * CV_PI / 180.0;
  const double cy = std::cos(yaw), sy = std::sin(yaw);
  const double cp = std::cos(pitch), sp = std::sin(pitch);
  const cv::Matx33d Ry(cy, 0, sy, 0, 1, 0, -sy, 0, cy);
  const cv::Matx33d Rx(1, 0, 0, 0, cp, -sp, 0, sp, cp);
  const cv::Matx33d R = Rx * Ry;
  std::vector<cv::Point3d> rot;
  rot.reserve(cloud.points.size());
  double minx = 1e18, maxx = -1e18, miny = 1e18, maxy = -1e18;
  for (const auto& p : cloud.points) {
    const cv::Vec3d q = R * cv::Vec3d(p.x, p.y, p.z);
    rot.emplace_back(q[0], q[1], q[2]);
    minx = std::min(minx, q[0]); maxx = std::max(maxx, q[0]);
    miny = std::min(miny, q[1]); maxy = std::max(maxy, q[1]);
  }
  const double span = std::max(maxx - minx, maxy - miny);
  if (span <= 0) return img;
  const double scale = 0.9 * std::min(size.width, size.height) / span;
  const double ox = size.width * 0.5 - 0.5 * (minx + maxx) * scale;
  const double oy = size.height * 0.5 + 0.5 * (miny + maxy) * scale;
  // Display-only grey-world balance: the raw colour stream is underexposed
  // and green-dominant.  The PLY keeps the measured sensor RGB.
  double mb = 0, mg = 0, mr = 0;
  std::size_t ncol = 0;
  for (const auto& c : cloud.colors) {
    if (c[0] + c[1] + c[2] == 0) continue;
    mb += c[0]; mg += c[1]; mr += c[2]; ++ncol;
  }
  double gb = 1, gg = 1, gr = 1;
  if (ncol > 0) {
    mb /= ncol; mg /= ncol; mr /= ncol;
    const double mean = (mb + mg + mr) / 3.0;
    gb = mean / std::max(mb, 1.0); gg = mean / std::max(mg, 1.0);
    gr = mean / std::max(mr, 1.0);
  }
  std::vector<float> zbuf(size.area(), std::numeric_limits<float>::infinity());
  for (std::size_t i = 0; i < rot.size(); ++i) {
    const int u = cvRound(ox + rot[i].x * scale);
    const int v = cvRound(oy - rot[i].y * scale);
    if (u < 0 || v < 0 || u >= size.width || v >= size.height) continue;
    const float z = float(rot[i].z);
    for (int dv = 0; dv < 2; ++dv)
      for (int du = 0; du < 2; ++du) {
        const int uu = u + du, vv = v + dv;
        if (uu >= size.width || vv >= size.height) continue;
        const std::size_t k = std::size_t(vv) * size.width + uu;
        if (z >= zbuf[k]) continue;
        zbuf[k] = z;
        if (!cloud.colors.empty()) {
          // Display-only gamma: the genuine colour stream is underexposed
          // (mean ~29), so a linear preview is almost black.  The PLY keeps
          // the raw sensor RGB; this affects the diagnostic image only.
          const cv::Vec3b& c = cloud.colors[i];
          auto disp = [&](int v_, double gain) {
            const double x = std::clamp(c[v_] * gain / 255.0, 0.0, 1.0);
            return cv::saturate_cast<uchar>(255.0 * std::pow(x, 0.45));
          };
          img.at<cv::Vec3b>(vv, uu) =
              cv::Vec3b(disp(0, gb), disp(1, gg), disp(2, gr));
        }
        else {
          const double t = std::clamp((rot[i].z - 150.0) / 650.0, 0.0, 1.0);
          img.at<cv::Vec3b>(vv, uu) =
              cv::Vec3b(cv::saturate_cast<uchar>(255 * (1 - t)),
                        cv::saturate_cast<uchar>(80 + 120 * (1 - std::abs(t - 0.5) * 2)),
                        cv::saturate_cast<uchar>(255 * t));
        }
      }
  }
  return img;
}

void write_report_json(const ReconResult& r, const std::string& path) {
  cv::FileStorage fs(path, cv::FileStorage::WRITE | cv::FileStorage::FORMAT_JSON);
  if (!fs.isOpened()) throw std::runtime_error("cannot write report: " + path);
  fs << "schema" << "einstar.recon.report/1";
  fs << "ok" << r.ok << "error" << r.error;
  fs << "calibration" << "{";
  fs << "id" << r.profile.id << "kind" << r.profile.kind
     << "plate_pitch_mm" << r.profile.plate_pitch_mm
     << "color_status" << r.profile.color_status;
  fs << "baseline_mm" << r.rectified.baseline_mm << "f" << r.rectified.f
     << "cx" << r.rectified.cx << "cy" << r.rectified.cy
     << "disp_offset" << r.rectified.disp_offset_full;
  fs << "}";
  fs << "input" << "{";
  fs << "source" << r.stats.source << "frame" << r.stats.frame
     << "sha256" << r.stats.frame_sha256
     << "width" << (r.frames.streams[0].width) << "height" << (r.frames.streams[0].height);
  fs << "}";
  fs << "stereo" << "{";
  fs << "scale" << r.stereo.geometry.scale
     << "min_disparity" << r.stereo.stats.min_disparity
     << "num_disparities" << r.stereo.stats.num_disparities
     << "valid_fraction" << r.stereo.stats.valid_fraction
     << "occluded_fraction" << r.stereo.stats.occluded_fraction
     << "median_disparity" << r.stereo.stats.median_disparity
     << "seconds" << r.stereo.stats.seconds;
  fs << "}";
  fs << "depth" << "{";
  fs << "valid_fraction" << r.depth.stats.valid_fraction
     << "p05_mm" << r.depth.stats.depth_p05_mm
     << "p50_mm" << r.depth.stats.depth_p50_mm
     << "p95_mm" << r.depth.stats.depth_p95_mm
     << "sigma_p50_mm" << r.depth.stats.sigma_p50_mm
     << "sigma_p95_mm" << r.depth.stats.sigma_p95_mm
     << "color_used" << r.depth.color_used
     << "color_bayer" << r.depth.color_bayer
     << "color_status" << r.depth.color_status
     << "color_fraction" << r.depth.stats.color_fraction
     << "seconds" << r.depth.stats.seconds;
  fs << "}";
  fs << "cloud" << "{";
  fs << "points" << (double)r.cloud.points.size()
     << "has_color" << !r.cloud.colors.empty();
  fs << "}";
  fs << "timings" << "{";
  fs << "rectify" << r.stats.rectify_seconds << "stereo" << r.stats.stereo_seconds
     << "depth" << r.stats.depth_seconds << "cloud" << r.stats.cloud_seconds
     << "total" << r.stats.total_seconds;
  fs << "}";
  fs.release();
}
}  // namespace einstar::recon
