#include "einstar/recon/profile.hpp"

#include <algorithm>
#include <cmath>
#include <iomanip>
#include <sstream>
#include <stdexcept>

#include <opencv2/core/persistence.hpp>

#include "einstar/recon/hash.hpp"

namespace einstar::recon {
namespace {
constexpr double kOrthoTol = 1e-6;
constexpr double kDetTol = 1e-6;

bool finite(double v) { return std::isfinite(v); }

std::array<double, 9> read9(const cv::FileNode& n, const char* what) {
  std::vector<double> v;
  n >> v;
  if (v.size() != 9) throw std::runtime_error(std::string("profile: ") + what + " must have 9 values");
  std::array<double, 9> out{};
  std::copy(v.begin(), v.end(), out.begin());
  return out;
}
std::array<double, 3> read3(const cv::FileNode& n, const char* what) {
  std::vector<double> v;
  n >> v;
  if (v.size() != 3) throw std::runtime_error(std::string("profile: ") + what + " must have 3 values");
  std::array<double, 3> out{};
  std::copy(v.begin(), v.end(), out.begin());
  return out;
}
double readd(const cv::FileNode& n, const char* what, double def = 0.0) {
  if (n.empty()) {
    if (def == 0.0) throw std::runtime_error(std::string("profile: missing ") + what);
    return def;
  }
  double v = def;
  n >> v;
  return v;
}
std::string reads(const cv::FileNode& n, const char*, const std::string& def = "") {
  if (n.empty()) return def;
  std::string s;
  n >> s;
  return s;
}
}  // namespace

const char* camera_name(Camera c) {
  switch (c) {
    case Camera::IrLeft: return "ir_left";
    case Camera::IrRight: return "ir_right";
    case Camera::Color: return "color";
  }
  return "?";
}
bool camera_from_name(const std::string& n, Camera& out) {
  if (n == "ir_left" || n == "left") { out = Camera::IrLeft; return true; }
  if (n == "ir_right" || n == "right") { out = Camera::IrRight; return true; }
  if (n == "color" || n == "colour" || n == "tex") { out = Camera::Color; return true; }
  return false;
}

cv::Matx33d CameraProfile::K() const {
  return cv::Matx33d(fx, skew, cx, 0.0, fy, cy, 0.0, 0.0, 1.0);
}
cv::Matx33d CameraProfile::rotation() const {
  return cv::Matx33d(R[0], R[1], R[2], R[3], R[4], R[5], R[6], R[7], R[8]);
}
cv::Vec3d CameraProfile::translation() const { return cv::Vec3d(T[0], T[1], T[2]); }
cv::Vec3d CameraProfile::centre() const {
  const cv::Matx33d Rt = rotation().t();
  return -(Rt * translation());
}
cv::Mat CameraProfile::dist_mat() const {
  cv::Mat d(1, 5, CV_64F);
  for (int i = 0; i < 5; ++i) d.at<double>(0, i) = dist[i];
  return d;
}

std::string canonical_profile(const CalibrationProfile& p) {
  std::ostringstream o;
  o << std::setprecision(17);
  o << p.schema << '|' << p.kind << '|' << p.image_width << 'x' << p.image_height << '|'
    << p.units << '|' << p.plate_pitch_mm << '|' << p.color_status;
  for (Camera c : kCameras) {
    const auto& cam = p.camera(c);
    o << '|' << camera_name(c) << ':' << cam.fx << ',' << cam.fy << ',' << cam.cx << ','
      << cam.cy << ',' << cam.skew << ';';
    for (double d : cam.dist) o << d << ',';
    o << ';';
    for (double r : cam.R) o << r << ',';
    o << ';';
    for (double t : cam.T) o << t << ',';
  }
  return o.str();
}

std::string finalise_profile(CalibrationProfile& p) {
  p.id = sha256_hex(canonical_profile(p));
  return p.id;
}

static double rotation_degrees(const cv::Matx33d& R) {
  const double c = std::clamp((R(0, 0) + R(1, 1) + R(2, 2) - 1.0) / 2.0, -1.0, 1.0);
  return std::acos(c) * 180.0 / CV_PI;
}

ProfileValidation validate_profile(const CalibrationProfile& p,
                                   double colour_translation_tolerance_mm,
                                   double colour_rotation_tolerance_deg) {
  ProfileValidation v;
  auto err = [&](const std::string& s) { v.errors.push_back(s); };
  auto warn = [&](const std::string& s) { v.warnings.push_back(s); };
  if (p.schema != kProfileSchema) err("wrong schema: " + p.schema);
  if (p.image_width <= 0 || p.image_height <= 0) err("invalid image size");
  if (p.plate_pitch_mm <= 0) err("plate_pitch_mm must be positive");
  for (Camera c : kCameras) {
    const auto& cam = p.camera(c);
    const std::string n = camera_name(c);
    if (!(finite(cam.fx) && finite(cam.fy) && finite(cam.cx) && finite(cam.cy) && finite(cam.skew)))
      err(n + ": non-finite intrinsics");
    if (cam.fx <= 0 || cam.fy <= 0) err(n + ": fx/fy must be positive");
    if (cam.cx < 0 || cam.cx > p.image_width || cam.cy < 0 || cam.cy > p.image_height)
      warn(n + ": principal point outside the image");
    for (double d : cam.dist) if (!finite(d)) err(n + ": non-finite distortion");
    const cv::Matx33d R = cam.rotation();
    const cv::Matx33d RtR = R.t() * R;
    double ortho = 0;
    for (int i = 0; i < 3; ++i)
      for (int j = 0; j < 3; ++j)
        ortho = std::max(ortho, std::abs(RtR(i, j) - (i == j ? 1.0 : 0.0)));
    if (ortho > kOrthoTol) err(n + ": rotation is not orthonormal (max |R^T R - I| = " +
                               std::to_string(ortho) + ")");
    const double det = cv::determinant(R);
    if (std::abs(det - 1.0) > kDetTol) err(n + ": det(R) = " + std::to_string(det));
    for (double t : cam.T) if (!finite(t)) err(n + ": non-finite translation");
  }
  const double b_ir = baseline_mm(p, Camera::IrLeft, Camera::IrRight);
  if (!(b_ir > 10.0 && b_ir < 1000.0)) err("IR baseline implausible: " + std::to_string(b_ir) + " mm");
  const double b_lc = baseline_mm(p, Camera::IrLeft, Camera::Color);
  const double b_rc = baseline_mm(p, Camera::IrRight, Camera::Color);
  if (!(b_lc > 5.0 && b_lc < 1000.0)) warn("colour-left baseline implausible: " + std::to_string(b_lc) + " mm");
  if (!(b_rc > 1.0 && b_rc < 1000.0)) warn("colour-right baseline implausible: " + std::to_string(b_rc) + " mm");
  // Colour-record consistency: the three baselines must satisfy the triangle
  // inequalities (with a small tolerance), and the colour baseline must be
  // plausible for this rig.
  if (b_lc > 0 && b_rc > 0) {
    const double tol = 5.0;
    if (b_ir > b_lc + b_rc + tol || b_lc > b_ir + b_rc + tol || b_rc > b_ir + b_lc + tol)
      warn("colour camera triangle violates the triangle inequality: baselines " +
           std::to_string(b_ir) + "/" + std::to_string(b_lc) + "/" +
           std::to_string(b_rc) + " mm");
    if (b_lc < 20.0 || b_lc > 300.0)
      warn("colour-left baseline implausible for this rig: " + std::to_string(b_lc) + " mm");
  }
  const cv::Matx33d R_lc = relative_rotation(p, Camera::IrLeft, Camera::Color);
  const cv::Matx33d R_lr = relative_rotation(p, Camera::IrLeft, Camera::IrRight);
  const double colour_angle = rotation_degrees(R_lc);
  const double ir_angle = rotation_degrees(R_lr);
  if (colour_angle > 45.0)
    warn("colour camera relative rotation " + std::to_string(colour_angle) + " deg is large");
  if (std::abs(colour_angle - ir_angle) > 20.0)
    warn("colour/IR relative rotations differ by " +
         std::to_string(std::abs(colour_angle - ir_angle)) + " deg");
  (void)colour_translation_tolerance_mm;
  (void)colour_rotation_tolerance_deg;
  if (p.color_status != "ok")
    warn("colour profile status is '" + p.color_status +
         "': colour registration is disabled unless explicitly enabled");
  v.ok = v.errors.empty();
  return v;
}

std::string ProfileValidation::summary() const {
  std::ostringstream o;
  o << (ok ? "VALID" : "INVALID");
  for (const auto& e : errors) o << "\n  error: " << e;
  for (const auto& w : warnings) o << "\n  warning: " << w;
  return o.str();
}

CalibrationProfile load_profile(const std::string& path) {
  cv::FileStorage fs(path, cv::FileStorage::READ);
  if (!fs.isOpened()) throw std::runtime_error("cannot open profile: " + path);
  CalibrationProfile p;
  p.schema = reads(fs["schema"], "schema", kProfileSchema);
  p.kind = reads(fs["kind"], "kind", "factory");
  p.image_width = static_cast<int>(readd(fs["image_width"], "image_width"));
  p.image_height = static_cast<int>(readd(fs["image_height"], "image_height"));
  p.units = reads(fs["units"], "units", "mm");
  p.plate_pitch_mm = readd(fs["plate_pitch_mm"], "plate_pitch_mm", 28.0);
  p.color_status = reads(fs["color_status"], "color_status", "unknown");
  const cv::FileNode cams = fs["cameras"];
  if (cams.empty()) throw std::runtime_error("profile: missing cameras");
  for (Camera c : kCameras) {
    const cv::FileNode n = cams[camera_name(c)];
    if (n.empty()) throw std::runtime_error(std::string("profile: missing camera ") + camera_name(c));
    auto& cam = p.camera(c);
    cam.fx = readd(n["fx"], "fx"); cam.fy = readd(n["fy"], "fy");
    cam.cx = readd(n["cx"], "cx"); cam.cy = readd(n["cy"], "cy");
    cam.skew = readd(n["skew"], "skew", 0.0);
    std::vector<double> d;
    n["dist"] >> d;
    if (d.size() != 5) throw std::runtime_error("profile: dist must have 5 values");
    std::copy(d.begin(), d.end(), cam.dist.begin());
    cam.R = read9(n["R"], "R");
    cam.T = read3(n["T"], "T");
  }
  const cv::FileNode prov = fs["provenance"];
  if (!prov.empty() && prov.isSeq()) {
    for (const auto& item : prov) {
      const std::string s = (std::string)item;
      const auto eq = s.find('=');
      p.provenance[eq == std::string::npos ? s : s.substr(0, eq)] =
          eq == std::string::npos ? "" : s.substr(eq + 1);
    }
  }
  if (p.id.empty()) finalise_profile(p);
  return p;
}

void save_profile(const CalibrationProfile& pin, const std::string& path) {
  CalibrationProfile p = pin;
  finalise_profile(p);
  cv::FileStorage fs(path, cv::FileStorage::WRITE | cv::FileStorage::FORMAT_JSON);
  if (!fs.isOpened()) throw std::runtime_error("cannot write profile: " + path);
  fs << "schema" << p.schema << "kind" << p.kind << "id" << p.id
     << "image_width" << p.image_width << "image_height" << p.image_height
     << "units" << p.units << "plate_pitch_mm" << p.plate_pitch_mm
     << "color_status" << p.color_status;
  fs << "cameras" << "{";
  for (Camera c : kCameras) {
    const auto& cam = p.camera(c);
    fs << camera_name(c) << "{";
    fs << "fx" << cam.fx << "fy" << cam.fy << "cx" << cam.cx << "cy" << cam.cy
       << "skew" << cam.skew;
    std::vector<double> d(cam.dist.begin(), cam.dist.end());
    std::vector<double> R(cam.R.begin(), cam.R.end());
    std::vector<double> T(cam.T.begin(), cam.T.end());
    fs << "dist" << d << "R" << R << "T" << T;
    fs << "}";
  }
  fs << "}";
  std::vector<std::string> prov;
  for (const auto& [k, val] : p.provenance) prov.push_back(k + "=" + val);
  fs << "provenance" << prov;
  fs.release();
}

cv::Matx33d relative_rotation(const CalibrationProfile& p, Camera from, Camera to) {
  return p.camera(to).rotation() * p.camera(from).rotation().t();
}
cv::Vec3d relative_translation(const CalibrationProfile& p, Camera from, Camera to) {
  const cv::Matx33d R = relative_rotation(p, from, to);
  return p.camera(to).translation() - R * p.camera(from).translation();
}
double baseline_mm(const CalibrationProfile& p, Camera a, Camera b) {
  return cv::norm(relative_translation(p, a, b));
}
}  // namespace einstar::recon
