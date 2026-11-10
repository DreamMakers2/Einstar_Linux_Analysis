// SPDX-License-Identifier: MIT
// Replay a recorded raw USB stream into reconstruction-ready frame triplets.
//
//   einstar-replay-frames --in RECORDING --out DIR [--max-groups N] [--manifest]
//
// The recording is verified first (header CRC, per-record CRC, trailer digest);
// corrupted input is refused before anything is written.  Every transfer is
// then replayed through SimulatedTransport and the acquisition pipeline /
// reassembler exactly like `einstar-record-replay play`, and each complete
// camera frame is written as frame_%06u_cam%u.pgm in USB stream order
// (0 = right IR, 1 = left IR, 2 = colour).  A group is the device's trigger
// group (the u64 header counter), not the per-camera frame id.
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <string_view>

#include "capture_core.hpp"

namespace {

using einstar::capture::CadenceStats;
using einstar::capture::DuplicateFrame;
using einstar::capture::GroupRecord;
using einstar::capture::GroupingStats;
using einstar::capture::IncompleteGroup;
using einstar::capture::ReplayOptions;
using einstar::capture::ReplayReport;

void print_usage() {
  std::printf(
      "usage:\n"
      "  einstar-replay-frames --in RECORDING --out DIR [--max-groups N]\n"
      "                        [--manifest] [--queue-depth N]\n"
      "\n"
      "  --in FILE         recording written by einstar-record-replay or\n"
      "                    einstar-capture-frames --record\n"
      "  --out DIR         output directory for frame_%%06u_cam%%u.pgm\n"
      "  --max-groups N    stop after N complete groups (default: all)\n"
      "  --manifest        also write DIR/frames_manifest.json\n"
      "  --queue-depth N   outstanding bulk reads (default 8)\n");
}

void print_group(const GroupRecord& group) {
  std::printf("  group %06u  device_group %llu  frame_ids %u/%u/%u\n",
              group.group, static_cast<unsigned long long>(group.device_group),
              group.frame_ids[0], group.frame_ids[1], group.frame_ids[2]);
}

void print_incomplete(const IncompleteGroup& group) {
  std::printf("  device_group %llu  present",
              static_cast<unsigned long long>(group.device_group));
  for (std::uint8_t camera = 0; camera < 3; ++camera) {
    if (group.present[camera]) {
      std::printf(" %u", static_cast<unsigned>(camera));
    }
  }
  std::printf("  missing");
  for (std::uint8_t camera = 0; camera < 3; ++camera) {
    if (!group.present[camera]) {
      std::printf(" %u", static_cast<unsigned>(camera));
    }
  }
  std::printf("\n");
}

}  // namespace

int main(int argc, char** argv) {
  ReplayOptions options;
  bool show_help = false;
  for (int i = 1; i < argc; ++i) {
    const std::string_view argument = argv[i];
    if (argument == "--in" && i + 1 < argc) {
      options.input = argv[++i];
    } else if (argument == "--out" && i + 1 < argc) {
      options.output_directory = argv[++i];
    } else if (argument == "--max-groups" && i + 1 < argc) {
      options.max_groups =
          static_cast<std::uint32_t>(std::strtoul(argv[++i], nullptr, 0));
    } else if (argument == "--manifest") {
      options.write_manifest = true;
    } else if (argument == "--queue-depth" && i + 1 < argc) {
      options.queue_depth =
          static_cast<std::uint32_t>(std::strtoul(argv[++i], nullptr, 0));
    } else if (argument == "--help" || argument == "-h") {
      show_help = true;
    } else {
      std::fprintf(stderr, "unknown argument: %s\n", argv[i]);
      print_usage();
      return 2;
    }
  }
  if (show_help) {
    print_usage();
    return 0;
  }
  if (options.input.empty()) {
    std::fprintf(stderr, "--in is required\n");
    print_usage();
    return 2;
  }
  if (options.output_directory.empty()) {
    std::fprintf(stderr, "--out is required\n");
    print_usage();
    return 2;
  }
  if (options.queue_depth == 0) {
    std::fprintf(stderr, "--queue-depth must be at least 1\n");
    return 2;
  }

  auto report = einstar::capture::replay_recording(options);
  if (!report) {
    std::fprintf(stderr, "replay failed: %s\n", report.error().message().c_str());
    return 1;
  }
  const ReplayReport& result = *report;
  const GroupingStats& grouping = result.grouping;
  const CadenceStats& cadence = result.cadence;

  std::printf("replay %s\n", options.input.c_str());
  std::printf("  verified       : header CRC, record CRC, trailer digest\n");
  std::printf("  transfers      : %llu\n",
              static_cast<unsigned long long>(result.manifest.source_transfers));
  std::printf("  recorded bytes : %llu\n",
              static_cast<unsigned long long>(result.manifest.source_bytes));
  std::printf("  digest         : 0x%016llX\n",
              static_cast<unsigned long long>(result.manifest.source_digest));
  std::printf("\n");
  std::printf("wrote %s\n", result.manifest.output_directory.c_str());
  std::printf("  groups written : %llu%s\n",
              static_cast<unsigned long long>(grouping.groups_written),
              result.manifest.stopped_early ? " (stopped at --max-groups)" : "");
  std::printf("  cameras/group  : 3 (0=right IR, 1=left IR, 2=colour)\n");
  std::printf("  incomplete     : %llu group(s), %llu missing camera frame(s)\n",
              static_cast<unsigned long long>(grouping.groups_incomplete),
              static_cast<unsigned long long>(grouping.missing_camera_frames));
  std::printf("  absent groups  : %llu\n",
              static_cast<unsigned long long>(grouping.groups_absent));
  std::printf("  duplicates     : %llu duplicate frame(s), %llu late frame(s)\n",
              static_cast<unsigned long long>(grouping.duplicate_frames),
              static_cast<unsigned long long>(grouping.late_frames));
  std::printf("  frames         : %llu written, %llu completed, %llu invalid\n",
              static_cast<unsigned long long>(grouping.frames_written),
              static_cast<unsigned long long>(result.manifest.metrics.frames_completed),
              static_cast<unsigned long long>(grouping.frames_invalid));
  std::printf("  frames dropped : %llu (reassembler), %llu queue drop(s)\n",
              static_cast<unsigned long long>(result.manifest.metrics.frames_dropped),
              static_cast<unsigned long long>(result.manifest.metrics.queue_drops));
  std::printf("  bytes          : %llu received, %llu written\n",
              static_cast<unsigned long long>(result.manifest.metrics.bytes_received),
              static_cast<unsigned long long>(grouping.bytes_written));
  std::printf("  duration       : %.3f s wall, %.3f s virtual\n",
              result.manifest.metrics.wall_seconds,
              result.manifest.metrics.virtual_seconds);
  std::printf("  throughput     : %.1f MB/s wall, %.1f MB/s virtual\n",
              result.manifest.throughput_mbps,
              result.manifest.virtual_throughput_mbps);
  std::printf("  cadence        : %.2f fps, %.3f ms mean (%.3f..%.3f ms)\n",
              cadence.fps, static_cast<double>(cadence.mean_ns) / 1e6,
              static_cast<double>(cadence.min_ns) / 1e6,
              static_cast<double>(cadence.max_ns) / 1e6);
  std::printf("  manifest       : %s\n",
              result.manifest_path.empty()
                  ? "not written (pass --manifest)"
                  : result.manifest_path.c_str());
  std::printf("  result         : %s\n",
              einstar::capture::grouping_complete(grouping) ? "complete" : "LOSSY");

  constexpr std::size_t kPrinted = 10;
  if (!result.incomplete_groups.empty()) {
    std::printf("\nincomplete groups (showing up to %zu of %zu):\n", kPrinted,
                result.incomplete_groups.size());
    for (std::size_t i = 0;
         i < result.incomplete_groups.size() && i < kPrinted; ++i) {
      print_incomplete(result.incomplete_groups[i]);
    }
  }
  if (!result.duplicates.empty()) {
    std::printf("\nduplicate frames (showing up to %zu of %zu):\n", kPrinted,
                result.duplicates.size());
    for (std::size_t i = 0; i < result.duplicates.size() && i < kPrinted; ++i) {
      const DuplicateFrame& duplicate = result.duplicates[i];
      std::printf("  device_group %llu  camera %u  kept %u  dropped %u%s\n",
                  static_cast<unsigned long long>(duplicate.device_group),
                  static_cast<unsigned>(duplicate.camera),
                  duplicate.kept_frame_id, duplicate.dropped_frame_id,
                  duplicate.group_already_written ? "  (group already written)"
                                                  : "");
    }
  }
  if (!result.groups.empty()) {
    std::printf("\nfirst groups (showing up to %zu of %zu):\n", kPrinted,
                result.groups.size());
    for (std::size_t i = 0; i < result.groups.size() && i < kPrinted; ++i) {
      print_group(result.groups[i]);
    }
  }
  return 0;
}
