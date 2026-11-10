#include "einstar/recon/rectify.hpp"

#include <cmath>
#include <stdexcept>

#include <opencv2/calib3d.hpp>
#include <opencv2/imgproc.hpp>

namespace einstar::recon {

RectifiedPair rectify(const FrameSet& frames, const CalibrationProfile& profile,
                      const RectifyOptions& options) {
  RectifiedPair out;
  cv::Mat left = calibration_image(frames, Camera::IrLeft);
  cv::Mat right = calibration_image(frames, Camera::IrRight);
  if (left.empty() || right.empty()) {
    out.error = "missing IR calibration image";
    return out;
  }
  if (left.channels() != 1) cv::cvtColor(left, left, cv::COLOR_BGR2GRAY);
  if (right.channels() != 1) cv::cvtColor(right, right, cv::COLOR_BGR2GRAY);
  if (left.size() != right.size()) {
    out.error = "IR image sizes differ";
    return out;
  }
  if (left.cols != profile.image_width || left.rows != profile.image_height) {
    out.error = "image size does not match the calibration profile";
    return out;
  }
  const auto& cl = profile.camera(Camera::IrLeft);
  const auto& cr = profile.camera(Camera::IrRight);
  const cv::Matx33d R = relative_rotation(profile, Camera::IrLeft, Camera::IrRight);
  const cv::Vec3d T = relative_translation(profile, Camera::IrLeft, Camera::IrRight);
  cv::Mat Rm(R), Tm(T);
  cv::Mat K1, D1, K2, D2;
  cv::Mat(cl.K()).copyTo(K1);
  cl.dist_mat().copyTo(D1);
  cv::Mat(cr.K()).copyTo(K2);
  cr.dist_mat().copyTo(D2);
  if (cv::norm(T) < 1e-6) {
    out.error = "degenerate IR baseline";
    return out;
  }
  cv::Mat R1, R2, P1, P2, Q;
  try {
    cv::stereoRectify(K1, D1, K2, D2, left.size(), Rm, Tm, R1, R2, P1, P2, Q,
                      cv::CALIB_ZERO_DISPARITY, options.alpha, left.size());
  } catch (const cv::Exception& e) {
    out.error = std::string("stereoRectify failed: ") + e.what();
    return out;
  }
  cv::Mat m1x, m1y, m2x, m2y;
  cv::initUndistortRectifyMap(K1, D1, R1, P1, left.size(), CV_32FC1, m1x, m1y);
  cv::initUndistortRectifyMap(K2, D2, R2, P2, left.size(), CV_32FC1, m2x, m2y);
  cv::remap(left, out.left, m1x, m1y, cv::INTER_LINEAR, cv::BORDER_CONSTANT, 0);
  cv::remap(right, out.right, m2x, m2y, cv::INTER_LINEAR, cv::BORDER_CONSTANT, 0);
  const cv::Mat white(left.size(), CV_8UC1, cv::Scalar(255));
  cv::remap(white, out.valid_left, m1x, m1y, cv::INTER_NEAREST, cv::BORDER_CONSTANT, 0);
  cv::remap(white, out.valid_right, m2x, m2y, cv::INTER_NEAREST, cv::BORDER_CONSTANT, 0);
  if (options.border_erode > 0) {
    const cv::Mat kernel = cv::getStructuringElement(
        cv::MORPH_RECT, {options.border_erode, options.border_erode});
    cv::erode(out.valid_left, out.valid_left, kernel);
    cv::erode(out.valid_right, out.valid_right, kernel);
  }
  out.R1 = R1; out.R2 = R2; out.P1 = P1; out.P2 = P2; out.Q = Q;
  out.K = cv::Matx33d(P1.at<double>(0, 0), 0, P1.at<double>(0, 2),
                      0, P1.at<double>(1, 1), P1.at<double>(1, 2), 0, 0, 1);
  out.f = P1.at<double>(0, 0);
  out.cx = P1.at<double>(0, 2);
  out.cy = P1.at<double>(1, 2);
  out.baseline_mm = -P2.at<double>(0, 3) / P2.at<double>(0, 0);
  out.disp_offset_full = P1.at<double>(0, 2) - P2.at<double>(0, 2);
  out.size = left.size();
  out.ok = std::isfinite(out.f) && out.f > 0 && std::isfinite(out.baseline_mm) &&
           out.baseline_mm > 0;
  if (!out.ok) out.error = "rectification produced invalid intrinsics/baseline";
  return out;
}

double StereoGeometry::disparity_at(double depth_mm) const {
  return f * baseline_mm / depth_mm + disp_offset;
}
double StereoGeometry::depth_at(double disparity) const {
  return f * baseline_mm / (disparity - disp_offset);
}

StereoGeometry scaled_geometry(const RectifiedPair& pair, double scale) {
  StereoGeometry g;
  g.scale = scale;
  g.f = pair.f * scale;
  g.cx = pair.cx * scale;
  g.cy = pair.cy * scale;
  g.baseline_mm = pair.baseline_mm;
  g.disp_offset = pair.disp_offset_full * scale;
  g.size = {(int)std::lround(pair.size.width * scale),
            (int)std::lround(pair.size.height * scale)};
  return g;
}
}  // namespace einstar::recon
