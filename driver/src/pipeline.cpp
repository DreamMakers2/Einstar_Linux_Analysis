// SPDX-License-Identifier: MIT
#include "einstar/pipeline.hpp"

#include <algorithm>
#include <chrono>
#include <cstring>
#include <utility>

namespace einstar {
namespace {

void close_and_drain(BoundedQueue<Frame>& queue) {
  queue.close();
  Frame frame;
  while (queue.try_pop(frame)) {
  }
}

}  // namespace

class AcquisitionPipeline::CompletionSinkImpl final : public CompletionSink {
 public:
  CompletionSinkImpl(AcquisitionPipeline& pipeline,
                     SpscRing<TransferCompletion>& ring)
      : pipeline_(pipeline), ring_(ring) {}

  void on_transfer_complete(
      const TransferCompletion& completion) noexcept override {
    if (!ring_.try_push(completion)) {
      // Safety valve: the ring is sized for several poll cycles.  If it ever
      // overflows, process inline rather than losing a completion (this only
      // changes ordering, never accounting).
      pipeline_.stats_.completion_ring_overflow++;
      pipeline_.handle_completion(completion);
    }
  }

 private:
  AcquisitionPipeline& pipeline_;
  SpscRing<TransferCompletion>& ring_;
};

AcquisitionPipeline::AcquisitionPipeline(std::unique_ptr<Transport> transport,
                                         PipelineConfig config)
    : transport_(std::move(transport)), config_(std::move(config)) {
  transfer_latency_ = LatencyHistogram(262144);
  frame_latency_ = LatencyHistogram(262144);
}

AcquisitionPipeline::~AcquisitionPipeline() {
  if (running_ || (transport_ && transport_->is_open())) {
    (void)stop();
  }
  reset_state();
}

void AcquisitionPipeline::reset_state() {
  if (frame_queue_) {
    close_and_drain(*frame_queue_);
  }
  frame_queue_.reset();
  sink_.reset();
  completions_.reset();
  reassembler_.reset();
  framer_.reset();
  recorder_.reset();
  transfers_.clear();
  active_slots_.clear();
  frame_pool_.reset();
  transfer_pool_.reset();
  outstanding_ = 0;
}

Status AcquisitionPipeline::start() {
  std::lock_guard lock(poll_mutex_);
  if (running_) {
    return Status::error(ErrorCode::AlreadyOpen, "pipeline is already running");
  }
  if (transport_ == nullptr) {
    return Status::error(ErrorCode::InvalidArgument, "null transport");
  }
  if (config_.queue_depth == 0 || config_.transfer_buffer_size < 64 ||
      config_.frame_size == 0) {
    return Status::error(ErrorCode::InvalidArgument,
                         "invalid pipeline configuration");
  }

  reset_state();
  stats_ = PipelineStats{};
  framer_stats_ = FramerStats{};
  transfer_latency_.reset();
  frame_latency_.reset();
  last_error_ = Status::success();
  record_sequence_ = 0;
  stopping_ = false;
  accepting_ = false;

  const std::size_t transfer_pool_buffers =
      config_.transfer_pool_buffers != 0
          ? config_.transfer_pool_buffers
          : (config_.copy_mode == CopyMode::ZeroCopy
                 ? std::max<std::size_t>(config_.queue_depth * 2,
                                         config_.queue_depth + 64)
                 : std::max<std::size_t>(config_.queue_depth * 2, 8));
  if (transfer_pool_buffers < config_.queue_depth) {
    return Status::error(
        ErrorCode::InvalidArgument,
        "transfer pool must be at least queue_depth buffers");
  }
  transfer_pool_ = std::make_unique<BufferPool>(config_.transfer_buffer_size,
                                                transfer_pool_buffers);

  if (config_.copy_mode == CopyMode::Copy) {
    const std::size_t frame_pool_buffers =
        config_.frame_pool_buffers != 0
            ? config_.frame_pool_buffers
            : std::max<std::size_t>(
                  config_.frame_queue_capacity + config_.queue_depth, 8);
    frame_pool_ =
        std::make_unique<BufferPool>(config_.frame_size, frame_pool_buffers);
  }

  framer_ = std::make_unique<PacketFramer>(config_.max_packet_bytes);

  ReassemblyConfig reassembly;
  reassembly.frame_size = config_.frame_size;
  reassembly.width = config_.frame_width;
  reassembly.height = config_.frame_height;
  reassembly.max_inflight_frames = config_.max_inflight_frames;
  reassembly.partial_frame_timeout_ns = config_.partial_frame_timeout_ns;
  reassembly.mode = config_.copy_mode;
  reassembly.frame_pool = frame_pool_.get();
  reassembler_ = std::make_unique<FrameReassembler>(reassembly);

  frame_queue_ = std::make_unique<BoundedQueue<Frame>>(
      std::max<std::size_t>(1, config_.frame_queue_capacity),
      config_.frame_backpressure);

  completions_ = std::make_unique<SpscRing<TransferCompletion>>(
      std::max<std::size_t>(config_.queue_depth * 8, 64));
  sink_ = std::make_unique<CompletionSinkImpl>(*this, *completions_);
  transfers_.assign(config_.queue_depth, TransferState{});

  Status status = transport_->open();
  if (!status.ok()) {
    reset_state();
    return status;
  }
  status = transport_->claim_interface(0);
  if (!status.ok()) {
    transport_->close();
    reset_state();
    return status;
  }

  if (config_.enable_recording) {
    if (config_.recording_path.empty()) {
      transport_->release_interface(0);
      transport_->close();
      reset_state();
      return Status::error(ErrorCode::InvalidArgument,
                           "recording enabled without a path");
    }
    recording::RecorderConfig recorder_config;
    recorder_config.queue_capacity = config_.recording_queue_capacity;
    recorder_config.policy = config_.recording_policy;
    recorder_config.metadata = config_.recording_metadata;
    recorder_config.start_host_ns =
        static_cast<std::uint64_t>(transport_->clock().now_ns());
    recorder_ = std::make_unique<recording::RawRecorder>(
        config_.recording_path, std::move(recorder_config));
    status = recorder_->start();
    if (!status.ok()) {
      transport_->release_interface(0);
      transport_->close();
      reset_state();
      return status;
    }
  }

  start_ns_ = transport_->clock().now_ns();
  last_poll_ns_ = start_ns_;
  last_frame_ns_ = start_ns_;
  running_ = true;
  accepting_ = true;

  for (std::uint32_t i = 0; i < config_.queue_depth; ++i) {
    status = submit_one();
    if (!status.ok()) {
      break;
    }
  }
  if (!status.ok()) {
    const Status stop_status = shutdown_locked();
    (void)stop_status;
    last_error_ = status;
    return status;
  }
  update_queue_stats();
  return Status::success();
}

Status AcquisitionPipeline::submit_one() {
  if (!running_ || stopping_ || !accepting_) {
    return Status::error(ErrorCode::Closed, "pipeline is stopping");
  }
  if (outstanding_ >= config_.queue_depth) {
    return Status::error(ErrorCode::Backpressure, "queue depth reached");
  }
  std::size_t slot = transfers_.size();
  for (std::size_t i = 0; i < transfers_.size(); ++i) {
    if (!transfers_[i].active) {
      slot = i;
      break;
    }
  }
  if (slot == transfers_.size()) {
    stats_.submission_starved++;
    return Status::error(ErrorCode::Exhausted, "no free transfer slot");
  }

  BufferHandle buffer = transfer_pool_ ? transfer_pool_->acquire()
                                       : BufferHandle{};
  if (!buffer.valid()) {
    stats_.submission_starved++;
    return Status::error(ErrorCode::Exhausted, "transfer pool exhausted");
  }

  TransferState& state = transfers_[slot];
  state.buffer = std::move(buffer);
  state.submit_ns = transport_->clock().now_ns();
  state.active = true;
  state.id = 0;

  const TransferId id = transport_->submit_bulk_in(
      config_.stream_endpoint, state.buffer.bytes(), *sink_, nullptr);
  if (id == 0) {
    state.active = false;
    state.buffer.reset();
    stats_.transfer_errors++;
    return Status::error(ErrorCode::TransportError,
                         "transport rejected bulk-in submission");
  }
  state.id = id;
  active_slots_[id] = slot;
  outstanding_++;
  stats_.transfers_submitted++;
  return Status::success();
}

void AcquisitionPipeline::pump_submissions() {
  while (running_ && !stopping_ && outstanding_ < config_.queue_depth) {
    const Status status = submit_one();
    if (!status.ok()) {
      if (status.code() == ErrorCode::Exhausted) {
        break;
      }
      last_error_ = status;
      break;
    }
  }
}

std::size_t AcquisitionPipeline::drain_locked() {
  std::size_t processed = 0;
  TransferCompletion completion;
  while (completions_->try_pop(completion)) {
    handle_completion(completion);
    processed++;
  }
  return processed;
}

void AcquisitionPipeline::handle_completion(
    const TransferCompletion& completion) {
  const auto slot_it = active_slots_.find(completion.id);
  if (slot_it == active_slots_.end()) {
    stats_.reassembly_errors++;
    return;
  }
  const std::size_t slot = slot_it->second;
  active_slots_.erase(slot_it);
  TransferState& state = transfers_[slot];
  BufferHandle& buffer = state.buffer;

  const std::int64_t latency = completion.host_time_ns - state.submit_ns;
  if (latency >= 0) {
    transfer_latency_.record(latency);
  }
  stats_.transfers_completed++;
  stats_.bytes_received += completion.bytes_transferred;

  const std::size_t received =
      std::min<std::size_t>(completion.bytes_transferred, buffer.capacity());
  bool resubmit = true;

  switch (completion.status) {
    case TransferStatus::Ok:
      process_transfer(buffer.data(), received, buffer, completion.host_time_ns);
      break;
    case TransferStatus::Timeout:
      stats_.transfer_timeouts++;
      break;
    case TransferStatus::Stall:
      stats_.transfer_stalls++;
      (void)transport_->clear_halt(config_.stream_endpoint);
      stats_.resubmits++;
      break;
    case TransferStatus::Overflow:
      stats_.transfer_errors++;
      framer_->reset();
      break;
    case TransferStatus::Error:
      stats_.transfer_errors++;
      framer_->reset();
      break;
    case TransferStatus::Cancelled:
      stats_.transfers_cancelled++;
      resubmit = false;
      break;
  }

  if (recorder_ && recorder_->running() && buffer.valid()) {
    recording::RecordedTransfer record;
    record.sequence = record_sequence_++;
    record.host_time_ns = completion.host_time_ns;
    record.device_timestamp = 0;
    record.status = completion.status;
    if (completion.status == TransferStatus::Ok && received > 0) {
      record.bytes.assign(buffer.data(), buffer.data() + received);
    }
    const Status recorded = recorder_->record(record);
    if (!recorded.ok()) {
      last_error_ = recorded;
    }
  }

  state.active = false;
  state.id = 0;
  state.buffer.reset();
  if (outstanding_ > 0) {
    outstanding_--;
  }

  if (running_ && !stopping_ && resubmit) {
    const Status status = submit_one();
    if (!status.ok() && status.code() != ErrorCode::Exhausted) {
      last_error_ = status;
    }
  }
}

void AcquisitionPipeline::process_transfer(const std::byte* data,
                                           std::size_t size,
                                           BufferHandle& buffer,
                                           std::int64_t host_ns) {
  if (size == 0) {
    return;
  }
  const std::span<const std::byte> transfer(data, size);
  framer_->consume(
      transfer,
      [&](const StreamPacket& packet) {
        if (config_.copy_mode == CopyMode::ZeroCopy) {
          if (packet.staged) {
            BufferHandle staging = transfer_pool_->acquire();
            if (!staging.valid()) {
              stats_.pool_exhausted++;
              return;
            }
            if (packet.payload_size > 0) {
              std::memcpy(staging.data(), packet.payload, packet.payload_size);
            }
            process_packet(packet, std::move(staging), 0, host_ns);
          } else {
            const std::size_t offset =
                static_cast<std::size_t>(packet.payload - data);
            process_packet(packet, buffer, offset, host_ns);
          }
        } else {
          process_packet(packet, BufferHandle{}, 0, host_ns);
        }
      },
      framer_stats_);
}

void AcquisitionPipeline::process_packet(const StreamPacket& packet,
                                         BufferHandle storage,
                                         std::size_t storage_offset,
                                         std::int64_t host_ns) {
  auto result = reassembler_->ingest(packet, std::move(storage), storage_offset,
                                     host_ns);
  if (!result.has_value()) {
    if (result.error().code() == ErrorCode::Exhausted) {
      stats_.pool_exhausted++;
    } else {
      stats_.reassembly_errors++;
    }
    last_error_ = result.error();
    return;
  }
  if (result->completed.has_value()) {
    finalize_frame(std::move(*result->completed), host_ns);
  }
}

void AcquisitionPipeline::finalize_frame(Frame&& frame, std::int64_t host_ns) {
  last_frame_ns_ = host_ns;
  frame_latency_.record(std::max<std::int64_t>(0, frame.assembly_ns));
  if (callback_) {
    callback_(frame);
  } else {
    (void)frame_queue_->push(std::move(frame));
  }
}

void AcquisitionPipeline::expire_partials(std::int64_t now_ns) {
  if (reassembler_) {
    (void)reassembler_->expire(now_ns);
  }
}

void AcquisitionPipeline::update_queue_stats() {
  if (frame_queue_) {
    stats_.frame_queue_depth = frame_queue_->size();
  }
}

std::size_t AcquisitionPipeline::poll(int timeout_ms) {
  std::lock_guard lock(poll_mutex_);
  return poll_locked(timeout_ms);
}

std::size_t AcquisitionPipeline::poll_locked(int timeout_ms) {
  if (!running_) {
    return 0;
  }
  transport_->poll(timeout_ms);
  last_poll_ns_ = transport_->clock().now_ns();
  const std::size_t processed = drain_locked();
  expire_partials(last_poll_ns_);
  pump_submissions();
  update_queue_stats();
  return processed;
}

std::size_t AcquisitionPipeline::drain_completions() {
  std::lock_guard lock(poll_mutex_);
  return drain_locked();
}

Status AcquisitionPipeline::shutdown_locked() {
  stopping_ = true;
  accepting_ = false;

  for (auto& state : transfers_) {
    if (state.active) {
      (void)transport_->cancel(state.id);
    }
  }

  const std::int64_t deadline =
      transport_->clock().now_ns() + 5'000'000'000LL;
  std::size_t guard = 0;
  while (outstanding_ > 0 && guard++ < 1'000'000) {
    transport_->poll(1);
    (void)drain_locked();
    if (transport_->clock().now_ns() > deadline) {
      break;
    }
  }
  if (outstanding_ > 0) {
    for (auto& state : transfers_) {
      if (state.active) {
        (void)transport_->cancel(state.id);
      }
    }
    transport_->poll(1);
    (void)drain_locked();
  }

  Status result;
  if (outstanding_ > 0) {
    result = Status::error(
        ErrorCode::Timeout,
        "transfers still outstanding after cancellation: " +
            std::to_string(outstanding_));
    // Reclaim bookkeeping so a restart is possible; the transport still owns
    // the buffers until it delivers the completions.
    for (auto& state : transfers_) {
      if (state.active) {
        active_slots_.erase(state.id);
        state.active = false;
        state.id = 0;
        state.buffer.reset();
      }
    }
    outstanding_ = 0;
  }

  if (reassembler_) {
    (void)reassembler_->flush(transport_->clock().now_ns());
  }
  if (recorder_) {
    const Status recorder_status = recorder_->stop();
    if (result.ok() && !recorder_status.ok()) {
      result = recorder_status;
    }
  }
  (void)transport_->release_interface(0);
  transport_->close();
  running_ = false;
  stopping_ = false;
  update_queue_stats();
  return result;
}

Status AcquisitionPipeline::stop() {
  std::lock_guard lock(poll_mutex_);
  if (!running_) {
    return Status::success();
  }
  return shutdown_locked();
}

bool AcquisitionPipeline::running() const noexcept {
  std::lock_guard lock(poll_mutex_);
  return running_;
}

bool AcquisitionPipeline::try_pop_frame(Frame& out) {
  return frame_queue_ != nullptr && frame_queue_->try_pop(out);
}

std::size_t AcquisitionPipeline::pop_frames(std::vector<Frame>& out,
                                            std::size_t max_frames) {
  std::size_t count = 0;
  Frame frame;
  while (count < max_frames && frame_queue_ != nullptr &&
         frame_queue_->try_pop(frame)) {
    out.push_back(std::move(frame));
    count++;
  }
  return count;
}

void AcquisitionPipeline::set_frame_callback(FrameCallback callback) {
  std::lock_guard lock(poll_mutex_);
  callback_ = std::move(callback);
}

PipelineStats AcquisitionPipeline::stats() const {
  std::lock_guard lock(poll_mutex_);
  PipelineStats snapshot = stats_;
  if (framer_ != nullptr) {
    snapshot.packets_parsed = framer_stats_.packets;
    snapshot.parse_errors = framer_stats_.protocol_errors;
    snapshot.resync_bytes = framer_stats_.resync_bytes;
    snapshot.staged_packets = framer_stats_.staged_packets;
  }
  if (reassembler_ != nullptr) {
    const ReassemblyStats& reassembly = reassembler_->stats();
    snapshot.duplicate_packets = reassembly.duplicate_packets;
    snapshot.orphan_packets = reassembly.orphan_packets;
    snapshot.frame_restarts = reassembly.frame_restarts;
    snapshot.frames_completed = reassembly.frames_completed;
    snapshot.frames_dropped = reassembly.frames_dropped;
    snapshot.sequence_gaps = reassembly.sequence_gaps;
    snapshot.partial_timeouts = reassembly.partial_timeouts;
    snapshot.reassembly_errors =
        stats_.reassembly_errors + reassembly.overflow_errors;
    snapshot.pool_exhausted =
        stats_.pool_exhausted + reassembly.pool_exhausted;
  }
  if (frame_queue_ != nullptr) {
    snapshot.frame_queue_depth = frame_queue_->size();
    snapshot.frame_queue_peak = frame_queue_->peak_size();
    snapshot.queue_drops = frame_queue_->dropped();
  }
  if (transfer_pool_ != nullptr) {
    snapshot.transfer_pool_in_use = transfer_pool_->in_use();
    snapshot.transfer_pool_peak = transfer_pool_->peak_in_use();
    snapshot.transfer_pool_capacity = transfer_pool_->capacity();
  }
  if (frame_pool_ != nullptr) {
    snapshot.frame_pool_in_use = frame_pool_->in_use();
    snapshot.frame_pool_peak = frame_pool_->peak_in_use();
  }
  if (recorder_ != nullptr) {
    snapshot.recorded_transfers = recorder_->recorded_transfers();
    snapshot.record_drops = recorder_->dropped_records();
    snapshot.recording = recorder_->running();
  }
  snapshot.transfer_latency = transfer_latency_.snapshot();
  snapshot.frame_latency = frame_latency_.snapshot();
  return snapshot;
}

Status AcquisitionPipeline::last_error() const {
  std::lock_guard lock(poll_mutex_);
  return last_error_;
}

AcquisitionRunner::AcquisitionRunner(AcquisitionPipeline& pipeline,
                                     int poll_timeout_ms)
    : pipeline_(pipeline), poll_timeout_ms_(poll_timeout_ms) {}

AcquisitionRunner::~AcquisitionRunner() { (void)stop(); }

Status AcquisitionRunner::start() {
  if (running_.load(std::memory_order_acquire)) {
    return Status::error(ErrorCode::AlreadyOpen, "runner already started");
  }
  stop_requested_.store(false, std::memory_order_release);
  running_.store(true, std::memory_order_release);
  thread_ = std::thread([this] { loop(); });
  return Status::success();
}

Status AcquisitionRunner::stop() {
  if (!running_.load(std::memory_order_acquire)) {
    return Status::success();
  }
  stop_requested_.store(true, std::memory_order_release);
  if (thread_.joinable()) {
    thread_.join();
  }
  running_.store(false, std::memory_order_release);
  return Status::success();
}

bool AcquisitionRunner::running() const noexcept {
  return running_.load(std::memory_order_acquire);
}

void AcquisitionRunner::loop() {
  while (!stop_requested_.load(std::memory_order_acquire)) {
    (void)pipeline_.poll(poll_timeout_ms_);
  }
}

}  // namespace einstar
