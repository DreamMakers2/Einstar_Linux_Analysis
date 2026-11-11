// SPDX-License-Identifier: MIT
//
// Packet framing tests.
//
// The hardware-verified wire format (2026-11-11) is RAW:
//
//   [32-byte header][payload]        no u32 length prefix
//
// 9,660/9,660 live USB transfers were exactly 40,992 = 32 + 40,960 bytes and
// the frame-relative packet index (header bytes 9..11, u24 BE) makes the raw
// format self-delimiting: packet `i` carries min(40,960, frame_size - i*40,960)
// payload bytes.  The legacy `[u32 BE len][header][payload]` form is still
// parsed for old recordings; the tests that specifically exercise it construct
// a LengthPrefixed framer explicitly.
#include <algorithm>
#include <array>
#include <cstdint>
#include <cstring>
#include <vector>

#include "einstar/frame.hpp"
#include "einstar/packet.hpp"
#include "einstar/reassembler.hpp"
#include "test_util.hpp"

using namespace einstar;

namespace {

// Build a canonical raw wire packet: one-hot camera in header[2] bits 3..5,
// sub-index in bits 0..2, packet index as u24 BE at header[9..11], header[8] ==
// 1 only on the last packet, frame id at header[12] (poisoned on non-last
// packets, as the device leaves it invalid until the final packet), timestamp
// at header[16].
std::vector<std::byte> make_packet(std::uint8_t camera_field, std::uint32_t frame,
                                   std::uint64_t timestamp, bool last,
                                   std::uint8_t sub, std::size_t payload_size,
                                   std::uint8_t base = 0x40,
                                   std::uint32_t packet_index = 0) {
  std::vector<std::byte> wire(kStreamHeaderSize + payload_size);
  std::byte* header = wire.data();
  header[kHdrCameraByte] = static_cast<std::byte>(
      static_cast<std::uint8_t>((camera_field & 0x07U) << 3U) |
      static_cast<std::uint8_t>(sub & 0x07U));
  header[kHdrLastFlag] = static_cast<std::byte>(last ? 1 : 0);
  header[kHdrPacketIndex] =
      static_cast<std::byte>((packet_index >> 16U) & 0xFFU);
  header[kHdrPacketIndex + 1] =
      static_cast<std::byte>((packet_index >> 8U) & 0xFFU);
  header[kHdrPacketIndex + 2] =
      static_cast<std::byte>(packet_index & 0xFFU);
  store_be32(header + kHdrFrameId, last ? frame : 0xFFFFFFFFU);
  store_be64(header + kHdrTimestamp, timestamp);
  for (std::size_t i = 0; i < payload_size; ++i) {
    wire[kStreamHeaderSize + i] =
        static_cast<std::byte>((base + i + sub) & 0xFFU);
  }
  return wire;
}

// Wrap a raw packet in the legacy [u32 BE len][header][payload] form.  This is
// only used to prove the LengthPrefixed parser still handles old recordings;
// real hardware never sends the prefix.
std::vector<std::byte> make_legacy_packet(const std::vector<std::byte>& raw) {
  std::vector<std::byte> wire(kStreamLengthSize + raw.size());
  store_be32(wire.data(), static_cast<std::uint32_t>(raw.size()));
  std::memcpy(wire.data() + kStreamLengthSize, raw.data(), raw.size());
  return wire;
}

std::vector<std::byte> make_legacy_packet(std::uint8_t camera_field,
                                          std::uint32_t frame,
                                          std::uint64_t timestamp, bool last,
                                          std::uint8_t sub,
                                          std::size_t payload_size,
                                          std::uint8_t base = 0x40) {
  return make_legacy_packet(
      make_packet(camera_field, frame, timestamp, last, sub, payload_size, base));
}

// A header-only packet is unrepresentable in raw framing (a raw packet's
// payload length is derived from its frame-relative index), so the legacy form
// is the only way to feed payload_size == 0.
std::vector<std::byte> make_legacy_header_only(std::uint8_t camera_field) {
  std::vector<std::byte> wire(kStreamLengthSize + kStreamHeaderSize);
  store_be32(wire.data(), static_cast<std::uint32_t>(kStreamHeaderSize));
  std::byte* header = wire.data() + kStreamLengthSize;
  header[kHdrCameraByte] =
      static_cast<std::byte>(static_cast<std::uint8_t>((camera_field & 0x07U) << 3U));
  header[kHdrLastFlag] = std::byte{1};
  return wire;
}

struct Collector {
  std::vector<StreamPacket> packets;
  void operator()(const StreamPacket& packet) { packets.push_back(packet); }
};

}  // namespace

EINSTAR_TEST(packet_single) {
  // Payload of packet 0 is min(40,960, frame_size); a 100-byte synthetic frame
  // lets the assertions keep using a small payload while the header layout and
  // raw (no length prefix) framing stay hardware-accurate.
  PacketFramer framer(kDefaultMaxPacketBytes, FramingMode::Raw, 100);
  FramerStats stats;
  const auto wire = make_packet(4, 0x11223344, 0x0102030405060708ULL, true, 1,
                                100, 0x10);
  std::vector<StreamPacket> packets;
  const std::size_t count = framer.consume(
      std::span<const std::byte>(wire),
      [&](const StreamPacket& packet) { packets.push_back(packet); }, stats);
  CHECK_EQ(count, 1U);
  REQUIRE(packets.size() == 1U);
  const StreamPacket& packet = packets[0];
  CHECK_EQ(packet.camera_index, 2U);
  CHECK_EQ(packet.sub_index, 1U);
  CHECK_EQ(packet.packet_index, 0U);
  CHECK(packet.last_of_frame);
  CHECK_EQ(packet.frame_index, 0x11223344U);
  CHECK_EQ(packet.device_timestamp, 0x0102030405060708ULL);
  CHECK_EQ(packet.payload_size, 100U);
  CHECK_EQ(packet.declared_length, 132U);
  CHECK(!packet.staged);
  CHECK(std::to_integer<std::uint8_t>(packet.payload[0]) ==
        static_cast<std::uint8_t>(0x10 + 1));
  CHECK_EQ(stats.packets, 1U);
  CHECK_EQ(stats.protocol_errors, 0U);
}

EINSTAR_TEST(packet_multiple_in_one_transfer) {
  // Three coalesced raw packets in one transfer.  A 64-byte frame makes packet
  // index 0 imply a 64-byte payload for each of them.
  PacketFramer framer(kDefaultMaxPacketBytes, FramingMode::Raw, 64);
  FramerStats stats;
  std::vector<std::byte> wire;
  for (int i = 0; i < 3; ++i) {
    const auto packet = make_packet(1, 7, 1000 + static_cast<std::uint64_t>(i),
                                    i == 2, static_cast<std::uint8_t>(i), 64);
    wire.insert(wire.end(), packet.begin(), packet.end());
  }
  std::vector<StreamPacket> packets;
  framer.consume(std::span<const std::byte>(wire),
                 [&](const StreamPacket& packet) { packets.push_back(packet); },
                 stats);
  REQUIRE(packets.size() == 3U);
  CHECK_EQ(stats.packets, 3U);
  CHECK_EQ(packets[0].payload_size, 64U);
  CHECK_EQ(packets[0].packet_index, 0U);
  CHECK_EQ(packets[2].packet_index, 0U);
  CHECK_EQ(packets[2].declared_length, 96U);
  CHECK_EQ(framer.buffered_bytes(), 0U);
}

EINSTAR_TEST(packet_header_split_across_transfers) {
  PacketFramer framer(kDefaultMaxPacketBytes, FramingMode::Raw, 32);
  FramerStats stats;
  const auto wire = make_packet(4, 9, 42, true, 0, 32);
  std::vector<StreamPacket> packets;
  const std::size_t first = framer.consume(
      std::span<const std::byte>(wire.data(), 2),
      [&](const StreamPacket& packet) { packets.push_back(packet); }, stats);
  CHECK_EQ(first, 0U);
  CHECK_EQ(framer.buffered_bytes(), 2U);
  const std::size_t second = framer.consume(
      std::span<const std::byte>(wire.data() + 2, wire.size() - 2),
      [&](const StreamPacket& packet) { packets.push_back(packet); }, stats);
  CHECK_EQ(second, 1U);
  REQUIRE(packets.size() == 1U);
  CHECK(packets[0].staged);
  CHECK_EQ(packets[0].payload_size, 32U);
  CHECK_EQ(stats.staged_packets, 1U);
}

EINSTAR_TEST(packet_payload_split_across_transfers) {
  PacketFramer framer(kDefaultMaxPacketBytes, FramingMode::Raw, 1000);
  FramerStats stats;
  const auto wire = make_packet(1, 3, 77, true, 0, 1000);
  std::vector<StreamPacket> packets;
  std::vector<std::byte> seen;
  framer.consume(
      std::span<const std::byte>(wire.data(), 100),
      [&](const StreamPacket& packet) { packets.push_back(packet); }, stats);
  CHECK_EQ(packets.size(), 0U);
  framer.consume(
      std::span<const std::byte>(wire.data() + 100, wire.size() - 100),
      [&](const StreamPacket& packet) { packets.push_back(packet); }, stats);
  REQUIRE(packets.size() == 1U);
  CHECK(packets[0].staged);
  CHECK_EQ(packets[0].payload_size, 1000U);
  seen.assign(packets[0].payload, packets[0].payload + packets[0].payload_size);
  CHECK(std::equal(seen.begin(), seen.end(),
                   wire.begin() + static_cast<std::ptrdiff_t>(kStreamHeaderSize)));
}

EINSTAR_TEST(packet_length_prefixed_corrupt_length_resyncs) {
  // Legacy format: a corrupt u32 length must resynchronise byte by byte and
  // still find the following valid packet.  Raw framing has no length field, so
  // this fault is only meaningful for old length-prefixed recordings.
  PacketFramer framer(kDefaultMaxPacketBytes, FramingMode::LengthPrefixed);
  FramerStats stats;
  std::vector<std::byte> wire;
  wire.push_back(std::byte{0x00});
  wire.push_back(std::byte{0xFF});
  wire.push_back(std::byte{0xFF});
  wire.push_back(std::byte{0xFF});  // 0x00FFFFFF length
  for (int i = 0; i < 8; ++i) {
    wire.push_back(std::byte{0xAB});
  }
  const auto good = make_legacy_packet(1, 5, 500, true, 0, 40);
  wire.insert(wire.end(), good.begin(), good.end());

  std::vector<StreamPacket> packets;
  framer.consume(std::span<const std::byte>(wire),
                 [&](const StreamPacket& packet) { packets.push_back(packet); },
                 stats);
  CHECK_EQ(packets.size(), 1U);
  if (!packets.empty()) {
    CHECK_EQ(packets[0].frame_index, 5U);
    CHECK_EQ(packets[0].payload_size, 40U);
  }
  CHECK(stats.protocol_errors > 0U);
  CHECK(stats.resync_bytes > 0U);
  CHECK(stats.oversized_lengths > 0U);
}

EINSTAR_TEST(packet_unknown_camera_is_passed_to_reassembly) {
  // The legacy parser does not gate headers on plausibility, so a 3-bit camera
  // field that is not one of the device's cameras reaches the reassembler,
  // which is the component that rejects unknown cameras.  (Raw framing rejects
  // such a header at the framer; see packet_raw_implausible_header_rejected.)
  PacketFramer framer(kDefaultMaxPacketBytes, FramingMode::LengthPrefixed);
  FramerStats stats;
  const auto bad = make_legacy_packet(0, 1, 10, true, 0, 20);   // field 0 -> invalid
  const auto good = make_legacy_packet(2, 2, 20, true, 0, 20);  // field 2 -> index 1
  std::vector<std::byte> wire(bad.begin(), bad.end());
  wire.insert(wire.end(), good.begin(), good.end());
  std::vector<StreamPacket> packets;
  framer.consume(std::span<const std::byte>(wire),
                 [&](const StreamPacket& packet) { packets.push_back(packet); },
                 stats);
  REQUIRE(packets.size() == 2U);
  CHECK_EQ(packets[0].camera_index, kInvalidCamera);
  CHECK_EQ(packets[1].camera_index, 1U);
  CHECK_EQ(packets[1].frame_index, 2U);
  CHECK_EQ(stats.protocol_errors, 0U);
}

EINSTAR_TEST(packet_header_only_and_reset) {
  // Header-only packets exist in the legacy format (declared length 32).
  PacketFramer framer(kDefaultMaxPacketBytes, FramingMode::LengthPrefixed);
  FramerStats stats;
  const auto wire = make_legacy_header_only(1);
  std::vector<StreamPacket> packets;
  framer.consume(std::span<const std::byte>(wire),
                 [&](const StreamPacket& packet) { packets.push_back(packet); },
                 stats);
  REQUIRE(packets.size() == 1U);
  CHECK_EQ(packets[0].payload_size, 0U);
  CHECK_EQ(framer.buffered_bytes(), 0U);
  framer.reset();
  CHECK_EQ(framer.buffered_bytes(), 0U);
}

EINSTAR_TEST(packet_short_fragments_then_continuation) {
  PacketFramer framer(kDefaultMaxPacketBytes, FramingMode::Raw, 64);
  FramerStats stats;
  const auto wire = make_packet(1, 11, 11, true, 0, 64);
  std::vector<StreamPacket> packets;
  // Feed the packet in 3-byte pieces: shorter than the raw 32-byte header.
  for (std::size_t offset = 0; offset < wire.size(); offset += 3) {
    const std::size_t size = std::min<std::size_t>(3, wire.size() - offset);
    framer.consume(std::span<const std::byte>(wire.data() + offset, size),
                   [&](const StreamPacket& packet) {
                     packets.push_back(packet);
                   },
                   stats);
  }
  REQUIRE(packets.size() == 1U);
  CHECK(packets[0].staged);
  CHECK_EQ(packets[0].frame_index, 11U);
  CHECK_EQ(packets[0].payload_size, 64U);
}

// ---------------------------------------------------------------------------
// Raw framing (hardware truth)
// ---------------------------------------------------------------------------

EINSTAR_TEST(packet_raw_payload_split_across_transfers) {
  // One full hardware packet split as a live short read could split it:
  // 20,000 + 20,992 = 40,992 = 32-byte header + 40,960-byte payload.
  PacketFramer framer;
  FramerStats stats;
  const auto wire = make_packet(1, 17, 1234, true, 0, kStreamChunkPayload);
  CHECK_EQ(wire.size(), static_cast<std::size_t>(kStreamPacketLen));
  std::vector<StreamPacket> packets;
  const std::size_t first = framer.consume(
      std::span<const std::byte>(wire.data(), 20'000),
      [&](const StreamPacket& packet) { packets.push_back(packet); }, stats);
  CHECK_EQ(first, 0U);
  CHECK_EQ(framer.buffered_bytes(), 20'000U);
  const std::size_t second = framer.consume(
      std::span<const std::byte>(wire.data() + 20'000, wire.size() - 20'000),
      [&](const StreamPacket& packet) { packets.push_back(packet); }, stats);
  CHECK_EQ(second, 1U);
  REQUIRE(packets.size() == 1U);
  CHECK(packets[0].staged);
  CHECK_EQ(packets[0].payload_size,
           static_cast<std::size_t>(kStreamChunkPayload));
  CHECK_EQ(packets[0].frame_index, 17U);
  CHECK_EQ(stats.staged_packets, 1U);
  CHECK_EQ(framer.buffered_bytes(), 0U);
  std::vector<std::byte> payload;
  payload.assign(packets[0].payload,
                 packets[0].payload + packets[0].payload_size);
  CHECK(std::equal(payload.begin(), payload.end(),
                   wire.begin() + static_cast<std::ptrdiff_t>(kStreamHeaderSize)));
}

EINSTAR_TEST(packet_raw_two_coalesced_packets) {
  // Two complete hardware packets in a single transfer: packet index 0 (not
  // last) followed by packet index 1 (last) of the same frame.
  PacketFramer framer;
  FramerStats stats;
  const auto first = make_packet(1, 21, 777, false, 0, kStreamChunkPayload,
                                 0x10, 0);
  const auto second = make_packet(1, 21, 777, true, 0, kStreamChunkPayload,
                                  0x90, 1);
  std::vector<std::byte> wire(first.begin(), first.end());
  wire.insert(wire.end(), second.begin(), second.end());
  std::vector<StreamPacket> packets;
  const std::size_t emitted = framer.consume(
      std::span<const std::byte>(wire),
      [&](const StreamPacket& packet) { packets.push_back(packet); }, stats);
  CHECK_EQ(emitted, 2U);
  REQUIRE(packets.size() == 2U);
  CHECK_EQ(packets[0].packet_index, 0U);
  CHECK_EQ(packets[1].packet_index, 1U);
  CHECK(!packets[0].last_of_frame);
  CHECK(packets[1].last_of_frame);
  CHECK_EQ(packets[0].payload_size,
           static_cast<std::size_t>(kStreamChunkPayload));
  CHECK_EQ(packets[1].payload_size,
           static_cast<std::size_t>(kStreamChunkPayload));
  CHECK(!packets[0].staged);
  CHECK(!packets[1].staged);
  CHECK_EQ(stats.packets, 2U);
  CHECK_EQ(stats.protocol_errors, 0U);
  CHECK_EQ(framer.buffered_bytes(), 0U);
  CHECK(std::to_integer<std::uint8_t>(packets[0].payload[0]) ==
        std::to_integer<std::uint8_t>(first[kStreamHeaderSize]));
  CHECK(std::to_integer<std::uint8_t>(packets[1].payload[0]) ==
        std::to_integer<std::uint8_t>(second[kStreamHeaderSize]));
}

EINSTAR_TEST(packet_raw_header_only_then_payload) {
  // The FX3 can complete a transfer with only the 32-byte header; the payload
  // arrives in the next transfer.  The packet must be stitched, not dropped.
  PacketFramer framer;
  FramerStats stats;
  const auto wire = make_packet(4, 31, 999, true, 0, kStreamChunkPayload, 0x22);
  std::vector<StreamPacket> packets;
  const std::size_t first = framer.consume(
      std::span<const std::byte>(wire.data(), kStreamHeaderSize),
      [&](const StreamPacket& packet) { packets.push_back(packet); }, stats);
  CHECK_EQ(first, 0U);
  CHECK_EQ(framer.buffered_bytes(), kStreamHeaderSize);
  const std::size_t second = framer.consume(
      std::span<const std::byte>(wire.data() + kStreamHeaderSize,
                                 wire.size() - kStreamHeaderSize),
      [&](const StreamPacket& packet) { packets.push_back(packet); }, stats);
  CHECK_EQ(second, 1U);
  REQUIRE(packets.size() == 1U);
  CHECK(packets[0].staged);
  CHECK_EQ(packets[0].camera_index, 2U);
  CHECK(packets[0].last_of_frame);
  CHECK_EQ(packets[0].payload_size,
           static_cast<std::size_t>(kStreamChunkPayload));
  CHECK_EQ(stats.staged_packets, 1U);
  CHECK_EQ(framer.buffered_bytes(), 0U);
  CHECK(std::to_integer<std::uint8_t>(packets[0].payload[0]) == 0x22);
}

EINSTAR_TEST(packet_raw_implausible_header_rejected) {
  // A raw packet cannot carry a length field, so a corrupt header is detected
  // by header_is_plausible: bad camera field (header[2] bits 3..5 == 0).  The
  // framer counts the skipped bytes as protocol errors/resync and still finds
  // the next valid packet in the same transfer.
  constexpr std::size_t kSmallFrame = 64;
  PacketFramer framer(kDefaultMaxPacketBytes, FramingMode::Raw, kSmallFrame);
  FramerStats stats;

  std::vector<std::byte> bad(kStreamHeaderSize + kSmallFrame, std::byte{0});
  bad[kHdrCameraByte] = std::byte{0};  // camera field 0 is not one-hot
  bad[kHdrLastFlag] = std::byte{1};
  const auto good = make_packet(2, 0x55, 5, true, 0, kSmallFrame, 0x00, 0);

  std::vector<std::byte> wire(bad.begin(), bad.end());
  wire.insert(wire.end(), good.begin(), good.end());
  std::vector<StreamPacket> packets;
  const std::size_t emitted = framer.consume(
      std::span<const std::byte>(wire),
      [&](const StreamPacket& packet) { packets.push_back(packet); }, stats);
  CHECK_EQ(emitted, 1U);
  REQUIRE(packets.size() == 1U);
  CHECK_EQ(packets[0].camera_index, 1U);  // field 2
  CHECK_EQ(packets[0].frame_index, 0x55U);
  CHECK_EQ(stats.packets, 1U);
  CHECK(stats.protocol_errors > 0U);
  CHECK(stats.resync_bytes > 0U);
  CHECK(stats.invalid_headers > 0U);
  CHECK_EQ(stats.resync_bytes, stats.protocol_errors);
  CHECK_EQ(framer.buffered_bytes(), 0U);
}

EINSTAR_TEST(packet_packet_index_parsed) {
  // Hardware-verified: header bytes 9..11 are a u24 BE frame-relative packet
  // index that cycles 0..31.  Index 31 must come from bytes 00 00 1F.
  for (std::uint32_t index : {0U, 1U, 31U}) {
    PacketFramer framer;
    FramerStats stats;
    const auto wire =
        make_packet(1, 3, 55, index == 31U, 0, kStreamChunkPayload, 0x33, index);
    CHECK_EQ(std::to_integer<std::uint8_t>(wire[kHdrPacketIndex]),
             static_cast<std::uint8_t>((index >> 16U) & 0xFFU));
    CHECK_EQ(std::to_integer<std::uint8_t>(wire[kHdrPacketIndex + 1]),
             static_cast<std::uint8_t>((index >> 8U) & 0xFFU));
    CHECK_EQ(std::to_integer<std::uint8_t>(wire[kHdrPacketIndex + 2]),
             static_cast<std::uint8_t>(index & 0xFFU));
    std::vector<StreamPacket> packets;
    const std::size_t emitted = framer.consume(
        std::span<const std::byte>(wire),
        [&](const StreamPacket& packet) { packets.push_back(packet); }, stats);
    CHECK_EQ(emitted, 1U);
    REQUIRE(packets.size() == 1U);
    CHECK_EQ(packets[0].packet_index, index);
    CHECK_EQ(stats.protocol_errors, 0U);
  }
}

EINSTAR_TEST(packet_raw_live_style_three_camera_groups) {
  // Replay the live shape exactly: each transfer is one raw packet of 32 header
  // + 40,960 payload bytes; a camera frame is 32 packets with the last flag on
  // packet 31; the three cameras of a trigger group share the u32 slot id and
  // the u64 group counter, and both differ between groups.
  constexpr std::uint32_t kGroups = 2;
  BufferPool pool(kDefaultFrameSize, 8);
  ReassemblyConfig config;
  config.frame_size = kDefaultFrameSize;
  config.width = 1280;
  config.height = 1024;
  config.mode = CopyMode::Copy;
  config.frame_pool = &pool;
  config.partial_frame_timeout_ns = 5'000'000;
  FrameReassembler reassembler(config);
  PacketFramer framer;  // Raw, default 1280x1024 frame size
  FramerStats stats;

  struct Completed {
    std::uint8_t camera = 0;
    std::uint32_t frame_index = 0;
    std::uint64_t device_timestamp = 0;
  };
  std::array<std::vector<Completed>, kCameraCount> completed;
  std::array<std::vector<std::uint32_t>, kCameraCount> seen_indices;
  std::size_t transfers = 0;

  for (std::uint32_t group = 0; group < kGroups; ++group) {
    const std::uint32_t slot_id = 0x1000U + group;
    const std::uint64_t group_counter = 0x0102030405060708ULL + group;
    for (std::uint32_t index = 0; index < kStreamChunksPerFrame; ++index) {
      const bool last = index + 1U == kStreamChunksPerFrame;
      for (std::uint8_t camera = 0; camera < kCameraCount; ++camera) {
        const auto wire = make_packet(
            camera_field_from_index(camera), slot_id, group_counter, last,
            /*sub=*/0, kStreamChunkPayload,
            static_cast<std::uint8_t>(0x20U + camera * 0x10U), index);
        CHECK_EQ(wire.size(), static_cast<std::size_t>(kStreamPacketLen));
        std::size_t emitted = 0;
        emitted = framer.consume(
            std::span<const std::byte>(wire),
            [&](const StreamPacket& packet) {
              CHECK_EQ(packet.packet_index, index);
              CHECK_EQ(packet.camera_index, camera);
              CHECK_EQ(packet.last_of_frame, last);
              // The three cameras of one trigger group carry the same u64
              // group counter; the u32 slot id is only meaningful on the last
              // packet but is likewise shared by the group.
              CHECK_EQ(packet.device_timestamp, group_counter);
              if (last) {
                CHECK_EQ(packet.frame_index, slot_id);
              }
              seen_indices[camera].push_back(packet.packet_index);
              auto result = reassembler.ingest(
                  packet, BufferHandle{}, 0,
                  static_cast<std::int64_t>(group) * 1'000'000 +
                      static_cast<std::int64_t>(index) * 1'000 +
                      static_cast<std::int64_t>(camera));
              CHECK(result.has_value());
              if (result.has_value() && result->completed.has_value()) {
                Completed record;
                record.camera = result->completed->camera_id;
                record.frame_index = result->completed->frame_index;
                record.device_timestamp =
                    result->completed->device_timestamp;
                CHECK_EQ(result->completed->payload_size, kDefaultFrameSize);
                CHECK(result->completed->is_contiguous());
                completed[camera].push_back(record);
              }
            },
            stats);
        CHECK_EQ(emitted, 1U);
        ++transfers;
      }
    }
  }

  CHECK_EQ(transfers, 2U * 3U * kStreamChunksPerFrame);
  CHECK_EQ(stats.packets, 2U * 3U * kStreamChunksPerFrame);
  CHECK_EQ(stats.protocol_errors, 0U);
  CHECK_EQ(stats.staged_packets, 0U);
  CHECK_EQ(reassembler.stats().frames_completed, 6U);
  CHECK_EQ(reassembler.stats().frames_dropped, 0U);
  CHECK_EQ(reassembler.stats().sequence_gaps, 0U);
  CHECK_EQ(reassembler.inflight(), 0U);

  for (std::uint8_t camera = 0; camera < kCameraCount; ++camera) {
    REQUIRE(seen_indices[camera].size() ==
            2U * kStreamChunksPerFrame);
    for (std::size_t i = 0; i < seen_indices[camera].size(); ++i) {
      CHECK_EQ(seen_indices[camera][i],
               static_cast<std::uint32_t>(i % kStreamChunksPerFrame));
    }
    REQUIRE(completed[camera].size() == kGroups);
    for (std::uint32_t group = 0; group < kGroups; ++group) {
      CHECK_EQ(completed[camera][group].camera, camera);
      CHECK_EQ(completed[camera][group].frame_index, 0x1000U + group);
      CHECK_EQ(completed[camera][group].device_timestamp,
               0x0102030405060708ULL + group);
    }
  }
  CHECK_EQ(pool.in_use(), 0U);
}

EINSTAR_TEST_MAIN()
