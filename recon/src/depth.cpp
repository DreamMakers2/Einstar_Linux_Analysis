#include "einstar/recon/depth.hpp"

#include <algorithm>
#include <cmath>
#include <chrono>
#include <vector>

#include <opencv2/calib3d.hpp>
#include <opencv2/imgproc.hpp>

namespace einstar::recon {
namespace {
std::vector<double> percentiles(const std::vector<float>& v, std::initializer_list<double> qs) {
  std::vector<double> out;
  if (v.empty()) return std::vector<double>(qs.size(), 0.0);
  std::vector<float> s = v;
  std::sort(s.begin(), s.end());
  for (double q : qs) {
    const double pos = q * (s.size() - 1);
    const auto i = static_cast<std::size_t>(pos);
    const double f = pos - i;
    out.push_back(i + 1 < s.size() ? s[i] * (1 - f) + s[i + 1] * f : s[i]);
  }
  return out;
}
}  // namespace

DepthResult compute_depth(const FrameSet& frames, const CalibrationProfile& profile,
                          const RectifiedPair& pair, const StereoResult& stereo,
                          const DepthOptions& options) {
  const auto t0 = std::chrono::steady_clock::now();
  DepthResult out;
  if (!pair.ok || !stereo.ok) {
    out.error = "rectification or stereo failed";
    return out;
  }
  if (stereo.disparity.empty() || stereo.geometry.f <= 0 || stereo.geometry.baseline_mm <= 0) {
    out.error = "invalid stereo geometry";
    return out;
  }
  const cv::Size sz = stereo.geometry.size;
  if (stereo.disparity.size() != sz || stereo.valid.size() != sz) {
    out.error = "stereo output size mismatch";
    return out;
  }
  out.z_mm = cv::Mat::zeros(sz, CV_32FC1);
  out.sigma_mm = cv::Mat::zeros(sz, CV_32FC1);
  out.sigma_total_mm = cv::Mat::zeros(sz, CV_32FC1);
  out.confidence = cv::Mat::zeros(sz, CV_32FC1);
  out.valid = cv::Mat::zeros(sz, CV_8UC1);
  out.xyz_mm = cv::Mat::zeros(sz, CV_32FC3);
  const StereoGeometry& g = stereo.geometry;
  const cv::Matx33d R1t = cv::Matx33d(pair.R1).t();
  std::vector<float> depths, sigmas;
  depths.reserve(sz.area() / 2);
  sigmas.reserve(sz.area() / 2);
  for (int v = 0; v < sz.height; ++v) {
    const float* drow = stereo.disparity.ptr<float>(v);
    const float* crow = stereo.confidence.ptr<float>(v);
    const uchar* vrow = stereo.valid.ptr<uchar>(v);
    float* zrow = out.z_mm.ptr<float>(v);
    float* srow = out.sigma_mm.ptr<float>(v);
    float* strow = out.sigma_total_mm.ptr<float>(v);
    uchar* okrow = out.valid.ptr<uchar>(v);
    cv::Vec3f* xrow = out.xyz_mm.ptr<cv::Vec3f>(v);
    for (int u = 0; u < sz.width; ++u) {
      if (!vrow[u] || !(drow[u] > 0)) continue;
      const double disp = drow[u];
      const double z = g.depth_at(disp);
      if (!std::isfinite(z) || z < 50.0 || z > 5000.0) continue;
      const double cx = g.cx, cy = g.cy;
      const double xr = (u - cx) * z / g.f;
      const double yr = (v - cy) * z / g.f;
      const cv::Vec3d pl = R1t * cv::Vec3d(xr, yr, z);
      const double sigma_rand = z * z / (g.f * g.baseline_mm) * options.disparity_sigma_px;
      const double sigma_scale = std::abs(pl[2]) * options.scale_rel_sigma;
      const double sigma_tot = std::sqrt(sigma_rand * sigma_rand + sigma_scale * sigma_scale);
      // z_mm is the depth along the LEFT IR camera z axis (same frame as
      // xyz_mm and the PLY), not the rectified-frame z.  The uncertainty
      // formulas use the rectified z (the disparity is measured there); the
      // difference is a rotation of the same error ellipse and is small.
      zrow[u] = static_cast<float>(pl[2]);
      srow[u] = static_cast<float>(sigma_rand);
      strow[u] = static_cast<float>(sigma_tot);
      out.confidence.at<float>(v, u) = std::max(0.0f, std::min(1.0f, crow[u]));
      okrow[u] = 255;
      xrow[u] = cv::Vec3f(static_cast<float>(pl[0]), static_cast<float>(pl[1]),
                          static_cast<float>(pl[2]));
      depths.push_back(static_cast<float>(pl[2]));
      sigmas.push_back(static_cast<float>(sigma_rand));
    }
  }
  out.ok = true;
  out.stats.valid_pixels = static_cast<std::int64_t>(depths.size());
  out.stats.valid_fraction = sz.area() ? double(depths.size()) / sz.area() : 0.0;
  if (!depths.empty()) {
    const auto dp = percentiles(depths, {0.05, 0.5, 0.95});
    out.stats.depth_p05_mm = dp[0];
    out.stats.depth_p50_mm = dp[1];
    out.stats.depth_p95_mm = dp[2];
    const auto sp = percentiles(sigmas, {0.5, 0.95});
    out.stats.sigma_p50_mm = sp[0];
    out.stats.sigma_p95_mm = sp[1];
  }
  // Optional colour registration.  The colour profile is only used when the
  // calibration explicitly marks it as validated; the shipped factory Tex CCF
  // is the known stale record, so the default is to skip it and say so.
  if (!options.color) {
    out.color_status = "disabled";
  } else if (profile.color_status != "ok") {
    out.color_status = "profile-inconsistent (" + profile.color_status + ")";
  } else {
    cv::Mat color_img = calibration_image(frames, Camera::Color);
    if (color_img.empty()) {
      out.color_status = "missing-color-frame";
    } else {
      if (color_img.channels() == 1) cv::cvtColor(color_img, color_img, cv::COLOR_GRAY2BGR);
      const auto& cc = profile.camera(Camera::Color);
      const cv::Matx33d R_lc = relative_rotation(profile, Camera::IrLeft, Camera::Color);
      const cv::Vec3d T_lc = relative_translation(profile, Camera::IrLeft, Camera::Color);
      cv::Mat K, D;
      cv::Mat(cc.K()).copyTo(K);
      cc.dist_mat().copyTo(D);
      out.bgr = cv::Mat::zeros(sz, CV_8UC3);
      std::vector<cv::Point3f> pts;
      std::vector<cv::Point2i> pix;
      for (int v = 0; v < sz.height; ++v)
        for (int u = 0; u < sz.width; ++u)
          if (out.valid.at<uchar>(v, u)) {
            const cv::Vec3f& p = out.xyz_mm.at<cv::Vec3f>(v, u);
            const cv::Vec3d pc = R_lc * cv::Vec3d(p[0], p[1], p[2]) + T_lc;
            pts.emplace_back(float(pc[0]), float(pc[1]), float(pc[2]));
            pix.emplace_back(u, v);
          }
      if (!pts.empty()) {
        std::vector<cv::Point2f> proj;
        cv::projectPoints(pts, cv::Vec3d(0, 0, 0), cv::Vec3d(0, 0, 0), K, D, proj);
        std::int64_t colored = 0;
        for (std::size_t i = 0; i < proj.size(); ++i) {
          const int u = cvRound(proj[i].x), v = cvRound(proj[i].y);
          if (u < 0 || v < 0 || u >= color_img.cols || v >= color_img.rows) continue;
          out.bgr.at<cv::Vec3b>(pix[i].y, pix[i].x) = color_img.at<cv::Vec3b>(v, u);
          ++colored;
        }
        out.stats.color_fraction = pts.empty() ? 0.0 : double(colored) / pts.size();
        out.color_used = colored > 0;
        out.color_status = out.color_used ? "ok" : "no-overlap";
      }
    }
  }
  out.stats.seconds =
      std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
  return out;
}
}  // namespace einstar::recon
