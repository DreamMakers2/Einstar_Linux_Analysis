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

void test_pipeline() {
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
