// SPDX-License-Identifier: MIT
// Offline recorder / replay tool.
//
//   einstar-record-replay record --out FILE [--frames N] [--seed S]
//                                [--faults clean|lossy|hostile]
//   einstar-record-replay play   --in FILE [--queue-depth N]
//   einstar-record-replay dump   --in FILE
//
// `play` first verifies the recording byte-for-byte against the trailer digest,
// then feeds it back through the simulated transport and counts reassembled
// frames.
#include <cstdint>
#include <memory>
#include <span>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <utility>
#include <vector>

#include "einstar/pipeline.hpp"
#include "einstar/recorder.hpp"
#include "einstar/sim.hpp"

namespace {

using einstar::AcquisitionPipeline;
using einstar::BufferHandle;
using einstar::BufferPool;
using einstar::CompletionSink;
using einstar::CopyMode;
using einstar::Endpoints;
using einstar::Frame;
using einstar::PipelineConfig;
using einstar::SimConfig;
using einstar::SimulatedTransport;
using einstar::TransferCompletion;
using einstar::TransferId;
using einstar::TransferStatus;
using einstar::recording::RawRecorder;
using einstar::recording::Recording;
using einstar::recording::ReplayTransfer;

struct Sink final : CompletionSink {
  std::vector<TransferCompletion> completions;
  void on_transfer_complete(const TransferCompletion& completion) noexcept override {
    completions.push_back(completion);
  }
};

void print_usage() {
  std::printf(
      "usage:\n"
      "  einstar-record-replay record --out FILE [--frames N] [--seed S]\n"
      "                               [--faults clean|lossy|hostile]\n"
      "                               [--queue-depth N]\n"
      "  einstar-record-replay play --in FILE [--queue-depth N]\n"
      "  einstar-record-replay dump --in FILE\n");
}

std::uint64_t total_bytes(const Recording& recording) {
  std::uint64_t bytes = 0;
  for (const auto& transfer : recording.transfers) {
    bytes += transfer.bytes.size();
  }
  return bytes;
}

int cmd_record(int argc, char** argv) {
  std::string output;
  std::uint32_t frames = 100;
  std::uint64_t seed = 1;
  std::string faults = "clean";
  std::uint32_t depth = 8;
  for (int i = 2; i < argc; ++i) {
    const std::string argument = argv[i];
    if (argument == "--out" && i + 1 < argc) {
      output = argv[++i];
    } else if (argument == "--frames" && i + 1 < argc) {
      frames = static_cast<std::uint32_t>(std::strtoul(argv[++i], nullptr, 0));
    } else if (argument == "--seed" && i + 1 < argc) {
      seed = std::strtoull(argv[++i], nullptr, 0);
    } else if (argument == "--faults" && i + 1 < argc) {
      faults = argv[++i];
    } else if (argument == "--queue-depth" && i + 1 < argc) {
      depth = static_cast<std::uint32_t>(std::strtoul(argv[++i], nullptr, 0));
    } else {
      std::fprintf(stderr, "unknown argument: %s\n", argument.c_str());
      return 2;
    }
  }
  if (output.empty()) {
    std::fprintf(stderr, "--out is required\n");
    return 2;
  }

  SimConfig sim;
  sim.seed = seed;
  sim.frames = frames;
  sim.expect_interval_quirk = false;
  if (faults == "lossy") {
    sim.faults.loss_probability = 0.02;
    sim.faults.short_read_probability = 0.25;
    sim.faults.timeout_probability = 0.02;
  } else if (faults == "hostile") {
    sim.faults.loss_probability = 0.10;
    sim.faults.short_read_probability = 0.40;
    sim.faults.corrupt_length_probability = 0.05;
    sim.faults.duplicate_header_probability = 0.05;
    sim.faults.stall_probability = 0.05;
    sim.faults.timeout_probability = 0.05;
    sim.faults.truncate_frame_probability = 0.10;
  } else if (faults != "clean") {
    std::fprintf(stderr, "unknown fault preset: %s\n", faults.c_str());
    return 2;
  }

  PipelineConfig config;
  config.queue_depth = depth;
  config.copy_mode = CopyMode::Copy;
  config.frame_queue_capacity = 16;
  config.partial_frame_timeout_ns = 500'000'000;
  config.enable_recording = true;
  config.recording_path = output;
  config.recording_metadata = "faults=" + faults + ";seed=" + std::to_string(seed);

  auto transport = std::make_unique<SimulatedTransport>(sim);
  auto pipeline = std::make_unique<AcquisitionPipeline>(std::move(transport),
                                                        config);
  if (!pipeline->start().ok()) {
    std::fprintf(stderr, "failed to start acquisition pipeline\n");
    return 1;
  }
  std::uint64_t received = 0;
  std::size_t iterations = 0;
  Frame frame;
  while (received < frames && iterations++ < 30000) {
    (void)pipeline->poll(2);
    while (pipeline->try_pop_frame(frame)) {
      ++received;
      frame = Frame{};
    }
  }
  const auto stop_status = pipeline->stop();
  if (!stop_status.ok()) {
    std::fprintf(stderr, "stop failed: %s\n", stop_status.message().c_str());
    return 1;
  }

  auto recording = einstar::recording::read_recording(output);
  if (!recording.has_value()) {
    std::fprintf(stderr, "recording is invalid: %s\n",
                 recording.error().message().c_str());
    return 1;
  }
  std::printf("recorded %s\n", output.c_str());
  std::printf("  transfers        : %zu\n", recording->transfers.size());
  std::printf("  bytes            : %llu\n",
              static_cast<unsigned long long>(total_bytes(*recording)));
  std::printf("  digest (FNV-1a64): 0x%016llX\n",
              static_cast<unsigned long long>(recording->trailer_digest));
  std::printf("  frames completed : %llu\n",
              static_cast<unsigned long long>(received));
  std::printf("  fault preset     : %s\n", faults.c_str());
  return 0;
}

int cmd_dump(int argc, char** argv) {
  std::string input;
  for (int i = 2; i < argc; ++i) {
    if (std::string(argv[i]) == "--in" && i + 1 < argc) {
      input = argv[++i];
    } else {
      std::fprintf(stderr, "unknown argument: %s\n", argv[i]);
      return 2;
    }
  }
  if (input.empty()) {
    std::fprintf(stderr, "--in is required\n");
    return 2;
  }
  auto recording = einstar::recording::read_recording(input);
  if (!recording.has_value()) {
    std::fprintf(stderr, "invalid recording: %s\n",
                 recording.error().message().c_str());
    return 1;
  }
  std::uint64_t statuses[6] = {};
  for (const auto& transfer : recording->transfers) {
    statuses[static_cast<std::size_t>(transfer.status)]++;
  }
  std::printf("file             : %s\n", input.c_str());
  std::printf("version          : %u\n", recording->header.version);
  std::printf("metadata         : %s\n", recording->header.metadata.c_str());
  std::printf("start_host_ns    : %llu\n",
              static_cast<unsigned long long>(recording->header.start_host_ns));
  std::printf("transfers        : %zu\n", recording->transfers.size());
  std::printf("bytes            : %llu\n",
              static_cast<unsigned long long>(total_bytes(*recording)));
  std::printf("digest           : 0x%016llX\n",
              static_cast<unsigned long long>(recording->trailer_digest));
  std::printf("has_trailer      : %s\n", recording->has_trailer ? "yes" : "no");
  std::printf("status counts    : ok=%llu timeout=%llu cancelled=%llu "
              "stall=%llu overflow=%llu error=%llu\n",
              static_cast<unsigned long long>(statuses[0]),
              static_cast<unsigned long long>(statuses[1]),
              static_cast<unsigned long long>(statuses[2]),
              static_cast<unsigned long long>(statuses[3]),
              static_cast<unsigned long long>(statuses[4]),
              static_cast<unsigned long long>(statuses[5]));
  return 0;
}

int cmd_play(int argc, char** argv) {
  std::string input;
  std::uint32_t depth = 8;
  for (int i = 2; i < argc; ++i) {
    const std::string argument = argv[i];
    if (argument == "--in" && i + 1 < argc) {
      input = argv[++i];
    } else if (argument == "--queue-depth" && i + 1 < argc) {
      depth = static_cast<std::uint32_t>(std::strtoul(argv[++i], nullptr, 0));
    } else {
      std::fprintf(stderr, "unknown argument: %s\n", argument.c_str());
      return 2;
    }
  }
  if (input.empty()) {
    std::fprintf(stderr, "--in is required\n");
    return 2;
  }
  auto recording = einstar::recording::read_recording(input);
  if (!recording.has_value()) {
    std::fprintf(stderr, "invalid recording: %s\n",
                 recording.error().message().c_str());
    return 1;
  }
  const std::uint64_t expected_bytes = total_bytes(*recording);

  // ---- byte-exact verification through the simulated transport -------------
  SimConfig replay_config;
  replay_config.expect_interval_quirk = false;
  replay_config.replay = einstar::recording::make_replay_source(*recording);
  SimulatedTransport replay(replay_config);
  if (!replay.open().ok() || !replay.claim_interface(0).ok()) {
    std::fprintf(stderr, "failed to open replay transport\n");
    return 1;
  }
  Sink sink;
  BufferPool pool(0xA400, 2);
  std::vector<std::uint8_t> delivered;
  std::size_t reads = 0;
  while (delivered.size() < expected_bytes && reads++ < 500000) {
    BufferHandle buffer = pool.acquire();
    if (!buffer.valid()) {
      break;
    }
    if (replay.submit_bulk_in(Endpoints::kStreamIn, buffer.bytes(), sink,
                              nullptr) == 0) {
      break;
    }
    while (sink.completions.empty()) {
      replay.poll(10);
    }
    const TransferCompletion completion = sink.completions.back();
    sink.completions.clear();
    if (completion.status == TransferStatus::Ok) {
      for (std::size_t i = 0; i < completion.bytes_transferred; ++i) {
        delivered.push_back(std::to_integer<std::uint8_t>(buffer.data()[i]));
      }
    }
  }
  const std::uint64_t digest = RawRecorder::fnv1a64(
      std::span<const std::byte>(
          reinterpret_cast<const std::byte*>(delivered.data()),
          delivered.size()));
  const bool bytes_ok = delivered.size() == expected_bytes;
  const bool digest_ok = digest == recording->trailer_digest;
  std::printf("replay %s\n", input.c_str());
  std::printf("  recorded bytes : %llu\n",
              static_cast<unsigned long long>(expected_bytes));
  std::printf("  replayed bytes : %zu\n", delivered.size());
  std::printf("  recorded digest: 0x%016llX\n",
              static_cast<unsigned long long>(recording->trailer_digest));
  std::printf("  replayed digest: 0x%016llX\n",
              static_cast<unsigned long long>(digest));
  std::printf("  byte-exact     : %s\n",
              (bytes_ok && digest_ok) ? "PASS" : "FAIL");

  // ---- frame-level replay ---------------------------------------------------
  SimConfig pipeline_sim;
  pipeline_sim.expect_interval_quirk = false;
  pipeline_sim.replay = einstar::recording::make_replay_source(*recording);
  pipeline_sim.frames = 0;
  PipelineConfig config;
  config.queue_depth = depth;
  config.copy_mode = CopyMode::Copy;
  config.frame_queue_capacity = 16;
  config.partial_frame_timeout_ns = 500'000'000;
  auto transport = std::make_unique<SimulatedTransport>(pipeline_sim);
  auto pipeline = std::make_unique<AcquisitionPipeline>(std::move(transport),
                                                        config);
  if (!pipeline->start().ok()) {
    std::fprintf(stderr, "failed to start replay pipeline\n");
    return 1;
  }
  std::uint64_t frames = 0;
  std::size_t iterations = 0;
  Frame frame;
  while (iterations++ < 50000) {
    (void)pipeline->poll(2);
    while (pipeline->try_pop_frame(frame)) {
      ++frames;
      frame = Frame{};
    }
  }
  const auto stats = pipeline->stats();
  (void)pipeline->stop();
  std::printf("  frames         : %llu (reassembly completed %llu)\n",
              static_cast<unsigned long long>(frames),
              static_cast<unsigned long long>(stats.frames_completed));
  std::printf("  packets parsed : %llu\n",
              static_cast<unsigned long long>(stats.packets_parsed));
  std::printf("  parse errors   : %llu\n",
              static_cast<unsigned long long>(stats.parse_errors));
  return (bytes_ok && digest_ok) ? 0 : 1;
}

}  // namespace

int main(int argc, char** argv) {
  if (argc < 2) {
    print_usage();
    return 2;
  }
  const std::string command = argv[1];
  if (command == "record") {
    return cmd_record(argc, argv);
  }
  if (command == "play") {
    return cmd_play(argc, argv);
  }
  if (command == "dump") {
    return cmd_dump(argc, argv);
  }
  if (command == "--help" || command == "-h" || command == "help") {
    print_usage();
    return 0;
  }
  print_usage();
  return 2;
}
