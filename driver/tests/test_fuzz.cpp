// SPDX-License-Identifier: MIT
//
// Deterministic fuzzing of the stream framer + reassembler.
//
// A fixed-seed PRNG mutates valid packets and generates random byte soup; the
// test asserts that nothing crashes, that memory stays bounded, and that every
// counter/limit invariant holds.  Run it under ASan/UBSan (see README) for the
// full value; it is also a normal regression test.
#include <array>
#include <cstdint>
#include <vector>

#include "einstar/frame.hpp"
#include "einstar/packet.hpp"
#include "einstar/reassembler.hpp"
#include "test_util.hpp"

using namespace einstar;

namespace {

struct Rng {
  std::uint64_t state;
  std::uint64_t next() {
    state ^= state << 13U;
    state ^= state >> 7U;
    state ^= state << 17U;
    return state;
  }
  std::uint32_t below(std::uint32_t n) {
    return static_cast<std::uint32_t>(next() % (n == 0 ? 1 : n));
  }
};

std::vector<std::byte> valid_packet(std::uint8_t field, std::uint8_t sub,
                                    bool last, std::uint32_t frame,
                                    std::uint64_t ts, std::size_t payload,
                                    std::uint8_t fill) {
  std::vector<std::byte> out(4 + kStreamHeaderSize + payload);
  store_be32(out.data(), static_cast<std::uint32_t>(kStreamHeaderSize + payload));
  std::byte* h = out.data() + 4;
  h[kHdrCameraByte] = static_cast<std::byte>(
      static_cast<std::uint8_t>((field & 7U) << 3U) |
      static_cast<std::uint8_t>(sub & 7U));
  h[kHdrLastFlag] = static_cast<std::byte>(last ? 1 : 0);
  store_be32(h + kHdrFrameId, last ? frame : 0xFFFFFFFFU);
  store_be64(h + kHdrTimestamp, ts);
  for (std::size_t i = 0; i < payload; ++i) {
    out[4 + kStreamHeaderSize + i] = static_cast<std::byte>(fill);
  }
  return out;
}

}  // namespace

EINSTAR_TEST(fuzz_random_soup_is_safe) {
  constexpr std::uint32_t kFrameSize = 4096;
  BufferPool pool(kFrameSize, 8);
  ReassemblyConfig config;
  config.frame_size = kFrameSize;
  config.width = 32;
  config.height = 32;
  config.mode = CopyMode::Copy;
  config.frame_pool = &pool;
  config.partial_frame_timeout_ns = 1'000'000;

  Rng rng{0x123456789ABCDEFULL};
  PacketFramer framer;
  FrameReassembler reassembler(config);
  FramerStats stats;
  std::uint64_t completed = 0;

  for (int round = 0; round < 2000; ++round) {
    std::vector<std::byte> blob;
    const std::uint32_t mode = rng.below(4);
    if (mode == 0) {
      // pure noise
      blob.resize(1 + rng.below(512));
      for (auto& b : blob) {
        b = static_cast<std::byte>(rng.next() & 0xFFU);
      }
    } else if (mode == 1) {
      // valid packet with random single-byte corruption
      blob = valid_packet(static_cast<std::uint8_t>(1U << rng.below(3)),
                          static_cast<std::uint8_t>(rng.below(8)), true,
                          rng.below(1000), rng.next(), 1 + rng.below(200),
                          static_cast<std::uint8_t>(rng.next()));
      if (!blob.empty()) {
        blob[rng.below(static_cast<std::uint32_t>(blob.size()))] ^=
            static_cast<std::byte>(1U << rng.below(8));
      }
    } else if (mode == 2) {
      // concatenation of several valid packets (possibly truncated)
      const std::uint32_t count = 1 + rng.below(4);
      for (std::uint32_t i = 0; i < count; ++i) {
        auto one = valid_packet(static_cast<std::uint8_t>(1U << rng.below(3)),
                                static_cast<std::uint8_t>(i & 7U),
                                rng.below(2) == 0, rng.below(50), rng.next(),
                                1 + rng.below(64),
                                static_cast<std::uint8_t>(rng.next()));
        blob.insert(blob.end(), one.begin(), one.end());
      }
      if (!blob.empty() && rng.below(2) == 0) {
        blob.resize(1 + rng.below(static_cast<std::uint32_t>(blob.size())));
      }
    } else {
      // oversized length prefix followed by data (resync path)
      blob.resize(4 + rng.below(64));
      store_be32(blob.data(), 0x00FFFFFFU);
      for (std::size_t i = 4; i < blob.size(); ++i) {
        blob[i] = static_cast<std::byte>(rng.next() & 0xFFU);
      }
    }

    framer.consume(std::span<const std::byte>(blob),
                   [&](const StreamPacket& packet) {
                     if (packet.payload_size > kFrameSize) {
                       return;
                     }
                     auto result =
                         reassembler.ingest(packet, BufferHandle{}, 0,
                                            static_cast<std::int64_t>(round));
                     if (result.has_value() && result->completed.has_value()) {
                       ++completed;
                       CHECK_EQ(result->completed->payload_size, kFrameSize);
                     }
                   },
                   stats);

    CHECK(framer.buffered_bytes() <= framer.max_packet_bytes() + 4U);
    CHECK(reassembler.inflight() <= kCameraCount);
    CHECK(pool.in_use() <= pool.capacity());
    (void)reassembler.expire(static_cast<std::int64_t>(round) + 2'000'000);
  }

  // Whatever happened, the invariants must hold and nothing may leak.
  (void)completed;
  reassembler.flush(100'000'000);
  CHECK_EQ(pool.in_use(), 0U);
  CHECK_EQ(reassembler.inflight(), 0U);
}

EINSTAR_TEST(fuzz_valid_stream_survives_loss) {
  // Drop a random 10% of packets from a clean 3-camera stream; the reassembler
  // must never panic, never produce a short frame, and must keep memory bounded.
  constexpr std::uint32_t kFrameSize = 2048;
  constexpr std::uint32_t kChunk = 512;
  BufferPool pool(kFrameSize, 8);
  ReassemblyConfig config;
  config.frame_size = kFrameSize;
  config.mode = CopyMode::Copy;
  config.frame_pool = &pool;
  FrameReassembler reassembler(config);
  PacketFramer framer;
  FramerStats stats;
  Rng rng{0xDEADBEEF1234ULL};

  std::uint32_t completed = 0;
  for (std::uint32_t frame = 0; frame < 200; ++frame) {
    for (std::uint8_t camera = 0; camera < 3; ++camera) {
      const std::uint8_t field = camera_field_from_index(camera);
      const std::uint32_t chunks = kFrameSize / kChunk;
      for (std::uint32_t chunk = 0; chunk < chunks; ++chunk) {
        if (rng.below(10) == 0) {
          continue;  // loss
        }
        const bool last = chunk + 1 == chunks;
        auto wire = valid_packet(field, static_cast<std::uint8_t>(chunk), last,
                                 frame, frame * 1000ULL, kChunk,
                                 static_cast<std::uint8_t>(camera * 16 + chunk));
        framer.consume(std::span<const std::byte>(wire),
                       [&](const StreamPacket& packet) {
                         auto r = reassembler.ingest(packet, BufferHandle{}, 0,
                                                     frame * 1000 + chunk);
                         if (r.has_value() && r->completed.has_value()) {
                           CHECK_EQ(r->completed->payload_size, kFrameSize);
                           ++completed;
                         }
                       },
                       stats);
        CHECK(reassembler.inflight() <= kCameraCount);
      }
    }
  }
  CHECK(completed > 0U);
  CHECK(pool.in_use() <= pool.capacity());
  reassembler.flush(1'000'000'000);
  CHECK_EQ(pool.in_use(), 0U);
}

EINSTAR_TEST_MAIN()
