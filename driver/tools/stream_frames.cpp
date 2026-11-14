// SPDX-License-Identifier: MIT
// Memory-bounded replay of a raw `.e1r` recording.
//
//   einstar-stream-frames --in RECORDING [--out DIR] [--start-group G]
//                         [--max-groups M] [--groups-file FILE]
//                         [--manifest] [--verify-only] [--single-pass]
//                         [--pipe] [--json REPORT]
//
// The recording is read by `RecordingStreamReader`: one transfer payload at a
// time, O(1) memory instead of O(file).  The replay pass feeds those transfers
// to `SimulatedTransport` on demand, exactly like `einstar-replay-frames`, and
// writes the same `frame_%06d_cam%u.pgm` triplets.  Groups keep their dense
// full-run id, so `--start-group 6000` writes `frame_006000_*` (identical bytes
// to a full replay) and never writes the skipped groups.
//
// `--pipe` writes the decoded frames to stdout as a binary record stream (see
// docs/calibration/streaming_replay.md) and writes no files; it is the path the
// offline feature extractor uses.
//
// Exit codes: 0 success, 1 replay/verification failure, 2 usage error.
#include <algorithm>
#include <array>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <memory>
#include <set>
#include <span>
#include <stdexcept>
#include <string>
#include <string_view>
#include <unordered_set>
#include <vector>

#include "capture_core.hpp"

namespace {

using einstar::capture::CadenceStats;
using einstar::capture::GroupRecord;
using einstar::capture::GroupingStats;
using einstar::capture::IncompleteGroup;
using einstar::capture::StreamReplayOptions;
using einstar::capture::StreamReplayReport;
using einstar::recording::StreamingVerifyStats;

constexpr std::uint32_t kPipeMagicFrame = 0x314D5246U;  // "FRM1"
constexpr std::uint32_t kPipeMagicEnd = 0x31444E45U;    // "END1"
constexpr std::uint16_t kPipeVersion = 1;
constexpr std::size_t kPipeHeaderBytes = 64;

void put_u16(std::span<std::byte> out, std::size_t offset,
             std::uint16_t value) {
  out[offset] = static_cast<std::byte>(value & 0xFFU);
  out[offset + 1] = static_cast<std::byte>((value >> 8U) & 0xFFU);
}

void put_u32(std::span<std::byte> out, std::size_t offset,
             std::uint32_t value) {
  out[offset] = static_cast<std::byte>(value & 0xFFU);
  out[offset + 1] = static_cast<std::byte>((value >> 8U) & 0xFFU);
  out[offset + 2] = static_cast<std::byte>((value >> 16U) & 0xFFU);
  out[offset + 3] = static_cast<std::byte>((value >> 24U) & 0xFFU);
}

void put_u64(std::span<std::byte> out, std::size_t offset,
             std::uint64_t value) {
  put_u32(out, offset, static_cast<std::uint32_t>(value & 0xFFFFFFFFULL));
  put_u32(out, offset + 4, static_cast<std::uint32_t>(value >> 32U));
}

void print_usage() {
  std::fprintf(
      stderr,
      "usage:\n"
      "  einstar-stream-frames --in RECORDING --out DIR [options]\n"
      "  einstar-stream-frames --in RECORDING --pipe [options] > FRAMES.bin\n"
      "  einstar-stream-frames --in RECORDING --verify-only [--json FILE]\n"
      "\n"
      "  --in FILE          recording written by einstar-capture-frames\n"
      "  --out DIR          PGM output directory (frame_%%06d_cam%%u.pgm)\n"
      "  --start-group G    first absolute group id to write (0 = from start);\n"
      "                     earlier groups are decoded, validated and counted\n"
      "  --max-groups M     stop after M written groups (0 = all)\n"
      "  --groups-file F    sparse selection: one absolute group id per line\n"
      "  --manifest         write DIR/frames_manifest.json\n"
      "  --verify-only      stream-verify the whole recording, write nothing\n"
      "  --single-pass      skip the separate full verification pass; the replay\n"
      "                     pass still validates every byte it reads and the\n"
      "                     trailer at EOF (late corruption fails the run)\n"
      "  --pipe             write decoded frames to stdout as binary records\n"
      "                     (no PGM files; 64-byte header + w*h payload/frame)\n"
      "  --json FILE        write a machine-readable JSON report\n"
      "  --queue-depth N    outstanding bulk reads (default 8, 32 for --pipe)\n"
      "  --fast-pacing      offline fast replay: zero virtual USB latency and\n"
      "                     effectively unbounded simulated bandwidth; recorded\n"
      "                     capture timestamps, ids and bytes are unchanged\n"
      "                     (default for --pipe)\n"
      "  --paced            keep the virtual USB pacing model even for --pipe\n"
      "  --digests          compute per-frame FNV digests for PGM/pipe records\n"
      "                     (default: on for PGM output, off for --pipe)\n"
      "  --help             show this help\n");
}

std::string json_quote(std::string_view text) {
  return einstar::capture::json_quote(text);
}

/// Binary frame stream written to stdout by `--pipe`.
class PipeFrameSink {
 public:
  PipeFrameSink() = default;

  einstar::Status write_group(
      const GroupRecord& group,
      std::array<std::vector<std::uint8_t>, einstar::capture::kStreamCount>&
          pixels) {
    for (std::uint8_t camera = 0; camera < einstar::capture::kStreamCount;
         ++camera) {
      std::array<std::byte, kPipeHeaderBytes> header{};
      put_u32(header, 0, kPipeMagicFrame);
      put_u16(header, 4, kPipeVersion);
      put_u16(header, 6, 0);
      put_u32(header, 8, group.group);
      put_u32(header, 12, camera);
      put_u32(header, 16, group.frame_ids[camera]);
      put_u32(header, 20, einstar::capture::kFrameWidth);
      put_u32(header, 24, einstar::capture::kFrameHeight);
      put_u64(header, 32, group.device_group);
      put_u64(header, 40, static_cast<std::uint64_t>(group.host_time_ns));
      put_u32(header, 48, static_cast<std::uint32_t>(pixels[camera].size()));
      put_u32(header, 52, 0);
      put_u64(header, 56, group.digests[camera]);
      if (std::fwrite(header.data(), 1, header.size(), stdout) !=
          header.size()) {
        return pipe_error();
      }
      if (!pixels[camera].empty() &&
          std::fwrite(pixels[camera].data(), 1, pixels[camera].size(),
                      stdout) != pixels[camera].size()) {
        return pipe_error();
      }
      ++frames_written_;
      bytes_written_ += pixels[camera].size();
    }
    if (std::fflush(stdout) != 0) {
      return pipe_error();
    }
    return einstar::Status::success();
  }

  /// Terminator record: magic END1, group 0xFFFFFFFF, no payload.
  einstar::Status finish(std::uint64_t groups, std::uint64_t frames) {
    std::array<std::byte, kPipeHeaderBytes> header{};
    put_u32(header, 0, kPipeMagicEnd);
    put_u16(header, 4, kPipeVersion);
    put_u32(header, 8, 0xFFFFFFFFU);
    put_u64(header, 32, groups);
    put_u64(header, 40, frames);
    if (std::fwrite(header.data(), 1, header.size(), stdout) !=
            header.size() ||
        std::fflush(stdout) != 0) {
      return pipe_error();
    }
    return einstar::Status::success();
  }

  [[nodiscard]] std::uint64_t frames_written() const noexcept {
    return frames_written_;
  }
  [[nodiscard]] std::uint64_t bytes_written() const noexcept {
    return bytes_written_;
  }

 private:
  static einstar::Status pipe_error() {
    return einstar::Status::error(
        einstar::ErrorCode::IoError,
        "cannot write frame stream to stdout (is the consumer gone?): " +
            std::string(std::strerror(errno)));
  }

  std::uint64_t frames_written_ = 0;
  std::uint64_t bytes_written_ = 0;
};

const char* mode_name(bool verify_only, bool pipe) {
  if (verify_only) {
    return "verify-only";
  }
  return pipe ? "pipe" : "frames";
}

void print_verification(std::FILE* stream,
                        const StreamingVerifyStats& stats,
                        bool verification_complete) {
  std::fprintf(stream, "verification %s\n",
               verification_complete
                   ? (stats.trailer_verified ? "VERIFIED" : "NOT VERIFIED")
                   : "INCOMPLETE (stopped before end of stream)");
  std::fprintf(stream, "  records       : %llu (metadata %llu, transfers %llu)\n",
               static_cast<unsigned long long>(stats.record_count),
               static_cast<unsigned long long>(stats.metadata_records),
               static_cast<unsigned long long>(stats.transfer_records));
  std::fprintf(stream, "  payload bytes : %llu\n",
               static_cast<unsigned long long>(stats.payload_bytes));
  std::fprintf(stream, "  digest        : 0x%016llX\n",
               static_cast<unsigned long long>(stats.digest));
  std::fprintf(stream, "  non-Ok/empty  : %llu / %llu transfer(s)\n",
               static_cast<unsigned long long>(stats.non_ok_transfers),
               static_cast<unsigned long long>(stats.zero_length_transfers));
  if (stats.has_trailer) {
    std::fprintf(stream,
                 "  trailer       : digest %s (%s), counts %s "
                 "(%llu transfers/%llu bytes recorded)\n",
                 stats.digest_matches ? "MATCH" : "MISMATCH",
                 stats.trailer_verified ? "verified" : "NOT verified",
                 stats.counts_match ? "MATCH" : "MISMATCH",
                 static_cast<unsigned long long>(stats.trailer_transfers),
                 static_cast<unsigned long long>(stats.trailer_bytes));
  } else {
    std::fprintf(stream, "  trailer       : MISSING (hard failure)\n");
  }
}

void print_cadence(std::FILE* stream, const CadenceStats& cadence) {
  std::fprintf(
      stream, "  cadence       : %.2f fps, %.3f ms mean (%.3f..%.3f ms)\n",
      cadence.fps, static_cast<double>(cadence.mean_ns) / 1e6,
      static_cast<double>(cadence.min_ns) / 1e6,
      static_cast<double>(cadence.max_ns) / 1e6);
}

void print_replay(std::FILE* stream, const StreamReplayReport& report,
                  bool pipe) {
  const GroupingStats& grouping = report.grouping;
  std::fprintf(stream, "replay %s\n", report.stopped_early ? "(stopped early)" : "");
  std::fprintf(stream, "  transfers     : %llu read, %llu payload bytes\n",
               static_cast<unsigned long long>(report.transfers_replayed),
               static_cast<unsigned long long>(report.bytes_replayed));
  std::fprintf(stream, "  groups written: %llu%s\n",
               static_cast<unsigned long long>(grouping.groups_written),
               report.stopped_early ? " (stopped at --max-groups)" : "");
  std::fprintf(stream, "  groups skipped: %llu\n",
               static_cast<unsigned long long>(grouping.groups_skipped));
  std::fprintf(stream, "  incomplete    : %llu group(s), %llu missing frame(s)\n",
               static_cast<unsigned long long>(grouping.groups_incomplete),
               static_cast<unsigned long long>(grouping.missing_camera_frames));
  std::fprintf(stream, "  absent groups : %llu\n",
               static_cast<unsigned long long>(grouping.groups_absent));
  std::fprintf(stream, "  duplicates    : %llu duplicate, %llu late frame(s)\n",
               static_cast<unsigned long long>(grouping.duplicate_frames),
               static_cast<unsigned long long>(grouping.late_frames));
  std::fprintf(stream, "  frames        : %llu %s, %llu seen, %llu invalid\n",
               static_cast<unsigned long long>(grouping.frames_written),
               pipe ? "emitted" : "written",
               static_cast<unsigned long long>(grouping.frames_seen),
               static_cast<unsigned long long>(grouping.frames_invalid));
  if (grouping.slot_id_mismatches != 0) {
    std::fprintf(stream, "  slot mismatch : %llu\n",
                 static_cast<unsigned long long>(grouping.slot_id_mismatches));
  }
  std::fprintf(stream, "  bytes         : %llu replayed, %llu %s\n",
               static_cast<unsigned long long>(report.bytes_replayed),
               static_cast<unsigned long long>(grouping.bytes_written),
               pipe ? "emitted" : "written");
  print_cadence(stream, report.cadence);
  std::fprintf(stream, "  wall          : %.3f s total, %.3f s replay\n",
               report.wall_seconds, report.replay_seconds);
  std::fprintf(stream, "  throughput    : %.1f MB/s replay\n",
               report.throughput_mbps);
  std::fprintf(stream, "  peak RSS      : %llu KiB\n",
               static_cast<unsigned long long>(report.peak_rss_kb));
}

void print_incomplete(std::FILE* stream, const IncompleteGroup& group) {
  std::fprintf(stream, "  device_group %llu  present",
               static_cast<unsigned long long>(group.device_group));
  for (std::uint8_t camera = 0; camera < einstar::capture::kStreamCount;
       ++camera) {
    if (group.present[camera]) {
      std::fprintf(stream, " %u", static_cast<unsigned>(camera));
    }
  }
  std::fprintf(stream, "  missing");
  for (std::uint8_t camera = 0; camera < einstar::capture::kStreamCount;
       ++camera) {
    if (!group.present[camera]) {
      std::fprintf(stream, " %u", static_cast<unsigned>(camera));
    }
  }
  std::fprintf(stream, "\n");
}

einstar::Result<std::shared_ptr<const std::unordered_set<std::uint32_t>>>
load_groups_file(const std::string& path) {
  std::ifstream file(path);
  if (!file) {
    return einstar::fail(einstar::ErrorCode::IoError,
                         "cannot open groups file: " + path);
  }
  auto groups = std::make_shared<std::unordered_set<std::uint32_t>>();
  std::string token;
  while (file >> token) {
    if (token.empty() || token[0] == '#') {
      continue;
    }
    try {
      const unsigned long value = std::stoul(token, nullptr, 0);
      if (value > einstar::capture::kMaxOutputGroups) {
        return einstar::fail(
            einstar::ErrorCode::InvalidArgument,
            "groups file " + path + ": group id " + token + " is out of range");
      }
      groups->insert(static_cast<std::uint32_t>(value));
    } catch (const std::exception&) {
      return einstar::fail(einstar::ErrorCode::InvalidArgument,
                           "groups file " + path + ": invalid group id " +
                               token);
    }
  }
  return groups;
}

bool write_json_report(const std::string& path, const StreamReplayOptions& options,
                       const StreamReplayReport& report, bool verify_only,
                       bool pipe) {
  const StreamingVerifyStats& v = report.verification;
  const GroupingStats& g = report.grouping;
  const std::string temporary = path + ".tmp";
  std::ofstream out(temporary, std::ios::binary | std::ios::trunc);
  if (!out) {
    return false;
  }
  char digest_hex[32];
  std::snprintf(digest_hex, sizeof(digest_hex), "0x%016llX",
                static_cast<unsigned long long>(v.digest));
  out << "{\n";
  out << "  \"schema\": \"einstar-stream-replay-report/1\",\n";
  out << "  \"tool\": " << json_quote(options.tool_name) << ",\n";
  out << "  \"mode\": " << json_quote(mode_name(verify_only, pipe)) << ",\n";
  out << "  \"input\": " << json_quote(options.input) << ",\n";
  out << "  \"output\": " << json_quote(report.manifest.output_directory)
      << ",\n";
  out << "  \"start_group\": " << options.start_group << ",\n";
  out << "  \"max_groups\": " << options.max_groups << ",\n";
  out << "  \"single_pass\": " << (report.single_pass ? "true" : "false")
      << ",\n";
  out << "  \"verification\": {\n";
  out << "    \"complete\": " << (report.verification_complete ? "true" : "false")
      << ",\n";
  out << "    \"record_count\": " << v.record_count << ",\n";
  out << "    \"metadata_records\": " << v.metadata_records << ",\n";
  out << "    \"transfer_records\": " << v.transfer_records << ",\n";
  out << "    \"non_ok_transfers\": " << v.non_ok_transfers << ",\n";
  out << "    \"zero_length_transfers\": " << v.zero_length_transfers << ",\n";
  out << "    \"payload_bytes\": " << v.payload_bytes << ",\n";
  out << "    \"digest\": " << v.digest << ",\n";
  out << "    \"digest_hex\": " << json_quote(digest_hex) << ",\n";
  out << "    \"file_size\": " << v.file_size << ",\n";
  out << "    \"has_trailer\": " << (v.has_trailer ? "true" : "false") << ",\n";
  out << "    \"trailer_digest\": " << v.trailer_digest << ",\n";
  out << "    \"trailer_transfers\": " << v.trailer_transfers << ",\n";
  out << "    \"trailer_bytes\": " << v.trailer_bytes << ",\n";
  out << "    \"digest_matches\": " << (v.digest_matches ? "true" : "false")
      << ",\n";
  out << "    \"counts_match\": " << (v.counts_match ? "true" : "false")
      << ",\n";
  out << "    \"trailer_verified\": "
      << (v.trailer_verified ? "true" : "false") << "\n";
  out << "  },\n";
  out << "  \"replay\": {\n";
  out << "    \"transfers_replayed\": " << report.transfers_replayed << ",\n";
  out << "    \"bytes_replayed\": " << report.bytes_replayed << ",\n";
  out << "    \"groups_written\": " << g.groups_written << ",\n";
  out << "    \"groups_skipped\": " << g.groups_skipped << ",\n";
  out << "    \"groups_incomplete\": " << g.groups_incomplete << ",\n";
  out << "    \"missing_camera_frames\": " << g.missing_camera_frames << ",\n";
  out << "    \"groups_absent\": " << g.groups_absent << ",\n";
  out << "    \"duplicate_frames\": " << g.duplicate_frames << ",\n";
  out << "    \"late_frames\": " << g.late_frames << ",\n";
  out << "    \"frames_seen\": " << g.frames_seen << ",\n";
  out << "    \"frames_written\": " << g.frames_written << ",\n";
  out << "    \"frames_invalid\": " << g.frames_invalid << ",\n";
  out << "    \"bytes_written\": " << g.bytes_written << ",\n";
  out << "    \"slot_id_mismatches\": " << g.slot_id_mismatches << ",\n";
  out << "    \"stopped_early\": " << (report.stopped_early ? "true" : "false")
      << "\n";
  out << "  },\n";
  out << "  \"cadence\": {\n";
  out << "    \"intervals\": " << report.cadence.intervals << ",\n";
  out << "    \"fps\": " << report.cadence.fps << ",\n";
  out << "    \"mean_ns\": " << report.cadence.mean_ns << ",\n";
  out << "    \"min_ns\": " << report.cadence.min_ns << ",\n";
  out << "    \"max_ns\": " << report.cadence.max_ns << "\n";
  out << "  },\n";
  out << "  \"timing\": {\n";
  out << "    \"wall_seconds\": " << report.wall_seconds << ",\n";
  out << "    \"replay_seconds\": " << report.replay_seconds << ",\n";
  out << "    \"throughput_mbps\": " << report.throughput_mbps << "\n";
  out << "  },\n";
  out << "  \"memory\": {\"peak_rss_kb\": " << report.peak_rss_kb << "},\n";
  out << "  \"manifest_path\": " << json_quote(report.manifest_path) << "\n";
  out << "}\n";
  out.flush();
  if (!out) {
    return false;
  }
  // Atomic publish so a reader never sees a half-written report.
  out.close();
  std::error_code error;
  std::filesystem::rename(temporary, path, error);
  if (error) {
    return false;
  }
  return true;
}

}  // namespace

int main(int argc, char** argv) {
  StreamReplayOptions options;
  options.tool_name = "einstar-stream-frames";
  bool pipe = false;
  bool verify_only = false;
  bool digests_set = false;
  bool pacing_set = false;
  bool fast_pacing = false;
  bool queue_depth_set = false;
  bool show_help = false;
  std::string groups_file;
  std::string json_path;

  for (int i = 1; i < argc; ++i) {
    const std::string_view argument = argv[i];
    const auto next = [&](const char* name) -> const char* {
      if (i + 1 >= argc) {
        std::fprintf(stderr, "%s requires a value\n", name);
        std::exit(2);
      }
      return argv[++i];
    };
    if (argument == "--in") {
      options.input = next("--in");
    } else if (argument == "--out") {
      options.output_directory = next("--out");
    } else if (argument == "--start-group") {
      options.start_group =
          static_cast<std::uint32_t>(std::strtoul(next("--start-group"),
                                                 nullptr, 0));
    } else if (argument == "--max-groups") {
      options.max_groups = static_cast<std::uint32_t>(
          std::strtoul(next("--max-groups"), nullptr, 0));
    } else if (argument == "--groups-file") {
      groups_file = next("--groups-file");
    } else if (argument == "--queue-depth") {
      options.queue_depth = static_cast<std::uint32_t>(
          std::strtoul(next("--queue-depth"), nullptr, 0));
      queue_depth_set = true;
    } else if (argument == "--fast-pacing") {
      fast_pacing = true;
      pacing_set = true;
    } else if (argument == "--paced") {
      fast_pacing = false;
      pacing_set = true;
    } else if (argument == "--manifest") {
      options.write_manifest = true;
    } else if (argument == "--verify-only") {
      verify_only = true;
    } else if (argument == "--single-pass") {
      options.single_pass = true;
    } else if (argument == "--pipe") {
      pipe = true;
    } else if (argument == "--json") {
      json_path = next("--json");
    } else if (argument == "--digests") {
      digests_set = true;
      options.compute_digests = true;
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
  if (options.queue_depth == 0) {
    std::fprintf(stderr, "--queue-depth must be at least 1\n");
    return 2;
  }
  if (verify_only) {
    if (!options.output_directory.empty() || pipe ||
        options.start_group != 0 || options.max_groups != 0 ||
        !groups_file.empty() || options.write_manifest) {
      std::fprintf(stderr,
                   "--verify-only cannot be combined with --out/--pipe/"
                   "--start-group/--max-groups/--groups-file/--manifest\n");
      return 2;
    }
  } else if (pipe) {
    if (!options.output_directory.empty()) {
      std::fprintf(stderr, "--pipe and --out are mutually exclusive\n");
      return 2;
    }
    if (options.write_manifest) {
      std::fprintf(stderr, "--manifest requires --out (not --pipe)\n");
      return 2;
    }
  } else if (options.output_directory.empty()) {
    std::fprintf(stderr, "--out or --pipe (or --verify-only) is required\n");
    print_usage();
    return 2;
  }
  if (!groups_file.empty()) {
    auto groups = load_groups_file(groups_file);
    if (!groups) {
      std::fprintf(stderr, "groups file: %s\n",
                   groups.error().message().c_str());
      return 2;
    }
    options.selection = *groups;
  }
  if (!pacing_set) {
    fast_pacing = pipe;  // offline pipe extraction defaults to the fast path
  }
  options.fast_pacing = fast_pacing;
  if (pipe && !queue_depth_set) {
    options.queue_depth = 32;
  }
  if (!digests_set) {
    // Digest computation is only useful for manifest/PGM provenance; the pipe
    // path skips it to keep the full-corpus pass CPU-bound on validation.
    options.compute_digests = !pipe;
  }

  std::FILE* human = pipe ? stderr : stdout;
  if (verify_only) {
    options.verify_only = true;
    auto result = einstar::capture::stream_replay(options);
    if (!result) {
      std::fprintf(stderr, "verification failed: %s\n",
                   result.error().message().c_str());
      return 1;
    }
    const StreamReplayReport& report = *result;
    std::fprintf(human, "stream-verify %s\n", options.input.c_str());
    print_verification(human, report.verification, true);
    std::fprintf(human, "  wall          : %.3f s (%.1f MB/s)\n",
                 report.wall_seconds, report.throughput_mbps);
    std::fprintf(human, "  peak RSS      : %llu KiB\n",
                 static_cast<unsigned long long>(report.peak_rss_kb));
    if (!json_path.empty() &&
        !write_json_report(json_path, options, report, true, false)) {
      std::fprintf(stderr, "cannot write JSON report: %s\n",
                   json_path.c_str());
      return 1;
    }
    return report.verification.trailer_verified ? 0 : 1;
  }

  PipeFrameSink sink;
  if (pipe) {
    options.group_sink = [&sink](const GroupRecord& group,
                                 std::array<std::vector<std::uint8_t>,
                                            einstar::capture::kStreamCount>&
                                     pixels) {
      return sink.write_group(group, pixels);
    };
  }

  auto result = einstar::capture::stream_replay(options);
  if (!result) {
    std::fprintf(stderr, "replay failed: %s\n",
                 result.error().message().c_str());
    return 1;
  }
  const StreamReplayReport& report = *result;
  if (pipe) {
    const einstar::Status ended =
        sink.finish(report.grouping.groups_written,
                    report.grouping.frames_written);
    if (!ended.ok()) {
      std::fprintf(stderr, "pipe terminator failed: %s\n",
                   ended.message().c_str());
      return 1;
    }
  }

  std::fprintf(human, "stream-replay %s\n", options.input.c_str());
  print_verification(human, report.verification, report.verification_complete);
  print_replay(human, report, pipe);
  if (!report.incomplete_groups.empty()) {
    constexpr std::size_t kPrinted = 10;
    std::fprintf(human, "\nincomplete groups (showing up to %zu of %zu):\n",
                 kPrinted, report.incomplete_groups.size());
    for (std::size_t i = 0;
         i < report.incomplete_groups.size() && i < kPrinted; ++i) {
      print_incomplete(human, report.incomplete_groups[i]);
    }
  }
  std::fprintf(human, "  result        : %s\n",
               report.verification_complete &&
                       report.verification.trailer_verified
                   ? "VERIFIED"
                   : (report.stopped_early ? "STOPPED EARLY (tail unverified)"
                                           : "UNVERIFIED"));
  if (!json_path.empty() &&
      !write_json_report(json_path, options, report, false, pipe)) {
    std::fprintf(stderr, "cannot write JSON report: %s\n", json_path.c_str());
    return 1;
  }
  if (report.verification_complete && !report.verification.trailer_verified) {
    return 1;
  }
  return 0;
}
