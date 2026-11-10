// Stereo correspondence for the Einstar IR pair.
//
// The pair is rectified by rectify() (Bouguet, alpha=-1), so epipolar lines are
// horizontal and the disparity of a point at depth z is f*B/z + offset.  The
// Einstar geometry is strongly verged (~22 deg) and the rectified views are
// wedges, so a large part of each rectified image samples outside the source
// frame; those pixels are excluded through RectifiedPair::valid_left/right.
//
// Matching strategy
// -----------------
// 1. The metric depth range is converted to a disparity window
//    [min_disp, min_disp + num_disp).  OpenCV SGBM can only search
//    [0, numDisparities) and always leaves its leftmost numDisparities columns
//    invalid, so the images are padded on the left instead of using
//    minDisparity: left by num_disp + pad_l, right by num_disp + pad_r with
//    pad_l - pad_r = -min_disp.  SGBM then searches [0, num_disp), its own
//    border falls inside the constant-zero padding, and the cropped result is
//    shifted by +min_disp.  The same trick is used by the libeinstar prototype
//    (libeinstar/src/depth.cpp, match()); this is an independent
//    re-implementation.  It is what makes negative disparities (offset < 0)
//    representable, which plain SGBM cannot do.
// 2. A second SGBM pass on the mirrored pair (flip(right), flip(left)) yields
//    the right-view disparity: for a right pixel u_r the mirror matcher returns
//    u_l - u_r directly, because mirroring both images and swapping their roles
//    maps "match to the left" back onto "match to the right" with the same
//    sign and the same search window.  cv::ximgproc::createRightMatcher was not
//    used: it is an optional module that is not part of ${OpenCV_LIBS} on every
//    build and its sign convention would have to be verified anyway.
// 3. Left-right consistency: a left pixel is kept only when the right-view
//    disparity sampled at the physical correspondence agrees within
//    lr_tolerance_px.
//
// Disparity convention (the stereo.hpp contract)
// ---------------------------------------------
// The returned map value is the *offset* disparity, not the raw pixel shift:
//     disparity      = (u_l - u_r) + min_disparity
//     geometry.disp_offset = min_disparity + pair.disp_offset_full * scale
// hence depth_at(disparity) = f*B / (disparity - disp_offset) = f*B/((u_l-u_r) - off).
// The physical pixel shift between the rectified images is
// disparity - geometry.disp_offset, and the left-right test samples the right
// map at u_l - (disparity - disp_offset).  `min_disparity` is reported in
// StereoStats::min_disparity.  With the depth-range window the physical shift
// lives in [min_disparity, min_disparity + num_disparities), so the map values
// live in [2*min_disparity, 2*min_disparity + num_disparities); for the real
// frame at half resolution that is [224, 736) with a measured median of 382.
// The outer half of the window (physical shift < min_disparity) is not
// searched; that keeps the far depth limit exact and costs no extra work.
// Note that a strongly negative P-matrix offset can make the far end of the
// depth range map to disparity <= 0; such pixels are reported invalid because
// the contract reserves <= 0 for "invalid".
//
// Sub-pixel
// ---------
// SGBM already interpolates its cost parabola into 1/16 pixel.  `subpixel`
// additionally fits a parabola through the 3x3 disparity neighbourhood
// (horizontal + vertical through the centre, clamped to +-0.5/16 px, and only
// where the fit is convex).  Measured on synthetic band-limited textures:
// at a half-pixel shift the raw SGBM median error is 0.000-0.063 px depending
// on the texture and the refined 0.031 px; at quarter-pixel shifts SGBM itself
// has a +-0.15..0.28 px bias (the 3x3 fit changes it by <0.04 px and cannot
// remove it), and at integer shifts both are exact.  The refinement is
// therefore kept for its small gain at the half-pixel boundary, not because it
// is a large correction.
//
// Confidence
// ----------
// SGBM does not expose the cost margin between the best and the second-best
// hypothesis, so "uniqueness" cannot be measured directly here.  What is
// measurable is used instead:
//   * left/right agreement (hard requirement; the soft term is
//     exp(-0.5 (diff/sigma)^2) and only shapes confidence inside the accepted
//     set),
//   * local texture: variance of the rectified left patch (a flat patch has no
//     well-defined disparity no matter what the cost says),
//   * distance from the search-range edges (a match at the very end of the
//     window is a censored observation),
//   * SGBM's own uniqueness and speckle filters, which have already removed
//     small isolated blobs by the time the maps are produced.
// The texture/LR terms are combined additively and then multiplied by the
// border factor, so a flat patch cannot reach a high confidence through LR
// agreement alone (measured on a synthetic half-flat pair: mean 0.33 on the
// std=1 grey-level half versus 0.76 on the textured half).
// Confidence is exactly 0 wherever `valid` is 0.
#include "einstar/recon/stereo.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <limits>
#include <string>
#include <utility>
#include <vector>

#include <opencv2/calib3d.hpp>
#include <opencv2/imgproc.hpp>

namespace einstar::recon {
namespace {

// OpenCV's own default: a Sobel-normalised Birchfield-Tomasi cost.  Measured on
// the real frame it matters (half resolution, everything else default): raw
// SGBM coverage 0.404 -> 0.435 and LR-consistent coverage inside both valid
// masks 0.106 -> 0.170, because it removes most of the 1.7x exposure difference
// between the IR streams.  preFilterCap = 0 (the libeinstar prototype's value)
// leaves raw intensities in the cost and loses that robustness.
constexpr int kPreFilterCap = 63;
// -1 disables SGBM's internal left-right check; the consistency test in this
// file is stricter and knows about the padded coordinates.
constexpr int kDisp12MaxDiff = -1;

constexpr float kInvalid = -1.0f;               // disparity sentinel in this file
constexpr double kMaxDisparitySearch = 8192.0;  // sanity cap on the window

int align_down(double v, int a) {
  return static_cast<int>(std::floor(v / static_cast<double>(a))) * a;
}
int align_up(double v, int a) {
  return static_cast<int>(std::ceil(v / static_cast<double>(a))) * a;
}

struct Window {
  int min_disparity = 0;
  int num_disparities = 0;
};

// Disparity window covering [min_depth_mm, max_depth_mm].  disparity_at(z) is
// decreasing in z for a positive baseline, but the bounds are kept explicit in
// min/max form so an unusual (negative) disparity offset still produces an
// ascending window.
Window disparity_window(const StereoGeometry& g, double min_depth_mm, double max_depth_mm,
                        int align) {
  const double at_min_depth = g.disparity_at(min_depth_mm);
  const double at_max_depth = g.disparity_at(max_depth_mm);
  const double lo = std::min(at_min_depth, at_max_depth);
  const double hi = std::max(at_min_depth, at_max_depth);
  Window w;
  w.min_disparity = align_down(lo, align);
  w.num_disparities = std::max(align, align_up(hi - w.min_disparity, align));
  return w;
}

// SGBM disparity for `left` versus `right`, searched in
// [min_disp, min_disp + num_disp); unmatched pixels are kInvalid.
cv::Mat match_padded(const cv::Mat& left, const cv::Mat& right, int min_disp, int num_disp,
                     const StereoOptions& options, int block) {
  const int pad_l = std::max(0, -min_disp);
  const int pad_r = std::max(0, min_disp);
  const int off_l = num_disp + pad_l;
  const int off_r = num_disp + pad_r;
  const int width = std::max(left.cols, right.cols) + num_disp + std::max(pad_l, pad_r) + 2;
  cv::Mat lpad, rpad;
  cv::copyMakeBorder(left, lpad, 0, 0, off_l, width - left.cols - off_l,
                     cv::BORDER_CONSTANT, cv::Scalar(0));
  cv::copyMakeBorder(right, rpad, 0, 0, off_r, width - right.cols - off_r,
                     cv::BORDER_CONSTANT, cv::Scalar(0));
  auto sgbm = cv::StereoSGBM::create(
      0, num_disp, block, options.p1_factor * block * block,
      options.p2_factor * block * block, kDisp12MaxDiff, kPreFilterCap,
      options.uniqueness_ratio, options.speckle_window, options.speckle_range,
      cv::StereoSGBM::MODE_SGBM_3WAY);
  cv::Mat d16;
  sgbm->compute(lpad, rpad, d16);
  cv::Mat d;
  d16.colRange(off_l, off_l + left.cols).convertTo(d, CV_32F, 1.0 / 16.0);
  for (int v = 0; v < d.rows; ++v) {
    float* row = d.ptr<float>(v);
    for (int u = 0; u < d.cols; ++u)
      row[u] = row[u] < 0.0f ? kInvalid : row[u] + static_cast<float>(min_disp);
  }
  return d;
}

// 3x3 disparity parabola.  The centre sample is kept unless the horizontal or
// vertical fit is convex; the correction is clamped to +-0.5/16 px because the
// SGBM value is already a 1/16-px cost interpolation and a larger move would be
// extrapolation, not refinement.  Pixels with an invalid neighbour are left
// untouched (nothing to fit).
void refine_subpixel(cv::Mat& d) {
  const cv::Mat src = d.clone();
  constexpr float kClamp = 0.5f / 16.0f;
  for (int v = 1; v + 1 < src.rows; ++v) {
    const float* up = src.ptr<float>(v - 1);
    const float* mid = src.ptr<float>(v);
    const float* down = src.ptr<float>(v + 1);
    float* out_row = d.ptr<float>(v);
    for (int u = 1; u + 1 < src.cols; ++u) {
      const float c = mid[u];
      if (c < 0.0f) continue;
      const float l = mid[u - 1], r = mid[u + 1];
      const float t = up[u], b = down[u];
      float offset = 0.0f;
      int fits = 0;
      if (l >= 0.0f && r >= 0.0f) {
        const float den = l - 2.0f * c + r;
        if (den > 1e-6f) {
          offset += 0.5f * (l - r) / den;
          ++fits;
        }
      }
      if (t >= 0.0f && b >= 0.0f) {
        const float den = t - 2.0f * c + b;
        if (den > 1e-6f) {
          offset += 0.5f * (t - b) / den;
          ++fits;
        }
      }
      if (fits == 0) continue;
      offset /= static_cast<float>(fits);
      out_row[u] = c + std::clamp(offset, -kClamp, kClamp);
    }
  }
}

// Bilinear sample of a disparity map; a neighbour only has to be valid when its
// weight is non-negligible, so integer positions do not require the next column
// to be valid as well.
bool sample_disparity(const cv::Mat& d, float x, float y, float& value) {
  if (x < 0.0f || y < 0.0f || x > static_cast<float>(d.cols) - 1.001f ||
      y > static_cast<float>(d.rows) - 1.001f)
    return false;
  const int x0 = static_cast<int>(std::floor(x));
  const int y0 = static_cast<int>(std::floor(y));
  const float fx = x - static_cast<float>(x0);
  const float fy = y - static_cast<float>(y0);
  const int x1 = std::min(x0 + 1, d.cols - 1);
  const int y1 = std::min(y0 + 1, d.rows - 1);
  const float w00 = (1.0f - fx) * (1.0f - fy), w10 = fx * (1.0f - fy);
  const float w01 = (1.0f - fx) * fy, w11 = fx * fy;
  const float v00 = d.at<float>(y0, x0), v10 = d.at<float>(y0, x1);
  const float v01 = d.at<float>(y1, x0), v11 = d.at<float>(y1, x1);
  if ((w00 > 1e-4f && v00 < 0.0f) || (w10 > 1e-4f && v10 < 0.0f) ||
      (w01 > 1e-4f && v01 < 0.0f) || (w11 > 1e-4f && v11 < 0.0f))
    return false;
  float sum = 0.0f, weight = 0.0f;
  const auto add = [&](float w, float v) {
    if (w <= 1e-4f) return;
    sum += w * v;
    weight += w;
  };
  add(w00, v00);
  add(w10, v10);
  add(w01, v01);
  add(w11, v11);
  if (weight <= 0.0f) return false;
  value = sum / weight;
  return true;
}

// Resize/erode a validity mask to the matching resolution; an empty mask means
// "everything the rectification produced is usable".
cv::Mat scaled_mask(const cv::Mat& mask, cv::Size size) {
  if (mask.empty()) return cv::Mat(size, CV_8UC1, cv::Scalar(255));
  cv::Mat scaled = mask;
  if (mask.size() != size) cv::resize(mask, scaled, size, 0, 0, cv::INTER_AREA);
  cv::Mat out;
  // A reduced-resolution mask pixel survives only if (almost) all of the
  // full-resolution pixels it covers were valid: INTER_AREA of a 0/255 mask at
  // scale 0.5 is 255*k/4, so >=254 means k == 4.
  cv::compare(scaled, 254, out, cv::CMP_GE);
  return out;
}

std::vector<double> percentiles(const std::vector<float>& values,
                                std::initializer_list<double> qs) {
  std::vector<double> out;
  if (values.empty()) return std::vector<double>(qs.size(), 0.0);
  std::vector<float> sorted = values;
  std::sort(sorted.begin(), sorted.end());
  for (double q : qs) {
    const double pos = q * static_cast<double>(sorted.size() - 1);
    const auto i = static_cast<std::size_t>(pos);
    const double f = pos - static_cast<double>(i);
    out.push_back(i + 1 < sorted.size() ? sorted[i] * (1.0 - f) + sorted[i + 1] * f
                                       : sorted[i]);
  }
  return out;
}

}  // namespace

StereoResult compute_stereo(const RectifiedPair& pair, const StereoOptions& options) {
  const auto t0 = std::chrono::steady_clock::now();
  StereoResult out;
  const auto fail = [&out](std::string message) {
    out.ok = false;
    out.error = std::move(message);
    return out;
  };

  // ---- input validation -------------------------------------------------
  if (!pair.ok) return fail("rectified pair is not usable: " + pair.error);
  if (pair.left.empty() || pair.right.empty()) return fail("empty rectified IR pair");
  if (pair.left.size() != pair.right.size()) return fail("rectified IR pair size mismatch");
  if (pair.left.channels() != 1 || pair.right.channels() != 1)
    return fail("rectified IR pair is not single channel");
  if (!(std::isfinite(pair.f) && pair.f > 0.0)) return fail("invalid rectified focal length");
  if (!(std::isfinite(pair.baseline_mm) && pair.baseline_mm > 0.0))
    return fail("invalid stereo baseline");
  if (!std::isfinite(pair.disp_offset_full)) return fail("invalid disparity offset");
  if (!(std::isfinite(options.min_depth_mm) && options.min_depth_mm > 0.0) ||
      !std::isfinite(options.max_depth_mm) || options.max_depth_mm <= options.min_depth_mm)
    return fail("empty depth range (min_depth_mm=" + std::to_string(options.min_depth_mm) +
                ", max_depth_mm=" + std::to_string(options.max_depth_mm) + ")");
  if (!(std::isfinite(options.scale) && options.scale > 0.0 && options.scale <= 4.0))
    return fail("invalid matching scale");
  if (!(std::isfinite(options.lr_tolerance_px) && options.lr_tolerance_px > 0.0))
    return fail("invalid left-right tolerance");

  // SGBM needs an odd block size; normalise rather than reject a plausible
  // request (an even block size has no defined window centre).
  const int block = std::max(3, options.block_size | 1);
  int align = options.min_disparity_alignment;
  if (align < 16 || align % 16 != 0) align = 16;

  // ---- geometry ---------------------------------------------------------
  // A caller-built RectifiedPair may leave `size` unset; the images are the
  // authority then.
  RectifiedPair geometry_pair = pair;
  if (geometry_pair.size.width <= 0 || geometry_pair.size.height <= 0)
    geometry_pair.size = pair.left.size();
  StereoGeometry geometry = scaled_geometry(geometry_pair, options.scale);
  if (geometry.size.width < 16 || geometry.size.height < 16)
    return fail("scaled matching image is too small");
  if (!(std::isfinite(geometry.f) && geometry.f > 0.0) ||
      !(std::isfinite(geometry.baseline_mm) && geometry.baseline_mm > 0.0))
    return fail("scaled stereo geometry is invalid");

  const Window window =
      disparity_window(geometry, options.min_depth_mm, options.max_depth_mm, align);
  if (window.num_disparities <= 0 ||
      window.num_disparities > static_cast<int>(kMaxDisparitySearch))
    return fail("disparity search window is degenerate or too large (" +
                std::to_string(window.num_disparities) + " px)");
  // The header contract: map values include the search-window minimum, so
  // depth_at() has to subtract it together with the P-matrix offset.
  geometry.disp_offset += static_cast<double>(window.min_disparity);

  // ---- resize -----------------------------------------------------------
  const cv::Size size = geometry.size;
  const int interpolation = options.scale < 1.0 ? cv::INTER_AREA : cv::INTER_LINEAR;
  cv::Mat left = pair.left, right = pair.right;
  if (pair.left.size() != size) {
    cv::resize(pair.left, left, size, 0, 0, interpolation);
    cv::resize(pair.right, right, size, 0, 0, interpolation);
  }
  const cv::Mat mask_left = scaled_mask(pair.valid_left, size);
  const cv::Mat mask_right = scaled_mask(pair.valid_right, size);

  // CLAHE is optional: on the real frame it raises the raw SGBM coverage from
  // 0.404 to 0.411 and the LR-consistent coverage from 0.170 to 0.177, i.e. a
  // small but consistent gain, and it does not change the median depth.
  if (options.equalize) {
    auto clahe = cv::createCLAHE(3.0, cv::Size(16, 16));
    clahe->apply(left, left);
    clahe->apply(right, right);
  }

  // ---- matching ---------------------------------------------------------
  cv::Mat disp_left, disp_right;
  try {
    disp_left = match_padded(left, right, window.min_disparity, window.num_disparities,
                             options, block);
    // Right-view disparity from the mirrored pair: flip(right) becomes the
    // "left" image and flip(left) the "right" one, so the mirror matcher's
    // u_l' - u_r' equals u_l - u_r of the original pair.
    cv::Mat flipped_right, flipped_left;
    cv::flip(right, flipped_right, 1);
    cv::flip(left, flipped_left, 1);
    cv::Mat mirrored = match_padded(flipped_right, flipped_left, window.min_disparity,
                                    window.num_disparities, options, block);
    cv::flip(mirrored, disp_right, 1);
  } catch (const cv::Exception& e) {
    return fail(std::string("SGBM failed: ") + e.what());
  } catch (const std::exception& e) {
    return fail(std::string("SGBM failed: ") + e.what());
  }

  if (options.subpixel) {
    refine_subpixel(disp_left);
    refine_subpixel(disp_right);
  }
  // The right view is only defined where the right image has real samples.
  for (int v = 0; v < disp_right.rows; ++v) {
    float* d = disp_right.ptr<float>(v);
    const std::uint8_t* m = mask_right.ptr<std::uint8_t>(v);
    for (int u = 0; u < disp_right.cols; ++u)
      if (!m[u]) d[u] = kInvalid;
  }

  // ---- consistency, validity, confidence --------------------------------
  out.disparity = cv::Mat::zeros(size, CV_32FC1);
  out.confidence = cv::Mat::zeros(size, CV_32FC1);
  out.valid = cv::Mat::zeros(size, CV_8UC1);
  out.occluded = cv::Mat::zeros(size, CV_8UC1);

  cv::Mat left_f;
  left.convertTo(left_f, CV_32F);
  cv::Mat mean, mean_sq;
  constexpr int kTextureWindow = 9;
  cv::blur(left_f, mean, {kTextureWindow, kTextureWindow});
  cv::blur(left_f.mul(left_f), mean_sq, {kTextureWindow, kTextureWindow});

  const double lo = window.min_disparity;
  const double hi = window.min_disparity + window.num_disparities;
  // Hard margin: a disparity at the very end of the window is censored.  2 px
  // or 0.5% of the window, whichever is larger (2.6 px at half resolution with
  // the default 150-800 mm range).
  const double hard_margin = std::max(2.0, 0.005 * window.num_disparities);
  // Soft margin used by the confidence term: 2% of the window.
  const double soft_margin = std::max(2.0, 0.02 * window.num_disparities);
  const double sigma_lr = std::max(options.lr_tolerance_px / 3.0, 1e-3);
  constexpr double kTextureSigma = 6.0;  // grey levels; var(6) -> t = 0.39
  // Texture and LR agreement are combined, then multiplied by the border
  // reliability: a flat patch must not look confident just because its
  // accidental match is left/right consistent.  The border factor is the weak
  // stand-in for the unavailable cost-margin (uniqueness) measure.
  constexpr double kTextureWeight = 0.65, kLrWeight = 0.35;

  std::vector<float> valid_disparities;
  valid_disparities.reserve(static_cast<std::size_t>(size.area() / 4));
  std::int64_t occluded_count = 0;

  for (int v = 0; v < size.height; ++v) {
    const float* dl = disp_left.ptr<float>(v);
    const std::uint8_t* ml = mask_left.ptr<std::uint8_t>(v);
    const float* mrow = mean.ptr<float>(v);
    const float* msrow = mean_sq.ptr<float>(v);
    float* drow = out.disparity.ptr<float>(v);
    float* crow = out.confidence.ptr<float>(v);
    std::uint8_t* vrow = out.valid.ptr<std::uint8_t>(v);
    std::uint8_t* orow = out.occluded.ptr<std::uint8_t>(v);
    for (int u = 0; u < size.width; ++u) {
      const float d = dl[u];
      if (d <= 0.0f || !ml[u]) continue;
      const float ur = static_cast<float>(u) - d;
      float dr_value = 0.0f;
      const bool have_right = sample_disparity(disp_right, ur, static_cast<float>(v), dr_value);
      const double diff = have_right ? std::abs(static_cast<double>(d) - dr_value) : 0.0;
      const bool consistent = have_right && diff <= options.lr_tolerance_px;
      if (!consistent) {
        // Either occluded in the left view (the right camera sees the
        // background) or unverifiable because the right sample falls outside
        // its valid wedge / matched nothing.  Both are reported as occluded.
        orow[u] = 255;
        ++occluded_count;
        continue;
      }
      if (d < lo + hard_margin || d > hi - hard_margin) continue;  // censored
      // Report the offset disparity of the header contract; the physical shift
      // is d.  A value <= 0 is reserved for "invalid", so a pixel whose offset
      // value would not be positive (only possible with a strongly negative
      // P-matrix offset) is dropped here.
      const double d_out = static_cast<double>(d) + lo;  // lo == min_disparity
      if (d_out <= 0.0) continue;
      vrow[u] = 255;
      drow[u] = static_cast<float>(d_out);
      valid_disparities.push_back(static_cast<float>(d_out));
      if (!options.compute_confidence) {
        crow[u] = 1.0f;
        continue;
      }
      const double mu = mrow[u];
      const double var = std::max(0.0, static_cast<double>(msrow[u]) - mu * mu);
      const double texture = 1.0 - std::exp(-var / (2.0 * kTextureSigma * kTextureSigma));
      const double lr_term = std::exp(-0.5 * (diff / sigma_lr) * (diff / sigma_lr));
      const double border = std::clamp((d - (lo + soft_margin)) / soft_margin, 0.0, 1.0) *
                            std::clamp(((hi - soft_margin) - d) / soft_margin, 0.0, 1.0);
      crow[u] = static_cast<float>(std::clamp(
          border * (kTextureWeight * texture + kLrWeight * lr_term), 0.0, 1.0));
    }
  }

  // ---- statistics -------------------------------------------------------
  const double area = static_cast<double>(size.area());
  out.stats.valid_fraction =
      area > 0 ? static_cast<double>(valid_disparities.size()) / area : 0.0;
  out.stats.occluded_fraction = area > 0 ? static_cast<double>(occluded_count) / area : 0.0;
  const std::vector<double> q = percentiles(valid_disparities, {0.05, 0.5, 0.95});
  out.stats.p05_disparity = q[0];
  out.stats.median_disparity = q[1];
  out.stats.p95_disparity = q[2];
  out.stats.min_disparity = window.min_disparity;
  out.stats.num_disparities = window.num_disparities;
  out.stats.width = size.width;
  out.stats.height = size.height;
  out.geometry = geometry;
  out.stats.seconds =
      std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
  out.ok = true;
  return out;
}

}  // namespace einstar::recon
