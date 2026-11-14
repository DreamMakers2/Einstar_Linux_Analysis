// SPDX-License-Identifier: MIT
// Asynchronous acquisition pipeline: queue depth, buffer pool, framing,
// reassembly, backpressure, recording and statistics.
#pragma once

#include <atomic>
#include <cstdint>
#include <functional>
#include <mutex>
#include <thread>
#include <memory>
#include <optional>
#include <string>
#include <unordered_map>
#include <vector>

#include "einstar/frame.hpp"
#include "einstar/queue.hpp"
#include "einstar/reassembler.hpp"
#include "einstar/recorder.hpp"
#include "einstar/stats.hpp"
#include "einstar/transport.hpp"

namespace einstar {

struct PipelineConfig {
  std::uint32_t queue_depth = 8;
  std::uint32_t transfer_buffer_size = 0xA400;
  std::uint32_t max_packet_bytes = 0xA400;
  std::uint32_t frame_size = kDefaultFrameSize;
  std::uint32_t frame_width = 1280;
  std::uint32_t frame_height = 1024;
  /// Wire framing.  Raw is the hardware-verified format (no length prefix) and
  /// is the default; LengthPrefixed only serves legacy recordings/tests.
  FramingMode framing = FramingMode::Raw;

  CopyMode copy_mode = CopyMode::Copy;
  BackpressurePolicy frame_backpressure = BackpressurePolicy::DropOldest;
  std::size_t frame_queue_capacity = 16;
  std::size_t max_inflight_frames = 16;
  std::int64_t partial_frame_timeout_ns = 100'000'000;

  /// 0 = derive: max(queue_depth * 2, 8).
  std::size_t transfer_pool_buffers = 0;
  /// 0 = derive: max(frame_queue_capacity + queue_depth, 8).  Only used in Copy
  /// mode.
  std::size_t frame_pool_buffers = 0;

  bool enable_recording = false;
  std::string recording_path;
  std::string recording_metadata;
  std::size_t recording_queue_capacity = 256;
  BackpressurePolicy recording_policy = BackpressurePolicy::Block;

  std::uint8_t stream_endpoint = Endpoints::kStreamIn;
};

class AcquisitionPipeline {
 public:
  using FrameCallback = std::function<void(const Frame&)>;

  AcquisitionPipeline(std::unique_ptr<Transport> transport, PipelineConfig config);
  AcquisitionPipeline(const AcquisitionPipeline&) = delete;
  AcquisitionPipeline& operator=(const AcquisitionPipeline&) = delete;
  ~AcquisitionPipeline();

  /// Open + claim the transport, allocate pools, start the recorder, submit the
  /// first `queue_depth` reads.
  Status start();
  /// Cancel every outstanding transfer, drain its completion, flush partial
  /// frames, finalise the recording and close the transport.  Returns an error
  /// if a transfer could not be reclaimed.
  Status stop();
  [[nodiscard]] bool running() const noexcept;

  /// Poll the transport and process every completion.  Returns the number of
  /// transfer completions processed.
  std::size_t poll(int timeout_ms);

  /// Deliver completions already queued (used after cancel/stop).
  std::size_t drain_completions();

  /// Non-blocking frame dequeue.
  bool try_pop_frame(Frame& out);
  std::size_t pop_frames(std::vector<Frame>& out, std::size_t max_frames);

  /// When set, completed frames are delivered on the poll thread instead of
  /// being queued.  The reference is only valid for the duration of the call.
  void set_frame_callback(FrameCallback callback);

  [[nodiscard]] PipelineStats stats() const;
  /// O(1) counter read for hot loops.  `stats()` also snapshots (and sorts) the
  /// latency histograms, which costs milliseconds once they fill; a replay loop
  /// that polls hundreds of thousands of times must not call it per iteration.
  [[nodiscard]] std::uint64_t bytes_received() const;
  [[nodiscard]] Status last_error() const;
  [[nodiscard]] const PipelineConfig& config() const noexcept { return config_; }
  [[nodiscard]] const Transport& transport() const noexcept { return *transport_; }
  [[nodiscard]] Transport& transport() noexcept { return *transport_; }
  [[nodiscard]] std::int64_t start_time_ns() const noexcept { return start_ns_; }
  [[nodiscard]] std::int64_t last_frame_time_ns() const noexcept {
    return last_frame_ns_;
  }

 private:
  class CompletionSinkImpl;

  Status submit_one();
  void pump_submissions();
  void handle_completion(const TransferCompletion& completion);
  void process_transfer(const std::byte* data, std::size_t size,
                        BufferHandle& buffer, std::int64_t host_ns);
  void process_packet(const StreamPacket& packet, BufferHandle storage,
                      std::size_t storage_offset, std::int64_t host_ns);
  void finalize_frame(Frame&& frame, std::int64_t host_ns);
  void expire_partials(std::int64_t now_ns);
  void update_queue_stats();
  std::size_t drain_locked();
  std::size_t poll_locked(int timeout_ms);
  Status shutdown_locked();
  void reset_state();

  std::unique_ptr<Transport> transport_;
  PipelineConfig config_;
  std::unique_ptr<BufferPool> transfer_pool_;
  std::unique_ptr<BufferPool> frame_pool_;
  std::unique_ptr<PacketFramer> framer_;
  std::unique_ptr<FrameReassembler> reassembler_;
  std::unique_ptr<recording::RawRecorder> recorder_;
  std::unique_ptr<BoundedQueue<Frame>> frame_queue_;
  std::unique_ptr<CompletionSinkImpl> sink_;
  std::unique_ptr<SpscRing<TransferCompletion>> completions_;

  struct TransferState {
    TransferId id = 0;
    BufferHandle buffer;
    std::int64_t submit_ns = 0;
    bool active = false;
  };
  std::vector<TransferState> transfers_;

  std::uint32_t outstanding_ = 0;
  bool running_ = false;
  bool stopping_ = false;
  bool accepting_ = false;
  std::int64_t start_ns_ = 0;
  std::int64_t last_poll_ns_ = 0;
  std::int64_t last_frame_ns_ = 0;
  std::uint64_t record_sequence_ = 0;
  std::unordered_map<TransferId, std::size_t> active_slots_;

  FrameCallback callback_;
  mutable std::mutex poll_mutex_;
  PipelineStats stats_;
  LatencyHistogram transfer_latency_;
  LatencyHistogram frame_latency_;
  FramerStats framer_stats_;
  Status last_error_;
};

/// Convenience: drive a pipeline on a dedicated thread.  `poll` runs until
/// `stop()` is called; frames remain available through the pipeline queue.
class AcquisitionRunner {
 public:
  explicit AcquisitionRunner(AcquisitionPipeline& pipeline,
                             int poll_timeout_ms = 1);
  AcquisitionRunner(const AcquisitionRunner&) = delete;
  AcquisitionRunner& operator=(const AcquisitionRunner&) = delete;
  ~AcquisitionRunner();

  Status start();
  Status stop();
  [[nodiscard]] bool running() const noexcept;

 private:
  void loop();

  AcquisitionPipeline& pipeline_;
  int poll_timeout_ms_;
  std::atomic<bool> stop_requested_{false};
  std::atomic<bool> running_{false};
  std::thread thread_;
};

}  // namespace einstar
