// SPDX-License-Identifier: MIT
// Frame reassembly for the Einstar image stream.
//
// Wire semantics (verified: einscan10.2.0.0.dll::ImageStreamThread::run and the
// hardware-proven libeinstar parser):
//   * the frame id is only valid on the packet whose header[8] == 1 (the LAST
//     packet), so a frame cannot be keyed by its id while it is being received;
//   * there is no "first packet" flag: a frame starts implicitly after the
//     previous one completed, and the timestamp is latched from the first packet
//     the reassembler sees for that frame;
//   * camera selection is the one-hot field in header byte 2 bits 3..5.
//
// Therefore the reassembler keeps exactly ONE in-flight frame per camera and
// completes it when a last-flagged packet arrives after exactly frame_size
// payload bytes.  A size mismatch, overflow or timeout drops that camera's
// partial frame; the next packet starts a new one.
#pragma once

#include <array>
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
  Incomplete = 9,   // last packet arrived but the payload size did not match
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
  std::uint64_t size_mismatch = 0;
  std::uint64_t invalid_camera = 0;
  /// Per-reason drop counters (sum == frames_dropped).  These make a live
  /// anomaly actionable: a burst of `dropped_incomplete` is lost packets, a
  /// `dropped_stale` burst is duplicated/replayed device frames.
  std::uint64_t dropped_incomplete = 0;
  std::uint64_t dropped_overflow = 0;
  std::uint64_t dropped_stale = 0;
  std::uint64_t dropped_capacity = 0;
  std::uint64_t dropped_pool = 0;
  std::uint64_t dropped_timeout = 0;
  std::uint64_t dropped_flush = 0;
  std::uint64_t dropped_sequence_gap = 0;
  std::uint64_t packet_index_gaps = 0;
  /// The 8-bit slot counter (wire header 12..15) repeated while the u64 trigger
  /// group advanced: the frame was accepted as the next one instead of being
  /// rejected as stale.
  std::uint64_t slot_id_repeats = 0;
};

struct ReassemblyConfig {
  std::uint32_t frame_size = kDefaultFrameSize;
  std::uint32_t width = 1280;
  std::uint32_t height = 1024;
  /// Kept for API compatibility; the wire format allows one in-flight frame
  /// per camera (3 total).
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
  [[nodiscard]] std::size_t inflight() const noexcept;
  [[nodiscard]] std::uint32_t expected_next(std::uint8_t camera_id) const;
  [[nodiscard]] const ReassemblyConfig& config() const noexcept { return config_; }

  void reset();

 private:
  struct Slot {
    std::uint8_t camera_index = kInvalidCamera;
    std::uint64_t device_timestamp = 0;
    std::int64_t first_host_ns = 0;
    std::int64_t last_host_ns = 0;
    std::size_t filled = 0;
    std::size_t chunk_count = 0;
    /// Frame-relative index the next packet must carry (hardware header bytes
    /// 9..11).  A mismatch means a packet was lost or duplicated.
    std::uint32_t expected_packet_index = 0;
    BufferHandle contiguous;
    std::vector<FrameChunk> chunks;
  };

  struct CameraState {
    bool completed_any = false;
    std::uint32_t last_completed = 0;
    std::uint32_t expected_next = 0;   // last_completed + 1 once initialised
    /// u64 trigger-group counter of the last completed frame (hardware header
    /// bytes 16..23).  It is monotonic on real hardware and is used only to
    /// rescue the 8-bit slot counter if firmware ever repeats a slot id.
    std::uint64_t last_group = 0;
    std::uint64_t packets = 0;
    std::uint64_t dropped = 0;
  };

  Slot* find_slot(std::uint8_t camera_index);
  void drop_slot(Slot& slot, FrameLossReason reason, std::int64_t host_ns,
                 std::uint32_t missing_frames = 0);
  void record_loss(std::uint8_t camera_index, std::uint32_t frame_index,
                   FrameLossReason reason, std::uint32_t missing_frames,
                   std::int64_t host_ns);

  ReassemblyConfig config_;
  std::array<std::optional<Slot>, kCameraCount> slots_;
  std::array<CameraState, kCameraCount> cameras_{};
  std::vector<FrameLossEvent> loss_events_;
  ReassemblyStats stats_;
};

}  // namespace einstar
