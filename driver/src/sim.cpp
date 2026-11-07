// SPDX-License-Identifier: MIT
#include "einstar/sim.hpp"

#include <algorithm>
#include <array>
#include <cstring>
#include <limits>
#include <utility>

#include "einstar/packet.hpp"

namespace einstar {
namespace {

std::uint64_t mix64(std::uint64_t value) {
  value += 0x9E3779B97F4A7C15ULL;
  value = (value ^ (value >> 30U)) * 0xBF58476D1CE4E5B9ULL;
  value = (value ^ (value >> 27U)) * 0x94D049BB133111EBULL;
  return value ^ (value >> 31U);
}

constexpr std::uint64_t kFnvOffset = 14695981039346656037ULL;

}  // namespace

bool FaultConfig::clean() const noexcept {
  return loss_probability <= 0.0 && reorder_probability <= 0.0 &&
         short_read_probability <= 0.0 && stall_probability <= 0.0 &&
         timeout_probability <= 0.0 && corrupt_length_probability <= 0.0 &&
         duplicate_header_probability <= 0.0 &&
         truncate_frame_probability <= 0.0 && injected_latency_ns == 0;
}

std::uint8_t simulated_payload_byte(std::uint32_t frame_index,
                                    std::uint32_t chunk_index,
                                    std::size_t offset_in_chunk) noexcept {
  return static_cast<std::uint8_t>(
      (frame_index * 131U + chunk_index * 17U +
       static_cast<std::uint32_t>(offset_in_chunk & 0xFFU)) &
      0xFFU);
}

void fill_simulated_payload(std::uint32_t frame_index,
                            std::uint32_t chunk_index,
                            std::span<std::byte> out) noexcept {
  std::array<std::byte, 256> block{};
  for (std::size_t i = 0; i < block.size(); ++i) {
    block[i] = static_cast<std::byte>(
        simulated_payload_byte(frame_index, chunk_index, i));
  }
  std::size_t offset = 0;
  while (offset < out.size()) {
    const std::size_t take = std::min(block.size(), out.size() - offset);
    std::memcpy(out.data() + offset, block.data(), take);
    offset += take;
  }
}

std::uint64_t simulated_frame_digest(std::uint32_t frame_index,
                                     std::uint32_t frame_size,
                                     std::uint32_t chunk_payload) noexcept {
  std::uint64_t hash = kFnvOffset;
  const std::uint32_t chunks =
      chunk_payload == 0 ? 1 : (frame_size + chunk_payload - 1) / chunk_payload;
  for (std::uint32_t chunk = 0; chunk < chunks; ++chunk) {
    const std::size_t offset =
        static_cast<std::size_t>(chunk) * chunk_payload;
    const std::size_t size = std::min<std::size_t>(
        chunk_payload, static_cast<std::size_t>(frame_size) -
                           std::min<std::size_t>(frame_size, offset));
    for (std::size_t i = 0; i < size; ++i) {
      hash ^= static_cast<std::uint64_t>(
          simulated_payload_byte(frame_index, chunk, i));
      hash *= 1099511628211ULL;
    }
  }
  return hash;
}

SimulatedTransport::SimulatedTransport(SimConfig config)
    : config_(std::move(config)) {
  if (config_.frame_size == 0) {
    config_.frame_size = 1280U * 1024U;
  }
  if (config_.chunk_payload == 0) {
    config_.chunk_payload = 0xA000;
  }
  if (config_.bandwidth_bytes_per_sec == 0) {
    config_.bandwidth_bytes_per_sec = 1;
  }
  if (config_.second_camera_id == 0) {
    config_.second_camera_id = 2;
  }

  InterfaceInfo interface_info;
  interface_info.number = 0;
  interface_info.alternate_setting = 0;
  interface_info.class_code = 0xEF;  // vendor specific
  interface_info.subclass = 0x02;
  interface_info.protocol = 0x01;
  interface_info.endpoints = {
      EndpointInfo{0x01, 0x03, 64, 0, 0},
      EndpointInfo{0x81, 0x03, 64, 0, 0},
      EndpointInfo{0x02, 0x02, 512, 0, 0},
      EndpointInfo{0x82, 0x02, 512, 0, 0},
      EndpointInfo{0x83, 0x02, 512, 0, 0},
  };
  descriptor_ = UsbConfigDescriptor::make_synthetic(
      1, {std::move(interface_info)}, /*zero_intervals=*/true);
  patched_ = !config_.require_interval_patch;
}

SimulatedTransport::~SimulatedTransport() = default;

Status SimulatedTransport::open() {
  if (open_) {
    return Status::error(ErrorCode::AlreadyOpen, "simulator already open");
  }
  open_ = true;
  claimed_ = false;
  return Status::success();
}

void SimulatedTransport::close() {
  if (!open_) {
    return;
  }
  for (auto& [id, pending] : pending_) {
    if (pending.done) {
      continue;
    }
    TransferCompletion completion;
    completion.id = id;
    completion.status = TransferStatus::Cancelled;
    completion.bytes_transferred = 0;
    completion.host_time_ns = clock_.now_ns();
    completion.user = pending.user;
    if (pending.sink != nullptr) {
      pending.sink->on_transfer_complete(completion);
    }
  }
  pending_.clear();
  while (!events_.empty()) {
    events_.pop();
  }
  open_ = false;
  claimed_ = false;
}

Status SimulatedTransport::claim_interface(std::uint8_t interface_number) {
  if (!open_) {
    return Status::error(ErrorCode::NotOpen, "simulator is not open");
  }
  if (interface_number != 0) {
    return Status::error(ErrorCode::InvalidArgument,
                         "Einstar only exposes interface 0");
  }
  if (config_.require_interval_patch && !patched_) {
    std::string message =
        "interrupt endpoints declare bInterval=0; apply_descriptor_patch() "
        "before claiming (endpoints:";
    for (const auto address : descriptor_.zero_interval_interrupt_endpoints()) {
      message += " 0x";
      constexpr char hex[] = "0123456789ABCDEF";
      message += hex[(address >> 4U) & 0x0FU];
      message += hex[address & 0x0FU];
    }
    message += ")";
    return Status::error(ErrorCode::DescriptorQuirk, std::move(message));
  }
  claimed_ = true;
  return Status::success();
}

Status SimulatedTransport::release_interface(std::uint8_t interface_number) {
  if (interface_number != 0) {
    return Status::error(ErrorCode::InvalidArgument, "invalid interface");
  }
  claimed_ = false;
  return Status::success();
}

Status SimulatedTransport::apply_descriptor_patch() {
  auto patched = descriptor_.patch_zero_intervals(7);
  if (!patched.has_value()) {
    return patched.error();
  }
  patched_ = descriptor_.zero_interval_interrupt_endpoints().empty();
  return Status::success();
}

Status SimulatedTransport::clear_halt(std::uint8_t endpoint) {
  halted_[endpoint] = false;
  return Status::success();
}

bool SimulatedTransport::halted(std::uint8_t endpoint) const {
  const auto it = halted_.find(endpoint);
  return it != halted_.end() && it->second;
}

std::uint64_t SimulatedTransport::hash(std::uint64_t a, std::uint64_t b,
                                       std::uint64_t c) const noexcept {
  return mix64(config_.seed ^ mix64(a ^ (b << 32U) ^ (c * 0x9E3779B97F4A7C15ULL)));
}

bool SimulatedTransport::chance(double probability, std::uint64_t a,
                                std::uint64_t b, std::uint64_t c) const noexcept {
  if (probability <= 0.0) {
    return false;
  }
  if (probability >= 1.0) {
    return true;
  }
  constexpr std::uint64_t kScale = 1'000'000;
  const auto threshold = static_cast<std::uint64_t>(
      probability * static_cast<double>(kScale));
  return (hash(a, b, c) % kScale) < threshold;
}

void SimulatedTransport::append_stream(const std::byte* data, std::size_t size) {
  stream_.insert(stream_.end(), data, data + size);
  stream_generated_total_ += size;
}

void SimulatedTransport::start_frame(GenFrame& frame, std::uint32_t frame_index,
                                     std::uint8_t camera_id) {
  frame.frame_index = frame_index;
  frame.camera_id = camera_id;
  frame.chunk = 0;
  frame.started = true;
  const std::uint32_t chunks =
      (config_.frame_size + config_.chunk_payload - 1) / config_.chunk_payload;
  frame.chunks_total = chunks == 0 ? 1 : chunks;
  frame.chunks_emit = frame.chunks_total;
  if (chance(config_.faults.truncate_frame_probability, frame_index, 0xFFFF,
             0x71)) {
    const std::uint32_t max_missing =
        frame.chunks_total > 1 ? frame.chunks_total - 1 : 1;
    const std::uint32_t missing =
        1 + static_cast<std::uint32_t>(hash(frame_index, 0x72, 0) % max_missing);
    frame.chunks_emit = frame.chunks_total > missing
                            ? frame.chunks_total - missing
                            : 1;
  }
}

std::vector<std::byte> SimulatedTransport::build_packet_bytes(
    const GenFrame& frame, std::uint32_t chunk) {
  std::vector<std::byte> output;
  const std::size_t byte_offset =
      static_cast<std::size_t>(chunk) * config_.chunk_payload;
  if (byte_offset >= config_.frame_size) {
    return output;
  }
  const std::uint32_t payload_size = static_cast<std::uint32_t>(
      std::min<std::size_t>(config_.chunk_payload,
                            config_.frame_size - byte_offset));

  std::array<std::byte, kStreamHeaderSize> header{};
  header[2] = static_cast<std::byte>(
      static_cast<std::uint8_t>(frame.camera_id & 0x07U) |
      static_cast<std::uint8_t>((chunk & 0x07U) << 3U));
  header[8] = static_cast<std::byte>(chunk == 0 ? 1 : 0);
  store_be32(header.data() + 12, frame.frame_index);
  const std::uint64_t timestamp =
      static_cast<std::uint64_t>(frame.frame_index) *
      static_cast<std::uint64_t>(
          config_.trigger_period_ns > 0 ? config_.trigger_period_ns : 1);
  store_be64(header.data() + 16, timestamp);

  std::uint32_t declared = kStreamHeaderSize + payload_size;
  if (chance(config_.faults.corrupt_length_probability, frame.frame_index,
             chunk, 0x73)) {
    static constexpr std::uint32_t kBadLengths[] = {0U, 31U, 0x00FFFFFFU,
                                                    0xA500U};
    declared = kBadLengths[hash(frame.frame_index, chunk, 0x74) % 4U];
  }

  // Duplicate header-only packet in front of the real one.
  if (chance(config_.faults.duplicate_header_probability, frame.frame_index,
             chunk, 0x75)) {
    const std::size_t offset = output.size();
    output.resize(offset + kStreamHeaderSize + kStreamLengthSize);
    store_be32(output.data() + offset, kStreamHeaderSize);
    std::memcpy(output.data() + offset + kStreamLengthSize, header.data(),
                kStreamHeaderSize);
  }

  if (declared == kStreamHeaderSize && payload_size > 0) {
    // Header-only duplicate marker: keep declared length consistent.
    declared = kStreamHeaderSize + payload_size;
  }
  const std::size_t start = output.size();
  output.resize(start + kStreamLengthSize + kStreamHeaderSize + payload_size);
  store_be32(output.data() + start, declared);
  std::memcpy(output.data() + start + kStreamLengthSize, header.data(),
              kStreamHeaderSize);
  fill_simulated_payload(
      frame.frame_index, chunk,
      std::span<std::byte>(output.data() + start + kStreamLengthSize +
                               kStreamHeaderSize,
                           payload_size));
  return output;
}

void SimulatedTransport::generate_one(GenFrame& frame) {
  if (!frame.started) {
    return;
  }
  auto bytes = build_packet_bytes(frame, frame.chunk);
  if (!bytes.empty()) {
    const std::int64_t availability =
        config_.trigger_period_ns > 0
            ? static_cast<std::int64_t>(frame.frame_index) *
                  config_.trigger_period_ns
            : 0;
    append_stream(bytes.data(), bytes.size());
    stream_marks_.push_back(
        AvailabilityMark{stream_generated_total_, availability});
    generated_packets_++;
  }
  frame.chunk++;
  if (frame.chunk >= frame.chunks_emit) {
    frame.started = false;
    generated_frames_++;
  }
}

void SimulatedTransport::generate_packet() {
  const bool interleaving = config_.interleave_frames || reorder_pair_;
  if (interleaving) {
    bool produced = false;
    for (int attempt = 0; attempt < 2 && !produced; ++attempt) {
      const int index = emit_index_;
      emit_index_ ^= 1;
      if (!gen_[index].started) {
        if (next_frame_ >= config_.frames) {
          continue;
        }
        const std::uint8_t camera =
            config_.interleave_frames
                ? ((next_frame_ % 2U) == 0U ? config_.camera_id
                                           : config_.second_camera_id)
                : config_.camera_id;
        start_frame(gen_[index], next_frame_, camera);
        next_frame_++;
      }
      if (gen_[index].started) {
        generate_one(gen_[index]);
        produced = true;
      }
    }
    if (!produced) {
      if (config_.interleave_frames) {
        generator_done_ = true;
        return;
      }
      reorder_pair_ = false;
      // Fall through to the single-frame path.
    } else {
      if (!gen_[0].started && !gen_[1].started) {
        reorder_pair_ = false;
      }
      if (next_frame_ >= config_.frames && !gen_[0].started &&
          !gen_[1].started) {
        generator_done_ = true;
      }
      return;
    }
  }

  if (!gen_[0].started) {
    if (next_frame_ >= config_.frames) {
      generator_done_ = true;
      return;
    }
    start_frame(gen_[0], next_frame_, config_.camera_id);
    next_frame_++;
    if (chance(config_.faults.reorder_probability, next_frame_, 0x90, 0)) {
      reorder_pair_ = true;
      emit_index_ = 1;
    }
  }
  generate_one(gen_[0]);
  if (next_frame_ >= config_.frames && !gen_[0].started && !gen_[1].started) {
    generator_done_ = true;
  }
}

void SimulatedTransport::ensure_stream(std::size_t bytes) {
  while (stream_.size() - stream_offset_ < bytes && !generator_done_) {
    generate_packet();
  }
  if (stream_offset_ > (1U << 20U) && stream_offset_ * 2U > stream_.size()) {
    stream_.erase(stream_.begin(),
                  stream_.begin() + static_cast<std::ptrdiff_t>(stream_offset_));
    stream_offset_ = 0;
  }
}

std::size_t SimulatedTransport::serve_replay(
    std::span<std::byte> destination, TransferStatus& status) {
  status = TransferStatus::Ok;
  const auto& transfers = *config_.replay.transfers;
  while (replay_index_ < transfers.size()) {
    const recording::ReplayTransfer& record = transfers[replay_index_];
    if (record.status != TransferStatus::Ok && replay_offset_ == 0) {
      replay_index_++;
      status = record.status;
      return 0;
    }
    if (record.bytes.empty()) {
      replay_index_++;
      replay_offset_ = 0;
      continue;
    }
    const std::size_t remaining = record.bytes.size() - replay_offset_;
    const std::size_t take = std::min(destination.size(), remaining);
    std::memcpy(destination.data(), record.bytes.data() + replay_offset_, take);
    replay_offset_ += take;
    if (replay_offset_ >= record.bytes.size()) {
      replay_index_++;
      replay_offset_ = 0;
    }
    return take;
  }
  status = TransferStatus::Timeout;
  return 0;
}

std::int64_t SimulatedTransport::schedule(std::size_t bytes,
                                          std::int64_t now_ns,
                                          std::int64_t available_at_ns) {
  const std::int64_t start =
      std::max(std::max(now_ns, device_free_ns_), available_at_ns);
  const std::int64_t transmit_ns =
      bytes == 0 ? 0
                 : static_cast<std::int64_t>(
                       (static_cast<long double>(bytes) * 1'000'000'000.0L) /
                       static_cast<long double>(config_.bandwidth_bytes_per_sec));
  device_free_ns_ = start + std::max<std::int64_t>(transmit_ns, 1);
  return device_free_ns_ + config_.transfer_latency_ns +
         config_.faults.injected_latency_ns;
}

void SimulatedTransport::schedule_event(Pending& pending) {
  events_.push(Event{pending.ready_ns, pending.id, pending.epoch});
}

TransferId SimulatedTransport::submit_bulk_in(
    std::uint8_t endpoint, std::span<std::byte> destination,
    CompletionSink& sink, void* user) {
  if (!open_ || !claimed_) {
    return 0;
  }
  const TransferId id = next_id_++;
  const std::int64_t now = clock_.now_ns();
  TransferStatus status = TransferStatus::Ok;
  std::size_t take = 0;
  std::int64_t available_at = now;

  const auto halt_it = halted_.find(endpoint);
  const bool is_halted = halt_it != halted_.end() && halt_it->second;

  if (is_halted) {
    status = TransferStatus::Stall;
  } else if (config_.replay.transfers) {
    take = serve_replay(destination, status);
    delivered_bytes_ += take;
  } else {
    submit_counter_++;
    if (chance(config_.faults.timeout_probability, submit_counter_, 0x80, 0)) {
      status = TransferStatus::Timeout;
    } else if (chance(config_.faults.stall_probability, submit_counter_, 0x81,
                      0)) {
      status = TransferStatus::Stall;
      halted_[endpoint] = true;
    } else {
      ensure_stream(destination.size());
      const std::size_t available = stream_.size() - stream_offset_;
      take = std::min(destination.size(), available);
      if (take == 0) {
        status = TransferStatus::Timeout;
      } else {
        if (take > 1 &&
            chance(config_.faults.short_read_probability, submit_counter_,
                   0x82, 0)) {
          take = 1 + static_cast<std::size_t>(
                         hash(submit_counter_, 0x83, 0) % (take - 1));
        }
        std::memcpy(destination.data(), stream_.data() + stream_offset_, take);
        stream_offset_ += take;
        delivered_bytes_ += take;
        stream_consumed_total_ += take;
        while (!stream_marks_.empty() &&
               stream_marks_.front().end_offset <= stream_consumed_total_) {
          available_at = std::max(available_at,
                                  stream_marks_.front().available_ns);
          stream_marks_.pop_front();
        }
        if (!stream_marks_.empty()) {
          available_at =
              std::max(available_at, stream_marks_.front().available_ns);
        }
      }
    }
  }

  if (take == 0 && status == TransferStatus::Ok) {
    status = TransferStatus::Timeout;
  }

  Pending pending;
  pending.id = id;
  pending.destination = destination;
  pending.sink = &sink;
  pending.user = user;
  pending.submitted_ns = now;
  pending.ready_ns = schedule(take, now, available_at);
  pending.bytes = take;
  pending.status = status;
  pending.epoch = 0;
  pending.done = false;
  scheduled_++;
  auto [it, inserted] = pending_.emplace(id, pending);
  (void)inserted;
  schedule_event(it->second);
  return id;
}

bool SimulatedTransport::cancel(TransferId id) {
  auto it = pending_.find(id);
  if (it == pending_.end() || it->second.done) {
    return false;
  }
  Pending& pending = it->second;
  pending.cancelled = true;
  pending.epoch++;
  pending.ready_ns = clock_.now_ns();
  schedule_event(pending);
  cancels_++;
  return true;
}

void SimulatedTransport::emulate_completion(Pending& pending) {
  TransferCompletion completion;
  completion.id = pending.id;
  completion.status =
      pending.cancelled ? TransferStatus::Cancelled : pending.status;
  completion.bytes_transferred = pending.cancelled ? 0 : pending.bytes;
  completion.host_time_ns = clock_.now_ns();
  completion.user = pending.user;
  if (completion.status == TransferStatus::Timeout) {
    timeouts_++;
  } else if (completion.status == TransferStatus::Stall) {
    stalls_++;
  }
  if (pending.sink != nullptr) {
    pending.sink->on_transfer_complete(completion);
  }
}

std::size_t SimulatedTransport::poll(int timeout_ms) {
  if (!open_) {
    return 0;
  }
  const std::int64_t now = clock_.now_ns();
  const std::int64_t limit =
      timeout_ms < 0
          ? std::numeric_limits<std::int64_t>::max()
          : now + static_cast<std::int64_t>(timeout_ms) * 1'000'000;
  std::size_t delivered = 0;

  while (!events_.empty()) {
    const Event event = events_.top();
    auto it = pending_.find(event.id);
    if (it == pending_.end() || it->second.done ||
        it->second.epoch != event.epoch) {
      events_.pop();
      continue;
    }
    if (event.ready_ns > limit) {
      break;
    }
    events_.pop();
    clock_.set_ns(std::max(clock_.now_ns(), event.ready_ns));
    Pending pending = std::move(it->second);
    pending.done = true;
    pending_.erase(it);
    emulate_completion(pending);
    delivered++;
  }

  if (delivered == 0 && timeout_ms > 0) {
    clock_.set_ns(std::max(clock_.now_ns(), limit));
  }
  return delivered;
}

}  // namespace einstar
