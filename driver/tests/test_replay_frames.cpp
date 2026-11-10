// SPDX-License-Identifier: MIT
// Offline tests for the replay/capture tooling (test_replay_frames).
//
// These tests exercise the same functions the tools run:
//   * a deterministic simulation is recorded to a temp file and replayed into
//     frame_%06u_cam%u.pgm triplets, and the bytes are compared across the
//     capture and two replays (deterministic output);
//   * a one-byte corruption is refused before anything is written;
//   * a lossy simulation yields explicit incomplete/absent groups rather than
//     a silent success, and replaying the lossy recording reproduces the same
//     grouping;
//   * no device group is written twice and no stream is mixed across groups.
#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <string>
#include <string_view>
#include <system_error>
#include <vector>

#include "capture_core.hpp"
#include "einstar/packet.hpp"
#include "test_util.hpp"

using namespace einstar;
using namespace einstar::capture;

namespace {

const std::string kRoot = "/tmp/einstar_replay_frames_test";
constexpr std::uint64_t kSimTriggerNs = 40'000'000ULL;  // 40 ms trigger period

void reset_root() {
  std::error_code ignored;
  std::filesystem::remove_all(kRoot, ignored);
  std::filesystem::create_directories(kRoot, ignored);
}

std::string path_in(const std::string& name) {
  return kRoot + "/" + name;
}

std::vector<std::uint8_t> read_file(const std::string& path) {
  std::ifstream file(path, std::ios::binary);
  return std::vector<std::uint8_t>((std::istreambuf_iterator<char>(file)),
                                   std::istreambuf_iterator<char>());
}

void write_file(const std::string& path,
                const std::vector<std::uint8_t>& bytes) {
  std::ofstream file(path, std::ios::binary | std::ios::trunc);
  file.write(reinterpret_cast<const char*>(bytes.data()),
             static_cast<std::streamsize>(bytes.size()));
}

std::string read_text(const std::string& path) {
  const std::vector<std::uint8_t> bytes = read_file(path);
  return std::string(bytes.begin(), bytes.end());
}

bool contains(const std::string& text, std::string_view needle) {
  return text.find(needle) != std::string::npos;
}

std::size_t count_pgm(const std::string& directory) {
  std::size_t count = 0;
  std::error_code error;
  for (const auto& entry :
       std::filesystem::directory_iterator(directory, error)) {
    if (entry.is_regular_file() &&
        entry.path().extension() == ".pgm") {
      ++count;
    }
  }
  return count;
}

std::uint64_t digest_of(const std::vector<std::uint8_t>& bytes) {
  return fnv1a64(bytes);
}

std::string pgm_path(const std::string& directory, std::uint32_t group,
                     std::uint8_t camera) {
  char name[64];
  std::snprintf(name, sizeof(name), "frame_%06u_cam%u.pgm", group,
                static_cast<unsigned>(camera));
  return directory + "/" + name;
}

/// Compares every stream file of two output directories byte for byte.
bool streams_identical(const std::string& left, const std::string& right,
                       std::uint32_t groups) {
  for (std::uint32_t group = 0; group < groups; ++group) {
    for (std::uint8_t camera = 0; camera < kStreamCount; ++camera) {
      if (read_file(pgm_path(left, group, camera)) !=
          read_file(pgm_path(right, group, camera))) {
        return false;
      }
    }
  }
  return true;
}

/// Verifies one group's files against the simulator's deterministic payload.
bool group_matches_simulation(const GroupRecord& group,
                              const std::string& directory) {
  for (std::uint8_t camera = 0; camera < kStreamCount; ++camera) {
    std::uint32_t width = 0;
    std::uint32_t height = 0;
    auto pixels = read_pgm(pgm_path(directory, group.group, camera), width,
                           height);
    if (!pixels) {
      return false;
    }
    if (width != kFrameWidth || height != kFrameHeight ||
        pixels->size() != kFrameBytes) {
      return false;
    }
    const std::uint64_t expected =
        simulated_frame_digest(group.frame_ids[camera], kFrameBytes, 0xA000);
    if (digest_of(*pixels) != expected) {
      return false;
    }
  }
  return true;
}

/// Rebuilds a single-transfer recording with one camera's frame removed.
/// Used to prove the incomplete-group path deterministically: the simulator's
/// own fault knobs are keyed on the per-group frame index, so they drop all
/// three cameras of a group together (whole-group loss), never one camera.
std::vector<std::byte> remove_one_camera_frame(
    const recording::Recording& source, std::uint64_t device_group,
    std::uint8_t camera) {
  PacketFramer framer;
  FramerStats framer_stats;
  std::vector<std::byte> out;
  for (const recording::RecordedTransfer& transfer : source.transfers) {
    if (transfer.status != TransferStatus::Ok || transfer.bytes.empty()) {
      continue;
    }
    framer.consume(
        std::span<const std::byte>(transfer.bytes.data(), transfer.bytes.size()),
        [&](const StreamPacket& packet) {
          if (packet.camera_index == camera &&
              packet.device_timestamp == device_group) {
            return;
          }
          const std::size_t start = out.size();
          const std::size_t payload = packet.payload_size;
          out.resize(start + 4U + kStreamHeaderSize + payload);
          store_be32(out.data() + start,
                     static_cast<std::uint32_t>(kStreamHeaderSize + payload));
          std::memcpy(out.data() + start + 4U, packet.header.data(),
                      kStreamHeaderSize);
          if (payload > 0) {
            std::memcpy(out.data() + start + 4U + kStreamHeaderSize,
                        packet.payload, payload);
          }
        },
        framer_stats);
  }
  return out;
}

}  // namespace

EINSTAR_TEST(simulated_capture_records_three_cameras_per_group) {
  reset_root();
  SimCaptureOptions options;
  options.output_directory = path_in("capture");
  options.groups = 4;
  options.seed = 5;
  options.recording_path = path_in("clean.e1r");
  auto report = capture_simulated(options);
  REQUIRE(report.has_value());
  const SimCaptureReport& capture = *report;

  CHECK_EQ(capture.grouping.groups_written, 4U);
  CHECK_EQ(capture.grouping.groups_incomplete, 0U);
  CHECK_EQ(capture.grouping.groups_absent, 0U);
  CHECK_EQ(capture.grouping.duplicate_frames, 0U);
  CHECK_EQ(capture.grouping.frames_written, 12U);
  CHECK_EQ(capture.grouping.bytes_written,
           static_cast<std::uint64_t>(kFrameBytes) * 12U);
  CHECK(grouping_complete(capture.grouping));
  CHECK(std::filesystem::exists(capture.manifest_path));
  CHECK_EQ(count_pgm(capture.manifest.output_directory), 12U);
  CHECK(std::filesystem::exists(options.recording_path));
  CHECK(capture.manifest.recorded_bytes > 0U);

  REQUIRE(capture.groups.size() == 4U);
  for (std::uint32_t group = 0; group < 4; ++group) {
    const GroupRecord& record = capture.groups[group];
    CHECK_EQ(record.group, group);
    CHECK_EQ(record.device_group, static_cast<std::uint64_t>(group) * kSimTriggerNs);
    for (std::uint8_t camera = 0; camera < kStreamCount; ++camera) {
      CHECK_EQ(record.frame_ids[camera], group);
    }
    CHECK(group_matches_simulation(record, capture.manifest.output_directory));
  }

  const std::string manifest = read_text(capture.manifest_path);
  CHECK(contains(manifest, "\"groups_written\": 4"));
  CHECK(contains(manifest, "\"complete\": true"));
  CHECK(contains(manifest, "\"cameras\": 3"));
  CHECK(contains(manifest, "\"device_group\": 0"));
  CHECK(contains(manifest, "frame_000000_cam0.pgm"));
  CHECK(contains(manifest, "\"source\": {"));
  CHECK(contains(manifest, "\"persistent_writes\": false"));

  // A second simulation with the same seed is byte-identical.
  SimCaptureOptions repeat = options;
  repeat.output_directory = path_in("capture_repeat");
  repeat.recording_path = path_in("clean_repeat.e1r");
  auto repeat_report = capture_simulated(repeat);
  REQUIRE(repeat_report.has_value());
  CHECK(streams_identical(capture.manifest.output_directory,
                          repeat_report->manifest.output_directory, 4));

  reset_root();
}

EINSTAR_TEST(replay_produces_the_reconstruction_layout_deterministically) {
  reset_root();
  SimCaptureOptions options;
  options.output_directory = path_in("capture");
  options.groups = 4;
  options.seed = 11;
  options.recording_path = path_in("replay_source.e1r");
  auto capture = capture_simulated(options);
  REQUIRE(capture.has_value());

  ReplayOptions replay;
  replay.input = options.recording_path;
  replay.output_directory = path_in("replay_a");
  replay.write_manifest = true;
  auto first = replay_recording(replay);
  REQUIRE(first.has_value());
  CHECK_EQ(first->grouping.groups_written, 4U);
  CHECK_EQ(first->grouping.groups_incomplete, 0U);
  CHECK_EQ(first->grouping.frames_written, 12U);
  CHECK(grouping_complete(first->grouping));
  CHECK_EQ(first->manifest.source_bytes, capture->manifest.recorded_bytes);
  CHECK(first->manifest.source_verified);
  CHECK(std::filesystem::exists(first->manifest_path));
  CHECK_EQ(count_pgm(first->manifest.output_directory), 12U);

  REQUIRE(first->groups.size() == 4U);
  for (std::uint32_t group = 0; group < 4; ++group) {
    CHECK_EQ(first->groups[group].group, group);
    CHECK_EQ(first->groups[group].device_group,
             static_cast<std::uint64_t>(group) * kSimTriggerNs);
    CHECK(group_matches_simulation(first->groups[group],
                                   first->manifest.output_directory));
  }
  CHECK(contains(read_text(first->manifest_path), "\"complete\": true"));
  CHECK(contains(read_text(first->manifest_path), "\"verified\": true"));

  // Replay again: identical bytes, identical group/device mapping.
  ReplayOptions again = replay;
  again.output_directory = path_in("replay_b");
  auto second = replay_recording(again);
  REQUIRE(second.has_value());
  CHECK(streams_identical(first->manifest.output_directory,
                          second->manifest.output_directory, 4));
  REQUIRE(second->groups.size() == first->groups.size());
  for (std::size_t i = 0; i < first->groups.size(); ++i) {
    CHECK_EQ(second->groups[i].device_group, first->groups[i].device_group);
    for (std::uint8_t camera = 0; camera < kStreamCount; ++camera) {
      CHECK_EQ(second->groups[i].frame_ids[camera],
               first->groups[i].frame_ids[camera]);
      CHECK_EQ(second->groups[i].digests[camera],
               first->groups[i].digests[camera]);
    }
  }

  // The replayed files are byte-identical to the capture that produced the
  // recording: replay is not a different data path.
  CHECK(streams_identical(capture->manifest.output_directory,
                          first->manifest.output_directory, 4));

  reset_root();
}

EINSTAR_TEST(max_groups_stops_replay_without_reporting_false_loss) {
  reset_root();
  SimCaptureOptions options;
  options.output_directory = path_in("capture");
  options.groups = 4;
  options.seed = 3;
  options.recording_path = path_in("source.e1r");
  auto capture = capture_simulated(options);
  REQUIRE(capture.has_value());

  ReplayOptions replay;
  replay.input = options.recording_path;
  replay.output_directory = path_in("partial");
  replay.max_groups = 2;
  replay.write_manifest = true;
  auto partial = replay_recording(replay);
  REQUIRE(partial.has_value());
  CHECK_EQ(partial->grouping.groups_written, 2U);
  CHECK_EQ(partial->grouping.groups_incomplete, 0U);
  CHECK(partial->manifest.stopped_early);
  CHECK_EQ(count_pgm(partial->manifest.output_directory), 6U);
  CHECK(contains(read_text(partial->manifest_path),
                 "\"stopped_early\": true"));

  reset_root();
}

EINSTAR_TEST(corrupt_recording_is_refused_before_writing) {
  reset_root();
  SimCaptureOptions options;
  options.output_directory = path_in("capture");
  options.groups = 2;
  options.seed = 2;
  options.recording_path = path_in("clean.e1r");
  auto capture = capture_simulated(options);
  REQUIRE(capture.has_value());

  const std::vector<std::uint8_t> original = read_file(options.recording_path);
  REQUIRE(original.size() > 256U);

  // One flipped payload byte is caught by the per-record CRC.
  std::vector<std::uint8_t> corrupt = original;
  corrupt[200] ^= 0x01U;
  write_file(path_in("corrupt_payload.e1r"), corrupt);

  // One flipped byte in the file magic is caught by the header validation.
  std::vector<std::uint8_t> bad_magic = original;
  bad_magic[3] ^= 0x01U;
  write_file(path_in("corrupt_header.e1r"), bad_magic);

  // A truncated trailer cannot be verified against the stream digest.
  std::vector<std::uint8_t> truncated = original;
  truncated.resize(truncated.size() - 12U);
  write_file(path_in("truncated.e1r"), truncated);

  const std::string cases[] = {"corrupt_payload.e1r", "corrupt_header.e1r",
                               "truncated.e1r"};
  for (const std::string& name : cases) {
    ReplayOptions replay;
    replay.input = path_in(name);
    replay.output_directory = path_in("refused_" + name);
    replay.write_manifest = true;
    auto result = replay_recording(replay);
    CHECK(!result.has_value());
    if (!result.has_value()) {
      CHECK(result.error().code() == ErrorCode::Protocol);
    }
    CHECK(!std::filesystem::exists(replay.output_directory));
  }

  // The untouched recording still replays (the corruptions are the only
  // difference), so the refusal above is not a blanket failure.
  ReplayOptions good;
  good.input = options.recording_path;
  good.output_directory = path_in("accepted");
  auto accepted = replay_recording(good);
  CHECK(accepted.has_value());

  reset_root();
}

EINSTAR_TEST(lossy_capture_reports_missing_and_incomplete_groups) {
  reset_root();
  SimCaptureOptions options;
  options.output_directory = path_in("lossy_capture");
  options.groups = 8;
  options.seed = 7;
  options.faults = "lossy";
  options.recording_path = path_in("lossy.e1r");
  auto capture = capture_simulated(options);
  REQUIRE(capture.has_value());

  const GroupingStats& grouping = capture->grouping;
  CHECK(!grouping_complete(grouping));
  // The simulator's frame faults are keyed on the per-group frame index, so
  // every camera of a faulted group is faulted together: the observable result
  // is whole missing groups.  (A single-camera loss is covered separately by
  // incomplete_group_is_reported for the partial-group path.)
  CHECK(grouping.groups_incomplete + grouping.groups_absent > 0U);
  CHECK(grouping.groups_absent > 0U);
  CHECK(grouping.groups_written < 8U);
  CHECK(grouping.groups_written > 0U);
  CHECK_EQ(grouping.frames_written, grouping.groups_written * 3U);
  for (const GroupRecord& group : capture->groups) {
    CHECK_EQ(group.frame_ids.size(), 3U);
    for (std::uint8_t camera = 0; camera < kStreamCount; ++camera) {
      CHECK_EQ(group.files[camera].empty(), false);
    }
  }
  CHECK(contains(read_text(capture->manifest_path), "\"complete\": false"));
  CHECK(contains(read_text(capture->manifest_path),
                 "\"incomplete_groups\": ["));

  // Replaying the recorded lossy stream must reproduce the same grouping:
  // replay is the same pipeline, not a re-simulation.
  ReplayOptions replay;
  replay.input = options.recording_path;
  replay.output_directory = path_in("lossy_replay");
  replay.write_manifest = true;
  auto replayed = replay_recording(replay);
  REQUIRE(replayed.has_value());
  CHECK_EQ(replayed->grouping.groups_written, grouping.groups_written);
  CHECK_EQ(replayed->grouping.groups_incomplete, grouping.groups_incomplete);
  CHECK_EQ(replayed->grouping.groups_absent, grouping.groups_absent);
  CHECK_EQ(replayed->grouping.frames_written, grouping.frames_written);
  REQUIRE(replayed->groups.size() == capture->groups.size());
  for (std::size_t i = 0; i < capture->groups.size(); ++i) {
    CHECK_EQ(replayed->groups[i].device_group,
             capture->groups[i].device_group);
    for (std::uint8_t camera = 0; camera < kStreamCount; ++camera) {
      CHECK_EQ(replayed->groups[i].frame_ids[camera],
               capture->groups[i].frame_ids[camera]);
      CHECK_EQ(replayed->groups[i].digests[camera],
               capture->groups[i].digests[camera]);
    }
  }
  CHECK(contains(read_text(replayed->manifest_path), "\"complete\": false"));

  reset_root();
}

EINSTAR_TEST(incomplete_group_is_reported_not_silently_dropped) {
  reset_root();
  SimCaptureOptions options;
  options.output_directory = path_in("capture");
  options.groups = 4;
  options.seed = 13;
  options.recording_path = path_in("clean.e1r");
  auto capture = capture_simulated(options);
  REQUIRE(capture.has_value());

  auto source = recording::read_recording(options.recording_path);
  REQUIRE(source.has_value());
  const std::uint64_t lost_group = kSimTriggerNs;  // second group
  std::vector<std::byte> filtered = remove_one_camera_frame(*source, lost_group, 1);
  REQUIRE(!filtered.empty());

  recording::Recording crafted;
  crafted.header = source->header;
  // The recorder format caps one record at 4 MiB; split the filtered stream the
  // same way the acquisition pipeline recorded it (0xA400-byte transfers).
  constexpr std::size_t kTransferBytes = 0xA400;
  for (std::size_t offset = 0; offset < filtered.size();
       offset += kTransferBytes) {
    const std::size_t take = std::min(kTransferBytes, filtered.size() - offset);
    recording::RecordedTransfer transfer;
    transfer.bytes.assign(filtered.begin() + static_cast<std::ptrdiff_t>(offset),
                          filtered.begin() +
                              static_cast<std::ptrdiff_t>(offset + take));
    crafted.transfers.push_back(std::move(transfer));
  }
  const std::string crafted_path = path_in("one_camera_missing.e1r");
  REQUIRE(recording::write_recording(crafted_path, crafted).ok());

  // The crafted recording is still structurally valid (write_recording
  // recomputes the trailer digest), so this is a data-loss case, not a
  // corruption case.
  auto verified = verify_recording(crafted_path);
  REQUIRE(verified.has_value());

  ReplayOptions replay;
  replay.input = crafted_path;
  replay.output_directory = path_in("one_camera_missing_out");
  replay.write_manifest = true;
  auto replayed = replay_recording(replay);
  REQUIRE(replayed.has_value());
  CHECK_EQ(replayed->grouping.groups_written, 3U);
  CHECK_EQ(replayed->grouping.groups_incomplete, 1U);
  CHECK_EQ(replayed->grouping.groups_absent, 0U);
  CHECK_EQ(replayed->grouping.frames_written, 9U);
  REQUIRE(replayed->incomplete_groups.size() == 1U);
  CHECK_EQ(replayed->incomplete_groups[0].device_group, lost_group);
  CHECK_EQ(replayed->incomplete_groups[0].missing_mask, 2U);  // camera 1
  CHECK(!replayed->incomplete_groups[0].present[1]);
  CHECK(replayed->incomplete_groups[0].present[0]);
  CHECK(replayed->incomplete_groups[0].present[2]);
  CHECK(!grouping_complete(replayed->grouping));
  CHECK(contains(read_text(replayed->manifest_path), "\"complete\": false"));
  CHECK(contains(read_text(replayed->manifest_path),
                 "\"incomplete_groups\": ["));
  CHECK(contains(read_text(replayed->manifest_path),
                 "\"missing_mask\": 2"));

  reset_root();
}

EINSTAR_TEST_MAIN()
