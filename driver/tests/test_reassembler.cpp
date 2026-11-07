// SPDX-License-Identifier: MIT
#include <algorithm>
#include <cstring>
#include <vector>

#include "einstar/frame.hpp"
#include "einstar/reassembler.hpp"
#include "test_util.hpp"

using namespace einstar;

namespace {

constexpr std::uint32_t kFrameSize = 100;

struct TestPacket {
  StreamPacket packet;
  std::vector<std::byte> payload;
};

TestPacket make_packet(std::uint8_t camera, std::uint32_t frame, bool first,
                       std::size_t payload_size, std::uint8_t fill = 0x20) {
  TestPacket result;
  result.payload.resize(payload_size);
  for (std::size_t i = 0; i < payload_size; ++i) {
    result.payload[i] =
        static_cast<std::byte>((fill + frame + i) & 0xFFU);
  }
  result.packet.camera_id = camera;
  result.packet.raw_camera_byte = camera;
  result.packet.first_of_frame = first;
  result.packet.frame_index = frame;
  result.packet.device_timestamp =
      1000ULL * static_cast<std::uint64_t>(frame);
  result.packet.payload = result.payload.data();
  result.packet.payload_size = payload_size;
  result.packet.declared_length =
      static_cast<std::uint32_t>(32 + payload_size);
  return result;
}

ReassemblyConfig copy_config(BufferPool& pool) {
  ReassemblyConfig config;
  config.frame_size = kFrameSize;
  config.width = 10;
  config.height = 10;
  config.mode = CopyMode::Copy;
  config.frame_pool = &pool;
  config.max_inflight_frames = 8;
  config.partial_frame_timeout_ns = 1'000'000;
  return config;
}

}  // namespace

EINSTAR_TEST(reassembly_completes_across_chunks) {
  BufferPool pool(kFrameSize, 8);
  FrameReassembler reassembler(copy_config(pool));
  auto feed = [&](std::uint32_t frame, bool first, std::size_t size,
                  std::int64_t now) {
    auto test_packet = make_packet(1, frame, first, size);
    test_packet.packet.payload = test_packet.payload.data();
    auto result = reassembler.ingest(test_packet.packet, BufferHandle{}, 0, now);
    CHECK(result.has_value());
    if (!result.has_value()) {
      return std::optional<Frame>{};
    }
    return std::move(result->completed);
  };

  CHECK(!feed(0, true, 40, 10).has_value());
  CHECK(!feed(0, false, 40, 20).has_value());
  auto completed = feed(0, false, 20, 30);
  REQUIRE(completed.has_value());
  CHECK_EQ(completed->payload_size, kFrameSize);
  CHECK(completed->is_contiguous());
  CHECK_EQ(completed->camera_id, 1U);
  CHECK_EQ(completed->frame_index, 0U);
  CHECK_EQ(completed->device_timestamp, 0U);
  CHECK_EQ(reassembler.stats().frames_completed, 1U);
  CHECK_EQ(pool.in_use(), 1U);
  completed.reset();
  CHECK_EQ(pool.in_use(), 0U);
}

EINSTAR_TEST(reassembly_multi_camera_interleaved) {
  BufferPool pool(kFrameSize, 8);
  FrameReassembler reassembler(copy_config(pool));
  auto ingest = [&](std::uint8_t camera, std::uint32_t frame, bool first,
                    std::size_t size, std::int64_t now) {
    auto test_packet = make_packet(camera, frame, first, size);
    test_packet.packet.payload = test_packet.payload.data();
    return reassembler.ingest(test_packet.packet, BufferHandle{}, 0, now);
  };
  CHECK(ingest(1, 0, true, 60, 10).has_value());
  CHECK(ingest(2, 0, true, 60, 11).has_value());
  CHECK(ingest(1, 0, false, 40, 12).has_value());
  auto second = ingest(2, 0, false, 40, 13);
  REQUIRE(second.has_value());
  CHECK(second->completed.has_value());
  CHECK_EQ(second->completed->camera_id, 2U);
  CHECK_EQ(reassembler.stats().frames_completed, 2U);
  CHECK_EQ(reassembler.stats().sequence_gaps, 0U);
}

EINSTAR_TEST(reassembly_sequence_gap_detected) {
  BufferPool pool(kFrameSize, 8);
  FrameReassembler reassembler(copy_config(pool));
  auto ingest = [&](std::uint32_t frame, bool first, std::size_t size,
                    std::int64_t now) {
    auto test_packet = make_packet(1, frame, first, size);
    test_packet.packet.payload = test_packet.payload.data();
    return reassembler.ingest(test_packet.packet, BufferHandle{}, 0, now);
  };
  auto first = ingest(0, true, kFrameSize, 10);
  REQUIRE(first.has_value());
  REQUIRE(first->completed.has_value());
  REQUIRE(ingest(3, true, 10, 20).has_value());
  CHECK_EQ(reassembler.stats().sequence_gaps, 2U);
  auto events = reassembler.take_loss_events();
  bool found = false;
  for (const auto& event : events) {
    if (event.reason == FrameLossReason::SequenceGap && event.missing_frames == 2) {
      found = true;
    }
  }
  CHECK(found);
}

EINSTAR_TEST(reassembly_partial_timeout) {
  BufferPool pool(kFrameSize, 8);
  FrameReassembler reassembler(copy_config(pool));
  auto test_packet = make_packet(1, 0, true, 30);
  test_packet.packet.payload = test_packet.payload.data();
  REQUIRE(reassembler.ingest(test_packet.packet, BufferHandle{}, 0, 100)
              .has_value());
  CHECK_EQ(reassembler.inflight(), 1U);
  const auto events = reassembler.expire(2'000'000);
  CHECK_EQ(events.size(), 1U);
  CHECK_EQ(reassembler.stats().partial_timeouts, 1U);
  CHECK_EQ(reassembler.stats().frames_dropped, 1U);
  CHECK_EQ(reassembler.inflight(), 0U);
  CHECK_EQ(pool.in_use(), 0U);
}

EINSTAR_TEST(reassembly_duplicate_header_and_orphan) {
  BufferPool pool(kFrameSize, 8);
  FrameReassembler reassembler(copy_config(pool));
  auto packet = make_packet(1, 0, true, 50);
  packet.packet.payload = packet.payload.data();
  REQUIRE(reassembler.ingest(packet.packet, BufferHandle{}, 0, 10).has_value());
  auto header_only = make_packet(1, 0, true, 0);
  header_only.packet.payload = nullptr;
  REQUIRE(reassembler.ingest(header_only.packet, BufferHandle{}, 0, 11)
              .has_value());
  CHECK_EQ(reassembler.stats().duplicate_packets, 1U);

  auto orphan = make_packet(1, 1, false, 10);
  orphan.packet.payload = orphan.payload.data();
  REQUIRE(reassembler.ingest(orphan.packet, BufferHandle{}, 0, 12).has_value());
  CHECK_EQ(reassembler.stats().orphan_packets, 1U);
}

EINSTAR_TEST(reassembly_restart_and_overflow) {
  BufferPool pool(kFrameSize, 8);
  FrameReassembler reassembler(copy_config(pool));
  auto first = make_packet(1, 0, true, 30);
  first.packet.payload = first.payload.data();
  REQUIRE(reassembler.ingest(first.packet, BufferHandle{}, 0, 10).has_value());
  auto restart = make_packet(1, 0, true, 40);
  restart.packet.payload = restart.payload.data();
  REQUIRE(reassembler.ingest(restart.packet, BufferHandle{}, 0, 20).has_value());
  CHECK_EQ(reassembler.stats().frame_restarts, 1U);

  auto overflow = make_packet(1, 0, false, kFrameSize);
  overflow.packet.payload = overflow.payload.data();
  auto result = reassembler.ingest(overflow.packet, BufferHandle{}, 0, 30);
  CHECK(!result.has_value());
  CHECK_EQ(reassembler.stats().overflow_errors, 1U);
}

EINSTAR_TEST(reassembly_zero_copy_chunks) {
  BufferPool pool(64, 8);
  ReassemblyConfig config;
  config.frame_size = kFrameSize;
  config.mode = CopyMode::ZeroCopy;
  config.frame_pool = nullptr;
  FrameReassembler reassembler(config);
  auto ingest = [&](bool first, std::size_t size,
                    std::int64_t now) -> Result<FrameReassembler::IngestResult> {
    auto test_packet = make_packet(1, 0, first, size);
    test_packet.packet.payload = test_packet.payload.data();
    BufferHandle storage = pool.acquire();
    if (!storage.valid()) {
      return fail(ErrorCode::Exhausted, "test pool exhausted");
    }
    std::memcpy(storage.data(), test_packet.payload.data(), size);
    test_packet.packet.payload = storage.data();
    return reassembler.ingest(test_packet.packet, std::move(storage), 0, now);
  };
  REQUIRE(ingest(true, 60, 10).has_value());
  auto result = ingest(false, 40, 20);
  REQUIRE(result.has_value());
  REQUIRE(result->completed.has_value());
  Frame frame = std::move(*result->completed);
  CHECK_EQ(frame.chunks.size(), 2U);
  CHECK(!frame.is_contiguous());
  CHECK_EQ(frame.payload_size, kFrameSize);
  std::vector<std::byte> gathered(kFrameSize);
  CHECK_EQ(frame.copy_payload_to(gathered), kFrameSize);
  // First chunk must be the 60 bytes, second 40 bytes.
  CHECK_EQ(std::to_integer<std::uint8_t>(gathered[0]),
           static_cast<std::uint8_t>(0x20 + 0));
  CHECK_EQ(gathered.size(), 100U);
  CHECK_EQ(pool.in_use(), 2U);
  frame = Frame{};  // release chunks back to the pool
  CHECK_EQ(pool.in_use(), 0U);
}

EINSTAR_TEST(reassembly_capacity_eviction) {
  BufferPool pool(kFrameSize, 8);
  auto config = copy_config(pool);
  config.max_inflight_frames = 2;
  FrameReassembler reassembler(config);
  for (std::uint8_t camera : {1, 2, 3}) {
    auto packet = make_packet(camera, 0, true, 10);
    packet.packet.payload = packet.payload.data();
    REQUIRE(reassembler.ingest(packet.packet, BufferHandle{}, 0, 10)
                .has_value());
  }
  CHECK_EQ(reassembler.stats().capacity_evictions, 1U);
  CHECK_EQ(reassembler.inflight(), 2U);
}

EINSTAR_TEST(reassembly_flush) {
  BufferPool pool(kFrameSize, 8);
  FrameReassembler reassembler(copy_config(pool));
  auto packet = make_packet(4, 0, true, 10);
  packet.packet.payload = packet.payload.data();
  REQUIRE(reassembler.ingest(packet.packet, BufferHandle{}, 0, 10).has_value());
  const auto events = reassembler.flush(20);
  CHECK_EQ(events.size(), 1U);
  CHECK_EQ(reassembler.stats().frames_dropped, 1U);
  CHECK_EQ(pool.in_use(), 0U);
}

EINSTAR_TEST_MAIN()
