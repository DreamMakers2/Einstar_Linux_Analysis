// SPDX-License-Identifier: MIT
#include "einstar/reassembler.hpp"

#include <algorithm>
#include <cstring>
#include <limits>
#include <utility>

namespace einstar {

std::string_view to_string(FrameLossReason reason) noexcept {
  switch (reason) {
    case FrameLossReason::SequenceGap:
      return "sequence-gap";
    case FrameLossReason::PartialTimeout:
      return "partial-timeout";
    case FrameLossReason::Restart:
      return "restart";
    case FrameLossReason::Capacity:
      return "capacity";
    case FrameLossReason::Overflow:
      return "overflow";
    case FrameLossReason::Orphan:
      return "orphan";
    case FrameLossReason::Stale:
      return "stale";
    case FrameLossReason::Flush:
      return "flush";
    case FrameLossReason::PoolExhausted:
      return "pool-exhausted";
  }
  return "unknown";
}

FrameReassembler::FrameReassembler(ReassemblyConfig config)
    : config_(std::move(config)) {
  if (config_.frame_size == 0) {
    config_.frame_size = kDefaultFrameSize;
  }
  if (config_.max_inflight_frames == 0) {
    config_.max_inflight_frames = 1;
  }
  if (config_.width == 0 || config_.height == 0) {
    config_.width = 1280;
    config_.height = 1024;
  }
}

FrameReassembler::Slot* FrameReassembler::find_slot(std::uint8_t camera_id,
                                                    std::uint32_t frame_index) {
  auto it = slots_.find(key(camera_id, frame_index));
  return it == slots_.end() ? nullptr : &it->second;
}

void FrameReassembler::record_loss(std::uint8_t camera_id,
                                   std::uint32_t frame_index,
                                   FrameLossReason reason,
                                   std::uint32_t missing_frames,
                                   std::int64_t host_ns) {
  if (loss_events_.size() >= config_.max_loss_events) {
    return;
  }
  loss_events_.push_back(
      FrameLossEvent{camera_id, frame_index, reason, missing_frames, host_ns});
}

void FrameReassembler::drop_slot(Slot& slot, FrameLossReason reason,
                                 std::int64_t host_ns,
                                 std::uint32_t missing_frames) {
  stats_.frames_dropped++;
  record_loss(slot.camera_id, slot.frame_index, reason, missing_frames, host_ns);
  slots_.erase(key(slot.camera_id, slot.frame_index));
}

void FrameReassembler::enforce_capacity(std::int64_t host_ns) {
  while (slots_.size() >= config_.max_inflight_frames) {
    auto oldest = slots_.begin();
    for (auto it = slots_.begin(); it != slots_.end(); ++it) {
      if (it->second.first_host_ns < oldest->second.first_host_ns) {
        oldest = it;
      }
    }
    const std::uint8_t camera = oldest->second.camera_id;
    const std::uint32_t frame_index = oldest->second.frame_index;
    stats_.capacity_evictions++;
    stats_.frames_dropped++;
    record_loss(camera, frame_index, FrameLossReason::Capacity, 0, host_ns);
    slots_.erase(oldest);
  }
}

Result<FrameReassembler::IngestResult> FrameReassembler::ingest(
    const StreamPacket& packet, BufferHandle storage,
    std::size_t storage_offset, std::int64_t host_ns) {
  stats_.packets++;
  IngestResult result;

  if (packet.payload_size > config_.frame_size) {
    stats_.overflow_errors++;
    return fail(ErrorCode::Protocol,
                "packet payload exceeds configured frame size");
  }

  const std::uint8_t camera = packet.camera_id;
  if (camera == 0) {
    stats_.orphan_packets++;
    return result;
  }

  CameraState& camera_state = cameras_[camera];
  Slot* existing = find_slot(camera, packet.frame_index);
  if (camera_state.completed_any &&
      packet.frame_index <= camera_state.last_completed && existing == nullptr) {
    stats_.stale_packets++;
    return result;
  }

  // Header-only packets (payload 0) are tolerated: they are what a duplicated
  // header looks like on the wire.
  if (packet.payload_size == 0) {
    if (find_slot(camera, packet.frame_index) != nullptr) {
      stats_.duplicate_packets++;
    } else {
      stats_.header_only_packets++;
    }
    return result;
  }

  Slot* slot = existing;
  if (slot != nullptr && packet.first_of_frame) {
    // A second "first" packet for an in-flight frame means the producer
    // restarted the frame (or a duplicate escaped deduplication).
    stats_.frame_restarts++;
    drop_slot(*slot, FrameLossReason::Restart, host_ns);
    slot = nullptr;
  }

  if (slot == nullptr) {
    if (!packet.first_of_frame) {
      // We joined the stream mid-frame: unusable without a chunk index.
      stats_.orphan_packets++;
      record_loss(camera, packet.frame_index, FrameLossReason::Orphan, 0,
                  host_ns);
      return result;
    }

    // Sequence-gap detection is based on the start order of frames.  Counting
    // at completion time would mis-report legitimate cross-frame interleaving.
    if (camera_state.initialized) {
      if (packet.frame_index > camera_state.highest_started) {
        const std::uint32_t missing =
            packet.frame_index - camera_state.highest_started - 1;
        if (missing > 0) {
          stats_.sequence_gaps += missing;
          record_loss(camera, packet.frame_index, FrameLossReason::SequenceGap,
                      missing, host_ns);
        }
      } else if (packet.frame_index < camera_state.highest_started) {
        // A late start of an already-started, already-dropped frame.
        stats_.stale_packets++;
        return result;
      }
    }
    camera_state.highest_started =
        std::max(camera_state.highest_started, packet.frame_index);
    camera_state.initialized = true;

    // Do not evict older partial frames just because a newer frame started:
    // the stream may interleave packets from several frames of the same camera
    // (the simulator's reordering fault, and observed multi-camera firmware).
    // Stale partials are bounded by max_inflight_frames and the partial-frame
    // timeout instead.
    enforce_capacity(host_ns);

    Slot fresh;
    fresh.camera_id = camera;
    fresh.frame_index = packet.frame_index;
    fresh.device_timestamp = packet.device_timestamp;
    fresh.first_host_ns = host_ns;
    fresh.last_host_ns = host_ns;
    if (config_.mode == CopyMode::Copy) {
      if (config_.frame_pool == nullptr) {
        return fail(ErrorCode::InvalidArgument,
                    "copy-mode reassembly requires a frame pool");
      }
      fresh.contiguous = config_.frame_pool->acquire();
      if (!fresh.contiguous.valid()) {
        stats_.pool_exhausted++;
        stats_.frames_dropped++;
        record_loss(camera, packet.frame_index, FrameLossReason::PoolExhausted,
                    0, host_ns);
        return fail(ErrorCode::Exhausted, "frame pool exhausted");
      }
    }
    auto [it, inserted] =
        slots_.emplace(key(camera, packet.frame_index), std::move(fresh));
    (void)inserted;
    slot = &it->second;
  } else {
    slot->last_host_ns = host_ns;
  }

  if (packet.payload_size > 0 &&
      slot->filled + packet.payload_size > config_.frame_size) {
    stats_.overflow_errors++;
    record_loss(camera, packet.frame_index, FrameLossReason::Overflow, 0,
                host_ns);
    drop_slot(*slot, FrameLossReason::Overflow, host_ns);
    return fail(ErrorCode::Overflow, "frame overflow: payload exceeds frame size");
  }

  if (config_.mode == CopyMode::Copy) {
    if (!slot->contiguous.valid()) {
      return fail(ErrorCode::Internal, "copy-mode slot has no frame buffer");
    }
    auto bytes = slot->contiguous.bytes();
    if (slot->filled + packet.payload_size > bytes.size()) {
      return fail(ErrorCode::Overflow, "frame buffer smaller than frame size");
    }
    if (packet.payload_size > 0) {
      std::memcpy(bytes.data() + slot->filled, packet.payload,
                  packet.payload_size);
    }
  } else {
    if (!storage.valid()) {
      stats_.pool_exhausted++;
      return fail(ErrorCode::Exhausted,
                  "zero-copy packet has no storage handle");
    }
    if (storage_offset + packet.payload_size > storage.capacity()) {
      return fail(ErrorCode::InvalidArgument,
                  "zero-copy chunk exceeds storage capacity");
    }
    slot->chunks.push_back(
        FrameChunk{std::move(storage), storage_offset, packet.payload_size});
  }

  slot->filled += packet.payload_size;
  slot->chunk_count++;
  result.accepted = true;

  if (slot->filled == config_.frame_size) {
    Frame frame;
    frame.camera_id = slot->camera_id;
    frame.frame_index = slot->frame_index;
    frame.width = config_.width;
    frame.height = config_.height;
    frame.payload_size = config_.frame_size;
    frame.mode = config_.mode;
    frame.device_timestamp = slot->device_timestamp;
    frame.host_timestamp_ns = slot->first_host_ns;
    frame.assembly_ns = std::max<std::int64_t>(0, host_ns - slot->first_host_ns);
    if (config_.mode == CopyMode::Copy) {
      frame.chunks.push_back(
          FrameChunk{std::move(slot->contiguous), 0, config_.frame_size});
    } else {
      frame.chunks = std::move(slot->chunks);
    }
    camera_state.last_completed =
        camera_state.completed_any
            ? std::max(camera_state.last_completed, slot->frame_index)
            : slot->frame_index;
    camera_state.completed_any = true;
    stats_.frames_completed++;
    slots_.erase(key(camera, slot->frame_index));
    result.completed = std::move(frame);
  }

  return result;
}

std::vector<FrameLossEvent> FrameReassembler::expire(std::int64_t now_ns) {
  std::vector<FrameLossEvent> result;
  std::vector<std::uint64_t> victims;
  for (const auto& [slot_key, slot] : slots_) {
    const std::int64_t age = now_ns - slot.last_host_ns;
    if (age > config_.partial_frame_timeout_ns) {
      victims.push_back(slot_key);
    }
  }
  for (const std::uint64_t victim : victims) {
    auto it = slots_.find(victim);
    if (it == slots_.end()) {
      continue;
    }
    stats_.partial_timeouts++;
    stats_.frames_dropped++;
    record_loss(it->second.camera_id, it->second.frame_index,
                FrameLossReason::PartialTimeout, 0, now_ns);
    result.push_back(FrameLossEvent{it->second.camera_id,
                                    it->second.frame_index,
                                    FrameLossReason::PartialTimeout, 0, now_ns});
    slots_.erase(it);
  }
  return result;
}

std::vector<FrameLossEvent> FrameReassembler::flush(std::int64_t now_ns) {
  std::vector<FrameLossEvent> result;
  for (auto& [slot_key, slot] : slots_) {
    (void)slot_key;
    stats_.frames_dropped++;
    record_loss(slot.camera_id, slot.frame_index, FrameLossReason::Flush, 0,
                now_ns);
    result.push_back(FrameLossEvent{slot.camera_id, slot.frame_index,
                                    FrameLossReason::Flush, 0, now_ns});
  }
  slots_.clear();
  return result;
}

std::vector<FrameLossEvent> FrameReassembler::take_loss_events() {
  std::vector<FrameLossEvent> result;
  result.swap(loss_events_);
  return result;
}

std::uint32_t FrameReassembler::expected_next(std::uint8_t camera_id) const {
  auto it = cameras_.find(camera_id);
  if (it == cameras_.end()) {
    return 0;
  }
  if (it->second.completed_any) {
    return it->second.last_completed + 1;
  }
  return it->second.initialized ? it->second.highest_started : 0;
}

void FrameReassembler::reset() {
  slots_.clear();
  cameras_.clear();
  loss_events_.clear();
  stats_ = ReassemblyStats{};
}

}  // namespace einstar
