#include "einstar/recon/pipeline.hpp"

#include <chrono>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <stdexcept>

#include <opencv2/imgcodecs.hpp>
#include <opencv2/imgproc.hpp>

#include "einstar/recon/diagnostics.hpp"
#include "einstar/recon/hash.hpp"

namespace fs = std::filesystem;

namespace einstar::recon {
namespace {
double seconds_since(std::chrono::steady_clock::time_point t) {
  return std::chrono::duration<double>(std::chrono::steady_clock::now() - t).count();
}
std::string hash_frames(const FrameSet& fs_) {
  std::string bytes;
  for (const auto& f : fs_.streams) {
    bytes.append(reinterpret_cast<const char*>(f.mono.data),
                 f.mono.total() * f.mono.elemSize());
  }
  return sha256_hex(bytes);
}
}  // namespace

ReconResult reconstruct(const FrameSet& frames, const CalibrationProfile& profile,
                        const ReconOptions& options) {
  const auto t0 = std::chrono::steady_clock::now();
  ReconResult r;
  r.profile = profile;
  r.frames = frames;
  r.stats.profile_id = profile.id;
  r.stats.profile_kind = profile.kind;
  r.stats.source = frames.source;
  r.stats.frame = std::to_string(frames.group);
  if (!frames.complete()) {
    r.error = "incomplete frame set (need three streams)";
    return r;
  }
  const auto validation = validate_profile(profile);
  if (!validation.ok) {
    r.error = "invalid calibration profile: " + validation.summary();
    return r;
  }
  r.stats.frame_sha256 = hash_frames(frames);
  auto t = std::chrono::steady_clock::now();
  r.rectified = rectify(frames, profile, options.rectify);
  r.stats.rectify_seconds = seconds_since(t);
  if (!r.rectified.ok) { r.error = r.rectified.error; return r; }
  t = std::chrono::steady_clock::now();
  r.stereo = compute_stereo(r.rectified, options.stereo);
  r.stats.stereo_seconds = seconds_since(t);
  if (!r.stereo.ok) { r.error = r.stereo.error; return r; }
  t = std::chrono::steady_clock::now();
  r.depth = compute_depth(frames, profile, r.rectified, r.stereo, options.depth);
  r.stats.depth_seconds = seconds_since(t);
  if (!r.depth.ok) { r.error = r.depth.error; return r; }
  t = std::chrono::steady_clock::now();
  r.cloud = make_cloud(r.depth, options.cloud);
  r.stats.cloud_seconds = seconds_since(t);
  r.stats.points = static_cast<std::int64_t>(r.cloud.points.size());
  r.stats.depth_p05_mm = r.depth.stats.depth_p05_mm;
  r.stats.depth_p50_mm = r.depth.stats.depth_p50_mm;
  r.stats.depth_p95_mm = r.depth.stats.depth_p95_mm;
  r.stats.sigma_p50_mm = r.depth.stats.sigma_p50_mm;
  r.stats.valid_fraction = r.depth.stats.valid_fraction;
  r.stats.color = !r.cloud.colors.empty();
  r.stats.total_seconds = seconds_since(t0);
  r.ok = true;
  return r;
}

std::string write_outputs(const ReconResult& result, const std::string& outdir,
                          const OutputOptions& options) {
  fs::create_directories(outdir);
  const std::string prefix = options.prefix.empty() ? "recon" : options.prefix;
  const auto path = [&](const std::string& suffix) {
    return (fs::path(outdir) / (prefix + suffix)).string();
  };
  std::string ply;
  if (!result.ok) {
    std::ofstream e(path("_error.txt"));
    e << result.error << "\n";
    return {};
  }
  if (options.write_maps) {
    cv::imwrite(path("_rectified_left.png"), result.rectified.left);
    cv::imwrite(path("_rectified_right.png"), result.rectified.right);
    cv::imwrite(path("_valid_left.png"), result.rectified.valid_left);
    cv::imwrite(path("_valid_right.png"), result.rectified.valid_right);
    cv::Mat disp = result.stereo.disparity.clone();
    disp.setTo(0, result.stereo.valid == 0);
    cv::Mat disp_c = colorize_scalar(disp, result.stereo.valid,
                                     result.stereo.stats.p05_disparity,
                                     result.stereo.stats.p95_disparity);
    cv::imwrite(path("_disparity.png"), disp_c);
    cv::imwrite(path("_depth.png"),
                colorize_scalar(result.depth.z_mm, result.depth.valid,
                                result.depth.stats.depth_p05_mm,
                                result.depth.stats.depth_p95_mm));
    cv::imwrite(path("_confidence.png"), colorize_confidence(result.depth.confidence));
    cv::imwrite(path("_invalid.png"), 255 - result.depth.valid);
    cv::imwrite(path("_depth_mm.tiff"), result.depth.z_mm);
    cv::imwrite(path("_disparity.tiff"), result.stereo.disparity);
    cv::imwrite(path("_confidence.tiff"), result.depth.confidence);
    if (!result.depth.bgr.empty()) cv::imwrite(path("_color.png"), result.depth.bgr);
  }
  if (options.write_preview) {
    cv::imwrite(path("_preview.png"), render_preview(result.cloud, {1000, 760}, 20, -25));
  }
  if (options.write_report) write_report_json(result, path("_report.json"));
  if (options.write_ply && !result.cloud.points.empty()) {
    PlyMetadata meta;
    meta.profile_id = result.profile.id;
    meta.profile_kind = result.profile.kind;
    meta.source = result.stats.source;
    meta.frame = result.stats.frame;
    meta.plate_pitch_mm = result.profile.plate_pitch_mm;
    meta.has_color = !result.cloud.colors.empty();
    meta.extra = std::string("format=") + (options.ascii_ply ? "ascii" : "binary") +
                 " frame_name=ir_left software=einstar_recon";
    ply = path("_cloud.ply");
    write_ply(ply, result.cloud, meta);
  }
  return ply;
}
}  // namespace einstar::recon
