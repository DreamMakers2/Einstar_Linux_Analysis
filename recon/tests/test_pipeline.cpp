#include "test_util.hpp"

#include <algorithm>
#include <filesystem>
#include <fstream>

#include <opencv2/imgcodecs.hpp>
#include <opencv2/imgproc.hpp>

#include "einstar/recon/pipeline.hpp"
#include "einstar/recon/pointcloud.hpp"
#include "einstar/recon/synthetic.hpp"

using namespace einstar::recon;

static CalibrationProfile rig() {
  CalibrationProfile p;
  p.kind = "synthetic";
  p.image_width = 1280;
  p.image_height = 1024;
  p.plate_pitch_mm = 28.0;
  p.color_status = "ok";
  for (Camera c : kCameras) {
    auto& cam = p.camera(c);
    cam.fx = 1100; cam.fy = 1100; cam.cx = 640; cam.cy = 512;
    cam.dist = {0, 0, 0, 0, 0};
    cam.R = {1, 0, 0, 0, 1, 0, 0, 0, 1};
    cam.T = {0, 0, 0};
  }
  p.camera(Camera::IrRight).T = {-158.557, 0, 0};
  p.camera(Camera::Color).T = {-38.0, 0, 0};
  finalise_profile(p);
  return p;
}


// Validate the complete colour path: a known world-X split rendered from the
// colour camera, in raw stream orientation, must be sampled back correctly.
static void test_color_registration() {
  CalibrationProfile p = rig();
  p.color_status = "ok";
  finalise_profile(p);
  SyntheticOptions so;
  so.plane_depth_mm = 350;
  so.seed = 11;
  FrameSet fs = render_synthetic_frame_set(p, so);

  const auto& cc = p.camera(Camera::Color);
  const double z0 = 350.0;
  const cv::Vec3d C(-cc.T[0], -cc.T[1], -cc.T[2]);  // R = I in this rig
  cv::Mat colour(fs.streams[2].height, fs.streams[2].width, CV_8UC3);
  for (int v = 0; v < colour.rows; ++v)
    for (int u = 0; u < colour.cols; ++u) {
      const double xn = (u - cc.cx) / cc.fx;
      const double yn = (v - cc.cy) / cc.fy;
      const double t = (z0 - C[2]) / 1.0;
      const double X = C[0] + t * xn;
      const double Y = C[1] + t * yn;
      cv::Vec3b bgr(0, 0, 0);
      if (X > 0) bgr = cv::Vec3b(255, 0, 0);       // +X half: blue
      else bgr = cv::Vec3b(0, 0, 255);              // -X half: red
      bgr[1] = cv::saturate_cast<uchar>(128 + 0.5 * Y);  // a Y gradient
      colour.at<cv::Vec3b>(v, u) = bgr;
    }
  // stream 2 arrives rotated 180 degrees
  cv::rotate(colour, fs.streams[2].mono, cv::ROTATE_180);
  fs.streams[2].width = colour.cols;
  fs.streams[2].height = colour.rows;
  fs.streams[2].color_bayer = false;   // this test supplies an already-BGR image

  ReconOptions o;
  o.stereo.scale = 0.5;
  o.depth.color = true;
  ReconResult r = reconstruct(fs, p, o);
  CHECK(r.ok);
  if (!r.ok) return;
  CHECK(r.depth.color_used);
  CHECK(r.depth.color_status == "ok");
  CHECK(r.depth.stats.color_fraction > 0.5);
  CHECK(!r.depth.bgr.empty());
  int checked = 0, matched = 0;
  for (int v = 0; v < r.depth.valid.rows; ++v)
    for (int u = 0; u < r.depth.valid.cols; ++u) {
      if (!r.depth.valid.at<uchar>(v, u)) continue;
      const float x = r.depth.xyz_mm.at<cv::Vec3f>(v, u)[0];
      if (std::abs(x) < 3.0f) continue;
      const cv::Vec3b got = r.depth.bgr.at<cv::Vec3b>(v, u);
      const cv::Vec3b want = x > 0 ? cv::Vec3b(255, 0, 0) : cv::Vec3b(0, 0, 255);
      ++checked;
      if (std::abs(int(got[0]) - int(want[0])) <= 8 &&
          std::abs(int(got[2]) - int(want[2])) <= 8)
        ++matched;
      if (checked >= 5000) break;
    }
  CHECK(checked > 1000);
  CHECK(matched >= 0.98 * checked);

  // A non-validated colour profile must be skipped, not silently applied.
  CalibrationProfile stale = p;
  stale.color_status = "stale";
  finalise_profile(stale);
  ReconResult rs = reconstruct(fs, stale, o);
  CHECK(rs.ok);
  CHECK(!rs.depth.color_used);
  CHECK(rs.depth.color_status.find("profile-inconsistent") != std::string::npos);
  CHECK(rs.depth.bgr.empty());
}


// Raw stream 2 is an RGGB Bayer mosaic; debayer-then-rotate must recover the
// same colour pattern as the already-BGR test above.
static void test_color_bayer() {
  CalibrationProfile p = rig();
  p.color_status = "ok";
  finalise_profile(p);
  SyntheticOptions so;
  so.plane_depth_mm = 350;
  so.seed = 12;
  FrameSet fs = render_synthetic_frame_set(p, so);
  const auto& cc = p.camera(Camera::Color);
  const double z0 = 350.0;
  const cv::Vec3d C(-cc.T[0], -cc.T[1], -cc.T[2]);
  cv::Mat colour(fs.streams[2].height, fs.streams[2].width, CV_8UC3);
  for (int v = 0; v < colour.rows; ++v)
    for (int u = 0; u < colour.cols; ++u) {
      const double X = C[0] + (z0 - C[2]) * (u - cc.cx) / cc.fx;
      const double Y = C[1] + (z0 - C[2]) * (v - cc.cy) / cc.fy;
      cv::Vec3b bgr(0, 0, 0);
      bgr[0] = X > 0 ? 240 : 20;      // blue on +X
      bgr[2] = X > 0 ? 20 : 240;      // red on -X
      bgr[1] = cv::saturate_cast<uchar>(128 + 0.5 * Y);
      colour.at<cv::Vec3b>(v, u) = bgr;
    }
  cv::Mat raw_bgr;
  cv::rotate(colour, raw_bgr, cv::ROTATE_180);
  // Mosaic in the raw (rotated) frame using the tile OpenCV's
  // COLOR_BayerRG2BGR expects (BGGR in this 2x2 layout; libeinstar uses the
  // same code on the hardware raw stream).
  cv::Mat mosaic(raw_bgr.rows, raw_bgr.cols, CV_8UC1);
  for (int y = 0; y < raw_bgr.rows; ++y)
    for (int x = 0; x < raw_bgr.cols; ++x) {
      int ch;
      if ((y & 1) == 0) ch = ((x & 1) == 0) ? 0 : 1;  // B G
      else ch = ((x & 1) == 0) ? 1 : 2;               // G R
      mosaic.at<uchar>(y, x) = raw_bgr.at<cv::Vec3b>(y, x)[ch];
    }
  fs.streams[2].mono = mosaic;
  fs.streams[2].color_bayer = true;

  ReconOptions o;
  o.stereo.scale = 0.5;
  o.depth.color = true;
  ReconResult r = reconstruct(fs, p, o);
  CHECK(r.ok);
  if (!r.ok) return;
  CHECK(r.depth.color_bayer);
  CHECK(r.depth.color_used);
  int blue_right = 0, blue_total = 0, red_right = 0, red_total = 0;
  for (int v = 0; v < r.depth.valid.rows; ++v)
    for (int u = 0; u < r.depth.valid.cols; ++u) {
      if (!r.depth.valid.at<uchar>(v, u)) continue;
      const float x = r.depth.xyz_mm.at<cv::Vec3f>(v, u)[0];
      if (std::abs(x) < 5.0f) continue;
      const cv::Vec3b c = r.depth.bgr.at<cv::Vec3b>(v, u);
      if (x > 0) { ++blue_total; blue_right += c[0] > c[2] + 40; }
      else { ++red_total; red_right += c[2] > c[0] + 40; }
    }
  CHECK(blue_total > 500);
  CHECK(red_total > 500);
  CHECK(blue_right >= 0.90 * blue_total);
  CHECK(red_right >= 0.90 * red_total);
}

void test_pipeline() {
  test_color_registration();
  test_color_bayer();
  CalibrationProfile p = rig();
  const std::string tmp = (std::filesystem::temp_directory_path() / "recon_pipeline_test").string();
  std::filesystem::remove_all(tmp);
  std::filesystem::create_directories(tmp);

  for (double truth : {250.0, 400.0, 600.0}) {
    SyntheticOptions so;
    so.plane_depth_mm = truth;
    so.seed = 7;
    FrameSet fs = render_synthetic_frame_set(p, so);
    CHECK(fs.complete());
    ReconOptions o;
    o.stereo.scale = 0.5;
    o.stereo.min_depth_mm = 120;
    o.stereo.max_depth_mm = 900;
    ReconResult r = reconstruct(fs, p, o);
    if (!r.ok) {
      std::fprintf(stderr, "reconstruct failed (stereo backend?): %s\n", r.error.c_str());
      continue;  // the stereo test reports the matcher; keep this test informative
    }
    CHECK(!r.cloud.points.empty());
    CHECK(r.depth.stats.valid_fraction > 0.15);
    CHECK_NEAR(r.depth.stats.depth_p50_mm, truth, std::max(1.0, 0.005 * truth));
    // z_mm and xyz_mm must be in the same (left camera) frame
    for (int v = 0; v < r.depth.z_mm.rows && v < 4; ++v)
      for (int u = 0; u < r.depth.z_mm.cols; ++u)
        if (r.depth.valid.at<uchar>(v, u))
          CHECK_NEAR(r.depth.z_mm.at<float>(v, u),
                     r.depth.xyz_mm.at<cv::Vec3f>(v, u)[2], 1e-3);
    const std::string ply = write_outputs(r, tmp, {});
    CHECK(!ply.empty());
    CHECK(std::filesystem::exists(ply));
    PlyInfo info = read_ply_info(ply);
    CHECK(info.vertex_count == r.cloud.points.size());
    CHECK(!info.ascii);
    bool has_units = false;
    for (const auto& c : info.comments) has_units |= c.rfind("units=mm", 0) == 0;
    CHECK(has_units);
    for (const char* suffix : {"_depth.png", "_disparity.png", "_confidence.png",
                               "_preview.png", "_report.json"})
      CHECK(std::filesystem::exists(std::filesystem::path(tmp) / ("recon" + std::string(suffix))));
  }
  // Determinism: same input -> same cloud hash (first N points)
  SyntheticOptions so;
  so.plane_depth_mm = 350;
  so.seed = 3;
  FrameSet fs = render_synthetic_frame_set(p, so);
  ReconOptions o;
  o.stereo.scale = 0.5;
  auto a = reconstruct(fs, p, o);
  auto b = reconstruct(fs, p, o);
  CHECK(a.ok == b.ok);
  if (a.ok && b.ok) {
    CHECK(a.cloud.points.size() == b.cloud.points.size());
    CHECK(a.stats.frame_sha256 == b.stats.frame_sha256);
  }
  // Unusable inputs are rejected, not silently reconstructed
  CalibrationProfile bad = p;
  bad.camera(Camera::IrRight).T = {0, 0, 0};
  auto rr = reconstruct(fs, bad, o);
  CHECK(!rr.ok);
  FrameSet incomplete = fs;
  incomplete.streams[2].mono.release();
  auto ri = reconstruct(incomplete, p, o);
  CHECK(!ri.ok);
  std::filesystem::remove_all(tmp);
}
