// SPDX-License-Identifier: MIT
// Instrumentation: latency histograms, pipeline counters, derived metrics.
#pragma once

#include <cstddef>
#include <cstdint>
#include <vector>

namespace einstar {

/// Keeps the most recent `capacity` samples in a ring and reports nearest-rank
/// percentiles.  Deterministic: identical sample sequences give identical
/// snapshots on every run.
class LatencyHistogram {
 public:
  struct Snapshot {
    std::uint64_t count = 0;
    std::int64_t min_ns = 0;
    std::int64_t p50_ns = 0;
    std::int64_t p95_ns = 0;
    std::int64_t p99_ns = 0;
    std::int64_t max_ns = 0;
    std::int64_t mean_ns = 0;
  };

  explicit LatencyHistogram(std::size_t capacity = 262144);

  void record(std::int64_t sample_ns);
  void reset();

  [[nodiscard]] Snapshot snapshot() const;
  [[nodiscard]] std::uint64_t total_count() const noexcept { return total_; }
  [[nodiscard]] std::size_t capacity() const noexcept { return samples_.size(); }

  static std::int64_t percentile(std::vector<std::int64_t> sorted, double p);

 private:
  std::vector<std::int64_t> samples_;
  std::size_t next_ = 0;
  std::size_t filled_ = 0;
  std::uint64_t total_ = 0;
  std::int64_t min_ = 0;
  std::int64_t max_ = 0;
  long double sum_ = 0;
};

struct PipelineStats {
  // Transfer layer
  std::uint64_t transfers_submitted = 0;
  std::uint64_t transfers_completed = 0;
  std::uint64_t bytes_received = 0;
  std::uint64_t transfer_timeouts = 0;
  std::uint64_t transfer_stalls = 0;
  std::uint64_t transfer_errors = 0;
  std::uint64_t transfers_cancelled = 0;
  std::uint64_t resubmits = 0;
  std::uint64_t submission_starved = 0;  // pool exhausted, no read submitted
  std::uint64_t completion_ring_overflow = 0;  // handled inline instead

  // Framing / packets
  std::uint64_t packets_parsed = 0;
  std::uint64_t parse_errors = 0;
  std::uint64_t resync_bytes = 0;
  std::uint64_t staged_packets = 0;

  // Reassembly
  std::uint64_t duplicate_packets = 0;
  std::uint64_t orphan_packets = 0;
  std::uint64_t frame_restarts = 0;
  std::uint64_t frames_completed = 0;
  std::uint64_t frames_dropped = 0;
  std::uint64_t sequence_gaps = 0;
  std::uint64_t partial_timeouts = 0;
  std::uint64_t reassembly_errors = 0;
  std::uint64_t pool_exhausted = 0;
  std::uint64_t queue_drops = 0;

  // Occupancy
  std::size_t frame_queue_depth = 0;
  std::size_t frame_queue_peak = 0;
  std::size_t transfer_pool_in_use = 0;
  std::size_t transfer_pool_peak = 0;
  std::size_t transfer_pool_capacity = 0;
  std::size_t frame_pool_in_use = 0;
  std::size_t frame_pool_peak = 0;

  // Recorder
  std::uint64_t recorded_transfers = 0;
  std::uint64_t record_drops = 0;
  bool recording = false;

  LatencyHistogram::Snapshot transfer_latency;
  LatencyHistogram::Snapshot frame_latency;
};

struct Metrics {
  double elapsed_seconds = 0.0;
  double fps = 0.0;
  double megabytes_per_second = 0.0;
  std::uint64_t frames = 0;
  std::uint64_t payload_bytes = 0;
};

/// Derive throughput/fps from a stats snapshot and two clock samples.
Metrics compute_metrics(const PipelineStats& stats, std::int64_t start_ns,
                        std::int64_t end_ns, std::uint32_t frame_size);

}  // namespace einstar
