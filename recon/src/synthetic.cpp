#include "einstar/recon/synthetic.hpp"

#include <algorithm>
#include <cmath>

#include <opencv2/calib3d.hpp>
#include <opencv2/imgproc.hpp>

namespace einstar::recon {
namespace {

cv::Mat make_texture(std::uint64_t seed, int size, double contrast, double offset) {
  cv::Mat noise(size, size, CV_32F);
  cv::RNG rng(seed);
  rng.fill(noise, cv::RNG::UNIFORM, 0.0f, 1.0f);
  cv::Mat smooth;
  cv::GaussianBlur(noise, smooth, {0, 0}, 1.2);
  cv::normalize(smooth, smooth, 0, 255, cv::NORM_MINMAX);
  cv::Mat tex(size, size, CV_8UC1);
  for (int y = 0; y < size; ++y)
    for (int x = 0; x < size; ++x) {
      // add a low-frequency gradient so flat regions are not perfectly
      // ambiguous, plus fine speckle
      const double g = 0.10 * 255.0 * (x + y) / (2.0 * size);
      const double v = offset + contrast * (smooth.at<float>(y, x) / 255.0) + g;
      tex.at<uchar>(y, x) = cv::saturate_cast<uchar>(v);
    }
  return tex;
}

cv::Mat render_camera(const CalibrationProfile& p, Camera c, const cv::Mat& tex,
                      double z0, int width, int height) {
  const auto& cam = p.camera(c);
  cv::Mat K, D;
  cv::Mat(cam.K()).copyTo(K);
  cam.dist_mat().copyTo(D);
  // Pixel grid -> normalized undistorted coordinates (inverse of the model).
  std::vector<cv::Point2f> pixels;
  pixels.reserve(std::size_t(width) * height);
  for (int v = 0; v < height; ++v)
    for (int u = 0; u < width; ++u) pixels.emplace_back(float(u), float(v));
  std::vector<cv::Point2f> norm;
  cv::undistortPoints(pixels, norm, K, D);
  const cv::Matx33d R = cam.rotation();
  const cv::Vec3d T = cam.translation();
  const cv::Matx33d Rt = R.t();
  const cv::Vec3d C = -Rt * T;
  cv::Mat img(height, width, CV_8UC1);
  const int ts = tex.cols;
  const double px_per_mm = 2.0;
  for (int v = 0; v < height; ++v)
    for (int u = 0; u < width; ++u) {
      const cv::Point2f& n = norm[std::size_t(v) * width + u];
      const cv::Vec3d d_cam(n.x, n.y, 1.0);
      const cv::Vec3d d = Rt * d_cam;
      if (std::abs(d[2]) < 1e-9) { img.at<uchar>(v, u) = 0; continue; }
      const double t = (z0 - C[2]) / d[2];
      if (t <= 0) { img.at<uchar>(v, u) = 0; continue; }
      const cv::Vec3d X = C + t * d;
      int tx = int(std::floor(X[0] * px_per_mm)) % ts;
      int ty = int(std::floor(X[1] * px_per_mm)) % ts;
      if (tx < 0) tx += ts;
      if (ty < 0) ty += ts;
      img.at<uchar>(v, u) = tex.at<uchar>(ty, tx);
    }
  return img;
}
}  // namespace

FrameSet render_synthetic_frame_set(const CalibrationProfile& profile,
                                    const SyntheticOptions& o) {
  const cv::Mat tex = make_texture(o.seed, 1024, o.texture_contrast, o.texture_offset);
  FrameSet fs;
  fs.group = 1;
  fs.source = "synthetic";
  for (Camera c : kCameras) {
    const int s = stream_for(c);
    cv::Mat img = render_camera(profile, c, tex, o.plane_depth_mm, o.width, o.height);
    if (o.blur_sigma > 0) cv::GaussianBlur(img, img, {0, 0}, o.blur_sigma);
    if (o.rotate_to_stream && stream_needs_rotation(s)) cv::rotate(img, img, cv::ROTATE_180);
    Frame f;
    f.stream = s;
    f.width = img.cols;
    f.height = img.rows;
    f.frame_id = 1;
    f.source_path = "synthetic";
    f.mono = img;
    fs.streams[s] = std::move(f);
  }
  return fs;
}

cv::Mat synthetic_depth_truth(const CalibrationProfile&, double plane_depth_mm, int width,
                              int height) {
  return cv::Mat(height, width, CV_32FC1, cv::Scalar(plane_depth_mm));
}
}  // namespace einstar::recon
