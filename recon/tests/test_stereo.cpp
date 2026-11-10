// Tests for the SGBM stereo matcher (recon/src/stereo.cpp).
//
// All synthetic scenes are built directly as a RectifiedPair: the rectification
// itself is covered by test_geometry/test_pipeline, and repeating it here would
// only add noise.  Ground truth is the shift used to synthesise the right view,
// so every tolerance below is a real matcher error, not a rendering error.
//
// Disparity convention under test (stereo.hpp): the map value is the offset
// disparity d = (u_l - u_r) + min_disparity and geometry.disp_offset includes
// min_disparity, so depth_at(d) = f*B/((u_l-u_r) - P_offset).  The tests below
// therefore compare (median - stats.min_disparity) with the known physical
// shift and depth_at(median) with the known metric depth.
//
// The real-frame test is opt-in through the environment (the capture is private
// and is never committed):
//   EINSTAR_CAPTURE_DIR  default <capture-dir>
//   EINSTAR_PROFILE      default <private-inputs>/einstar-profiles/factory-e10.json
// When either is missing the test prints a SKIP line and still passes.
#include "test_util.hpp"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <string>
#include <vector>

#include <opencv2/core.hpp>
#include <opencv2/imgcodecs.hpp>
#include <opencv2/imgproc.hpp>

#include "einstar/recon/frame_set.hpp"
#include "einstar/recon/profile.hpp"
#include "einstar/recon/rectify.hpp"
#include "einstar/recon/stereo.hpp"

using namespace einstar::recon;
namespace fs = std::filesystem;

namespace {

std::string env_or(const char* name, const std::string& fallback) {
  const char* value = std::getenv(name);
  return value && *value ? std::string(value) : fallback;
}

// Deterministic textured image: blurred uniform noise scaled around 120.
// OpenCV's RNG is seed-deterministic, so the same call always gives the same
// pixels (there is no std::random_device anywhere in this file).
cv::Mat make_texture(int width, int height, std::uint64_t seed, double contrast = 55.0) {
  cv::Mat noise(height, width, CV_32F);
  cv::RNG rng(seed);
  rng.fill(noise, cv::RNG::UNIFORM, 0.0f, 1.0f);
  cv::GaussianBlur(noise, noise, {0, 0}, 1.0);
  cv::normalize(noise, noise, 0.0, 1.0, cv::NORM_MINMAX);
  cv::Mat tex(height, width, CV_8UC1);
  for (int v = 0; v < height; ++v)
    for (int u = 0; u < width; ++u)
      tex.at<std::uint8_t>(v, u) = cv::saturate_cast<std::uint8_t>(
          120.0 + contrast * (2.0 * noise.at<float>(v, u) - 1.0));
  return tex;
}

// right(x) = left(x + dx): the right view of a scene at physical shift +dx.
// `big` is a wider canvas, so the translated crop never contains reflected
// border content that could hand the matcher a duplicate of the left image.
void make_scene_from(const cv::Mat& big, int width, double dx, cv::Mat& left,
                     cv::Mat& right) {
  left = big.colRange(0, width).clone();
  cv::Mat warped;
  const cv::Matx23d M(1.0, 0.0, -dx, 0.0, 1.0, 0.0);
  cv::warpAffine(big, warped, cv::Mat(M), big.size(), cv::INTER_CUBIC, cv::BORDER_REPLICATE);
  right = warped.colRange(0, width).clone();
}

void make_scene(int width, int height, double dx, std::uint64_t seed, cv::Mat& left,
                cv::Mat& right) {
  const int margin = static_cast<int>(std::ceil(std::abs(dx))) + 8;
  make_scene_from(make_texture(width + margin, height, seed), width, dx, left, right);
}

RectifiedPair make_pair(const cv::Mat& left, const cv::Mat& right, double f, double baseline,
                        double disp_offset = 0.0) {
  RectifiedPair p;
  p.left = left;
  p.right = right;
  p.valid_left = cv::Mat(left.size(), CV_8UC1, cv::Scalar(255));
  p.valid_right = cv::Mat(right.size(), CV_8UC1, cv::Scalar(255));
  p.f = f;
  p.cx = left.cols * 0.5;
  p.cy = left.rows * 0.5;
  p.K = cv::Matx33d(f, 0, p.cx, 0, f, p.cy, 0, 0, 1);
  p.baseline_mm = baseline;
  p.disp_offset_full = disp_offset;
  p.size = left.size();
  p.ok = true;
  return p;
}

// Physical pixel shift implied by a result's median map value.
double physical_shift(const StereoResult& r) {
  return r.stats.median_disparity - r.stats.min_disparity;
}

// Contract checks that must hold for every successful result.
void check_contract(const StereoResult& r, const char* what, double expected_offset = 0.0) {
  if (!r.ok) return;
  CHECK(r.disparity.type() == CV_32FC1);
  CHECK(r.confidence.type() == CV_32FC1);
  CHECK(r.valid.type() == CV_8UC1);
  CHECK(r.occluded.type() == CV_8UC1);
  CHECK(r.disparity.size() == r.geometry.size);
  CHECK(r.confidence.size() == r.geometry.size);
  CHECK(r.valid.size() == r.geometry.size);
  CHECK(r.occluded.size() == r.geometry.size);
  CHECK(r.geometry.f > 0 && r.geometry.baseline_mm > 0);
  CHECK(r.stats.num_disparities % 16 == 0);
  // The offset convention: disp_offset carries the search-window minimum (plus
  // any P-matrix offset, which is zero for a CALIB_ZERO_DISPARITY rectifier).
  CHECK_NEAR(r.geometry.disp_offset,
             static_cast<double>(r.stats.min_disparity) + expected_offset, 1e-9);
  double conf_min = 1.0, conf_max = 0.0;
  cv::minMaxLoc(r.confidence, &conf_min, &conf_max);
  CHECK(conf_min >= 0.0 && conf_max <= 1.0);
  int bad = 0;
  for (int v = 0; v < r.disparity.rows; ++v) {
    const float* d = r.disparity.ptr<float>(v);
    const float* c = r.confidence.ptr<float>(v);
    const std::uint8_t* ok = r.valid.ptr<std::uint8_t>(v);
    for (int u = 0; u < r.disparity.cols; ++u) {
      if (!ok[u] && (d[u] > 0.0f || c[u] != 0.0f)) ++bad;       // invalid => 0/0
      if (ok[u] && !(d[u] > 0.0f)) ++bad;                        // valid => positive
      if (ok[u] && r.occluded.at<std::uint8_t>(v, u)) ++bad;     // disjoint masks
    }
  }
  CHECK(bad == 0);
  if (bad) std::fprintf(stderr, "     %s: %d contract violations\n", what, bad);
}

// Fraction of a rectangle that is invalid or flagged occluded.
double invalid_or_occluded_fraction(const StereoResult& r, cv::Rect roi) {
  const cv::Rect box = roi & cv::Rect(0, 0, r.valid.cols, r.valid.rows);
  int hit = 0;
  for (int v = box.y; v < box.br().y; ++v)
    for (int u = box.x; u < box.br().x; ++u)
      if (!r.valid.at<std::uint8_t>(v, u) || r.occluded.at<std::uint8_t>(v, u)) ++hit;
  return box.area() ? double(hit) / box.area() : 0.0;
}

// ---------------------------------------------------------------------------
// 1. known integer shift: map value, physical shift, depth and LR agreement
// ---------------------------------------------------------------------------
void test_integer_shift() {
  const int width = 640, height = 384;
  const double truth = 60.0;  // physical pixel shift
  const double f = 1158.370 / 2.0, baseline = 40.0;
  cv::Mat left, right;
  make_scene(width, height, truth, 11, left, right);
  StereoOptions o;
  o.scale = 1.0;  // the scene is already at the matching resolution
  const StereoResult r = compute_stereo(make_pair(left, right, f, baseline), o);
  check_contract(r, "integer shift");
  CHECK(r.ok);
  if (!r.ok) {
    std::fprintf(stderr, "     integer shift failed: %s\n", r.error.c_str());
    return;
  }
  const double expected_dmin = std::floor(f * baseline / 800.0 / 16.0) * 16.0;
  const double z = r.geometry.depth_at(r.stats.median_disparity);
  const double lr_agreement =
      r.stats.valid_fraction / (r.stats.valid_fraction + r.stats.occluded_fraction);
  std::printf("  integer shift: truth %.2f  dmin %d  map median %.4f  physical %.4f  "
              "z %.2f mm  valid %.3f  occluded %.4f  LR agreement %.4f  %.3f s\n",
              truth, r.stats.min_disparity, r.stats.median_disparity, physical_shift(r), z,
              r.stats.valid_fraction, r.stats.occluded_fraction, lr_agreement,
              r.stats.seconds);
  // Offset convention: the map includes the window minimum, the physical shift
  // is the difference, and depth_at() undoes the offset exactly.
  CHECK(r.stats.min_disparity == static_cast<int>(expected_dmin));
  CHECK_NEAR(r.stats.median_disparity, expected_dmin + truth, 0.5);
  CHECK_NEAR(physical_shift(r), truth, 0.1);  // measured error is exactly 0
  CHECK_NEAR(z, f * baseline / truth, 0.01 * f * baseline / truth);
  CHECK(r.stats.valid_fraction > 0.80);
  CHECK(r.stats.median_disparity >= r.stats.min_disparity &&
        r.stats.median_disparity < r.stats.min_disparity + r.stats.num_disparities);
  // Clean pair: essentially every matched pixel must agree left/right.
  CHECK(lr_agreement > 0.95);
  CHECK(r.stats.occluded_fraction < 0.05);
}

// ---------------------------------------------------------------------------
// 2. half-pixel shift (SGBM's 1/16 px is the baseline; the 3x3 parabola is the
//    optional refinement)
// ---------------------------------------------------------------------------
void test_subpixel_shift() {
  const int width = 640, height = 384;
  const double truth = 60.5;
  cv::Mat left, right;
  make_scene(width, height, truth, 12, left, right);
  const RectifiedPair pair = make_pair(left, right, 1158.370 / 2.0, 40.0);
  StereoOptions o;
  o.scale = 1.0;
  o.subpixel = true;
  const StereoResult refined = compute_stereo(pair, o);
  o.subpixel = false;
  const StereoResult raw = compute_stereo(pair, o);
  check_contract(refined, "subpixel shift");
  CHECK(refined.ok && raw.ok);
  if (!refined.ok || !raw.ok) return;
  std::printf("  subpixel shift %.1f: refined %.4f (err %+.4f)  raw %.4f (err %+.4f)  "
              "valid %.3f\n",
              truth, physical_shift(refined), physical_shift(refined) - truth,
              physical_shift(raw), physical_shift(raw) - truth, refined.stats.valid_fraction);
  // Both are inside the 0.1 px budget at the half-pixel boundary.
  CHECK(std::abs(physical_shift(refined) - truth) <= 0.1);
  CHECK(std::abs(physical_shift(raw) - truth) <= 0.1);
  CHECK(refined.stats.valid_fraction > 0.80);
  CHECK(refined.stats.occluded_fraction < 0.05);
}

// ---------------------------------------------------------------------------
// 3. confidence: textured half versus nearly flat half
// ---------------------------------------------------------------------------
void test_confidence_texture() {
  const int width = 640, height = 384;
  const double truth = 60.0;
  const int margin = static_cast<int>(truth) + 8;
  cv::Mat big = make_texture(width + margin, height, 13);
  // Right half: almost flat (std ~1 grey level) so a match is possible but the
  // disparity is barely observable.  A perfectly constant patch would simply be
  // unmatched, which would make this test trivial.
  cv::Mat faint(height, width + margin, CV_32F);
  cv::RNG rng(77);
  rng.fill(faint, cv::RNG::UNIFORM, -1.0f, 1.0f);
  for (int v = 0; v < height; ++v)
    for (int u = width / 2; u < width + margin; ++u)
      big.at<std::uint8_t>(v, u) =
          cv::saturate_cast<std::uint8_t>(120.0 + faint.at<float>(v, u));
  cv::Mat left, right;
  make_scene_from(big, width, truth, left, right);
  StereoOptions o;
  o.scale = 1.0;
  const StereoResult r = compute_stereo(make_pair(left, right, 1158.370 / 2.0, 40.0), o);
  check_contract(r, "confidence texture");
  CHECK(r.ok);
  if (!r.ok) return;
  const cv::Rect textured(20, 20, width / 2 - 40, height - 40);
  const cv::Rect flat(width / 2 + 20, 20, width / 2 - 40, height - 40);
  const double c_textured = cv::mean(r.confidence(textured))[0];
  const double c_flat = cv::mean(r.confidence(flat))[0];
  std::printf("  confidence: textured %.3f  flat %.3f  (valid %.3f)\n", c_textured, c_flat,
              r.stats.valid_fraction);
  CHECK(c_textured > c_flat + 0.2);
  CHECK(c_textured > 0.5);
  CHECK(c_flat < 0.5);
}

// ---------------------------------------------------------------------------
// 4. depth mapping at 300 / 450 / 600 mm through the returned geometry
// ---------------------------------------------------------------------------
void test_depth_mapping() {
  // The real image width keeps the useful overlap large at 300 mm, where the
  // physical shift is 612 full-resolution pixels.
  const int width = 1280, height = 480;
  const double f_full = 1158.370;  // the real rectified focal length
  const double b = 158.557;        // the real baseline
  const double scale = 0.5;
  for (double truth : {300.0, 450.0, 600.0}) {
    // The pair is built at full resolution; compute_stereo rescales it.
    const double full_shift = f_full * b / truth;
    cv::Mat left, right;
    make_scene(width, height, full_shift, 21, left, right);
    const RectifiedPair pair = make_pair(left, right, f_full, b);
    StereoOptions o;
    o.scale = scale;
    const StereoResult r = compute_stereo(pair, o);
    check_contract(r, "depth mapping");
    CHECK(r.ok);
    if (!r.ok) continue;
    const double z = r.geometry.depth_at(r.stats.median_disparity);
    std::printf("  depth %.0f mm: physical %.3f (truth %.3f)  z %.2f mm  valid %.3f\n",
                truth, physical_shift(r), full_shift * scale, z, r.stats.valid_fraction);
    CHECK_NEAR(z, truth, 0.01 * truth);
    CHECK_NEAR(physical_shift(r), full_shift * scale, 0.5);
    // The geometry must agree with the profile-scaled parameters, plus the
    // search-window minimum that compute_stereo adds to disp_offset.
    CHECK_NEAR(r.geometry.f, f_full * scale, 1e-9);
    CHECK_NEAR(r.geometry.disp_offset, static_cast<double>(r.stats.min_disparity), 1e-9);
    // Overlap alone is (width - shift)/width: 52% at 300 mm, 68% at 450 mm and
    // 76% at 600 mm; the matcher keeps most of it.
    CHECK(r.stats.valid_fraction > 0.30);
  }
}

// ---------------------------------------------------------------------------
// 5. negative disparity offset (search-window minimum below zero)
// ---------------------------------------------------------------------------
void test_negative_offset() {
  const int width = 640, height = 384;
  const double f = 1158.370 / 2.0, b = 158.557;
  const double offset = -200.0;
  const double shift = f * b / 300.0 + offset;  // physical shift at 300 mm
  cv::Mat left, right;
  make_scene(width, height, shift, 31, left, right);
  StereoOptions o;
  o.scale = 1.0;
  const StereoResult r = compute_stereo(make_pair(left, right, f, b, offset), o);
  check_contract(r, "negative offset", offset);
  CHECK(r.ok);
  if (!r.ok) {
    std::fprintf(stderr, "     negative offset failed: %s\n", r.error.c_str());
    return;
  }
  std::printf("  negative offset: physical %.2f  dmin %d  map median %.4f  z %.2f mm  "
              "valid %.3f\n",
              shift, r.stats.min_disparity, r.stats.median_disparity,
              r.geometry.depth_at(r.stats.median_disparity), r.stats.valid_fraction);
  CHECK(r.stats.min_disparity < 0);  // the padded negative-disparity path is exercised
  CHECK_NEAR(physical_shift(r), shift, 0.5);
  CHECK_NEAR(r.geometry.depth_at(r.stats.median_disparity), 300.0, 3.0);
  CHECK(r.stats.valid_fraction > 0.5);
}

// ---------------------------------------------------------------------------
// 6. occlusion: a vertical bar only in the left image
// ---------------------------------------------------------------------------
void test_occlusion() {
  const int width = 640, height = 384;
  const double truth = 60.0;
  cv::Mat left, right;
  make_scene(width, height, truth, 41, left, right);
  const cv::Mat bar = make_texture(40, height, 99);
  bar.copyTo(left.colRange(300, 340));
  StereoOptions o;
  o.scale = 1.0;
  const StereoResult r = compute_stereo(make_pair(left, right, 1158.370 / 2.0, 40.0), o);
  check_contract(r, "occlusion");
  CHECK(r.ok);
  if (!r.ok) return;
  const double bad = invalid_or_occluded_fraction(r, cv::Rect(300, 20, 40, height - 40));
  std::printf("  occlusion: strip invalid/occluded %.3f  valid overall %.3f  occluded %.3f\n",
              bad, r.stats.valid_fraction, r.stats.occluded_fraction);
  CHECK(bad > 0.75);
  // The background next to the strip must still match.
  CHECK(r.stats.valid_fraction > 0.5);
}

// ---------------------------------------------------------------------------
// 7. exposure difference (the real streams differ by ~2.5x)
// ---------------------------------------------------------------------------
void test_exposure() {
  const int width = 640, height = 384;
  const double truth = 60.0;
  cv::Mat left, bright_right;
  make_scene(width, height, truth, 51, left, bright_right);
  cv::Mat dark, dark_right;
  left.convertTo(dark, CV_8U, 0.4, 0.0);           // 48 +- 22 grey levels
  bright_right.convertTo(dark_right, CV_8U, 0.4);  // same scene, same exposure
  StereoOptions o;
  o.scale = 1.0;
  const StereoResult equal = compute_stereo(make_pair(dark, dark_right, 1158.370 / 2.0, 40.0), o);
  const StereoResult uneven =
      compute_stereo(make_pair(dark, bright_right, 1158.370 / 2.0, 40.0), o);
  check_contract(equal, "exposure equal");
  check_contract(uneven, "exposure 2.5x");
  CHECK(equal.ok && uneven.ok);
  if (!equal.ok || !uneven.ok) return;
  const double ratio = uneven.stats.valid_fraction / equal.stats.valid_fraction;
  std::printf("  exposure: equal %.3f  2.5x %.3f  ratio %.3f  physical %.3f vs %.3f\n",
              equal.stats.valid_fraction, uneven.stats.valid_fraction, ratio,
              physical_shift(uneven), physical_shift(equal));
  CHECK(ratio > 0.70);
  CHECK(std::abs(physical_shift(uneven) - truth) <= 0.5);
}

// ---------------------------------------------------------------------------
// 8. determinism
// ---------------------------------------------------------------------------
void test_determinism() {
  const int width = 480, height = 320;
  cv::Mat left, right;
  make_scene(width, height, 47.25, 61, left, right);
  const RectifiedPair pair = make_pair(left, right, 1158.370 / 2.0, 40.0);
  StereoOptions o;
  o.scale = 1.0;
  const StereoResult a = compute_stereo(pair, o);
  const StereoResult b = compute_stereo(pair, o);
  CHECK(a.ok && b.ok);
  if (!a.ok || !b.ok) return;
  CHECK(cv::norm(a.disparity, b.disparity, cv::NORM_INF) == 0.0);
  CHECK(cv::norm(a.confidence, b.confidence, cv::NORM_INF) == 0.0);
  CHECK(cv::norm(a.valid, b.valid, cv::NORM_INF) == 0.0);
  CHECK(cv::norm(a.occluded, b.occluded, cv::NORM_INF) == 0.0);
  CHECK(a.stats.valid_fraction == b.stats.valid_fraction);
  CHECK(a.stats.median_disparity == b.stats.median_disparity);
  CHECK(a.stats.min_disparity == b.stats.min_disparity);
  CHECK(a.stats.num_disparities == b.stats.num_disparities);
  std::printf("  determinism: identical maps, valid %.3f, map median %.4f\n",
              a.stats.valid_fraction, a.stats.median_disparity);
}

// ---------------------------------------------------------------------------
// 9. invalid inputs are rejected with an explanation, never a crash
// ---------------------------------------------------------------------------
void test_invalid_inputs() {
  cv::Mat tex, shifted;
  make_scene(320, 240, 20.0, 71, tex, shifted);
  const RectifiedPair good = make_pair(tex, shifted, 579.185, 40.0);
  StereoOptions o;
  o.scale = 1.0;
  {
    RectifiedPair empty;
    empty.ok = false;
    empty.error = "test";
    const StereoResult r = compute_stereo(empty, o);
    CHECK(!r.ok);
    CHECK(!r.error.empty());
  }
  {
    RectifiedPair p = good;
    p.right = cv::Mat(240, 300, CV_8UC1, cv::Scalar(0));
    const StereoResult r = compute_stereo(p, o);
    CHECK(!r.ok);
    CHECK(!r.error.empty());
  }
  {
    RectifiedPair p = good;
    p.right = cv::Mat(p.right.size(), CV_8UC3, cv::Scalar(0, 0, 0));
    const StereoResult r = compute_stereo(p, o);
    CHECK(!r.ok);
  }
  {
    RectifiedPair p = good;
    p.f = 0.0;
    const StereoResult r = compute_stereo(p, o);
    CHECK(!r.ok);
    CHECK(r.error.find("focal") != std::string::npos);
  }
  {
    RectifiedPair p = good;
    p.baseline_mm = -1.0;
    const StereoResult r = compute_stereo(p, o);
    CHECK(!r.ok);
    CHECK(r.error.find("baseline") != std::string::npos);
  }
  {
    StereoOptions bad = o;
    bad.min_depth_mm = 500;
    bad.max_depth_mm = 400;
    const StereoResult r = compute_stereo(good, bad);
    CHECK(!r.ok);
    CHECK(r.error.find("depth range") != std::string::npos);
  }
  {
    StereoOptions bad = o;
    bad.min_depth_mm = 0.0;
    const StereoResult r = compute_stereo(good, bad);
    CHECK(!r.ok);
  }
  {
    StereoOptions bad = o;
    bad.scale = 0.0;
    const StereoResult r = compute_stereo(good, bad);
    CHECK(!r.ok);
    CHECK(r.error.find("scale") != std::string::npos);
  }
  {
    StereoOptions bad = o;
    bad.lr_tolerance_px = 0.0;
    const StereoResult r = compute_stereo(good, bad);
    CHECK(!r.ok);
  }
  {
    // Degenerate depth range so wide that the search window is refused.
    StereoOptions bad = o;
    bad.min_depth_mm = 1.0;
    bad.max_depth_mm = 100000.0;
    const StereoResult r = compute_stereo(good, bad);
    CHECK(!r.ok);
    CHECK(r.error.find("window") != std::string::npos);
  }
  std::printf("  invalid inputs: all rejection checks ran\n");
}

// ---------------------------------------------------------------------------
// 10. real frames (private capture, skipped when absent)
// ---------------------------------------------------------------------------
void test_real_frames() {
  const std::string dir =
      env_or("EINSTAR_CAPTURE_DIR", "<capture-dir>");
  const std::string profile_path =
      env_or("EINSTAR_PROFILE", "<private-inputs>/einstar-profiles/factory-e10.json");
  if (!fs::exists(dir + "/frame_000000_cam1.pgm") || !fs::exists(profile_path)) {
    std::printf("  real frames: SKIP (set EINSTAR_CAPTURE_DIR / EINSTAR_PROFILE to enable)\n");
    return;
  }
  FrameSet frames;
  CalibrationProfile profile;
  try {
    frames = load_frame_set(dir, 0);
    profile = load_profile(profile_path);
  } catch (const std::exception& e) {
    std::printf("  real frames: SKIP (load failed: %s)\n", e.what());
    return;
  }
  const RectifiedPair pair = rectify(frames, profile);
  CHECK(pair.ok);
  if (!pair.ok) {
    std::printf("  real frames: SKIP (rectify failed: %s)\n", pair.error.c_str());
    return;
  }
  StereoOptions o;
  o.scale = 0.5;
  const StereoResult half = compute_stereo(pair, o);
  check_contract(half, "real half");
  CHECK(half.ok);
  if (!half.ok) {
    std::printf("  real frames: stereo failed: %s\n", half.error.c_str());
    return;
  }
  const double z_half = half.geometry.depth_at(half.stats.median_disparity);
  std::printf("  real frame 0 @0.5: valid %.3f occluded %.3f  map p05/50/95 "
              "%.1f/%.1f/%.1f  physical median %.1f  depth %.1f mm  window [%d,%d)  %.3f s\n",
              half.stats.valid_fraction, half.stats.occluded_fraction,
              half.stats.p05_disparity, half.stats.median_disparity,
              half.stats.p95_disparity, physical_shift(half), z_half,
              half.stats.min_disparity,
              half.stats.min_disparity + half.stats.num_disparities, half.stats.seconds);
  CHECK(half.stats.valid_fraction > 0.15);
  CHECK(z_half > 150.0 && z_half < 800.0);
  CHECK(half.stats.p05_disparity > 0 && half.stats.p95_disparity > half.stats.p05_disparity);

  // Full resolution: same contract, and the runtime is part of the report.
  StereoOptions full_options;
  full_options.scale = 1.0;
  const StereoResult full = compute_stereo(pair, full_options);
  check_contract(full, "real full");
  CHECK(full.ok);
  if (!full.ok) return;
  const double z_full = full.geometry.depth_at(full.stats.median_disparity);
  std::printf("  real frame 0 @1.0: valid %.3f  map p05/50/95 %.1f/%.1f/%.1f  physical "
              "median %.1f  depth %.1f mm  window [%d,%d)  %.3f s\n",
              full.stats.valid_fraction, full.stats.p05_disparity,
              full.stats.median_disparity, full.stats.p95_disparity, physical_shift(full),
              z_full, full.stats.min_disparity,
              full.stats.min_disparity + full.stats.num_disparities, full.stats.seconds);
  CHECK(full.stats.valid_fraction > 0.15);
  CHECK(z_full > 150.0 && z_full < 800.0);
  std::printf("  real frame 0 runtime: half %.3f s  full %.3f s\n", half.stats.seconds,
              full.stats.seconds);
}

}  // namespace

void test_stereo() {
  std::printf("stereo:\n");
  test_integer_shift();
  test_subpixel_shift();
  test_confidence_texture();
  test_depth_mapping();
  test_negative_offset();
  test_occlusion();
  test_exposure();
  test_determinism();
  test_invalid_inputs();
  test_real_frames();
}
