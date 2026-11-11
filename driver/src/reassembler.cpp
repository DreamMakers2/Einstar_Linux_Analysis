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
    case FrameLossReason::Incomplete:
      return "incomplete";
  }
  return "unknown";
}

FrameReassembler::FrameReassembler(ReassemblyConfig config)
    : config_(std::move(config)) {
  if (config_.frame_size == 0) {
    config_.frame_size = kDefaultFrameSize;
  }
  if (config_.width == 0 || config_.height == 0) {
    config_.width = 1280;
    config_.height = 1024;
  }
}

FrameReassembler::Slot* FrameReassembler::find_slot(
    std::uint8_t camera_index) {
  if (camera_index >= kCameraCount) {
    return nullptr;
  }
  auto& slot = slots_[camera_index];
  return slot ? &*slot : nullptr;
}

std::size_t FrameReassembler::inflight() const noexcept {
  std::size_t n = 0;
  for (const auto& slot : slots_) {
    if (slot) {
      ++n;
    }
  }
  return n;
}

void FrameReassembler::record_loss(std::uint8_t camera_index,
                                   std::uint32_t frame_index,
                                   FrameLossReason reason,
                                   std::uint32_t missing_frames,
                                   std::int64_t host_ns) {
  stats_.frames_dropped++;
  if (camera_index < kCameraCount) {
    cameras_[camera_index].dropped++;
  }
  switch (reason) {
    case FrameLossReason::SequenceGap:
      stats_.sequence_gaps += missing_frames ? missing_frames : 1;
      stats_.dropped_sequence_gap++;
      break;
    case FrameLossReason::PartialTimeout:
      stats_.partial_timeouts++;
      stats_.dropped_timeout++;
      break;
    case FrameLossReason::Restart:
      stats_.frame_restarts++;
      break;
    case FrameLossReason::Capacity:
      stats_.capacity_evictions++;
      stats_.dropped_capacity++;
      break;
    case FrameLossReason::Overflow:
      stats_.overflow_errors++;
      stats_.dropped_overflow++;
      break;
    case FrameLossReason::PoolExhausted:
      stats_.pool_exhausted++;
      stats_.dropped_pool++;
      break;
    case FrameLossReason::Incomplete:
      stats_.size_mismatch++;
      stats_.dropped_incomplete++;
      break;
    case FrameLossReason::Stale:
      stats_.dropped_stale++;
      break;
    case FrameLossReason::Flush:
      stats_.dropped_flush++;
      break;
    default:
      break;
  }
  if (loss_events_.size() < config_.max_loss_events) {
    loss_events_.push_back(FrameLossEvent{camera_index, frame_index, reason,
                                          missing_frames, host_ns});
  }
}

void FrameReassembler::drop_slot(Slot& slot, FrameLossReason reason,
                                 std::int64_t host_ns,
                                 std::uint32_t missing_frames) {
  // The frame id is unknown until the last packet; for a dropped partial frame
  // report the camera's expected id so telemetry stays monotonic.
  const std::uint32_t guess =
      slot.camera_index < kCameraCount
          ? cameras_[slot.camera_index].expected_next
          : 0;
  record_loss(slot.camera_index, guess, reason, missing_frames, host_ns);
  slots_[slot.camera_index].reset();
}

Result<FrameReassembler::IngestResult> FrameReassembler::ingest(
    const StreamPacket& packet, BufferHandle storage, std::size_t storage_offset,
    std::int64_t host_ns) {
  IngestResult result;
  stats_.packets++;

  // Header-only packets (payload 0) are what a duplicated or padded header
  // looks like on the wire; tolerate and account for them.
  if (packet.payload_size == 0) {
    stats_.header_only_packets++;
    return result;
  }

  const std::uint8_t camera = packet.camera_index;
  if (camera >= kCameraCount) {
    // Not a camera selector from this device (noise or a foreign package).
    stats_.invalid_camera++;
    stats_.orphan_packets++;
    return result;
  }
  cameras_[camera].packets++;

  Slot* slot = find_slot(camera);
  if (slot == nullptr) {
    Slot fresh;
    fresh.camera_index = camera;
    fresh.device_timestamp = packet.device_timestamp;  // first packet we see
    fresh.first_host_ns = host_ns;
    fresh.last_host_ns = host_ns;
    if (config_.mode == CopyMode::Copy) {
      if (config_.frame_pool == nullptr) {
        return fail(ErrorCode::InvalidArgument,
                    "copy mode requires a frame pool");
      }
      auto handle = config_.frame_pool->acquire();
      if (!handle.valid()) {
        record_loss(camera, cameras_[camera].expected_next,
                    FrameLossReason::PoolExhausted, 0, host_ns);
        return fail(ErrorCode::Exhausted, "frame pool exhausted");
      }
      fresh.contiguous = std::move(handle);
    }
    slots_[camera] = std::move(fresh);
    slot = &*slots_[camera];
  }

  // Deterministic packet-continuity check: the wire carries a frame-relative
  // packet index, so a lost or duplicated packet is detected here instead of
  // only being inferred from a size mismatch at the last packet.
  if (slot->chunk_count == 0) {
    if (packet.packet_index != 0) {
      stats_.packet_index_gaps++;
    }
  } else if (packet.packet_index != slot->expected_packet_index) {
    stats_.packet_index_gaps++;
  }
  slot->expected_packet_index = packet.packet_index + 1;

  if (slot->filled + packet.payload_size > config_.frame_size) {
    // More data than a frame can hold: the stream lost sync for this camera.
    drop_slot(*slot, FrameLossReason::Overflow, host_ns);
    return result;
  }

  if (config_.mode == CopyMode::Copy) {
    auto* destination = slot->contiguous.data();
    if (destination == nullptr) {
      return fail(ErrorCode::InvalidArgument, "frame buffer is not mapped");
    }
    std::memcpy(destination + slot->filled, packet.payload, packet.payload_size);
  } else {
    if (!storage.valid()) {
      return fail(ErrorCode::InvalidArgument,
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
  slot->last_host_ns = host_ns;
  result.accepted = true;

  if (!packet.last_of_frame) {
    return result;
  }

  // ---- completion ----
  if (slot->filled != config_.frame_size) {
    // A last packet that does not complete the frame means packets were lost
    // (the vendor logs "missed hasReceived ...").  Drop; the next packet starts
    // a fresh frame for this camera.
    drop_slot(*slot, FrameLossReason::Incomplete, host_ns);
    return result;
  }

  // The slot counter in wire header 12..15 is only EIGHT bits wide and wraps
  // 255 -> 0 every 256 trigger groups (measured on the live unit 2026-11-11
  // over 1,200 consecutive groups).  Ordering therefore has to be modular; a
  // plain `frame_id <= last_completed` test declares every frame stale forever
  // once the counter wraps, which silently stops all capture after 256 groups.
  const std::uint32_t frame_id = packet.frame_index;   // 8-bit slot, wraps
  const std::uint64_t group = slot->device_timestamp;  // u64 trigger group
  CameraState& camera_state = cameras_[camera];
  std::uint32_t advance = 1;
  if (camera_state.completed_any) {
    const std::uint32_t delta =
        (frame_id - camera_state.last_completed) & 0xFFU;
    if (delta == 0) {
      // Same slot id.  If the monotonic u64 group counter moved on, the slot
      // counter is saturating rather than the frame being a duplicate; accept
      // it as the next frame and record the anomaly.
      if (group != 0 && camera_state.last_group != 0 &&
          group > camera_state.last_group) {
        stats_.slot_id_repeats++;
        advance = 1;
      } else {
        stats_.stale_packets++;
        record_loss(camera, frame_id, FrameLossReason::Stale, 0, host_ns);
        slots_[camera].reset();
        return result;
      }
    } else {
      advance = delta;
    }
    if (advance > 1) {
      const std::uint32_t missing = advance - 1;
      record_loss(camera, camera_state.expected_next,
                  FrameLossReason::SequenceGap, missing, host_ns);
    }
  }

  Frame frame;
  frame.camera_id = camera;  // 0-based camera index
  frame.frame_index = frame_id;
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

  camera_state.last_completed = frame_id;
  camera_state.expected_next = frame_id + 1;
  if (group != 0) {
    camera_state.last_group = group;
  }
  camera_state.completed_any = true;
  stats_.frames_completed++;
  slots_[camera].reset();
  result.completed = std::move(frame);
  return result;
}

std::vector<FrameLossEvent> FrameReassembler::expire(std::int64_t now_ns) {
  std::vector<FrameLossEvent> events;
  for (auto& slot : slots_) {
    if (!slot) {
      continue;
    }
    if (now_ns - slot->last_host_ns <= config_.partial_frame_timeout_ns) {
      continue;
    }
    const std::uint8_t camera = slot->camera_index;
    const std::uint32_t guess =
        camera < kCameraCount ? cameras_[camera].expected_next : 0;
    record_loss(camera, guess, FrameLossReason::PartialTimeout, 0, now_ns);
    events.push_back(FrameLossEvent{camera, guess,
                                    FrameLossReason::PartialTimeout, 0, now_ns});
    slot.reset();
  }
  return events;
}

std::vector<FrameLossEvent> FrameReassembler::flush(std::int64_t now_ns) {
  std::vector<FrameLossEvent> events;
  for (auto& slot : slots_) {
    if (!slot) {
      continue;
    }
    const std::uint8_t camera = slot->camera_index;
    const std::uint32_t guess =
        camera < kCameraCount ? cameras_[camera].expected_next : 0;
    record_loss(camera, guess, FrameLossReason::Flush, 0, now_ns);
    events.push_back(
        FrameLossEvent{camera, guess, FrameLossReason::Flush, 0, now_ns});
    slot.reset();
  }
  return events;
}

std::vector<FrameLossEvent> FrameReassembler::take_loss_events() {
  std::vector<FrameLossEvent> out;
  out.swap(loss_events_);
  return out;
}

std::uint32_t FrameReassembler::expected_next(std::uint8_t camera_index) const {
  if (camera_index >= kCameraCount) {
    return 0;
  }
  return cameras_[camera_index].expected_next;
}

void FrameReassembler::reset() {
  for (auto& slot : slots_) {
    slot.reset();
  }
  for (auto& camera : cameras_) {
    camera = {};
  }
  loss_events_.clear();
  stats_ = {};
}

}  // namespace einstar
