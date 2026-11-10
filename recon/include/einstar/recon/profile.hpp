// Calibration profile: the single calibration input of the reconstruction
// backend.  A profile is a JSON document (OpenCV FileStorage) with the three
// cameras' intrinsics/distortion and their world->camera poses in millimetres
// (OpenCV convention, X_cam = R X_world + T).  Two kinds are supported:
//
//   factory     the recovered CCF-based geometry, kept unchanged;
//   replacement a calibration produced by calib/ (or a synthetic test rig).
//
// The profile id is a SHA-256 over the canonical numeric content, so every
// reconstruction can record exactly which calibration it used.  Validation is
// strict: a profile with a non-orthonormal rotation, a degenerate IR baseline,
// non-finite values or a wrong image size is rejected instead of silently
// producing a plausible-looking cloud.
#pragma once

#include <array>
#include <map>
#include <string>
#include <vector>

#include <opencv2/core.hpp>

namespace einstar::recon {

inline constexpr const char* kProfileSchema = "einstar.recon.profile/1";
inline constexpr int kNumCameras = 3;

// Order used everywhere in the backend.  This is the *calibration* order, not
// the USB stream order (stream 0 is the right IR camera, stream 1 the left,
// stream 2 the colour camera; see frame_set.hpp).
enum class Camera : int { IrLeft = 0, IrRight = 1, Color = 2 };
inline constexpr std::array<Camera, kNumCameras> kCameras{Camera::IrLeft,
                                                          Camera::IrRight,
                                                          Camera::Color};
const char* camera_name(Camera c);
bool camera_from_name(const std::string& name, Camera& out);

struct CameraProfile {
  double fx = 0, fy = 0, cx = 0, cy = 0, skew = 0;
  std::array<double, 5> dist{};        // k1 k2 p1 p2 k3
  std::array<double, 9> R{};           // row-major world->camera
  std::array<double, 3> T{};           // millimetres

  cv::Matx33d K() const;
  cv::Matx33d rotation() const;
  cv::Vec3d translation() const;
  cv::Vec3d centre() const;            // -R^T T
  cv::Mat dist_mat() const;            // 1x5 CV_64F
};

struct CalibrationProfile {
  std::string schema = kProfileSchema;
  std::string id;                      // filled by finalise(); SHA-256 hex
  std::string kind = "factory";        // factory | replacement | synthetic
  int image_width = 1280;
  int image_height = 1024;
  std::string units = "mm";
  double plate_pitch_mm = 28.0;        // nominal; not a scale correction
  // "ok" = colour pose trusted; "stale" = known inconsistent record (skip
  // colour by default); "unknown" = never validated.  Recorded in every report.
  std::string color_status = "unknown";
  std::array<CameraProfile, kNumCameras> cameras{};
  std::map<std::string, std::string> provenance;  // free-form key=value

  CameraProfile& camera(Camera c) { return cameras[static_cast<int>(c)]; }
  const CameraProfile& camera(Camera c) const { return cameras[static_cast<int>(c)]; }
};

struct ProfileValidation {
  bool ok = false;
  std::vector<std::string> errors;
  std::vector<std::string> warnings;
  std::string summary() const;
};

// Canonical numeric representation (stable key order, 17 significant digits).
std::string canonical_profile(const CalibrationProfile& p);
// SHA-256 of canonical_profile(); sets and returns p.id.
std::string finalise_profile(CalibrationProfile& p);
// Validates geometry and dimensions.  Warnings cover the known colour-record
// inconsistency; errors are fatal for reconstruction.
ProfileValidation validate_profile(const CalibrationProfile& p,
                                   double colour_translation_tolerance_mm = 3.0,
                                   double colour_rotation_tolerance_deg = 0.5);

CalibrationProfile load_profile(const std::string& path);   // throws std::runtime_error
void save_profile(const CalibrationProfile& p, const std::string& path);

// Relative pose X_to = R X_from + T, derived from the shared world frame.
cv::Matx33d relative_rotation(const CalibrationProfile& p, Camera from, Camera to);
cv::Vec3d relative_translation(const CalibrationProfile& p, Camera from, Camera to);
double baseline_mm(const CalibrationProfile& p, Camera a, Camera b);

}  // namespace einstar::recon
