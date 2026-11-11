// SPDX-License-Identifier: MIT
#include <cstdio>
#include <filesystem>
#include <utility>
#include <vector>

#include "einstar/pipeline.hpp"
#include "einstar/sim.hpp"
#include "test_util.hpp"

using namespace einstar;

namespace {

struct FrameInfo {
  std::uint32_t index = 0;
  std::uint64_t digest = 0;
  std::uint64_t device_timestamp = 0;
  std::uint32_t payload_size = 0;
  std::uint32_t chunks = 0;
  bool contiguous = false;
};

struct RunOutcome {
  std::unique_ptr<AcquisitionPipeline> pipeline;
  std::vector<FrameInfo> frames;
  PipelineStats stats;
  bool started = false;
};

FrameInfo describe(Frame& frame) {
  FrameInfo info;
  info.index = frame.frame_index;
  info.digest = frame.payload_digest();
  info.device_timestamp = frame.device_timestamp;
  info.payload_size = frame.payload_size;
  info.chunks = static_cast<std::uint32_t>(frame.chunks.size());
  info.contiguous = frame.is_contiguous();
  return info;
}

std::unique_ptr<AcquisitionPipeline> make_pipeline(SimConfig sim,
                                                   PipelineConfig config) {
  auto transport = std::make_unique<SimulatedTransport>(sim);
  return std::make_unique<AcquisitionPipeline>(std::move(transport), config);
}

RunOutcome run_until(SimConfig sim, PipelineConfig config, std::size_t target,
                     std::size_t max_iterations = 200000) {
  RunOutcome outcome;
  outcome.pipeline = make_pipeline(sim, config);
  const Status started = outcome.pipeline->start();
  CHECK(started.ok());
  outcome.started = started.ok();
  if (!outcome.started) {
    return outcome;
  }
  std::size_t iterations = 0;
  Frame frame;
  while (outcome.frames.size() < target && iterations++ < max_iterations) {
    outcome.pipeline->poll(2);
    while (outcome.pipeline->try_pop_frame(frame)) {
      outcome.frames.push_back(describe(frame));
      frame = Frame{};
    }
  }
  const Status stopped = outcome.pipeline->stop();
  CHECK(stopped.ok());
  while (outcome.pipeline->try_pop_frame(frame)) {
    outcome.frames.push_back(describe(frame));
    frame = Frame{};
  }
  outcome.stats = outcome.pipeline->stats();
  return outcome;
}

SimConfig clean_sim(std::uint32_t frames) {
  SimConfig sim;
  sim.seed = 1234;
  sim.frames = frames;
  sim.trigger_period_ns = 40'000'000;
  sim.bandwidth_bytes_per_sec = 300'000'000;
  sim.expect_interval_quirk = false;
  sim.camera_count = 1;  // single-camera pipeline expectations
  sim.first_camera_index = 0;
  return sim;
}

PipelineConfig base_config(CopyMode mode = CopyMode::Copy) {
  PipelineConfig config;
  config.queue_depth = 4;
  config.frame_queue_capacity = 8;
  config.partial_frame_timeout_ns = 200'000'000;
  config.copy_mode = mode;
  return config;
}

}  // namespace

EINSTAR_TEST(pipeline_clean_capture) {
  SimConfig sim = clean_sim(12);
  PipelineConfig config = base_config();
  const RunOutcome outcome = run_until(sim, config, 12);
  REQUIRE(outcome.started);
  REQUIRE(outcome.frames.size() == 12U);
  for (std::size_t i = 0; i < outcome.frames.size(); ++i) {
    const FrameInfo& frame = outcome.frames[i];
    CHECK_EQ(frame.index, static_cast<std::uint32_t>(i));
    CHECK(frame.contiguous);
    CHECK_EQ(frame.payload_size, kDefaultFrameSize);
    CHECK_EQ(frame.digest, simulated_frame_digest(
                               static_cast<std::uint32_t>(i), kDefaultFrameSize,
                               kStreamChunkPayload));
    CHECK_EQ(frame.device_timestamp,
             static_cast<std::uint64_t>(i) * 40'000'000ULL);
  }
  CHECK_EQ(outcome.stats.frames_completed, 12U);
  CHECK_EQ(outcome.stats.sequence_gaps, 0U);
  CHECK_EQ(outcome.stats.partial_timeouts, 0U);
  CHECK_EQ(outcome.stats.frames_dropped, 0U);
  CHECK_EQ(outcome.stats.queue_drops, 0U);
  CHECK_EQ(outcome.stats.transfer_stalls, 0U);
  CHECK(outcome.stats.packets_parsed >= 12U * 32U);
  CHECK_EQ(outcome.stats.transfer_pool_in_use, 0U);
}

EINSTAR_TEST(pipeline_length_prefixed_capture) {
  // The legacy [u32 BE len][header][payload] format must still work end to end:
  // simulate it, record it (metadata identifies the framing), then replay the
  // recording through a LengthPrefixed pipeline and get identical frames.
  const std::string path = "/tmp/einstar_pipeline_lenprefixed.bin";
  std::error_code ignored;
  std::filesystem::remove(path, ignored);

  SimConfig sim = clean_sim(3);
  sim.framing = FramingMode::LengthPrefixed;
  PipelineConfig config = base_config();
  config.framing = FramingMode::LengthPrefixed;
  config.enable_recording = true;
  config.recording_path = path;
  config.recording_metadata = "legacy";
  const RunOutcome recorded = run_until(sim, config, 3);
  REQUIRE(recorded.frames.size() == 3U);
  for (std::size_t i = 0; i < recorded.frames.size(); ++i) {
    const FrameInfo& frame = recorded.frames[i];
    CHECK_EQ(frame.index, static_cast<std::uint32_t>(i));
    CHECK(frame.contiguous);
    CHECK_EQ(frame.payload_size, kDefaultFrameSize);
    CHECK_EQ(frame.digest, simulated_frame_digest(
                               static_cast<std::uint32_t>(i), kDefaultFrameSize,
                               kStreamChunkPayload));
    CHECK_EQ(frame.device_timestamp,
             static_cast<std::uint64_t>(i) * 40'000'000ULL);
  }
  CHECK_EQ(recorded.stats.frames_completed, 3U);
  CHECK_EQ(recorded.stats.parse_errors, 0U);
  CHECK(recorded.stats.packets_parsed >= 3U * 32U);
  CHECK_EQ(recorded.stats.frames_dropped, 0U);
  CHECK_EQ(recorded.stats.sequence_gaps, 0U);

  auto recording = recording::read_recording(path);
  REQUIRE(recording.has_value());
  CHECK_EQ(recording->header.metadata,
           std::string("legacy;framing=length-prefixed"));

  SimConfig replay_sim = clean_sim(0);
  replay_sim.framing = FramingMode::LengthPrefixed;
  replay_sim.replay = recording::make_replay_source(*recording);
  PipelineConfig replay_config = base_config();
  replay_config.framing = FramingMode::LengthPrefixed;
  replay_config.enable_recording = false;
  const RunOutcome replayed = run_until(replay_sim, replay_config, 3, 200000);
  REQUIRE(replayed.frames.size() == 3U);
  for (std::size_t i = 0; i < 3; ++i) {
    CHECK_EQ(replayed.frames[i].index, recorded.frames[i].index);
    CHECK_EQ(replayed.frames[i].digest, recorded.frames[i].digest);
    CHECK_EQ(replayed.frames[i].device_timestamp,
             recorded.frames[i].device_timestamp);
  }
  std::filesystem::remove(path, ignored);
}

EINSTAR_TEST(pipeline_zero_copy_capture) {
  SimConfig sim = clean_sim(6);
  PipelineConfig config = base_config(CopyMode::ZeroCopy);
  const RunOutcome outcome = run_until(sim, config, 6);
  REQUIRE(outcome.frames.size() == 6U);
  for (std::size_t i = 0; i < outcome.frames.size(); ++i) {
    const FrameInfo& frame = outcome.frames[i];
    CHECK(!frame.contiguous);
    CHECK_EQ(frame.chunks, 32U);
    CHECK_EQ(frame.digest, simulated_frame_digest(
                               static_cast<std::uint32_t>(i), kDefaultFrameSize,
                               kStreamChunkPayload));
  }
  CHECK_EQ(outcome.stats.transfer_pool_in_use, 0U);
  CHECK(outcome.stats.transfer_pool_peak <= outcome.stats.transfer_pool_capacity);
}

EINSTAR_TEST(pipeline_drop_oldest_backpressure) {
  SimConfig sim = clean_sim(400);
  PipelineConfig config = base_config();
  config.frame_queue_capacity = 3;
  config.frame_backpressure = BackpressurePolicy::DropOldest;
  auto pipeline = make_pipeline(sim, config);
  REQUIRE(pipeline->start().ok());
  for (int i = 0; i < 1500; ++i) {
    (void)pipeline->poll(2);
  }
  const PipelineStats stats = pipeline->stats();
  CHECK(stats.queue_drops > 0U);
  CHECK(stats.frame_queue_depth <= 3U);
  CHECK(stats.frames_completed > 0U);
  REQUIRE(pipeline->stop().ok());
  std::vector<Frame> remaining;
  (void)pipeline->pop_frames(remaining, 16);
  CHECK(remaining.size() <= 3U);
  remaining.clear();
  CHECK_EQ(pipeline->stats().transfer_pool_in_use, 0U);
}

EINSTAR_TEST(pipeline_partial_frame_timeout) {
  SimConfig sim = clean_sim(1);
  sim.faults.truncate_frame_probability = 1.0;
  PipelineConfig config = base_config();
  config.partial_frame_timeout_ns = 1'000'000;  // 1 ms
  auto pipeline = make_pipeline(sim, config);
  REQUIRE(pipeline->start().ok());
  for (int i = 0; i < 500; ++i) {
    (void)pipeline->poll(1);
  }
  const PipelineStats stats = pipeline->stats();
  CHECK(stats.partial_timeouts >= 1U);
  CHECK_EQ(stats.frames_completed, 0U);
  REQUIRE(pipeline->stop().ok());
  CHECK_EQ(pipeline->transport().outstanding(), 0U);
}

EINSTAR_TEST(pipeline_cancellation_leaves_no_transfers) {
  SimConfig sim = clean_sim(0);
  sim.transfer_latency_ns = 1'000'000'000;  // 1 s
  PipelineConfig config = base_config();
  config.queue_depth = 8;
  auto pipeline = make_pipeline(sim, config);
  REQUIRE(pipeline->start().ok());
  CHECK_EQ(pipeline->transport().outstanding(), 8U);
  REQUIRE(pipeline->stop().ok());
  CHECK_EQ(pipeline->transport().outstanding(), 0U);
  const PipelineStats stats = pipeline->stats();
  CHECK_EQ(stats.transfers_cancelled, 8U);
  CHECK_EQ(stats.transfer_pool_in_use, 0U);
}

EINSTAR_TEST(pipeline_stall_recovery) {
  SimConfig sim = clean_sim(8);
  sim.faults.stall_probability = 0.4;
  PipelineConfig config = base_config();
  const RunOutcome outcome = run_until(sim, config, 8, 400000);
  REQUIRE(outcome.frames.size() == 8U);
  CHECK(outcome.stats.transfer_stalls > 0U);
  CHECK_EQ(outcome.stats.frames_dropped, 0U);
  CHECK_EQ(outcome.stats.sequence_gaps, 0U);
}

EINSTAR_TEST(pipeline_timeout_recovery) {
  SimConfig sim = clean_sim(6);
  sim.faults.timeout_probability = 0.3;
  PipelineConfig config = base_config();
  const RunOutcome outcome = run_until(sim, config, 6, 400000);
  REQUIRE(outcome.frames.size() == 6U);
  CHECK(outcome.stats.transfer_timeouts > 0U);
  CHECK_EQ(outcome.stats.frames_dropped, 0U);
}

EINSTAR_TEST(pipeline_recording_and_replay) {
  const std::string path = "/tmp/einstar_pipeline_record.bin";
  std::error_code ignored;
  std::filesystem::remove(path, ignored);

  SimConfig sim = clean_sim(5);
  PipelineConfig config = base_config();
  config.enable_recording = true;
  config.recording_path = path;
  config.recording_metadata = "pipeline-test";
  const RunOutcome recorded = run_until(sim, config, 5);
  REQUIRE(recorded.frames.size() == 5U);

  auto recording = recording::read_recording(path);
  REQUIRE(recording.has_value());
  CHECK(recording->has_trailer);
  CHECK(!recording->transfers.empty());
  // The pipeline stamps the wire framing into the metadata so replay does not
  // have to guess; the user-supplied metadata is preserved.
  CHECK_EQ(recording->header.metadata,
           std::string("pipeline-test;framing=raw"));

  SimConfig replay_sim = clean_sim(0);
  replay_sim.replay = recording::make_replay_source(*recording);
  PipelineConfig replay_config = base_config();
  replay_config.enable_recording = false;
  const RunOutcome replayed = run_until(replay_sim, replay_config, 5, 200000);
  REQUIRE(replayed.frames.size() == 5U);
  for (std::size_t i = 0; i < 5; ++i) {
    CHECK_EQ(replayed.frames[i].index, recorded.frames[i].index);
    CHECK_EQ(replayed.frames[i].digest, recorded.frames[i].digest);
  }
  std::filesystem::remove(path, ignored);
}

EINSTAR_TEST(pipeline_callback_mode) {
  SimConfig sim = clean_sim(4);
  PipelineConfig config = base_config();
  auto pipeline = make_pipeline(sim, config);
  std::vector<std::uint64_t> digests;
  pipeline->set_frame_callback([&](const Frame& frame) {
    digests.push_back(frame.payload_digest());
  });
  REQUIRE(pipeline->start().ok());
  std::size_t iterations = 0;
  while (digests.size() < 4 && iterations++ < 100000) {
    (void)pipeline->poll(2);
  }
  REQUIRE(pipeline->stop().ok());
  CHECK_EQ(digests.size(), 4U);
  for (std::size_t i = 0; i < digests.size(); ++i) {
    CHECK_EQ(digests[i], simulated_frame_digest(
                             static_cast<std::uint32_t>(i), kDefaultFrameSize,
                             kStreamChunkPayload));
  }
}

EINSTAR_TEST(pipeline_cross_frame_reordering) {
  // The wire carries no frame id until the last packet, so two in-flight
  // frames of the SAME camera are indistinguishable by design.  Cross-frame
  // interleaving is therefore modelled between two cameras, which is legal and
  // is what the device does when it streams all sensors in one trigger group.
  SimConfig sim = clean_sim(10);
  sim.camera_count = 2;
  sim.faults.reorder_probability = 1.0;  // interleave every consecutive pair
  PipelineConfig config = base_config();
  const RunOutcome outcome = run_until(sim, config, 10, 400000);
  REQUIRE(outcome.frames.size() == 10U);
  std::vector<std::uint64_t> got;
  for (const auto& frame : outcome.frames) {
    got.push_back(frame.digest);
  }
  std::vector<std::uint64_t> expected;
  for (std::uint32_t id = 0; id < 5; ++id) {
    expected.push_back(simulated_frame_digest(id, kDefaultFrameSize,
                                              kStreamChunkPayload));
    expected.push_back(expected.back());  // one frame per camera per id
  }
  std::sort(got.begin(), got.end());
  std::sort(expected.begin(), expected.end());
  CHECK(got == expected);
  CHECK_EQ(outcome.stats.frames_dropped, 0U);
  CHECK_EQ(outcome.stats.sequence_gaps, 0U);
}

EINSTAR_TEST(pipeline_deterministic) {
  SimConfig sim = clean_sim(6);
  sim.faults.short_read_probability = 0.2;
  sim.faults.stall_probability = 0.1;
  const PipelineConfig config = base_config();
  const RunOutcome first = run_until(sim, config, 6, 400000);
  const RunOutcome second = run_until(sim, config, 6, 400000);
  REQUIRE(first.frames.size() == 6U);
  REQUIRE(second.frames.size() == 6U);
  for (std::size_t i = 0; i < 6; ++i) {
    CHECK_EQ(first.frames[i].index, second.frames[i].index);
    CHECK_EQ(first.frames[i].digest, second.frames[i].digest);
    CHECK_EQ(first.frames[i].device_timestamp,
             second.frames[i].device_timestamp);
  }
  CHECK_EQ(first.stats.frames_completed, second.stats.frames_completed);
  CHECK_EQ(first.stats.transfer_stalls, second.stats.transfer_stalls);
}

EINSTAR_TEST_MAIN()
