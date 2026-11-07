// SPDX-License-Identifier: MIT
// Bounded queues with explicit backpressure policies.
#pragma once

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <mutex>
#include <string_view>
#include <utility>
#include <vector>

namespace einstar {

enum class BackpressurePolicy : std::uint8_t {
  DropOldest = 0,  // keep the newest frames, discard the oldest queued one
  DropNewest = 1,  // keep the oldest frames, discard the incoming one
  Block = 2,       // block the producer until space is available (lossless)
};

std::string_view to_string(BackpressurePolicy policy) noexcept;

enum class PushOutcome : std::uint8_t {
  Accepted = 0,
  DroppedOldest = 1,
  DroppedNewest = 2,
  Closed = 3,
};

/// Mutex + condition variable bounded queue.  Used for frame delivery and for
/// the recording writer queue.  `push` applies the configured policy; a Block
/// push waits until capacity is free or the queue is closed.
template <typename T>
class BoundedQueue {
 public:
  BoundedQueue(std::size_t capacity, BackpressurePolicy policy)
      : capacity_(capacity == 0 ? 1 : capacity), policy_(policy) {}

  BoundedQueue(const BoundedQueue&) = delete;
  BoundedQueue& operator=(const BoundedQueue&) = delete;

  PushOutcome push(T value) {
    std::unique_lock lock(mutex_);
    if (closed_) {
      return PushOutcome::Closed;
    }
    if (items_.size() < capacity_) {
      return push_locked(std::move(value), lock);
    }
    switch (policy_) {
      case BackpressurePolicy::DropOldest: {
        items_.pop_front();
        dropped_++;
        return push_locked(std::move(value), lock, PushOutcome::DroppedOldest);
      }
      case BackpressurePolicy::DropNewest: {
        dropped_++;
        return PushOutcome::DroppedNewest;
      }
      case BackpressurePolicy::Block: {
        not_full_.wait(lock, [this] { return closed_ || items_.size() < capacity_; });
        if (closed_) {
          return PushOutcome::Closed;
        }
        return push_locked(std::move(value), lock);
      }
    }
    return PushOutcome::Closed;
  }

  /// Never blocks.  With the Block policy a full queue rejects the item.
  PushOutcome try_push(T value) {
    std::lock_guard lock(mutex_);
    if (closed_) {
      return PushOutcome::Closed;
    }
    if (items_.size() < capacity_) {
      items_.push_back(std::move(value));
      after_push_locked();
      return PushOutcome::Accepted;
    }
    if (policy_ == BackpressurePolicy::DropOldest) {
      items_.pop_front();
      dropped_++;
      items_.push_back(std::move(value));
      after_push_locked();
      return PushOutcome::DroppedOldest;
    }
    dropped_++;
    return PushOutcome::DroppedNewest;
  }

  bool pop(T& out, std::chrono::milliseconds timeout) {
    std::unique_lock lock(mutex_);
    if (!not_empty_.wait_for(lock, timeout,
                             [this] { return closed_ || !items_.empty(); })) {
      return false;
    }
    return pop_locked(out);
  }

  bool try_pop(T& out) {
    std::lock_guard lock(mutex_);
    return pop_locked(out);
  }

  void close() {
    std::lock_guard lock(mutex_);
    closed_ = true;
    not_empty_.notify_all();
    not_full_.notify_all();
  }

  [[nodiscard]] bool closed() const {
    std::lock_guard lock(mutex_);
    return closed_;
  }

  [[nodiscard]] std::size_t size() const {
    std::lock_guard lock(mutex_);
    return items_.size();
  }

  [[nodiscard]] std::size_t capacity() const noexcept { return capacity_; }

  [[nodiscard]] std::uint64_t pushed() const {
    std::lock_guard lock(mutex_);
    return pushed_;
  }
  [[nodiscard]] std::uint64_t dropped() const {
    std::lock_guard lock(mutex_);
    return dropped_;
  }
  [[nodiscard]] std::size_t peak_size() const {
    std::lock_guard lock(mutex_);
    return peak_;
  }
  [[nodiscard]] BackpressurePolicy policy() const noexcept { return policy_; }

 private:
  PushOutcome push_locked(T value, std::unique_lock<std::mutex>&,
                          PushOutcome outcome = PushOutcome::Accepted) {
    items_.push_back(std::move(value));
    after_push_locked();
    not_empty_.notify_one();
    return outcome;
  }

  void after_push_locked() {
    pushed_++;
    if (items_.size() > peak_) {
      peak_ = items_.size();
    }
  }

  bool pop_locked(T& out) {
    if (items_.empty()) {
      return false;
    }
    out = std::move(items_.front());
    items_.pop_front();
    not_full_.notify_one();
    return true;
  }

  std::size_t capacity_;
  BackpressurePolicy policy_;
  mutable std::mutex mutex_;
  std::condition_variable not_empty_;
  std::condition_variable not_full_;
  std::deque<T> items_;
  bool closed_ = false;
  std::uint64_t pushed_ = 0;
  std::uint64_t dropped_ = 0;
  std::size_t peak_ = 0;
};

/// Single-producer / single-consumer lock-free ring.  Capacity is rounded up to
/// a power of two.  No allocation after construction, no drops: try_push simply
/// fails when full and the caller decides what to do.
template <typename T>
class SpscRing {
 public:
  explicit SpscRing(std::size_t min_capacity) {
    std::size_t capacity = 2;
    while (capacity < min_capacity) {
      capacity <<= 1U;
    }
    capacity_ = capacity;
    mask_ = capacity - 1;
    buffer_.resize(capacity);
  }

  SpscRing(const SpscRing&) = delete;
  SpscRing& operator=(const SpscRing&) = delete;

  bool try_push(const T& value) {
    const std::size_t head = head_.load(std::memory_order_relaxed);
    if (head - tail_cache_ >= capacity_) {
      tail_cache_ = tail_.load(std::memory_order_acquire);
      if (head - tail_cache_ >= capacity_) {
        return false;
      }
    }
    buffer_[head & mask_] = value;
    head_.store(head + 1, std::memory_order_release);
    return true;
  }

  bool try_pop(T& out) {
    const std::size_t tail = tail_.load(std::memory_order_relaxed);
    if (tail == head_cache_) {
      head_cache_ = head_.load(std::memory_order_acquire);
      if (tail == head_cache_) {
        return false;
      }
    }
    out = buffer_[tail & mask_];
    tail_.store(tail + 1, std::memory_order_release);
    return true;
  }

  [[nodiscard]] std::size_t size() const noexcept {
    const std::size_t head = head_.load(std::memory_order_acquire);
    const std::size_t tail = tail_.load(std::memory_order_acquire);
    return head - tail;
  }

  [[nodiscard]] std::size_t capacity() const noexcept { return capacity_; }

 private:
  std::size_t capacity_ = 0;
  std::size_t mask_ = 0;
  std::vector<T> buffer_;
  alignas(64) std::atomic<std::size_t> head_{0};
  alignas(64) std::atomic<std::size_t> tail_{0};
  std::size_t tail_cache_ = 0;  // producer-owned
  std::size_t head_cache_ = 0;  // consumer-owned
};

}  // namespace einstar
