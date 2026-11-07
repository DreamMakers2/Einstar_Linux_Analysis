// SPDX-License-Identifier: MIT
//
// Reassembly tests for the canonical wire model:
//   * camera = one-hot field in header[2] bits 3..5 (1, 2, 4 -> index 0, 1, 2)
//   * header[8] == 1 only on the LAST packet of a frame
//   * frame id is valid only on that last packet (poisoned before it here)
//   * timestamp is latched from the first packet of a frame
//   * no start flag, one in-flight frame per camera, completion on last+size
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

TestPacket make_packet(std::uint8_t camera_index, std::uint32_t frame, bool last,
                       std::size_t payload_size, std::uint8_t fill = 0x20) {
  TestPacket result;
  result.payload.resize(payload_size);
  for (std::size_t i = 0; i < payload_size; ++i) {
    result.payload[i] = static_cast<std::byte>((fill + i + camera_index) & 0xFFU);
  }
  result.packet.camera_field = camera_field_from_index(camera_index);
  result.packet.camera_index = camera_index;
  result.packet.last_of_frame = last;
  result.packet.frame_index = last ? frame : 0xFFFFFFFFU;
  result.packet.device_timestamp = 1000ULL * static_cast<std::uint64_t>(frame);
  result.packet.payload = result.payload.data();
  result.packet.payload_size = payload_size;
  result.packet.declared_length = static_cast<std::uint32_t>(32 + payload_size);
  return result;
}

ReassemblyConfig copy_config(BufferPool& pool) {
  ReassemblyConfig config;
  config.frame_size = kFrameSize;
  config.width = 10;
  config.height = 10;
  config.mode = CopyMode::Copy;
  config.frame_pool = &pool;
  config.partial_frame_timeout_ns = 1'000'000;
  return config;
}

}  // namespace

EINSTAR_TEST(reassembly_completes_on_last_packet) {
  BufferPool pool(kFrameSize, 8);
  FrameReassembler reassembler(copy_config(pool));
  auto feed = [&](std::uint32_t frame, bool last, std::size_t size,
                  std::int64_t now) {
    auto test_packet = make_packet(1, frame, last, size);
    auto result = reassembler.ingest(test_packet.packet, BufferHandle{}, 0, now);
    CHECK(result.has_value());
    if (!result.has_value()) {
      return std::optional<Frame>{};
    }
    return std::move(result->completed);
  };

  CHECK(!feed(0, false, 40, 10).has_value());
  CHECK(!feed(0, false, 40, 20).has_value());
  auto completed = feed(0, true, 20, 30);
  REQUIRE(completed.has_value());
  CHECK_EQ(completed->payload_size, kFrameSize);
  CHECK(completed->is_contiguous());
  CHECK_EQ(completed->camera_id, 1U);
  CHECK_EQ(completed->frame_index, 0U);
  // Timestamp latched from the first packet (frame id 0 -> 0).
  CHECK_EQ(completed->device_timestamp, 0U);
  CHECK_EQ(reassembler.stats().frames_completed, 1U);
  CHECK_EQ(pool.in_use(), 1U);
  completed.reset();
  CHECK_EQ(pool.in_use(), 0U);
}

EINSTAR_TEST(reassembly_timestamp_latched_from_first_packet) {
  BufferPool pool(kFrameSize, 8);
  FrameReassembler reassembler(copy_config(pool));
  auto first = make_packet(0, 7, false, 60);
  first.packet.device_timestamp = 111111;
  REQUIRE(reassembler.ingest(first.packet, BufferHandle{}, 0, 10).has_value());
  auto second = make_packet(0, 7, true, 40);
  second.packet.device_timestamp = 999999;  // must be ignored
  auto result = reassembler.ingest(second.packet, BufferHandle{}, 0, 20);
  REQUIRE(result.has_value());
  REQUIRE(result->completed.has_value());
  CHECK_EQ(result->completed->device_timestamp, 111111ULL);
  CHECK_EQ(result->completed->frame_index, 7U);
}

EINSTAR_TEST(reassembly_multi_camera_interleaved) {
  BufferPool pool(kFrameSize, 8);
  FrameReassembler reassembler(copy_config(pool));
  auto ingest = [&](std::uint8_t camera, std::uint32_t frame, bool last,
                    std::size_t size, std::int64_t now) {
    auto test_packet = make_packet(camera, frame, last, size);
    return reassembler.ingest(test_packet.packet, BufferHandle{}, 0, now);
  };
  CHECK(ingest(0, 0, false, 60, 10).has_value());
  CHECK(ingest(2, 0, false, 60, 11).has_value());
  CHECK(ingest(0, 0, true, 40, 12).has_value());
  auto second = ingest(2, 0, true, 40, 13);
  REQUIRE(second.has_value());
  CHECK(second->completed.has_value());
  CHECK_EQ(second->completed->camera_id, 2U);
  CHECK_EQ(reassembler.stats().frames_completed, 2U);
  CHECK_EQ(reassembler.stats().sequence_gaps, 0U);
}

EINSTAR_TEST(reassembly_sequence_gap_detected) {
  BufferPool pool(kFrameSize, 8);
  FrameReassembler reassembler(copy_config(pool));
  auto ingest = [&](std::uint32_t frame, std::size_t size, std::int64_t now) {
    auto test_packet = make_packet(1, frame, true, size);
    return reassembler.ingest(test_packet.packet, BufferHandle{}, 0, now);
  };
  auto first = ingest(0, kFrameSize, 10);
  REQUIRE(first.has_value());
  REQUIRE(first->completed.has_value());
  CHECK_EQ(reassembler.expected_next(1), 1U);
  REQUIRE(ingest(3, kFrameSize, 20).has_value());
  CHECK_EQ(reassembler.stats().sequence_gaps, 2U);
  CHECK_EQ(reassembler.expected_next(1), 4U);
  auto events = reassembler.take_loss_events();
  bool found = false;
  for (const auto& event : events) {
    if (event.reason == FrameLossReason::SequenceGap &&
        event.missing_frames == 2) {
      found = true;
    }
  }
  CHECK(found);
}

EINSTAR_TEST(reassembly_partial_timeout) {
  BufferPool pool(kFrameSize, 8);
  FrameReassembler reassembler(copy_config(pool));
  auto test_packet = make_packet(1, 0, false, 30);
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

EINSTAR_TEST(reassembly_header_only_and_invalid_camera) {
  BufferPool pool(kFrameSize, 8);
  FrameReassembler reassembler(copy_config(pool));
  auto header_only = make_packet(1, 0, false, 0);
  header_only.packet.payload = nullptr;
  REQUIRE(reassembler.ingest(header_only.packet, BufferHandle{}, 0, 10)
              .has_value());
  CHECK_EQ(reassembler.stats().header_only_packets, 1U);

  auto invalid = make_packet(0, 0, false, 10);
  invalid.packet.camera_index = kInvalidCamera;  // e.g. field 3 or 5
  REQUIRE(reassembler.ingest(invalid.packet, BufferHandle{}, 0, 11).has_value());
  CHECK_EQ(reassembler.stats().invalid_camera, 1U);
  CHECK_EQ(reassembler.stats().orphan_packets, 1U);
  CHECK_EQ(reassembler.inflight(), 0U);
}

EINSTAR_TEST(reassembly_incomplete_last_packet) {
  BufferPool pool(kFrameSize, 8);
  FrameReassembler reassembler(copy_config(pool));
  auto first = make_packet(1, 0, false, 30);
  REQUIRE(reassembler.ingest(first.packet, BufferHandle{}, 0, 10).has_value());
  // Last packet arrives but the frame is short: packets were lost.
  auto last = make_packet(1, 0, true, 40);
  REQUIRE(reassembler.ingest(last.packet, BufferHandle{}, 0, 20).has_value());
  CHECK_EQ(reassembler.stats().size_mismatch, 1U);
  CHECK_EQ(reassembler.stats().frames_dropped, 1U);
  CHECK_EQ(reassembler.stats().frames_completed, 0U);
  CHECK_EQ(reassembler.inflight(), 0U);
  CHECK_EQ(pool.in_use(), 0U);
}

EINSTAR_TEST(reassembly_overflow_drops_partial) {
  BufferPool pool(kFrameSize, 8);
  FrameReassembler reassembler(copy_config(pool));
  auto first = make_packet(1, 0, false, 60);
  REQUIRE(reassembler.ingest(first.packet, BufferHandle{}, 0, 10).has_value());
  auto overflow = make_packet(1, 0, false, 60);  // 120 > frame_size
  REQUIRE(reassembler.ingest(overflow.packet, BufferHandle{}, 0, 20).has_value());
  CHECK_EQ(reassembler.stats().overflow_errors, 1U);
  CHECK_EQ(reassembler.inflight(), 0U);
  CHECK_EQ(pool.in_use(), 0U);
}

EINSTAR_TEST(reassembly_stale_frame_rejected) {
  BufferPool pool(kFrameSize, 8);
  FrameReassembler reassembler(copy_config(pool));
  auto ingest = [&](std::uint32_t frame, std::int64_t now) {
    auto p = make_packet(1, frame, true, kFrameSize);
    return reassembler.ingest(p.packet, BufferHandle{}, 0, now);
  };
  REQUIRE(ingest(5, 10).has_value());
  auto duplicate = ingest(5, 20);
  REQUIRE(duplicate.has_value());
  CHECK(!duplicate->completed.has_value());
  CHECK_EQ(reassembler.stats().stale_packets, 1U);
  CHECK_EQ(reassembler.stats().frames_completed, 1U);
}

EINSTAR_TEST(reassembly_zero_copy_chunks) {
  BufferPool pool(64, 8);
  ReassemblyConfig config;
  config.frame_size = kFrameSize;
  config.mode = CopyMode::ZeroCopy;
  config.frame_pool = nullptr;
  FrameReassembler reassembler(config);
  auto ingest = [&](bool last, std::size_t size,
                    std::int64_t now) -> Result<FrameReassembler::IngestResult> {
    auto test_packet = make_packet(1, 0, last, size);
    BufferHandle storage = pool.acquire();
    if (!storage.valid()) {
      return fail(ErrorCode::Exhausted, "test pool exhausted");
    }
    std::memcpy(storage.data(), test_packet.payload.data(), size);
    test_packet.packet.payload = storage.data();
    return reassembler.ingest(test_packet.packet, std::move(storage), 0, now);
  };
  REQUIRE(ingest(false, 60, 10).has_value());
  auto result = ingest(true, 40, 20);
  REQUIRE(result.has_value());
  REQUIRE(result->completed.has_value());
  Frame frame = std::move(*result->completed);
  CHECK_EQ(frame.chunks.size(), 2U);
  CHECK(!frame.is_contiguous());
  CHECK_EQ(frame.payload_size, kFrameSize);
  std::vector<std::byte> gathered(kFrameSize);
  CHECK_EQ(frame.copy_payload_to(gathered), kFrameSize);
  CHECK_EQ(std::to_integer<std::uint8_t>(gathered[0]),
           static_cast<std::uint8_t>(0x20 + 1));
  CHECK_EQ(pool.in_use(), 2U);
  frame = Frame{};
  CHECK_EQ(pool.in_use(), 0U);
}

EINSTAR_TEST(reassembly_three_cameras_inflight) {
  BufferPool pool(kFrameSize, 8);
  FrameReassembler reassembler(copy_config(pool));
  for (std::uint8_t camera : {0, 1, 2}) {
    auto packet = make_packet(camera, 0, false, 10);
    REQUIRE(reassembler.ingest(packet.packet, BufferHandle{}, 0, 10)
                .has_value());
  }
  CHECK_EQ(reassembler.inflight(), 3U);
  // The fourth camera index is not a valid selector.
  auto bad = make_packet(0, 0, false, 10);
  bad.packet.camera_index = 3;
  REQUIRE(reassembler.ingest(bad.packet, BufferHandle{}, 0, 11).has_value());
  CHECK_EQ(reassembler.stats().invalid_camera, 1U);
}

EINSTAR_TEST(reassembly_flush) {
  BufferPool pool(kFrameSize, 8);
  FrameReassembler reassembler(copy_config(pool));
  auto packet = make_packet(1, 0, false, 10);
  REQUIRE(reassembler.ingest(packet.packet, BufferHandle{}, 0, 10).has_value());
  const auto events = reassembler.flush(20);
  CHECK_EQ(events.size(), 1U);
  CHECK_EQ(reassembler.stats().frames_dropped, 1U);
  CHECK_EQ(pool.in_use(), 0U);
}

EINSTAR_TEST_MAIN()
