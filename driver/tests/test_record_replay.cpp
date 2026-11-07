// SPDX-License-Identifier: MIT
#include <algorithm>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

#include "einstar/pipeline.hpp"
#include "einstar/recorder.hpp"
#include "einstar/sim.hpp"
#include "test_util.hpp"

using namespace einstar;

namespace {

struct Sink final : CompletionSink {
  std::vector<TransferCompletion> completions;
  void on_transfer_complete(const TransferCompletion& completion) noexcept override {
    completions.push_back(completion);
  }
};

std::string temp_path(const char* name) {
  return std::string("/tmp/") + name;
}

void remove_file(const std::string& path) {
  std::error_code ignored;
  std::filesystem::remove(path, ignored);
}

}  // namespace

EINSTAR_TEST(crc32_known_vector) {
  const std::string text = "123456789";
  const std::span<const std::byte> bytes(
      reinterpret_cast<const std::byte*>(text.data()), text.size());
  CHECK_EQ(recording::RawRecorder::crc32(bytes), 0xCBF43926U);
  CHECK_EQ(recording::RawRecorder::fnv1a64(bytes), 0x06D5573923C6CDFCULL);
}

EINSTAR_TEST(recording_roundtrip) {
  const std::string path = temp_path("einstar_roundtrip.bin");
  remove_file(path);
  recording::Recording source;
  source.header.metadata = "unit-test";
  source.header.start_host_ns = 123456;
  for (int i = 0; i < 5; ++i) {
    recording::RecordedTransfer transfer;
    transfer.sequence = static_cast<std::uint64_t>(i);
    transfer.host_time_ns = 1000 + i;
    transfer.device_timestamp = static_cast<std::uint64_t>(i) * 7;
    transfer.status = i == 3 ? TransferStatus::Timeout : TransferStatus::Ok;
    transfer.bytes.assign(64, static_cast<std::byte>(i));
    source.transfers.push_back(std::move(transfer));
  }
  REQUIRE(recording::write_recording(path, source).ok());
  auto loaded = recording::read_recording(path);
  REQUIRE(loaded.has_value());
  CHECK(loaded->has_trailer);
  CHECK_EQ(loaded->header.metadata, std::string("unit-test"));
  CHECK_EQ(loaded->header.start_host_ns, 123456U);
  REQUIRE(loaded->transfers.size() == source.transfers.size());
  for (std::size_t i = 0; i < source.transfers.size(); ++i) {
    CHECK_EQ(loaded->transfers[i].sequence, source.transfers[i].sequence);
    CHECK(loaded->transfers[i].status == source.transfers[i].status);
    CHECK(loaded->transfers[i].bytes == source.transfers[i].bytes);
  }
  remove_file(path);
}

EINSTAR_TEST(recording_detects_corruption) {
  const std::string path = temp_path("einstar_corrupt.bin");
  remove_file(path);
  recording::Recording source;
  recording::RecordedTransfer transfer;
  transfer.bytes.assign(128, std::byte{0x42});
  source.transfers.push_back(transfer);
  REQUIRE(recording::write_recording(path, source).ok());

  std::ifstream input(path, std::ios::binary);
  std::vector<char> bytes((std::istreambuf_iterator<char>(input)),
                          std::istreambuf_iterator<char>());
  input.close();
  REQUIRE(bytes.size() > 90U);
  bytes[80] ^= 0x01;  // inside the first record payload
  const std::string corrupt_path = temp_path("einstar_corrupt2.bin");
  std::ofstream output(corrupt_path, std::ios::binary | std::ios::trunc);
  output.write(bytes.data(), static_cast<std::streamsize>(bytes.size()));
  output.close();
  auto loaded = recording::read_recording(corrupt_path);
  CHECK(!loaded.has_value());
  if (!loaded.has_value()) {
    CHECK(loaded.error().code() == ErrorCode::Protocol);
  }

  // Truncated trailer must also fail.
  bytes.resize(bytes.size() - 12);
  std::ofstream truncated(path, std::ios::binary | std::ios::trunc);
  truncated.write(bytes.data(), static_cast<std::streamsize>(bytes.size()));
  truncated.close();
  auto truncated_result = recording::read_recording(path);
  CHECK(!truncated_result.has_value());
  remove_file(path);
  remove_file(corrupt_path);
}

EINSTAR_TEST(recording_replay_is_byte_exact) {
  const std::string path = temp_path("einstar_replay.bin");
  remove_file(path);

  SimConfig sim;
  sim.seed = 77;
  sim.frames = 6;
  sim.require_interval_patch = false;
  sim.faults.loss_probability = 0.1;
  sim.faults.short_read_probability = 0.4;
  sim.faults.corrupt_length_probability = 0.05;
  sim.faults.duplicate_header_probability = 0.05;
  sim.faults.timeout_probability = 0.05;
  sim.faults.stall_probability = 0.05;

  PipelineConfig config;
  config.queue_depth = 4;
  config.copy_mode = CopyMode::ZeroCopy;
  config.frame_queue_capacity = 8;
  config.enable_recording = true;
  config.recording_path = path;
  auto transport = std::make_unique<SimulatedTransport>(sim);
  auto pipeline = std::make_unique<AcquisitionPipeline>(std::move(transport),
                                                        config);
  REQUIRE(pipeline->start().ok());
  for (int i = 0; i < 1200; ++i) {
    (void)pipeline->poll(2);
    Frame frame;
    while (pipeline->try_pop_frame(frame)) {
      frame = Frame{};
    }
  }
  REQUIRE(pipeline->stop().ok());

  auto recording = recording::read_recording(path);
  REQUIRE(recording.has_value());
  REQUIRE(!recording->transfers.empty());

  std::vector<std::uint8_t> recorded_bytes;
  std::uint64_t expected_digest = 14695981039346656037ULL;
  for (const auto& transfer : recording->transfers) {
    for (const auto byte : transfer.bytes) {
      recorded_bytes.push_back(std::to_integer<std::uint8_t>(byte));
      expected_digest ^= static_cast<std::uint64_t>(
          std::to_integer<std::uint8_t>(byte));
      expected_digest *= 1099511628211ULL;
    }
  }
  CHECK_EQ(expected_digest, recording->trailer_digest);

  SimConfig replay_sim;
  replay_sim.require_interval_patch = false;
  replay_sim.replay = recording::make_replay_source(*recording);
  replay_sim.frames = 0;
  SimulatedTransport replay(std::move(replay_sim));
  REQUIRE(replay.open().ok());
  REQUIRE(replay.claim_interface(0).ok());

  Sink sink;
  BufferPool pool(0xA400, 2);
  std::vector<std::uint8_t> replayed;
  std::size_t reads = 0;
  while (replayed.size() < recorded_bytes.size() && reads < 100000) {
    BufferHandle buffer = pool.acquire();
    REQUIRE(buffer.valid());
    REQUIRE(replay.submit_bulk_in(Endpoints::kStreamIn, buffer.bytes(), sink,
                                  nullptr) != 0);
    while (sink.completions.empty()) {
      replay.poll(10);
    }
    const TransferCompletion completion = sink.completions.back();
    sink.completions.clear();
    reads++;
    if (completion.status == TransferStatus::Ok) {
      for (std::size_t i = 0; i < completion.bytes_transferred; ++i) {
        replayed.push_back(std::to_integer<std::uint8_t>(buffer.data()[i]));
      }
    }
  }
  CHECK(replayed == recorded_bytes);
  CHECK_EQ(replay.delivered_bytes(), recorded_bytes.size());
  CHECK_EQ(recording->trailer_bytes, recorded_bytes.size());
  remove_file(path);
}

EINSTAR_TEST_MAIN()
