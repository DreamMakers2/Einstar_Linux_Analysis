#include "test_util.hpp"

#include <filesystem>
#include <string>

#include <opencv2/core.hpp>

#include "einstar/recon/hash.hpp"
#include "einstar/recon/profile.hpp"

using namespace einstar::recon;

static CalibrationProfile make_profile(double baseline = 158.557) {
  CalibrationProfile p;
  p.kind = "synthetic";
  p.image_width = 1280;
  p.image_height = 1024;
  p.plate_pitch_mm = 28.0;
  p.color_status = "ok";
  for (Camera c : kCameras) {
    auto& cam = p.camera(c);
    cam.fx = 1150; cam.fy = 1150; cam.cx = 640; cam.cy = 512;
    cam.dist = {0, 0, 0, 0, 0};
    cam.R = {1, 0, 0, 0, 1, 0, 0, 0, 1};
    cam.T = {0, 0, 0};
  }
  p.camera(Camera::IrRight).T = {-baseline, 0, 0};
  p.camera(Camera::Color).T = {-38.0, 0.0, 0.0};
  finalise_profile(p);
  return p;
}

void test_profile() {
  CalibrationProfile p = make_profile();
  const auto v = validate_profile(p);
  CHECK(v.ok);
  CHECK(v.errors.empty());
  CHECK_NEAR(baseline_mm(p, Camera::IrLeft, Camera::IrRight), 158.557, 1e-9);
  CHECK_NEAR(baseline_mm(p, Camera::IrLeft, Camera::Color), 38.0, 1e-9);
  const cv::Matx33d R = relative_rotation(p, Camera::IrLeft, Camera::IrRight);
  CHECK_NEAR(R(0, 0), 1.0, 1e-12);
  const cv::Vec3d T = relative_translation(p, Camera::IrLeft, Camera::IrRight);
  CHECK_NEAR(T[0], -158.557, 1e-9);

  // id is stable and content-sensitive
  CalibrationProfile q = p;
  const std::string id1 = finalise_profile(q);
  CHECK(id1 == p.id);
  q.camera(Camera::IrRight).T[0] += 0.1;
  const std::string id2 = finalise_profile(q);
  CHECK(id2 != id1);

  // file round trip
  const std::string path = (std::filesystem::temp_directory_path() / "recon_profile_test.json").string();
  save_profile(p, path);
  CalibrationProfile r = load_profile(path);
  CHECK(r.id == p.id);
  CHECK_NEAR(r.camera(Camera::IrRight).T[0], -158.557, 1e-9);
  std::filesystem::remove(path);

  // rejection: non-orthonormal rotation
  CalibrationProfile bad = p;
  bad.camera(Camera::IrLeft).R[1] = 0.1;
  const auto vb = validate_profile(bad);
  CHECK(!vb.ok);
  // rejection: degenerate baseline
  CalibrationProfile flat = p;
  flat.camera(Camera::IrRight).T = {0, 0, 0};
  CHECK(!validate_profile(flat).ok);
  // warning only: stale colour (geometry still usable)
  CalibrationProfile stale = p;
  stale.color_status = "stale";
  const auto vs = validate_profile(stale);
  CHECK(vs.ok);
  CHECK(!vs.warnings.empty());
}
