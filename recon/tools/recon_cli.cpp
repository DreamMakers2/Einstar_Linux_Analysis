// Einstar reconstruction CLI: profile validation, frame listing, rectification,
// reconstruction to PLY + diagnostics, benchmarking and a synthetic demo.
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <iostream>
#include <string>
#include <vector>

#include <opencv2/imgcodecs.hpp>

#include "einstar/recon/diagnostics.hpp"
#include "einstar/recon/hash.hpp"
#include "einstar/recon/pipeline.hpp"
#include "einstar/recon/profile.hpp"
#include "einstar/recon/synthetic.hpp"

using namespace einstar::recon;
namespace fs = std::filesystem;

namespace {
void usage() {
  std::cout <<
      "usage: recon_cli <command> [options]\n"
      "  profile   --profile FILE\n"
      "  list      --frames DIR\n"
      "  rectify   --frames DIR --id N --profile FILE --out DIR\n"
      "  reconstruct --frames DIR --id N --profile FILE --out DIR\n"
      "              [--scale 0.5] [--min-depth 150] [--max-depth 800]\n"
      "              [--block 5] [--equalize] [--no-color] [--ascii-ply]\n"
      "              [--min-confidence 0..1] [--maps full|depth|none]\n"
      "              [--min-depth MM] [--max-depth MM]\n"
      "              [--prefix recon]\n"
      "  synthetic --profile FILE --out DIR [--depth 350] [--seed 1]\n"
      "  bench     --frames DIR --ids 0,1,2 --profile FILE [--repeat 3]\n";
}
std::string arg(int argc, char** argv, const char* name, const std::string& def = {}) {
  for (int i = 2; i + 1 < argc; ++i)
    if (std::strcmp(argv[i], name) == 0) return argv[i + 1];
  return def;
}
bool flag(int argc, char** argv, const char* name) {
  for (int i = 2; i < argc; ++i)
    if (std::strcmp(argv[i], name) == 0) return true;
  return false;
}
int fail(const std::string& s) {
  std::cerr << "error: " << s << "\n";
  return 1;
}
void print_stats(const ReconResult& r) {
  std::printf("calibration: %s (%s)  baseline %.3f mm\n", r.profile.id.c_str(),
              r.profile.kind.c_str(), r.rectified.baseline_mm);
  std::printf("valid: stereo %.1f%%  depth %.1f%%  points %lld  color %s\n",
              100 * r.stereo.stats.valid_fraction, 100 * r.depth.stats.valid_fraction,
              (long long)r.cloud.points.size(), r.depth.color_status.c_str());
  std::printf("depth p05/p50/p95: %.1f / %.1f / %.1f mm  sigma p50 %.3f mm\n",
              r.depth.stats.depth_p05_mm, r.depth.stats.depth_p50_mm,
              r.depth.stats.depth_p95_mm, r.depth.stats.sigma_p50_mm);
  std::printf("timing: rectify %.3f  stereo %.3f  depth %.3f  total %.3f s\n",
              r.stats.rectify_seconds, r.stats.stereo_seconds, r.stats.depth_seconds,
              r.stats.total_seconds);
}
}  // namespace

int main(int argc, char** argv) {
  if (argc < 2) { usage(); return 2; }
  const std::string cmd = argv[1];
  try {
    if (cmd == "profile") {
      const std::string path = arg(argc, argv, "--profile");
      if (path.empty()) return fail("--profile required");
      CalibrationProfile p = load_profile(path);
      const auto v = validate_profile(p);
      std::printf("id: %s\nkind: %s\nimage: %dx%d\npitch: %.3f mm\ncolor: %s\n%s\n",
                  p.id.c_str(), p.kind.c_str(), p.image_width, p.image_height,
                  p.plate_pitch_mm, p.color_status.c_str(), v.summary().c_str());
      for (Camera c : kCameras)
        std::printf("%-8s fx %.3f fy %.3f cx %.3f cy %.3f  baseline-to-left %.3f mm\n",
                    camera_name(c), p.camera(c).fx, p.camera(c).fy, p.camera(c).cx,
                    p.camera(c).cy,
                    c == Camera::IrLeft ? 0.0 : baseline_mm(p, Camera::IrLeft, c));
      return v.ok ? 0 : 1;
    }
    if (cmd == "list") {
      const std::string dir = arg(argc, argv, "--frames");
      if (dir.empty()) return fail("--frames required");
      for (auto id : list_frame_groups(dir)) std::printf("%06u\n", id);
      return 0;
    }
    if (cmd == "rectify" || cmd == "reconstruct") {
      const std::string dir = arg(argc, argv, "--frames");
      const std::string out = arg(argc, argv, "--out");
      const std::string prof = arg(argc, argv, "--profile");
      const std::string man = arg(argc, argv, "--manifest");
      if (out.empty() || prof.empty()) return fail("--out and --profile required");
      const auto id = std::stoul(arg(argc, argv, "--id", "0"));
      FrameSet frames = man.empty() ? load_frame_set(dir, id) : load_frame_set_manifest(man);
      CalibrationProfile p = load_profile(prof);
      ReconOptions o;
      o.stereo.scale = std::stod(arg(argc, argv, "--scale", "0.5"));
      o.stereo.min_depth_mm = std::stod(arg(argc, argv, "--min-depth", "150"));
      o.stereo.max_depth_mm = std::stod(arg(argc, argv, "--max-depth", "800"));
      o.stereo.block_size = std::stoi(arg(argc, argv, "--block", "5"));
      o.stereo.equalize = flag(argc, argv, "--equalize");
      {
        // one flag, both consumers: the depth validity mask and the cloud
        // builder keep their own thresholds, so set them together
        const float min_conf = static_cast<float>(
            std::stod(arg(argc, argv, "--min-confidence", "0")));
        o.depth.min_confidence = min_conf;
        o.cloud.min_confidence = min_conf;

      }
      o.depth.color = !flag(argc, argv, "--no-color");
      // the disparity search range is derived from the depth limits, and its
      // width is the dominant cost of matching: a scene that starts at 350 mm
      // does not need the 150 mm lower bound
      o.stereo.min_depth_mm = std::stod(arg(argc, argv, "--min-depth", "150"));
      o.stereo.max_depth_mm = std::stod(arg(argc, argv, "--max-depth", "800"));
      fs::create_directories(out);
      if (cmd == "rectify") {
        auto pair = rectify(frames, p, o.rectify);
        if (!pair.ok) return fail(pair.error);
        cv::imwrite((fs::path(out) / "rectified_left.png").string(), pair.left);
        cv::imwrite((fs::path(out) / "rectified_right.png").string(), pair.right);
        cv::imwrite((fs::path(out) / "valid_left.png").string(), pair.valid_left);
        std::printf("rectified %dx%d f=%.3f baseline=%.3f offset=%.3f\n", pair.size.width,
                    pair.size.height, pair.f, pair.baseline_mm, pair.disp_offset_full);
        return 0;
      }
      auto r = reconstruct(frames, p, o);
      if (!r.ok) return fail(r.error);
      OutputOptions oo;
      oo.prefix = arg(argc, argv, "--prefix", "recon");
      oo.ascii_ply = flag(argc, argv, "--ascii-ply");
      // --maps depth writes only the depth TIFF (what tracking consumes) and the
      // report; --maps none writes neither.  The diagnostics (rectified pair,
      // disparity, confidence, previews) cost real time per frame in a live
      // loop and are not read by the scanner.
      const std::string maps = arg(argc, argv, "--maps", "full");
      if (maps == "none") {
        oo.write_maps = false;
      } else if (maps == "depth") {
        oo.write_maps = true;
        oo.write_extra_maps = false;
      } else if (maps != "full") {
        std::fprintf(stderr, "recon: --maps must be full, depth or none\n");
        return 2;
      }
      const auto ply = write_outputs(r, out, oo);
      print_stats(r);
      if (!ply.empty()) std::printf("ply: %s\n", ply.c_str());
      std::printf("outputs: %s\n", out.c_str());
      return 0;
    }
    if (cmd == "synthetic") {
      const std::string prof = arg(argc, argv, "--profile");
      const std::string out = arg(argc, argv, "--out");
      if (prof.empty() || out.empty()) return fail("--profile and --out required");
      CalibrationProfile p = load_profile(prof);
      SyntheticOptions so;
      so.plane_depth_mm = std::stod(arg(argc, argv, "--depth", "350"));
      so.seed = std::stoull(arg(argc, argv, "--seed", "1"));
      auto frames = render_synthetic_frame_set(p, so);
      ReconOptions o;
      o.stereo.scale = std::stod(arg(argc, argv, "--scale", "0.5"));
      auto r = reconstruct(frames, p, o);
      if (!r.ok) return fail(r.error);
      OutputOptions oo;
      oo.prefix = "synthetic";
      write_outputs(r, out, oo);
      std::printf("truth %.1f mm  measured p50 %.1f mm  error %.3f mm  coverage %.1f%%\n",
                  so.plane_depth_mm, r.depth.stats.depth_p50_mm,
                  r.depth.stats.depth_p50_mm - so.plane_depth_mm,
                  100 * r.depth.stats.valid_fraction);
      return 0;
    }
    if (cmd == "bench") {
      const std::string dir = arg(argc, argv, "--frames");
      const std::string prof = arg(argc, argv, "--profile");
      if (dir.empty() || prof.empty()) return fail("--frames and --profile required");
      CalibrationProfile p = load_profile(prof);
      const int repeat = std::stoi(arg(argc, argv, "--repeat", "3"));
      std::string ids = arg(argc, argv, "--ids", "0");
      std::size_t pos = 0;
      while (pos < ids.size()) {
        const auto comma = ids.find(',', pos);
        const auto id = std::stoul(ids.substr(pos, comma - pos));
        auto frames = load_frame_set(dir, static_cast<std::uint32_t>(id));
        for (int k = 0; k < repeat; ++k) {
          ReconOptions o;
          o.stereo.scale = std::stod(arg(argc, argv, "--scale", "0.5"));
          auto r = reconstruct(frames, p, o);
          if (!r.ok) return fail(r.error);
          std::printf("id=%u run=%d valid=%.3f depth=%.1f/%.1f/%.1f points=%lld "
                      "stereo=%.3f total=%.3f\n",
                      id, k, r.stereo.stats.valid_fraction, r.depth.stats.depth_p05_mm,
                      r.depth.stats.depth_p50_mm, r.depth.stats.depth_p95_mm,
                      (long long)r.cloud.points.size(), r.stereo.stats.seconds,
                      r.stats.total_seconds);
        }
        pos = comma == std::string::npos ? ids.size() : comma + 1;
      }
      return 0;
    }
    usage();
    return 2;
  } catch (const std::exception& e) {
    return fail(e.what());
  }
}
