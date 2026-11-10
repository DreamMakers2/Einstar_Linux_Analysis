#include "test_util.hpp"

#include <string>

#include <opencv2/calib3d.hpp>

#include "einstar/recon/hash.hpp"
#include "einstar/recon/profile.hpp"
#include "einstar/recon/rectify.hpp"

using namespace einstar::recon;

static CalibrationProfile rig(double baseline = 158.557) {
  CalibrationProfile p;
  p.kind = "synthetic";
  p.image_width = 1280;
  p.image_height = 1024;
  p.color_status = "ok";
  for (Camera c : kCameras) {
    auto& cam = p.camera(c);
    cam.fx = 1150; cam.fy = 1150; cam.cx = 640; cam.cy = 512;
    cam.R = {1, 0, 0, 0, 1, 0, 0, 0, 1};
    cam.T = {0, 0, 0};
  }
  p.camera(Camera::IrRight).T = {-baseline, 0, 0};
  p.camera(Camera::Color).T = {-38, 0, 0};
  finalise_profile(p);
  return p;
}

void test_geometry() {
  // SHA-256 known vectors
  CHECK(sha256_hex("abc") ==
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  CHECK(sha256_hex("") ==
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");

  // stream mapping (hardware-observed): 0=right IR, 1=left IR, 2=color
  CHECK(stream_for(Camera::IrRight) == 0);
  CHECK(stream_for(Camera::IrLeft) == 1);
  CHECK(stream_for(Camera::Color) == 2);
  CHECK(stream_needs_rotation(0) && !stream_needs_rotation(1) && stream_needs_rotation(2));

  // projection/triangulation consistency on a fronto-parallel plane
  CalibrationProfile p = rig();
  const double z = 420.0;
  const cv::Matx33d K1 = p.camera(Camera::IrLeft).K();
  const cv::Matx33d K2 = p.camera(Camera::IrRight).K();
  const cv::Vec3d T = relative_translation(p, Camera::IrLeft, Camera::IrRight);
  for (int i = 0; i < 5; ++i) {
    const double x = -100.0 + 50.0 * i, y = -60.0 + 30.0 * i;
    const cv::Vec3d X(x, y, z);
    const cv::Vec3d xl = K1 * X;
    const cv::Vec3d Xr = X + T;
    const cv::Vec3d xr = K2 * Xr;
    const double ul = xl[0] / xl[2], vl = xl[1] / xl[2];
    const double ur = xr[0] / xr[2], vr = xr[1] / xr[2];
    CHECK_NEAR(vl, vr, 1e-9);
    const double disparity = ul - ur;
    CHECK_NEAR(disparity, K1(0, 0) * (-T[0]) / z, 1e-6);
    CHECK_NEAR(K1(0, 0) * (-T[0]) / disparity, z, 1e-6);
  }

  // scaled geometry math
  StereoGeometry g;
  g.scale = 0.5; g.f = 575.0; g.baseline_mm = 158.557; g.disp_offset = 0.0;
  const double zz = 350.0;
  const double d = g.disparity_at(zz);
  CHECK_NEAR(d, 575.0 * 158.557 / 350.0, 1e-9);
  CHECK_NEAR(g.depth_at(d), zz, 1e-9);
}
