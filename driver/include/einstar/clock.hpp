// SPDX-License-Identifier: MIT
// Monotonic clock abstraction.  The simulator uses a virtual clock so that all
// timing-dependent logic is deterministic and hardware free.
#pragma once

#include <chrono>
#include <cstdint>

namespace einstar {

class Clock {
 public:
  Clock() = default;
  Clock(const Clock&) = delete;
  Clock& operator=(const Clock&) = delete;
  virtual ~Clock() = default;

  /// Nanoseconds since an arbitrary, monotonic origin.
  [[nodiscard]] virtual std::int64_t now_ns() const noexcept = 0;
};

/// Production clock: `std::chrono::steady_clock`.
class SteadyClock final : public Clock {
 public:
  [[nodiscard]] std::int64_t now_ns() const noexcept override;
};

/// Deterministic clock used by the simulated transport and by tests.
class VirtualClock final : public Clock {
 public:
  explicit VirtualClock(std::int64_t start_ns = 0) noexcept : now_(start_ns) {}

  [[nodiscard]] std::int64_t now_ns() const noexcept override { return now_; }

  void set_ns(std::int64_t value) noexcept {
    if (value > now_) {
      now_ = value;
    }
  }
  void advance_ns(std::int64_t delta) noexcept {
    if (delta > 0) {
      now_ += delta;
    }
  }

  static constexpr std::int64_t ns_per_us = 1'000;
  static constexpr std::int64_t ns_per_ms = 1'000'000;
  static constexpr std::int64_t ns_per_s = 1'000'000'000;

 private:
  std::int64_t now_ = 0;
};

}  // namespace einstar
