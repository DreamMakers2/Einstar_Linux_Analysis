// SPDX-License-Identifier: MIT
#include <cstdint>
#include <vector>

#include "einstar/pipeline.hpp"
#include "einstar/sim.hpp"
#include "test_util.hpp"

using namespace einstar;

namespace {

constexpr std::uint32_t kStressFrames = 10000;

}  // namespace

EINSTAR_TEST(stress_10k_frames_zero_copy) {
  SimConfig sim;
  sim.seed = 2024;
  sim.frames = kStressFrames;
  sim.trigger_period_ns = 40'000'000;
  sim.bandwidth_bytes_per_sec = 300'000'000;
  sim.require_interval_patch = false;

  PipelineConfig config;
  config.queue_depth = 16;
  config.copy_mode = CopyMode::ZeroCopy;
  config.frame_queue_capacity = 8;
  config.partial_frame_timeout_ns = 500'000'000;
  config.transfer_pool_buffers = 160;

  auto transport = std::make_unique<SimulatedTransport>(sim);
  auto pipeline =
      std::make_unique<AcquisitionPipeline>(std::move(transport), config);
  REQUIRE(pipeline->start().ok());

  std::uint32_t expected_index = 0;
  std::size_t received = 0;
  std::size_t iterations = 0;
  Frame frame;
  while (received < kStressFrames && iterations++ < 2'000'000) {
    (void)pipeline->poll(4);
    while (pipeline->try_pop_frame(frame)) {
      CHECK_EQ(frame.frame_index, expected_index);
      CHECK_EQ(frame.payload_size, kDefaultFrameSize);
      CHECK_EQ(frame.chunks.size(), 32U);
      const std::uint64_t digest = frame.payload_digest();
      CHECK_EQ(digest, simulated_frame_digest(expected_index, kDefaultFrameSize,
                                              kStreamChunkPayload));
      ++expected_index;
      ++received;
      frame = Frame{};
    }
  }
  CHECK_EQ(received, kStressFrames);
  CHECK_EQ(expected_index, kStressFrames);

  PipelineStats stats = pipeline->stats();
  CHECK_EQ(stats.frames_completed, static_cast<std::uint64_t>(kStressFrames));
  CHECK_EQ(stats.frames_dropped, 0U);
  CHECK_EQ(stats.sequence_gaps, 0U);
  CHECK_EQ(stats.partial_timeouts, 0U);
  CHECK_EQ(stats.queue_drops, 0U);
  CHECK_EQ(stats.duplicate_packets, 0U);
  CHECK_EQ(stats.orphan_packets, 0U);
  CHECK(stats.transfer_pool_peak <= stats.transfer_pool_capacity);
  CHECK(stats.transfer_latency.count > 0U);
  CHECK(stats.transfer_latency.p99_ns >= stats.transfer_latency.p50_ns);

  REQUIRE(pipeline->stop().ok());
  CHECK_EQ(pipeline->transport().outstanding(), 0U);
  stats = pipeline->stats();
  CHECK_EQ(stats.transfer_pool_in_use, 0U);
  CHECK_EQ(stats.transfer_pool_capacity, 160U);
}

EINSTAR_TEST_MAIN()
