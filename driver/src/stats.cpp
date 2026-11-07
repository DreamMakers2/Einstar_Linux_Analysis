// SPDX-License-Identifier: MIT
#include "einstar/stats.hpp"

#include <algorithm>
#include <cmath>

namespace einstar {

LatencyHistogram::LatencyHistogram(std::size_t capacity)
    : samples_(capacity == 0 ? 1 : capacity) {}

void LatencyHistogram::record(std::int64_t sample_ns) {
  if (sample_ns < 0) {
    sample_ns = 0;
  }
  if (total_ == 0) {
    min_ = sample_ns;
    max_ = sample_ns;
  } else {
    min_ = std::min(min_, sample_ns);
    max_ = std::max(max_, sample_ns);
  }
  sum_ += static_cast<long double>(sample_ns);
  samples_[next_] = sample_ns;
  next_ = (next_ + 1) % samples_.size();
  if (filled_ < samples_.size()) {
    filled_++;
  }
  total_++;
}

void LatencyHistogram::reset() {
  next_ = 0;
  filled_ = 0;
  total_ = 0;
  min_ = 0;
  max_ = 0;
  sum_ = 0;
}

std::int64_t LatencyHistogram::percentile(std::vector<std::int64_t> sorted,
                                          double p) {
  if (sorted.empty()) {
    return 0;
  }
  if (p <= 0.0) {
    return sorted.front();
  }
  if (p >= 100.0) {
    return sorted.back();
  }
  std::sort(sorted.begin(), sorted.end());
  const double rank = std::ceil((p / 100.0) * static_cast<double>(sorted.size()));
  std::size_t index = rank <= 1.0 ? 0 : static_cast<std::size_t>(rank) - 1;
  index = std::min(index, sorted.size() - 1);
  return sorted[index];
}

LatencyHistogram::Snapshot LatencyHistogram::snapshot() const {
  Snapshot result;
  if (total_ == 0) {
    return result;
  }
  result.count = total_;
  result.min_ns = min_;
  result.max_ns = max_;
  result.mean_ns = static_cast<std::int64_t>(
      sum_ / static_cast<long double>(total_));
  std::vector<std::int64_t> samples;
  samples.reserve(filled_);
  for (std::size_t i = 0; i < filled_; ++i) {
    samples.push_back(samples_[i]);
  }
  std::sort(samples.begin(), samples.end());
  result.p50_ns = percentile(samples, 50.0);
  result.p95_ns = percentile(samples, 95.0);
  result.p99_ns = percentile(samples, 99.0);
  return result;
}

Metrics compute_metrics(const PipelineStats& stats, std::int64_t start_ns,
                        std::int64_t end_ns, std::uint32_t frame_size) {
  Metrics metrics;
  if (end_ns <= start_ns) {
    return metrics;
  }
  const double elapsed_s =
      static_cast<double>(end_ns - start_ns) / 1'000'000'000.0;
  metrics.elapsed_seconds = elapsed_s;
  metrics.frames = stats.frames_completed;
  metrics.payload_bytes =
      stats.frames_completed * static_cast<std::uint64_t>(frame_size);
  if (elapsed_s > 0.0) {
    metrics.fps = static_cast<double>(stats.frames_completed) / elapsed_s;
    metrics.megabytes_per_second =
        static_cast<double>(metrics.payload_bytes) / elapsed_s / 1'000'000.0;
  }
  return metrics;
}

}  // namespace einstar
