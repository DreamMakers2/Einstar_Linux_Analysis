// SPDX-License-Identifier: MIT
// Offline tests for the memory-bounded streaming recording reader and the
// streaming replay path (`einstar-stream-frames` / `stream_replay`).
//
// Covered here:
//   * every record is validated incrementally: header CRC, record magic/length,
//     payload CRC, transfer status range, metadata/trailer, FNV-1a digest and
//     the trailer transfer/byte counts;
//   * destinations smaller than a record continue across calls (1/7/64 KiB
//     destinations produce the same byte stream and the same statuses);
//   * zero-length and non-Ok transfers are reported exactly once and still
//     contribute to the trailer digest;
//   * corruption is a hard failure (bad CRC, truncated record, wrong trailer
//     digest/counts, missing trailer, trailing bytes);
//   * replay through the acquisition pipeline/SimulatedTransport produces the
//     same PGM bytes as the in-memory path, keeps absolute dense group ids for
//     --start-group / --groups-file, skips without writing, and reports
//     verification/timing/peak-RSS;
//   * the pipe sink receives the complete groups with the simulator's expected
//     per-frame digests.
#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <map>
#include <memory>
#include <set>
#include <span>
#include <string>
#include <string_view>
#include <unordered_set>
#include <vector>

#include "capture_core.hpp"
#include "einstar/recorder.hpp"
#include "test_util.hpp"

using namespace einstar;
using namespace einstar::capture;

namespace {

const std::string kRoot = "/tmp/einstar_stream_recording_test";

void reset_root() {
  std::error_code ignored;
  std::filesystem::remove_all(kRoot, ignored);
  std::filesystem::create_directories(kRoot, ignored);
}

std::string path_in(const std::string& name) { return kRoot + "/" + name; }

std::vector<std::uint8_t> read_file(const std::string& path) {
  std::ifstream file(path, std::ios::binary);
  return std::vector<std::uint8_t>((std::istreambuf_iterator<char>(file)),
                                   std::istreambuf_iterator<char>());
}

std::string read_text(const std::string& path) {
  const std::vector<std::uint8_t> bytes = read_file(path);
  return std::string(bytes.begin(), bytes.end());
}

void write_file(const std::string& path,
                const std::vector<std::uint8_t>& bytes) {
  std::ofstream file(path, std::ios::binary | std::ios::trunc);
  file.write(reinterpret_cast<const char*>(bytes.data()),
             static_cast<std::streamsize>(bytes.size()));
}

std::uint64_t fnv_of(const std::vector<std::uint8_t>& bytes) {
  return recording::RawRecorder::fnv1a64(std::span<const std::byte>(
      reinterpret_cast<const std::byte*>(bytes.data()), bytes.size()));
}

std::uint32_t load_u32_at(const std::vector<std::uint8_t>& bytes,
                          std::size_t offset) {
  return static_cast<std::uint32_t>(bytes[offset]) |
         (static_cast<std::uint32_t>(bytes[offset + 1]) << 8U) |
         (static_cast<std::uint32_t>(bytes[offset + 2]) << 16U) |
         (static_cast<std::uint32_t>(bytes[offset + 3]) << 24U);
}

void store_u32_at(std::vector<std::uint8_t>& bytes, std::size_t offset,
                  std::uint32_t value) {
  bytes[offset] = static_cast<std::uint8_t>(value & 0xFFU);
  bytes[offset + 1] = static_cast<std::uint8_t>((value >> 8U) & 0xFFU);
  bytes[offset + 2] = static_cast<std::uint8_t>((value >> 16U) & 0xFFU);
  bytes[offset + 3] = static_cast<std::uint8_t>((value >> 24U) & 0xFFU);
}

/// Size of the trailer record (header + 32-byte payload + CRC).
constexpr std::size_t kTrailerRecordBytes =
    recording::kRecordHeaderSize + recording::kTrailerPayloadSize + 4;

/// Recompute the CRC of the trailer record at `offset`.
void fix_trailer_crc(std::vector<std::uint8_t>& bytes, std::size_t offset) {
  const std::span<const std::byte> covered(
      reinterpret_cast<const std::byte*>(bytes.data() + offset),
      recording::kRecordHeaderSize + recording::kTrailerPayloadSize);
  store_u32_at(bytes,
               offset + recording::kRecordHeaderSize +
                   recording::kTrailerPayloadSize,
               recording::RawRecorder::crc32(covered));
}

recording::Recording make_source_recording() {
  recording::Recording source;
  source.header.metadata = "unit-test;framing=raw";
  source.header.start_host_ns = 123456789;

  auto add = [&source](TransferStatus status, std::size_t size,
                       std::uint8_t fill) {
    recording::RecordedTransfer transfer;
    transfer.sequence = source.transfers.size();
    transfer.host_time_ns = static_cast<std::int64_t>(1000 + transfer.sequence);
    transfer.device_timestamp = transfer.sequence * 7;
    transfer.status = status;
    transfer.bytes.assign(size, std::byte{fill});
    source.transfers.push_back(std::move(transfer));
  };
  add(TransferStatus::Ok, 100, 0xAB);        // delivered
  add(TransferStatus::Timeout, 0, 0);        // reported once, no bytes
  add(TransferStatus::Ok, 0, 0);             // empty Ok: skipped, no hang
  add(TransferStatus::Stall, 37, 0xCD);      // payload validated, reported
  add(TransferStatus::Ok, 64 * 1024, 0xEF);  // delivered
  return source;
}

std::vector<std::uint8_t> expected_delivered_bytes(
    const recording::Recording& source) {
  std::vector<std::uint8_t> expected;
  for (const auto& transfer : source.transfers) {
    if (transfer.status == TransferStatus::Ok && !transfer.bytes.empty()) {
      for (const std::byte value : transfer.bytes) {
        expected.push_back(std::to_integer<std::uint8_t>(value));
      }
    }
  }
  return expected;
}

struct ReadOutcome {
  std::vector<std::uint8_t> delivered;
  std::vector<TransferStatus> non_ok;
  recording::StreamingVerifyStats stats;
  bool finished = false;
};

ReadOutcome drain_reader(recording::RecordingStreamReader& reader,
                         std::size_t chunk) {
  ReadOutcome outcome;
  std::vector<std::byte> buffer(chunk);
  while (!reader.at_end() && !reader.failed()) {
    TransferStatus status = TransferStatus::Ok;
    const std::size_t got = reader.read(buffer, status);
    for (std::size_t i = 0; i < got; ++i) {
      outcome.delivered.push_back(std::to_integer<std::uint8_t>(buffer[i]));
    }
    if (got == 0 && status != TransferStatus::Ok && !reader.at_end()) {
      outcome.non_ok.push_back(status);
    }
  }
  auto stats = reader.finish();
  outcome.finished = stats.has_value();
  if (stats.has_value()) {
    outcome.stats = *stats;
  }
  return outcome;
}

void write_source(const std::string& path) {
  REQUIRE(recording::write_recording(path, make_source_recording()).ok());
}

using PipedGroup = std::array<std::vector<std::uint8_t>, kStreamCount>;

struct PipeRun {
  Result<StreamReplayReport> report;
  std::shared_ptr<std::map<std::uint32_t, PipedGroup>> frames =
      std::make_shared<std::map<std::uint32_t, PipedGroup>>();
};

/// Replay through the pipe sink and collect the complete groups.
PipeRun run_pipe_case(const std::string& input, bool fast_pacing) {
  PipeRun run;
  StreamReplayOptions options;
  options.input = input;
  options.compute_digests = true;
  options.fast_pacing = fast_pacing;
  options.group_sink =
      [frames = run.frames](const GroupRecord& group,
                            std::array<std::vector<std::uint8_t>,
                                       kStreamCount>& pixels) {
        for (std::uint8_t camera = 0; camera < kStreamCount; ++camera) {
          (*frames)[group.group][camera] = std::move(pixels[camera]);
        }
        return Status::success();
      };
  run.report = stream_replay(options);
  return run;
}

}  // namespace

EINSTAR_TEST(stream_reader_validates_and_matches_trailer) {
  reset_root();
  const std::string path = path_in("source.e1r");
  write_source(path);

  auto opened = recording::RecordingStreamReader::open(path);
  REQUIRE(opened.has_value());
  auto& reader = **opened;
  CHECK(!reader.at_end());
  CHECK(!reader.failed());

  // `last_host_time_ns()` must describe the transfer whose bytes were just
  // returned (not the next record): replay stamps completions with it so group
  // times are the real capture times.
  {
    std::array<std::byte, 64 * 1024> probe{};
    TransferStatus probe_status = TransferStatus::Ok;
    CHECK_EQ(reader.read(probe, probe_status), static_cast<std::size_t>(100));
    CHECK_EQ(reader.last_host_time_ns(), static_cast<std::int64_t>(1000));
    CHECK_EQ(reader.read(probe, probe_status), static_cast<std::size_t>(0));
    CHECK(probe_status == TransferStatus::Timeout);
    CHECK_EQ(reader.last_host_time_ns(), static_cast<std::int64_t>(1001));
    // The empty Ok record is skipped and the Stall record is reported once.
    CHECK_EQ(reader.read(probe, probe_status), static_cast<std::size_t>(0));
    CHECK(probe_status == TransferStatus::Stall);
    CHECK_EQ(reader.last_host_time_ns(), static_cast<std::int64_t>(1003));
  }
  // Re-open to drain from the beginning.
  opened = recording::RecordingStreamReader::open(path);
  REQUIRE(opened.has_value());
  auto& restarted = **opened;
  CHECK_EQ(restarted.header().metadata, std::string("unit-test;framing=raw"));
  CHECK_EQ(restarted.header().start_host_ns,
           static_cast<std::uint64_t>(123456789));

  const ReadOutcome outcome = drain_reader(restarted, 64 * 1024);
  REQUIRE(outcome.finished);
  CHECK(outcome.delivered == expected_delivered_bytes(make_source_recording()));
  REQUIRE(outcome.non_ok.size() == 2U);
  CHECK(outcome.non_ok[0] == TransferStatus::Timeout);
  CHECK(outcome.non_ok[1] == TransferStatus::Stall);
  CHECK_EQ(outcome.stats.transfer_records,
           static_cast<std::uint64_t>(5));
  CHECK_EQ(outcome.stats.non_ok_transfers, static_cast<std::uint64_t>(2));
  CHECK_EQ(outcome.stats.zero_length_transfers, static_cast<std::uint64_t>(2));
  CHECK_EQ(outcome.stats.payload_bytes,
           static_cast<std::uint64_t>(100 + 37 + 64 * 1024));
  CHECK(outcome.stats.has_trailer);
  CHECK(outcome.stats.trailer_verified);
  CHECK(outcome.stats.digest_matches);
  CHECK(outcome.stats.counts_match);
  CHECK_EQ(outcome.stats.bytes_consumed, restarted.file_size());
  CHECK_EQ(outcome.stats.file_size, restarted.file_size());
  CHECK_EQ(outcome.stats.first_payload_prefix_size,
           static_cast<std::uint32_t>(64));
  CHECK(std::to_integer<std::uint8_t>(outcome.stats.first_payload_prefix[0]) ==
        0xAB);
  // finish() is idempotent and returns the same verified stats.
  auto again = restarted.finish();
  REQUIRE(again.has_value());
  CHECK(again->trailer_verified);

  // A second open starts from the beginning again.
  auto reopened = recording::RecordingStreamReader::open(path);
  REQUIRE(reopened.has_value());
  TransferStatus first_status = TransferStatus::Error;
  std::array<std::byte, 16> head{};
  CHECK_EQ((*reopened)->read(head, first_status), static_cast<std::size_t>(16));
  CHECK(first_status == TransferStatus::Ok);
  reset_root();
}

EINSTAR_TEST(stream_reader_partial_destinations_continue_a_record) {
  reset_root();
  const std::string path = path_in("source.e1r");
  write_source(path);
  const std::vector<std::uint8_t> expected =
      expected_delivered_bytes(make_source_recording());

  // Destination smaller than a payload (1, 7, 4096 bytes) must not lose data.
  for (const std::size_t chunk : {std::size_t{1}, std::size_t{7},
                                  std::size_t{4096}}) {
    auto opened = recording::RecordingStreamReader::open(path);
    REQUIRE(opened.has_value());
    auto& reader = **opened;
    std::vector<std::uint8_t> delivered;
    std::vector<TransferStatus> non_ok;
    std::vector<std::byte> buffer(chunk);
    std::size_t calls = 0;
    while (!reader.at_end() && !reader.failed()) {
      TransferStatus status = TransferStatus::Ok;
      const std::size_t got = reader.read(buffer, status);
      ++calls;
      for (std::size_t i = 0; i < got; ++i) {
        delivered.push_back(std::to_integer<std::uint8_t>(buffer[i]));
      }
      if (got == 0 && status != TransferStatus::Ok && !reader.at_end()) {
        non_ok.push_back(status);
      }
    }
    CHECK(!reader.failed());
    CHECK(reader.at_end());
    CHECK(delivered == expected);
    CHECK_EQ(non_ok.size(), static_cast<std::size_t>(2));
    // Calls never return more than the destination.
    CHECK(calls > expected.size() / chunk);
    auto stats = reader.finish();
    REQUIRE(stats.has_value());
    CHECK(stats->trailer_verified);
    CHECK_EQ(stats->bytes_consumed, reader.file_size());
  }

  // An empty destination is rejected, not silently looped on.
  auto opened = recording::RecordingStreamReader::open(path);
  REQUIRE(opened.has_value());
  TransferStatus status = TransferStatus::Ok;
  CHECK_EQ((*opened)->read(std::span<std::byte>(), status),
           static_cast<std::size_t>(0));
  CHECK(status == TransferStatus::Error);
  CHECK((*opened)->failed());
  reset_root();
}

EINSTAR_TEST(stream_reader_rejects_corruption) {
  reset_root();
  const std::string good_path = path_in("good.e1r");
  write_source(good_path);
  const std::vector<std::uint8_t> original = read_file(good_path);
  REQUIRE(original.size() > 200U);

  const std::size_t trailer_offset = original.size() - kTrailerRecordBytes;
  REQUIRE(load_u32_at(original, trailer_offset) ==
          recording::kRecordMagic);

  struct Case {
    const char* name;
    std::vector<std::uint8_t> bytes;
  };
  std::vector<Case> cases;

  {  // bad record CRC: flip one byte inside the first transfer payload
    std::vector<std::uint8_t> bytes = original;
    const std::size_t first_payload = recording::kFileHeaderSize +
                                      recording::kRecordHeaderSize;
    bytes[first_payload + 5] ^= 0x01U;
    cases.push_back({"bad_record_crc.e1r", std::move(bytes)});
  }
  {  // truncated payload: cut inside the first transfer
    std::vector<std::uint8_t> bytes = original;
    bytes.resize(recording::kFileHeaderSize + recording::kRecordHeaderSize + 10);
    cases.push_back({"truncated_payload.e1r", std::move(bytes)});
  }
  {  // truncated record header
    std::vector<std::uint8_t> bytes = original;
    bytes.resize(recording::kFileHeaderSize + 10);
    cases.push_back({"truncated_header.e1r", std::move(bytes)});
  }
  {  // missing trailer
    std::vector<std::uint8_t> bytes = original;
    bytes.resize(trailer_offset);
    cases.push_back({"missing_trailer.e1r", std::move(bytes)});
  }
  {  // wrong trailer digest (CRC repaired, so only the digest check can fail)
    std::vector<std::uint8_t> bytes = original;
    bytes[trailer_offset + recording::kRecordHeaderSize] ^= 0x01U;
    fix_trailer_crc(bytes, trailer_offset);
    cases.push_back({"wrong_trailer_digest.e1r", std::move(bytes)});
  }
  {  // wrong trailer transfer count (CRC repaired)
    std::vector<std::uint8_t> bytes = original;
    bytes[trailer_offset + recording::kRecordHeaderSize + 8] ^= 0x01U;
    fix_trailer_crc(bytes, trailer_offset);
    cases.push_back({"wrong_trailer_count.e1r", std::move(bytes)});
  }
  {  // trailing bytes after the trailer
    std::vector<std::uint8_t> bytes = original;
    bytes.insert(bytes.end(), {0x00, 0x01, 0x02, 0x03});
    cases.push_back({"trailing_bytes.e1r", std::move(bytes)});
  }
  {  // bad file magic
    std::vector<std::uint8_t> bytes = original;
    bytes[2] ^= 0x01U;
    cases.push_back({"bad_magic.e1r", std::move(bytes)});
  }
  {  // bad file-header CRC
    std::vector<std::uint8_t> bytes = original;
    bytes[32] ^= 0x01U;
    cases.push_back({"bad_header_crc.e1r", std::move(bytes)});
  }

  for (const Case& test : cases) {
    const std::string path = path_in(test.name);
    write_file(path, test.bytes);
    auto opened = recording::RecordingStreamReader::open(path);
    if (!opened.has_value()) {
      CHECK(opened.error().code() == ErrorCode::Protocol);
      continue;
    }
    auto& reader = **opened;
    std::array<std::byte, 4096> buffer{};
    while (!reader.at_end() && !reader.failed()) {
      TransferStatus status = TransferStatus::Ok;
      (void)reader.read(buffer, status);
    }
    CHECK(reader.failed());
    CHECK(reader.error().code() == ErrorCode::Protocol);
    CHECK(!reader.at_end());
    CHECK(!reader.finish().has_value());
  }

  // Missing file is an I/O error, not a protocol failure.
  auto missing = recording::RecordingStreamReader::open(path_in("nope.e1r"));
  REQUIRE(!missing.has_value());
  CHECK(missing.error().code() == ErrorCode::IoError);

  // verify_recording_stream wraps the same hard failure.
  write_file(path_in("bad_crc_verify.e1r"), cases[0].bytes);
  auto verified = recording::verify_recording_stream(
      path_in("bad_crc_verify.e1r"));
  REQUIRE(!verified.has_value());
  CHECK(verified.error().code() == ErrorCode::Protocol);
  reset_root();
}

EINSTAR_TEST(stream_replay_matches_capture_and_keeps_group_ids) {
  reset_root();
  SimCaptureOptions capture_options;
  capture_options.output_directory = path_in("capture");
  capture_options.groups = 4;
  capture_options.seed = 21;
  capture_options.recording_path = path_in("source.e1r");
  auto capture = capture_simulated(capture_options);
  REQUIRE(capture.has_value());
  REQUIRE(capture->grouping.groups_written == 4U);

  // Full streaming replay.
  StreamReplayOptions full_options;
  full_options.input = capture_options.recording_path;
  full_options.output_directory = path_in("full");
  full_options.write_manifest = true;
  auto full = stream_replay(full_options);
  REQUIRE(full.has_value());
  CHECK_EQ(full->grouping.groups_written, 4U);
  CHECK_EQ(full->grouping.frames_written, 12U);
  CHECK_EQ(full->grouping.groups_skipped, 0U);
  CHECK_EQ(full->grouping.groups_incomplete, 0U);
  CHECK(full->verification.trailer_verified);
  CHECK(full->verification_complete);
  CHECK_EQ(full->verification.transfer_records,
           capture->manifest.recorded_transfers);
  CHECK_EQ(full->verification.payload_bytes, capture->manifest.recorded_bytes);
  CHECK_EQ(full->verification.digest, capture->manifest.recorded_digest);
  CHECK(full->manifest.source_verified);
  CHECK(!full->manifest_path.empty());
  CHECK(full->peak_rss_kb > 0);
  CHECK(full->peak_rss_kb < 2U * 1024U * 1024U);  // < 2 GiB on a 4-group file
  CHECK_EQ(full->bytes_replayed, capture->manifest.recorded_bytes);

  // Every written frame is byte-identical to the capture that produced it.
  for (std::uint32_t group = 0; group < 4; ++group) {
    for (std::uint8_t camera = 0; camera < kStreamCount; ++camera) {
      const std::string name = frame_filename(group, camera);
      CHECK(read_file(path_in("full") + "/" + name) ==
            read_file(capture->manifest.output_directory + "/" + name));
    }
  }

  // --start-group 1 --max-groups 2: skip one group, write groups 1 and 2 with
  // their full-run ids, never write group 0.
  StreamReplayOptions range_options;
  range_options.input = capture_options.recording_path;
  range_options.output_directory = path_in("range");
  range_options.start_group = 1;
  range_options.max_groups = 2;
  auto range = stream_replay(range_options);
  REQUIRE(range.has_value());
  CHECK_EQ(range->grouping.groups_written, 2U);
  CHECK_EQ(range->grouping.groups_skipped, 1U);
  CHECK(range->stopped_early);
  CHECK(range->verification_complete);
  CHECK(range->verification.trailer_verified);
  CHECK(std::filesystem::exists(path_in("range") + "/" +
                                frame_filename(1, 0)));
  CHECK(std::filesystem::exists(path_in("range") + "/" +
                                frame_filename(2, 2)));
  CHECK(!std::filesystem::exists(path_in("range") + "/" +
                                 frame_filename(0, 0)));
  CHECK(!std::filesystem::exists(path_in("range") + "/" +
                                 frame_filename(3, 0)));
  for (std::uint32_t group = 1; group <= 2; ++group) {
    for (std::uint8_t camera = 0; camera < kStreamCount; ++camera) {
      const std::string name = frame_filename(group, camera);
      CHECK(read_file(path_in("range") + "/" + name) ==
            read_file(path_in("full") + "/" + name));
    }
  }

  // Sparse selection {0,2}: same bytes, ids preserved, skipped counted.
  auto selection = std::make_shared<std::unordered_set<std::uint32_t>>();
  selection->insert(0);
  selection->insert(2);
  StreamReplayOptions select_options;
  select_options.input = capture_options.recording_path;
  select_options.output_directory = path_in("selected");
  select_options.selection = selection;
  auto selected = stream_replay(select_options);
  REQUIRE(selected.has_value());
  CHECK_EQ(selected->grouping.groups_written, 2U);
  CHECK_EQ(selected->grouping.groups_skipped, 1U);
  for (std::uint32_t group : {0U, 2U}) {
    for (std::uint8_t camera = 0; camera < kStreamCount; ++camera) {
      const std::string name = frame_filename(group, camera);
      CHECK(read_file(path_in("selected") + "/" + name) ==
            read_file(path_in("full") + "/" + name));
    }
  }
  CHECK(!std::filesystem::exists(path_in("selected") + "/" +
                                 frame_filename(1, 0)));

  // Verify-only reports the trailer result without writing anything.
  StreamReplayOptions verify_options;
  verify_options.input = capture_options.recording_path;
  verify_options.verify_only = true;
  auto verified = stream_replay(verify_options);
  REQUIRE(verified.has_value());
  CHECK(verified->verification.trailer_verified);
  CHECK(verified->verification_complete);
  CHECK_EQ(verified->verification.digest, capture->manifest.recorded_digest);
  CHECK(verified->bytes_replayed == 0U);

  // Pipe sink: complete groups are delivered with the simulator's expected
  // per-camera pixel digests.  Fast pacing is a pure scheduling change: the
  // delivered records, host timestamps and pixels must be identical to paced.
  PipeRun paced = run_pipe_case(capture_options.recording_path, false);
  REQUIRE(paced.report.has_value());
  CHECK_EQ(paced.report->grouping.groups_written, 4U);
  CHECK_EQ(paced.report->grouping.frames_written, 12U);
  REQUIRE(paced.frames->size() == 4U);
  for (std::uint32_t group = 0; group < 4; ++group) {
    REQUIRE((*paced.frames)[group][0].size() == kFrameBytes);
    for (std::uint8_t camera = 0; camera < kStreamCount; ++camera) {
      CHECK_EQ(fnv_of((*paced.frames)[group][camera]),
               simulated_frame_digest(
                   paced.report->groups[group].frame_ids[camera], kFrameBytes,
                   0xA000));
    }
  }
  PipeRun fast = run_pipe_case(capture_options.recording_path, true);
  REQUIRE(fast.report.has_value());
  REQUIRE(paced.report->groups.size() == fast.report->groups.size());
  for (std::size_t i = 0; i < paced.report->groups.size(); ++i) {
    const GroupRecord& left = paced.report->groups[i];
    const GroupRecord& right = fast.report->groups[i];
    CHECK_EQ(left.group, right.group);
    CHECK_EQ(left.device_group, right.device_group);
    CHECK_EQ(left.host_time_ns, right.host_time_ns);
    CHECK(left.frame_ids == right.frame_ids);
    CHECK(left.digests == right.digests);
  }
  REQUIRE(paced.frames->size() == fast.frames->size());
  for (std::uint32_t group = 0; group < 4; ++group) {
    for (std::uint8_t camera = 0; camera < kStreamCount; ++camera) {
      CHECK((*paced.frames)[group][camera] == (*fast.frames)[group][camera]);
    }
  }

  reset_root();
}

EINSTAR_TEST(stream_replay_refuses_corruption_before_writing) {
  reset_root();
  SimCaptureOptions capture_options;
  capture_options.output_directory = path_in("capture");
  capture_options.groups = 3;
  capture_options.seed = 5;
  capture_options.recording_path = path_in("source.e1r");
  auto capture = capture_simulated(capture_options);
  REQUIRE(capture.has_value());

  std::vector<std::uint8_t> bytes = read_file(capture_options.recording_path);
  REQUIRE(bytes.size() > 512U);
  bytes[300] ^= 0x01U;  // inside the recorded transfer stream
  const std::string corrupt = path_in("corrupt.e1r");
  write_file(corrupt, bytes);

  StreamReplayOptions options;
  options.input = corrupt;
  options.output_directory = path_in("refused");
  auto refused = stream_replay(options);
  CHECK(!refused.has_value());
  if (!refused.has_value()) {
    CHECK(refused.error().code() == ErrorCode::Protocol);
  }
  CHECK(!std::filesystem::exists(options.output_directory));

  // Single-pass mode surfaces the same failure from the replay pass.
  StreamReplayOptions single;
  single.input = corrupt;
  single.output_directory = path_in("single_pass");
  single.single_pass = true;
  auto single_result = stream_replay(single);
  CHECK(!single_result.has_value());
  if (!single_result.has_value()) {
    CHECK(single_result.error().code() == ErrorCode::Protocol);
  }

  // A missing trailer is a hard failure in both modes.
  std::vector<std::uint8_t> missing = bytes;
  missing.resize(missing.size() - kTrailerRecordBytes);
  write_file(path_in("no_trailer.e1r"), missing);
  StreamReplayOptions no_trailer;
  no_trailer.input = path_in("no_trailer.e1r");
  no_trailer.output_directory = path_in("no_trailer_out");
  no_trailer.single_pass = true;
  auto no_trailer_result = stream_replay(no_trailer);
  CHECK(!no_trailer_result.has_value());
  if (!no_trailer_result.has_value()) {
    CHECK(no_trailer_result.error().code() == ErrorCode::Protocol);
  }
  reset_root();
}


EINSTAR_TEST(trailer_only_check_reports_declared_evidence) {
  reset_root();
  const std::string path = path_in("source.e1r");
  write_source(path);
  auto loaded = recording::read_recording(path);
  REQUIRE(loaded.has_value());
  const std::uint64_t file_size =
      static_cast<std::uint64_t>(read_file(path).size());

  auto info = read_recording_trailer(path);
  REQUIRE(info.has_value());
  CHECK_EQ(info->file_size, file_size);
  CHECK_EQ(info->trailer_offset,
           file_size - static_cast<std::uint64_t>(kTrailerRecordBytes));
  CHECK_EQ(info->transfers, loaded->trailer_transfers);
  CHECK_EQ(info->bytes, loaded->trailer_bytes);
  CHECK_EQ(info->digest, loaded->trailer_digest);
  CHECK_EQ(info->version, recording::kFormatVersion);
  CHECK_EQ(info->start_host_ns, static_cast<std::uint64_t>(123456789));
  CHECK(info->trailer_present);
  CHECK(!info->digest_verified);  // declared evidence only

  // Bounded mode must match the authoritative in-memory trailer values used by
  // the full verifier; it never claims a recomputed digest.
  auto full = verify_recording_stream(path);
  REQUIRE(full.has_value());
  CHECK_EQ(info->digest, full->trailer_digest);
  CHECK_EQ(info->transfers, full->trailer_transfers);
  CHECK_EQ(info->bytes, full->trailer_bytes);

  // Missing trailer: header-only file is too small to contain one.
  std::vector<std::uint8_t> bytes = read_file(path);
  write_file(path_in("header_only.e1r"),
             std::vector<std::uint8_t>(bytes.begin(), bytes.begin() + 64));
  auto missing = read_recording_trailer(path_in("header_only.e1r"));
  REQUIRE(!missing.has_value());
  CHECK(missing.error().code() == ErrorCode::Protocol);

  // Truncated trailer: cutting the final record means the last 72 bytes are no
  // longer a trailer record.
  bytes.resize(bytes.size() - 12U);
  write_file(path_in("truncated.e1r"), bytes);
  auto truncated = read_recording_trailer(path_in("truncated.e1r"));
  REQUIRE(!truncated.has_value());
  CHECK(truncated.error().code() == ErrorCode::Protocol);

  // Bad trailer CRC.
  bytes = read_file(path);
  bytes[bytes.size() - 1] ^= 0x01U;
  write_file(path_in("bad_crc.e1r"), bytes);
  auto bad_crc = read_recording_trailer(path_in("bad_crc.e1r"));
  REQUIRE(!bad_crc.has_value());
  CHECK(bad_crc.error().code() == ErrorCode::Protocol);

  // Bad file magic / header CRC.
  bytes = read_file(path);
  bytes[1] ^= 0x01U;
  write_file(path_in("bad_magic.e1r"), bytes);
  auto bad_magic = read_recording_trailer(path_in("bad_magic.e1r"));
  REQUIRE(!bad_magic.has_value());
  CHECK(bad_magic.error().code() == ErrorCode::Protocol);
  bytes = read_file(path);
  bytes[32] ^= 0x01U;
  write_file(path_in("bad_header.e1r"), bytes);
  auto bad_header = read_recording_trailer(path_in("bad_header.e1r"));
  REQUIRE(!bad_header.has_value());
  CHECK(bad_header.error().code() == ErrorCode::Protocol);

  auto absent = read_recording_trailer(path_in("does_not_exist.e1r"));
  REQUIRE(!absent.has_value());
  CHECK(absent.error().code() == ErrorCode::IoError);
  reset_root();
}

EINSTAR_TEST(trailer_only_check_is_bounded_on_a_large_padded_recording) {
  reset_root();
  const std::string source = path_in("seed.e1r");
  write_source(source);
  auto loaded = recording::read_recording(source);
  REQUIRE(loaded.has_value());
  const std::vector<std::uint8_t> seed = read_file(source);
  REQUIRE(seed.size() > kTrailerRecordBytes);
  const std::vector<std::uint8_t> header(seed.begin(),
                                         seed.begin() + 64);
  const std::vector<std::uint8_t> trailer(seed.end() - kTrailerRecordBytes,
                                          seed.end());

  // A 16 GiB sparse file whose header and trailer are the only real records:
  // the bounded check must read only those 136 bytes, never the payload gap.
  constexpr std::uint64_t kPaddedSize = 16ULL * 1024ULL * 1024ULL * 1024ULL;
  const std::string padded = path_in("padded.e1r");
  {
    std::ofstream out(padded, std::ios::binary | std::ios::trunc);
    out.write(reinterpret_cast<const char*>(header.data()),
              static_cast<std::streamsize>(header.size()));
    out.seekp(
        static_cast<std::streamoff>(kPaddedSize - kTrailerRecordBytes),
        std::ios::beg);
    out.write(reinterpret_cast<const char*>(trailer.data()),
              static_cast<std::streamsize>(trailer.size()));
  }
  const auto started = std::chrono::steady_clock::now();
  auto info = read_recording_trailer(padded);
  const double elapsed =
      std::chrono::duration<double>(std::chrono::steady_clock::now() - started)
          .count();
  REQUIRE(info.has_value());
  CHECK_EQ(info->file_size, kPaddedSize);
  CHECK_EQ(info->trailer_offset, kPaddedSize - kTrailerRecordBytes);
  CHECK_EQ(info->transfers, loaded->trailer_transfers);
  CHECK_EQ(info->bytes, loaded->trailer_bytes);
  CHECK_EQ(info->digest, loaded->trailer_digest);
  CHECK(info->trailer_present);
  CHECK(!info->digest_verified);
  CHECK(elapsed < 1.0);  // a full 16 GiB read cannot finish this fast

  // Mode resolution: Auto picks full for small/simulated and trailer for a
  // large real recording; explicit modes always win.
  CHECK(resolve_recording_verify_mode(RecordingVerifyMode::Auto, true,
                                      16ULL * 1024ULL * 1024ULL * 1024ULL) ==
        RecordingVerifyMode::Full);
  CHECK(resolve_recording_verify_mode(RecordingVerifyMode::Auto, false,
                                      16ULL * 1024ULL * 1024ULL * 1024ULL) ==
        RecordingVerifyMode::Trailer);
  CHECK(resolve_recording_verify_mode(RecordingVerifyMode::Auto, false,
                                      1024ULL * 1024ULL) ==
        RecordingVerifyMode::Full);
  CHECK(resolve_recording_verify_mode(RecordingVerifyMode::Full, false,
                                      16ULL * 1024ULL * 1024ULL * 1024ULL) ==
        RecordingVerifyMode::Full);
  CHECK(resolve_recording_verify_mode(RecordingVerifyMode::Trailer, true,
                                      1024ULL) ==
        RecordingVerifyMode::Trailer);

  std::error_code ignored;
  std::filesystem::remove(padded, ignored);
  reset_root();
}

EINSTAR_TEST(capture_session_records_trailer_evidence_without_full_reread) {
  reset_root();
  // Small simulated capture in Auto mode keeps the historical full check.
  SimCaptureOptions auto_options;
  auto_options.output_directory = path_in("auto_frames");
  auto_options.groups = 2;
  auto_options.seed = 4;
  auto_options.recording_path = path_in("auto.e1r");
  auto auto_capture = capture_simulated(auto_options);
  REQUIRE(auto_capture.has_value());
  CHECK_EQ(auto_capture->manifest.recorded_verification, std::string("full"));
  CHECK(auto_capture->manifest.recorded_digest_verified);
  CHECK(auto_capture->manifest.recorded_trailer_checked);

  // Explicit trailer mode: the stop path must produce the same declared
  // digest/counts as the full verifier, but flag them as not recomputed.
  SimCaptureOptions trailer_options;
  trailer_options.output_directory = path_in("trailer_frames");
  trailer_options.groups = 2;
  trailer_options.seed = 5;
  trailer_options.recording_path = path_in("trailer.e1r");
  trailer_options.recording_verify = RecordingVerifyMode::Trailer;
  auto trailer_capture = capture_simulated(trailer_options);
  REQUIRE(trailer_capture.has_value());
  CHECK_EQ(trailer_capture->manifest.recorded_verification,
           std::string("trailer"));
  CHECK(trailer_capture->manifest.recorded_trailer_checked);
  CHECK(!trailer_capture->manifest.recorded_digest_verified);
  auto trailer_info = read_recording_trailer(trailer_options.recording_path);
  REQUIRE(trailer_info.has_value());
  CHECK_EQ(trailer_capture->manifest.recorded_digest, trailer_info->digest);
  CHECK_EQ(trailer_capture->manifest.recorded_transfers,
           trailer_info->transfers);
  CHECK_EQ(trailer_capture->manifest.recorded_bytes, trailer_info->bytes);

  const std::string manifest_text = read_text(
      trailer_options.output_directory + "/frames_manifest.json");
  CHECK(manifest_text.find("\"verification\": \"trailer\"") !=
        std::string::npos);
  CHECK(manifest_text.find("\"digest_verified\": false") != std::string::npos);
  CHECK(manifest_text.find("\"trailer_checked\": true") != std::string::npos);

  reset_root();
}

EINSTAR_TEST_MAIN()
