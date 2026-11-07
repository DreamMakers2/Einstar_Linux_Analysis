// SPDX-License-Identifier: MIT
// Frame reassembly keyed by (camera_id, frame_index), with gap detection,
// duplicate/orphan handling, partial-frame timeouts and bounded in-flight state.
#pragma once

#include <cstdint>
#include <map>
#include <optional>
#include <string_view>
#include <vector>

#include "einstar/frame.hpp"
#include "einstar/packet.hpp"
#include "einstar/status.hpp"

namespace einstar {

enum class FrameLossReason : std::uint8_t {
  SequenceGap = 0,
  PartialTimeout = 1,
  Restart = 2,
  Capacity = 3,
  Overflow = 4,
  Orphan = 5,
  Stale = 6,
  Flush = 7,
  PoolExhausted = 8,
};

std::string_view to_string(FrameLossReason reason) noexcept;

struct FrameLossEvent {
  std::uint8_t camera_id = 0;
  std::uint32_t frame_index = 0;
  FrameLossReason reason = FrameLossReason::SequenceGap;
  std::uint32_t missing_frames = 0;
  std::int64_t host_ns = 0;
};

struct ReassemblyStats {
  std::uint64_t packets = 0;
  std::uint64_t duplicate_packets = 0;
  std::uint64_t orphan_packets = 0;
  std::uint64_t header_only_packets = 0;
  std::uint64_t frame_restarts = 0;
  std::uint64_t sequence_gaps = 0;
  std::uint64_t partial_timeouts = 0;
  std::uint64_t overflow_errors = 0;
  std::uint64_t stale_packets = 0;
  std::uint64_t frames_completed = 0;
  std::uint64_t frames_dropped = 0;
  std::uint64_t capacity_evictions = 0;
  std::uint64_t pool_exhausted = 0;
};

struct ReassemblyConfig {
  std::uint32_t frame_size = kDefaultFrameSize;
  std::uint32_t width = 1280;
  std::uint32_t height = 1024;
  std::size_t max_inflight_frames = 16;
  std::int64_t partial_frame_timeout_ns = 100'000'000;  // 100 ms
  CopyMode mode = CopyMode::Copy;
  /// Required in Copy mode: supplies one contiguous frame buffer per partial
  /// frame.  Ignored in ZeroCopy mode.
  BufferPool* frame_pool = nullptr;
  /// Optional: maximum number of loss events retained for telemetry.  Events
  /// beyond this are dropped (counters still increment).
  std::size_t max_loss_events = 4096;
};

class FrameReassembler {
 public:
  struct IngestResult {
    bool accepted = false;
    std::optional<Frame> completed;
  };

  explicit FrameReassembler(ReassemblyConfig config);

  /// Feed one parsed packet.  `storage` is the preallocated block that owns
  /// pkt.payload when zero-copy retention is wanted; `storage_offset` is the
  /// payload offset inside it.  Copy mode ignores both and copies the payload
  /// into the frame's contiguous buffer.
  Result<IngestResult> ingest(const StreamPacket& packet, BufferHandle storage,
                              std::size_t storage_offset, std::int64_t host_ns);

  /// Drop partial frames older than the configured timeout.
  std::vector<FrameLossEvent> expire(std::int64_t now_ns);

  /// Drop every partial frame (shutdown path).
  std::vector<FrameLossEvent> flush(std::int64_t now_ns);

  /// Loss events accumulated since the last call, capped at max_loss_events.
  std::vector<FrameLossEvent> take_loss_events();

  [[nodiscard]] const ReassemblyStats& stats() const noexcept { return stats_; }
  [[nodiscard]] std::size_t inflight() const noexcept { return slots_.size(); }
  [[nodiscard]] std::uint32_t expected_next(std::uint8_t camera_id) const;
  [[nodiscard]] const ReassemblyConfig& config() const noexcept { return config_; }

  void reset();

 private:
  struct Slot {
    std::uint8_t camera_id = 0;
    std::uint32_t frame_index = 0;
    std::uint64_t device_timestamp = 0;
    std::int64_t first_host_ns = 0;
    std::int64_t last_host_ns = 0;
    std::size_t filled = 0;
    std::size_t chunk_count = 0;
    BufferHandle contiguous;
    std::vector<FrameChunk> chunks;
  };

  struct CameraState {
    bool initialized = false;
    bool completed_any = false;
    std::uint32_t last_completed = 0;
    std::uint32_t highest_started = 0;
    std::uint32_t expected_next = 0;
  };

  static std::uint64_t key(std::uint8_t camera_id, std::uint32_t frame_index) {
    return (static_cast<std::uint64_t>(camera_id) << 32U) | frame_index;
  }

  Slot* find_slot(std::uint8_t camera_id, std::uint32_t frame_index);
  void drop_slot(Slot& slot, FrameLossReason reason, std::int64_t host_ns,
                 std::uint32_t missing_frames = 0);
  void record_loss(std::uint8_t camera_id, std::uint32_t frame_index,
                   FrameLossReason reason, std::uint32_t missing_frames,
                   std::int64_t host_ns);
  void enforce_capacity(std::int64_t host_ns);

  ReassemblyConfig config_;
  std::map<std::uint64_t, Slot> slots_;
  std::map<std::uint8_t, CameraState> cameras_;
  std::vector<FrameLossEvent> loss_events_;
  ReassemblyStats stats_;
};

}  // namespace einstar
