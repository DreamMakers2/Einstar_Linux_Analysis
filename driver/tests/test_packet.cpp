// SPDX-License-Identifier: MIT
#include <algorithm>
#include <array>
#include <cstring>
#include <vector>

#include "einstar/packet.hpp"
#include "test_util.hpp"

using namespace einstar;

namespace {

// Build a canonical wire packet: one-hot camera in header[2] bits 3..5,
// sub-index in bits 0..2, header[8] == 1 only on the last packet, frame id at
// header[12] (poisoned on non-last packets, as the device leaves it invalid
// until the final packet), timestamp at header[16].
std::vector<std::byte> make_packet(std::uint8_t camera_field, std::uint32_t frame,
                                   std::uint64_t timestamp, bool last,
                                   std::uint32_t chunk,
                                   std::size_t payload_size,
                                   std::uint8_t base = 0x40) {
  std::vector<std::byte> wire(4 + kStreamHeaderSize + payload_size);
  store_be32(wire.data(), static_cast<std::uint32_t>(kStreamHeaderSize +
                                                     payload_size));
  std::byte* header = wire.data() + 4;
  header[kHdrCameraByte] = static_cast<std::byte>(
      static_cast<std::uint8_t>((camera_field & 0x07U) << 3U) |
      static_cast<std::uint8_t>(chunk & 0x07U));
  header[kHdrLastFlag] = static_cast<std::byte>(last ? 1 : 0);
  store_be32(header + kHdrFrameId, last ? frame : 0xFFFFFFFFU);
  store_be64(header + kHdrTimestamp, timestamp);
  for (std::size_t i = 0; i < payload_size; ++i) {
    wire[4 + kStreamHeaderSize + i] =
        static_cast<std::byte>((base + i + chunk) & 0xFFU);
  }
  return wire;
}

struct Collector {
  std::vector<StreamPacket> packets;
  void operator()(const StreamPacket& packet) { packets.push_back(packet); }
};

}  // namespace

EINSTAR_TEST(packet_single) {
  PacketFramer framer;
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
  PacketFramer framer;
  FramerStats stats;
  std::vector<std::byte> wire;
  for (int i = 0; i < 3; ++i) {
    const auto packet = make_packet(1, 7, 1000 + static_cast<std::uint64_t>(i),
                                    i == 2, static_cast<std::uint32_t>(i), 64);
    wire.insert(wire.end(), packet.begin(), packet.end());
  }
  std::vector<StreamPacket> packets;
  framer.consume(std::span<const std::byte>(wire),
                 [&](const StreamPacket& packet) { packets.push_back(packet); },
                 stats);
  CHECK_EQ(packets.size(), 3U);
  CHECK_EQ(stats.packets, 3U);
  CHECK_EQ(packets[0].payload_size, 64U);
  CHECK_EQ(packets[2].declared_length, 96U);
}

EINSTAR_TEST(packet_header_split_across_transfers) {
  PacketFramer framer;
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
  PacketFramer framer;
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
  CHECK(std::equal(seen.begin(), seen.end(), wire.begin() + 36));
}

EINSTAR_TEST(packet_corrupt_length_resyncs) {
  PacketFramer framer;
  FramerStats stats;
  std::vector<std::byte> wire;
  wire.push_back(std::byte{0x00});
  wire.push_back(std::byte{0xFF});
  wire.push_back(std::byte{0xFF});
  wire.push_back(std::byte{0xFF});  // 0x00FFFFFF length
  for (int i = 0; i < 8; ++i) {
    wire.push_back(std::byte{0xAB});
  }
  const auto good = make_packet(1, 5, 500, true, 0, 40);
  wire.insert(wire.end(), good.begin(), good.end());

  std::vector<StreamPacket> packets;
  framer.consume(std::span<const std::byte>(wire),
                 [&](const StreamPacket& packet) { packets.push_back(packet); },
                 stats);
  CHECK_EQ(packets.size(), 1U);
  if (!packets.empty()) {
    CHECK_EQ(packets[0].frame_index, 5U);
  }
  CHECK(stats.protocol_errors > 0U);
  CHECK(stats.resync_bytes > 0U);
  CHECK(stats.oversized_lengths > 0U);
}

EINSTAR_TEST(packet_unknown_camera_is_passed_to_reassembly) {
  PacketFramer framer;
  FramerStats stats;
  const auto bad = make_packet(0, 1, 10, true, 0, 20);   // field 0 -> invalid
  const auto good = make_packet(2, 2, 20, true, 0, 20);  // field 2 -> index 1
  std::vector<std::byte> wire(bad.begin(), bad.end());
  wire.insert(wire.end(), good.begin(), good.end());
  std::vector<StreamPacket> packets;
  framer.consume(std::span<const std::byte>(wire),
                 [&](const StreamPacket& packet) { packets.push_back(packet); },
                 stats);
  // A 3-bit camera field is legal on the wire even when the value is not one
  // of the device's cameras.  The framer passes it through; the reassembler is
  // the component that rejects unknown cameras.
  REQUIRE(packets.size() == 2U);
  CHECK_EQ(packets[0].camera_index, kInvalidCamera);
  CHECK_EQ(packets[1].camera_index, 1U);
  CHECK_EQ(packets[1].frame_index, 2U);
  CHECK_EQ(stats.protocol_errors, 0U);
}

EINSTAR_TEST(packet_header_only_and_reset) {
  PacketFramer framer;
  FramerStats stats;
  const auto wire = make_packet(1, 1, 1, true, 0, 0);
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

EINSTAR_TEST(packet_short_prefix_then_continuation) {
  PacketFramer framer;
  FramerStats stats;
  const auto wire = make_packet(1, 11, 11, true, 0, 64);
  std::vector<StreamPacket> packets;
  // Feed the packet in 3-byte pieces (shorter than the 4-byte length prefix).
  for (std::size_t offset = 0; offset < wire.size(); offset += 3) {
    const std::size_t size = std::min<std::size_t>(3, wire.size() - offset);
    framer.consume(std::span<const std::byte>(wire.data() + offset, size),
                   [&](const StreamPacket& packet) {
                     packets.push_back(packet);
                   },
                   stats);
  }
  REQUIRE(packets.size() == 1U);
  CHECK_EQ(packets[0].frame_index, 11U);
  CHECK_EQ(packets[0].payload_size, 64U);
}

EINSTAR_TEST_MAIN()
