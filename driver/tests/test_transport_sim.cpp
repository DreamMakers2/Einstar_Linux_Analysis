// SPDX-License-Identifier: MIT
#include <algorithm>
#include <cstdint>
#include <vector>

#include "einstar/frame.hpp"
#include "einstar/packet.hpp"
#include "einstar/reassembler.hpp"
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

struct Delivery {
  std::vector<std::uint8_t> bytes;
  std::vector<TransferStatus> statuses;
  std::vector<std::size_t> sizes;
};

Delivery deliver_serial(SimulatedTransport& transport, std::size_t reads,
                        std::size_t capacity) {
  Delivery delivery;
  BufferPool pool(capacity, 2);
  Sink sink;
  for (std::size_t read = 0; read < reads; ++read) {
    BufferHandle buffer = pool.acquire();
    if (!buffer.valid()) {
      return delivery;
    }
    const TransferId id =
        transport.submit_bulk_in(Endpoints::kStreamIn, buffer.bytes(), sink,
                                 nullptr);
    if (id == 0) {
      return delivery;
    }
    while (sink.completions.empty()) {
      transport.poll(10);
    }
    const TransferCompletion completion = sink.completions.back();
    sink.completions.clear();
    delivery.statuses.push_back(completion.status);
    delivery.sizes.push_back(completion.bytes_transferred);
    if (completion.status == TransferStatus::Ok) {
      const auto span = buffer.bytes();
      for (std::size_t i = 0; i < completion.bytes_transferred; ++i) {
        delivery.bytes.push_back(std::to_integer<std::uint8_t>(span[i]));
      }
    }
  }
  return delivery;
}

SimConfig clean_config(std::uint64_t seed = 1, std::uint32_t frames = 4) {
  SimConfig config;
  config.seed = seed;
  config.frames = frames;
  config.expect_interval_quirk = false;
  return config;
}

void open_claim(SimulatedTransport& transport) {
  REQUIRE(transport.open().ok());
  REQUIRE(transport.claim_interface(0).ok());
}

}  // namespace

EINSTAR_TEST(sim_deterministic_stream) {
  auto run = [](std::uint64_t seed) {
    SimulatedTransport transport(clean_config(seed));
    open_claim(transport);
    return deliver_serial(transport, 64, 0xA400);
  };
  const Delivery first = run(7);
  const Delivery second = run(7);
  CHECK(first.bytes == second.bytes);
  CHECK(first.statuses == second.statuses);
  CHECK(first.sizes == second.sizes);
  CHECK(first.bytes.size() > 100000U);

}

EINSTAR_TEST(sim_parses_to_complete_frames) {
  SimulatedTransport transport(clean_config(1, 2));
  open_claim(transport);
  BufferPool pool(0xA400, 4);
  Sink sink;
  BufferPool frame_pool(kDefaultFrameSize, 4);
  ReassemblyConfig config;
  config.frame_size = kDefaultFrameSize;
  config.mode = CopyMode::Copy;
  config.frame_pool = &frame_pool;
  FrameReassembler reassembler(config);
  PacketFramer framer;
  FramerStats framer_stats;

  std::size_t reads = 0;
  std::size_t completed = 0;
  while (completed < 2 && reads < 2000) {
    BufferHandle buffer = pool.acquire();
    REQUIRE(buffer.valid());
    const TransferId id = transport.submit_bulk_in(
        Endpoints::kStreamIn, buffer.bytes(), sink, nullptr);
    REQUIRE(id != 0);
    while (sink.completions.empty()) {
      transport.poll(10);
    }
    const TransferCompletion completion = sink.completions.back();
    sink.completions.clear();
    reads++;
    if (completion.status != TransferStatus::Ok) {
      continue;
    }
    framer.consume(
        std::span<const std::byte>(buffer.data(), completion.bytes_transferred),
        [&](const StreamPacket& packet) {
          auto result =
              reassembler.ingest(packet, BufferHandle{}, 0, 0);
          REQUIRE(result.has_value());
          if (result->completed.has_value()) {
            CHECK_EQ(result->completed->payload_size, kDefaultFrameSize);
            completed++;
          }
        },
        framer_stats);
  }
  CHECK_EQ(completed, 2U);
  CHECK_EQ(reassembler.stats().frames_completed, 2U);
  CHECK_EQ(frame_pool.in_use(), 0U);
}

EINSTAR_TEST(sim_short_reads_stitch_packets) {
  SimConfig config = clean_config(3, 1);
  config.faults.short_read_probability = 1.0;
  SimulatedTransport transport(config);
  open_claim(transport);
  BufferPool pool(0xA400, 2);
  Sink sink;
  std::size_t reads = 0;
  bool saw_short = false;
  while (reads < 20) {
    BufferHandle buffer = pool.acquire();
    REQUIRE(buffer.valid());
    REQUIRE(transport.submit_bulk_in(Endpoints::kStreamIn, buffer.bytes(), sink,
                                     nullptr) != 0);
    while (sink.completions.empty()) {
      transport.poll(10);
    }
    const TransferCompletion completion = sink.completions.back();
    sink.completions.clear();
    reads++;
    if (completion.status == TransferStatus::Ok) {
      CHECK(completion.bytes_transferred <= 0xA400U);
      if (completion.bytes_transferred < 0xA400U) {
        saw_short = true;
      }
    }
  }
  CHECK(saw_short);
}

EINSTAR_TEST(sim_stall_and_clear_halt) {
  SimConfig config = clean_config(4, 4);
  config.faults.stall_probability = 1.0;
  SimulatedTransport transport(config);
  open_claim(transport);
  BufferPool pool(0xA400, 4);
  Sink sink;
  BufferHandle buffer = pool.acquire();
  REQUIRE(transport.submit_bulk_in(Endpoints::kStreamIn, buffer.bytes(), sink,
                                   nullptr) != 0);
  while (sink.completions.empty()) {
    transport.poll(100);
  }
  CHECK(sink.completions.back().status == TransferStatus::Stall);
  CHECK(transport.halted(Endpoints::kStreamIn));
  CHECK_EQ(transport.stalled_completions(), 1U);
  sink.completions.clear();

  // While halted every submission stalls without touching the stream.
  REQUIRE(transport.submit_bulk_in(Endpoints::kStreamIn, buffer.bytes(), sink,
                                   nullptr) != 0);
  while (sink.completions.empty()) {
    transport.poll(100);
  }
  CHECK(sink.completions.back().status == TransferStatus::Stall);
  CHECK(transport.halted(Endpoints::kStreamIn));
  sink.completions.clear();

  REQUIRE(transport.clear_halt(Endpoints::kStreamIn).ok());
  CHECK(!transport.halted(Endpoints::kStreamIn));
}

EINSTAR_TEST(sim_timeout_is_reported) {
  SimConfig config = clean_config(5, 4);
  config.faults.timeout_probability = 1.0;
  SimulatedTransport transport(config);
  open_claim(transport);
  BufferPool pool(0xA400, 4);
  Sink sink;
  BufferHandle buffer = pool.acquire();
  REQUIRE(transport.submit_bulk_in(Endpoints::kStreamIn, buffer.bytes(), sink,
                                   nullptr) != 0);
  while (sink.completions.empty()) {
    transport.poll(100);
  }
  CHECK(sink.completions.back().status == TransferStatus::Timeout);
  CHECK_EQ(sink.completions.back().bytes_transferred, 0U);
  CHECK_EQ(transport.timed_out_completions(), 1U);
}

EINSTAR_TEST(sim_cancel_delivers_cancelled) {
  SimConfig config = clean_config(6, 100);
  config.transfer_latency_ns = 10'000'000;  // 10 ms
  SimulatedTransport transport(config);
  open_claim(transport);
  BufferPool pool(0xA400, 8);
  Sink sink;
  std::vector<BufferHandle> buffers;
  std::vector<TransferId> ids;
  for (int i = 0; i < 8; ++i) {
    buffers.push_back(pool.acquire());
    REQUIRE(buffers.back().valid());
    ids.push_back(transport.submit_bulk_in(Endpoints::kStreamIn,
                                           buffers.back().bytes(), sink,
                                           nullptr));
    REQUIRE(ids.back() != 0);
  }
  CHECK_EQ(transport.outstanding(), 8U);
  for (const TransferId id : ids) {
    CHECK(transport.cancel(id));
  }
  CHECK(!transport.cancel(999999));
  while (transport.outstanding() > 0) {
    transport.poll(0);
  }
  CHECK_EQ(sink.completions.size(), 8U);
  for (const auto& completion : sink.completions) {
    CHECK(completion.status == TransferStatus::Cancelled);
  }
  CHECK_EQ(transport.cancels(), 8U);
}

EINSTAR_TEST(sim_descriptor_quirk_patch) {
  SimConfig config;
  config.frames = 1;
  config.expect_interval_quirk = true;
  SimulatedTransport transport(config);
  REQUIRE(transport.open().ok());
  // bInterval=0 is detected but must NOT block claiming: Linux USB core
  // repairs the interval to 7 at high speed (usb_parse_endpoint).
  const auto claim = transport.claim_interface(0);
  CHECK(claim.ok());
  CHECK(!transport.descriptor().quirk_notes().empty());
  CHECK(!transport.descriptor().zero_interval_interrupt_endpoints().empty());
  // The patch API stays available for diagnostics and must be idempotent.
  CHECK(transport.apply_descriptor_patch().ok());
  CHECK(transport.apply_descriptor_patch().ok());
  CHECK(transport.release_interface(0).ok());
}

EINSTAR_TEST(sim_replay_is_byte_exact) {
  const std::vector<std::byte> first_bytes = {
      std::byte{0x01}, std::byte{0x02}, std::byte{0x03}, std::byte{0x04},
      std::byte{0x05}, std::byte{0x06}};
  const std::vector<std::byte> second_bytes(500, std::byte{0x5A});
  auto transfers = std::make_shared<std::vector<recording::ReplayTransfer>>();
  recording::ReplayTransfer first;
  first.status = TransferStatus::Ok;
  first.bytes = first_bytes;
  transfers->push_back(first);
  recording::ReplayTransfer timeout;
  timeout.status = TransferStatus::Timeout;
  transfers->push_back(timeout);
  recording::ReplayTransfer second;
  second.status = TransferStatus::Ok;
  second.bytes = second_bytes;
  transfers->push_back(second);

  SimConfig config;
  config.replay = recording::ReplaySource{transfers};
  config.expect_interval_quirk = false;
  config.frames = 0;
  SimulatedTransport transport(config);
  open_claim(transport);
  const Delivery delivery = deliver_serial(transport, 12, 1024);
  CHECK(delivery.statuses[0] == TransferStatus::Ok);
  CHECK(delivery.sizes[0] == first_bytes.size());
  CHECK(delivery.statuses[1] == TransferStatus::Timeout);
  CHECK(delivery.sizes[1] == 0U);
  std::vector<std::uint8_t> expected;
  for (const auto byte : first_bytes) {
    expected.push_back(std::to_integer<std::uint8_t>(byte));
  }
  for (const auto byte : second_bytes) {
    expected.push_back(std::to_integer<std::uint8_t>(byte));
  }
  CHECK(delivery.bytes == expected);
}

EINSTAR_TEST(sim_fault_storm_is_deterministic) {
  auto run = [] {
    SimConfig config = clean_config(11, 3);
    config.faults.loss_probability = 0.1;
    config.faults.stall_probability = 0.05;
    config.faults.timeout_probability = 0.05;
    config.faults.short_read_probability = 0.3;
    config.faults.corrupt_length_probability = 0.05;
    config.faults.duplicate_header_probability = 0.05;
    config.faults.truncate_frame_probability = 0.2;
    config.faults.reorder_probability = 0.3;
    SimulatedTransport transport(config);
    open_claim(transport);
    return deliver_serial(transport, 40, 0x2000);
  };
  const Delivery first = run();
  const Delivery second = run();
  CHECK(first.bytes == second.bytes);
  CHECK(first.statuses == second.statuses);
  CHECK(first.sizes == second.sizes);
}

EINSTAR_TEST_MAIN()
