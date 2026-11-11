// SPDX-License-Identifier: MIT
//
// Independent protocol-conformance tests.
//
// These do NOT use the simulator: every byte vector below is written by hand
// from the hardware-verified wire format (9,660/9,660 live transfers on
// 2026-11-11 were exactly 32 + 40,960 bytes).  If the decoder and the simulator
// ever drift into a shared mistake, these vectors still fail.
//
//   packet = [32-byte header][payload]     (raw: NO length prefix on the wire)
//   header[2]      bits 3..5 = one-hot camera (1, 2, 4), bits 0..2 = sub-index
//   header[8]      == 1 only on the LAST packet of a frame
//   header[9..11]  u24 BE frame-relative packet index (0..31)
//   header[12..15] u32 BE slot/frame id, valid on the last packet only
//   header[16..23] u64 BE trigger-group counter, latched from the first packet
//
// The payload length of packet `i` is min(40,960, frame_size - i*40,960), so
// the raw form is self-delimiting.  A framer is therefore always constructed
// with the frame size the vectors use.
//
// The legacy [u32 BE len][header][payload] form still exists for old
// recordings; the few tests that need to feed a header the raw plausibility
// gate would reject (a non-one-hot camera, an implausible last flag) build that
// form explicitly and use FramingMode::LengthPrefixed.
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

// One complete golden raw packet: camera field 4 (index 2), sub-index 1, LAST
// packet, frame id 0x01020304, timestamp 0x1122334455667788, payload 0xDEADBEEF.
// The frame size is 4 bytes, so packet index 0 implies a 4-byte payload.
constexpr std::uint8_t kGoldenPacket[] = {
    // [0..31] 32-byte header
    0x00, 0x00, 0x21, 0x00, 0x00, 0x00, 0x00, 0x00,  // hdr[0..7]: camera 4, sub 1
    0x01,                                            // hdr[8] = LAST
    0x00, 0x00, 0x00,                                // hdr[9..11] packet index 0
    0x01, 0x02, 0x03, 0x04,                          // hdr[12..15] frame id BE
    0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88,  // hdr[16..23] timestamp BE
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,  // hdr[24..31]
    // [32..35] payload
    0xDE, 0xAD, 0xBE, 0xEF,
};

constexpr std::uint32_t kGoldenFrameSize = 4;

struct Collect {
  std::vector<StreamPacket> packets;
  void operator()(const StreamPacket& p) { packets.push_back(p); }
};

}  // namespace

EINSTAR_TEST(conformance_golden_packet_fields) {
  PacketFramer framer(kDefaultMaxPacketBytes, FramingMode::Raw,
                      kGoldenFrameSize);
  FramerStats stats;
  Collect collect;
  const std::size_t n = framer.consume(
      std::span<const std::byte>(reinterpret_cast<const std::byte*>(kGoldenPacket),
                                 sizeof(kGoldenPacket)),
      [&](const StreamPacket& p) { collect(p); }, stats);
  CHECK_EQ(n, 1U);
  REQUIRE(collect.packets.size() == 1U);
  const StreamPacket& p = collect.packets[0];
  CHECK_EQ(p.declared_length, 36U);
  CHECK_EQ(p.camera_field, 4U);
  CHECK_EQ(p.camera_index, 2U);   // one-hot 4 -> index 2
  CHECK_EQ(p.sub_index, 1U);      // NOT the camera
  CHECK_EQ(p.packet_index, 0U);
  CHECK(p.last_of_frame);
  CHECK_EQ(p.frame_index, 0x01020304U);
  CHECK_EQ(p.device_timestamp, 0x1122334455667788ULL);
  CHECK_EQ(p.payload_size, 4U);
  CHECK_EQ(std::to_integer<std::uint8_t>(p.payload[0]), 0xDEU);
  CHECK_EQ(stats.protocol_errors, 0U);
}

EINSTAR_TEST(conformance_camera_is_bits_3_to_5_not_0_to_2) {
  // field 2 -> index 1; a wrong decoder that reads bits 0..2 would report 2.
  std::array<std::uint8_t, kStreamHeaderSize + kGoldenFrameSize> wire{};
  wire[kHdrCameraByte] = 0x10;  // 0b0001_0000 -> field 2, sub 0
  PacketFramer framer(kDefaultMaxPacketBytes, FramingMode::Raw,
                      kGoldenFrameSize);
  FramerStats stats;
  Collect collect;
  framer.consume(std::span<const std::byte>(
                     reinterpret_cast<const std::byte*>(wire.data()), wire.size()),
                 [&](const StreamPacket& p) { collect(p); }, stats);
  REQUIRE(collect.packets.size() == 1U);
  CHECK_EQ(collect.packets[0].camera_field, 2U);
  CHECK_EQ(collect.packets[0].camera_index, 1U);
}

EINSTAR_TEST(conformance_only_value_one_marks_last) {
  // The vendor compares header[8] == 1 exactly; 2 or 0xFF must not complete.
  // The legacy parser passes the header through unchanged, which is where this
  // exact-comparison semantics can be observed for every flag value.
  for (std::uint8_t flag : {0x00, 0x02, 0x80, 0xFF}) {
    std::array<std::uint8_t, kStreamLengthSize + kStreamHeaderSize +
                                 kGoldenFrameSize>
        wire{};
    store_be32(reinterpret_cast<std::byte*>(wire.data()),
               static_cast<std::uint32_t>(kStreamHeaderSize + kGoldenFrameSize));
    wire[kStreamLengthSize + kHdrCameraByte] = 0x08;  // camera field 1
    wire[kStreamLengthSize + kHdrLastFlag] = flag;
    PacketFramer framer(kDefaultMaxPacketBytes, FramingMode::LengthPrefixed);
    FramerStats stats;
    Collect collect;
    framer.consume(std::span<const std::byte>(
                       reinterpret_cast<const std::byte*>(wire.data()),
                       wire.size()),
                   [&](const StreamPacket& p) { collect(p); }, stats);
    REQUIRE(collect.packets.size() == 1U);
    CHECK(!collect.packets[0].last_of_frame);
  }

  // Raw framing is stricter: any flag above 1 makes the header implausible, so
  // such a packet is rejected as a protocol error rather than completing a
  // frame.  Only exactly 1 can ever mark a frame complete.
  for (std::uint8_t flag : {0x02, 0x80, 0xFF}) {
    std::array<std::uint8_t, kStreamHeaderSize + kGoldenFrameSize> wire{};
    wire[kHdrCameraByte] = 0x08;
    wire[kHdrLastFlag] = flag;
    PacketFramer framer(kDefaultMaxPacketBytes, FramingMode::Raw,
                        kGoldenFrameSize);
    FramerStats stats;
    Collect collect;
    framer.consume(std::span<const std::byte>(
                       reinterpret_cast<const std::byte*>(wire.data()),
                       wire.size()),
                   [&](const StreamPacket& p) { collect(p); }, stats);
    CHECK_EQ(collect.packets.size(), 0U);
    CHECK(stats.protocol_errors > 0U);
  }
}

EINSTAR_TEST(conformance_invalid_camera_fields) {
  // Legacy pass-through: the 3-bit field is decoded and handed to the
  // reassembler, which rejects unknown cameras.
  for (std::uint8_t field : {0U, 3U, 5U, 6U, 7U}) {
    std::array<std::uint8_t, kStreamLengthSize + kStreamHeaderSize +
                                 kGoldenFrameSize>
        wire{};
    store_be32(reinterpret_cast<std::byte*>(wire.data()),
               static_cast<std::uint32_t>(kStreamHeaderSize + kGoldenFrameSize));
    wire[kStreamLengthSize + kHdrCameraByte] =
        static_cast<std::uint8_t>(field << 3U);
    PacketFramer framer(kDefaultMaxPacketBytes, FramingMode::LengthPrefixed);
    FramerStats stats;
    Collect collect;
    framer.consume(std::span<const std::byte>(
                       reinterpret_cast<const std::byte*>(wire.data()),
                       wire.size()),
                   [&](const StreamPacket& p) { collect(p); }, stats);
    REQUIRE(collect.packets.size() == 1U);
    CHECK_EQ(collect.packets[0].camera_index, kInvalidCamera);
  }

  // Raw framing rejects the same headers before reassembly: a non-one-hot
  // camera selector is not a plausible hardware header.
  for (std::uint8_t field : {0U, 3U, 5U, 6U, 7U}) {
    std::array<std::uint8_t, kStreamHeaderSize + kGoldenFrameSize> wire{};
    wire[kHdrCameraByte] = static_cast<std::uint8_t>(field << 3U);
    PacketFramer framer(kDefaultMaxPacketBytes, FramingMode::Raw,
                        kGoldenFrameSize);
    FramerStats stats;
    Collect collect;
    framer.consume(std::span<const std::byte>(
                       reinterpret_cast<const std::byte*>(wire.data()),
                       wire.size()),
                   [&](const StreamPacket& p) { collect(p); }, stats);
    CHECK_EQ(collect.packets.size(), 0U);
    CHECK(stats.protocol_errors > 0U);
  }
}

EINSTAR_TEST(conformance_framer_is_byte_granular) {
  // Feeding the golden packet one byte at a time must produce the same packet.
  PacketFramer framer(kDefaultMaxPacketBytes, FramingMode::Raw,
                      kGoldenFrameSize);
  FramerStats stats;
  Collect one_shot;
  framer.consume(std::span<const std::byte>(
                     reinterpret_cast<const std::byte*>(kGoldenPacket),
                     sizeof(kGoldenPacket)),
                 [&](const StreamPacket& p) { one_shot(p); }, stats);
  REQUIRE(one_shot.packets.size() == 1U);

  PacketFramer dribble(kDefaultMaxPacketBytes, FramingMode::Raw,
                       kGoldenFrameSize);
  FramerStats dribble_stats;
  Collect drip;
  for (std::size_t i = 0; i < sizeof(kGoldenPacket); ++i) {
    const std::byte b = reinterpret_cast<const std::byte*>(kGoldenPacket)[i];
    dribble.consume(std::span<const std::byte>(&b, 1),
                    [&](const StreamPacket& p) { drip(p); }, dribble_stats);
  }
  REQUIRE(drip.packets.size() == 1U);
  CHECK_EQ(drip.packets[0].camera_index, 2U);
  CHECK(drip.packets[0].last_of_frame);
  CHECK_EQ(drip.packets[0].frame_index, 0x01020304U);
  CHECK_EQ(drip.packets[0].device_timestamp, 0x1122334455667788ULL);
  CHECK(drip.packets[0].staged);
}

namespace {

// Hand-built raw packet with explicit fields, independent of the simulator.
// The payload length is derived from packet_index and frame_size exactly as
// the hardware format requires.
std::vector<std::byte> wire_packet(std::uint8_t camera_field, std::uint8_t sub,
                                   bool last, std::uint32_t frame_id,
                                   std::uint64_t timestamp,
                                   std::uint32_t packet_index,
                                   std::uint32_t frame_size,
                                   std::uint8_t fill) {
  const std::size_t offset =
      static_cast<std::size_t>(packet_index) * kStreamChunkPayload;
  const std::size_t payload_size =
      offset >= frame_size
          ? 0
          : std::min<std::size_t>(kStreamChunkPayload, frame_size - offset);
  std::vector<std::byte> out(kStreamHeaderSize + payload_size);
  std::byte* h = out.data();
  h[kHdrCameraByte] = static_cast<std::byte>(
      static_cast<std::uint8_t>((camera_field & 0x7U) << 3U) |
      static_cast<std::uint8_t>(sub & 0x7U));
  h[kHdrLastFlag] = std::byte{static_cast<unsigned char>(last ? 1 : 0)};
  h[kHdrPacketIndex] =
      static_cast<std::byte>((packet_index >> 16U) & 0xFFU);
  h[kHdrPacketIndex + 1] =
      static_cast<std::byte>((packet_index >> 8U) & 0xFFU);
  h[kHdrPacketIndex + 2] =
      static_cast<std::byte>(packet_index & 0xFFU);
  store_be32(h + kHdrFrameId, last ? frame_id : 0xFFFFFFFFU);
  store_be64(h + kHdrTimestamp, timestamp);
  for (std::size_t i = 0; i < payload_size; ++i) {
    out[kStreamHeaderSize + i] = static_cast<std::byte>(fill);
  }
  return out;
}

ReassemblyConfig single_config(BufferPool& pool, std::uint32_t frame_size) {
  ReassemblyConfig c;
  c.frame_size = frame_size;
  c.width = 8;
  c.height = 8;
  c.mode = CopyMode::Copy;
  c.frame_pool = &pool;
  c.partial_frame_timeout_ns = 5'000'000;
  return c;
}

Result<FrameReassembler::IngestResult> feed(FrameReassembler& r, PacketFramer& f,
                                            const std::vector<std::byte>& wire,
                                            std::int64_t now) {
  std::vector<StreamPacket> packets;
  FramerStats stats;
  f.consume(std::span<const std::byte>(wire),
            [&](const StreamPacket& p) { packets.push_back(p); }, stats);
  Result<FrameReassembler::IngestResult> last =
      FrameReassembler::IngestResult{};
  for (const auto& p : packets) {
    last = r.ingest(p, BufferHandle{}, 0, now);
    if (!last.has_value()) {
      return last;
    }
  }
  return last;
}

}  // namespace

EINSTAR_TEST(conformance_frame_id_only_from_last_packet) {
  // Two 40,960-byte hardware packets: index 0 (not last) and index 1 (last).
  constexpr std::uint32_t kSize = 2U * kStreamChunkPayload;
  BufferPool pool(kSize, 4);
  FrameReassembler r(single_config(pool, kSize));
  PacketFramer f(kDefaultMaxPacketBytes, FramingMode::Raw, kSize);
  // Two chunks with POISONED frame ids on the non-last packet.
  auto a = wire_packet(1, 0, false, 0, 111, 0, kSize, 0xAA);
  auto b = wire_packet(1, 1, true, 77, 999, 1, kSize, 0xBB);
  REQUIRE(feed(r, f, a, 10).has_value());
  auto out = feed(r, f, b, 20);
  REQUIRE(out.has_value());
  REQUIRE(out->completed.has_value());
  CHECK_EQ(out->completed->frame_index, 77U);       // from the last packet
  CHECK_EQ(out->completed->device_timestamp, 111U); // from the first packet
  CHECK_EQ(out->completed->camera_id, 0U);
}

EINSTAR_TEST(conformance_loss_then_recovery) {
  // Three hardware packets per frame; losing the middle one must be reported,
  // and the next complete frame must still assemble.
  constexpr std::uint32_t kSize = 3U * kStreamChunkPayload;
  BufferPool pool(kSize, 4);
  FrameReassembler r(single_config(pool, kSize));
  PacketFramer f(kDefaultMaxPacketBytes, FramingMode::Raw, kSize);
  // Frame 0: first chunk, then the last chunk without the middle one.
  REQUIRE(feed(r, f, wire_packet(1, 0, false, 0, 1, 0, kSize, 0x01), 10)
              .has_value());
  auto broken = feed(r, f, wire_packet(1, 2, true, 0, 2, 2, kSize, 0x03), 20);
  REQUIRE(broken.has_value());
  CHECK(!broken->completed.has_value());
  CHECK_EQ(r.stats().size_mismatch, 1U);
  // Frame 1 must complete cleanly afterwards.
  REQUIRE(feed(r, f, wire_packet(1, 0, false, 1, 3, 0, kSize, 0x10), 30)
              .has_value());
  REQUIRE(feed(r, f, wire_packet(1, 1, false, 1, 4, 1, kSize, 0x11), 30)
              .has_value());
  auto ok = feed(r, f, wire_packet(1, 2, true, 1, 4, 2, kSize, 0x11), 40);
  REQUIRE(ok.has_value());
  REQUIRE(ok->completed.has_value());
  CHECK_EQ(ok->completed->frame_index, 1U);
  CHECK_EQ(r.stats().frames_completed, 1U);
}

EINSTAR_TEST(conformance_three_cameras_one_trigger_group) {
  // One hardware-sized packet already fills a 40,960-byte frame.
  constexpr std::uint32_t kSize = kStreamChunkPayload;
  BufferPool pool(kSize, 8);
  FrameReassembler r(single_config(pool, kSize));
  PacketFramer f(kDefaultMaxPacketBytes, FramingMode::Raw, kSize);
  // The device streams one frame per camera per trigger group; the group
  // timestamp is identical, camera ids are one-hot 1/2/4.
  std::vector<Frame> done;
  for (int step = 0; step < 2; ++step) {
    for (std::uint8_t field : {1U, 2U, 4U}) {
      auto wire = wire_packet(field, static_cast<std::uint8_t>(step), true,
                              static_cast<std::uint32_t>(step), 5000, 0, kSize,
                              static_cast<std::uint8_t>(0x30 + field));
      auto out = feed(r, f, wire, 100 + step * 10 + field);
      REQUIRE(out.has_value());
      if (out->completed.has_value()) {
        done.push_back(std::move(*out->completed));
      }
    }
  }
  CHECK_EQ(done.size(), 6U);
  CHECK_EQ(r.stats().frames_completed, 6U);
  CHECK_EQ(r.stats().sequence_gaps, 0U);
  // Cameras 0,1,2 each completed step 0 and step 1.
  std::array<int, 3> per_camera{};
  for (const auto& frame : done) {
    REQUIRE(frame.camera_id < 3);
    per_camera[frame.camera_id]++;
  }
  CHECK_EQ(per_camera[0], 2);
  CHECK_EQ(per_camera[1], 2);
  CHECK_EQ(per_camera[2], 2);
}

EINSTAR_TEST(conformance_sequence_gap_counts_missing) {
  constexpr std::uint32_t kSize = kStreamChunkPayload;
  BufferPool pool(kSize, 4);
  FrameReassembler r(single_config(pool, kSize));
  PacketFramer f(kDefaultMaxPacketBytes, FramingMode::Raw, kSize);
  for (std::uint32_t id : {0U, 4U}) {
    auto out =
        feed(r, f, wire_packet(2, 0, true, id, id, 0, kSize, 0x55), 10 + id);
    REQUIRE(out.has_value());
    REQUIRE(out->completed.has_value());
  }
  // ids 1,2,3 were skipped.
  CHECK_EQ(r.stats().sequence_gaps, 3U);
  CHECK_EQ(r.expected_next(1), 5U);
}

EINSTAR_TEST_MAIN()
