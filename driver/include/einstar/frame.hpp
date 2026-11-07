// SPDX-License-Identifier: MIT
// Frame model: preallocated buffer pool with ownership handles, chunked
// (zero-copy) or contiguous (copy) frames.
#pragma once

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <mutex>
#include <span>
#include <vector>

#include "einstar/status.hpp"

namespace einstar {

enum class CopyMode : std::uint8_t {
  /// Payload is copied into one contiguous frame buffer (default, simplest for
  /// consumers that need a stable linear image).
  Copy = 0,
  /// Payload chunks retain the transfer buffers; no payload copy at all.  A
  /// frame is a scatter list and can be gathered on demand.
  ZeroCopy = 1,
};

std::string_view to_string(CopyMode mode) noexcept;

struct BufferBlock;

class BufferPool;

/// Move/copyable owning handle to one preallocated block.  Copies share the
/// block (intrusive reference count); the block returns to its pool when the
/// last handle dies.  Handles never allocate.
class BufferHandle {
 public:
  BufferHandle() noexcept = default;
  BufferHandle(const BufferHandle& other) noexcept;
  BufferHandle(BufferHandle&& other) noexcept;
  BufferHandle& operator=(const BufferHandle& other) noexcept;
  BufferHandle& operator=(BufferHandle&& other) noexcept;
  ~BufferHandle();

  [[nodiscard]] bool valid() const noexcept { return block_ != nullptr; }
  [[nodiscard]] std::span<std::byte> bytes() noexcept;
  [[nodiscard]] std::span<const std::byte> bytes() const noexcept;
  [[nodiscard]] std::byte* data() noexcept;
  [[nodiscard]] const std::byte* data() const noexcept;
  [[nodiscard]] std::size_t capacity() const noexcept;
  void reset() noexcept;

  friend class BufferPool;

 private:
  explicit BufferHandle(BufferBlock* block) noexcept : block_(block) {}
  void release() noexcept;
  BufferBlock* block_ = nullptr;
};

/// Fixed-size preallocated pool.  Thread-safe acquire/release.
class BufferPool {
 public:
  BufferPool(std::size_t block_size, std::size_t block_count);
  BufferPool(const BufferPool&) = delete;
  BufferPool& operator=(const BufferPool&) = delete;
  ~BufferPool();

  /// Non-blocking.  Returns an invalid handle when the pool is exhausted.
  [[nodiscard]] BufferHandle acquire() noexcept;

  [[nodiscard]] std::size_t block_size() const noexcept { return block_size_; }
  [[nodiscard]] std::size_t capacity() const noexcept { return capacity_; }
  [[nodiscard]] std::size_t in_use() const noexcept;
  [[nodiscard]] std::size_t available() const noexcept;
  [[nodiscard]] std::size_t peak_in_use() const noexcept;
  [[nodiscard]] std::size_t total_acquires() const noexcept;

 private:
  friend class BufferHandle;
  void return_block(BufferBlock* block) noexcept;

  std::size_t block_size_ = 0;
  std::size_t capacity_ = 0;
  std::vector<std::unique_ptr<BufferBlock>> blocks_;
  std::vector<BufferBlock*> free_;
  mutable std::mutex mutex_;
  std::size_t in_use_ = 0;
  std::size_t peak_ = 0;
  std::uint64_t acquires_ = 0;
};

struct FrameChunk {
  BufferHandle buffer;
  std::size_t offset = 0;
  std::size_t size = 0;
};

class Frame {
 public:
  Frame() = default;

  std::uint8_t camera_id = 0;
  std::uint32_t frame_index = 0;
  std::uint32_t width = 0;
  std::uint32_t height = 0;
  std::uint32_t payload_size = 0;
  CopyMode mode = CopyMode::Copy;
  std::uint64_t device_timestamp = 0;
  std::int64_t host_timestamp_ns = 0;   // first packet of the frame
  std::int64_t assembly_ns = 0;         // completion - first packet
  std::vector<FrameChunk> chunks;

  [[nodiscard]] bool valid() const noexcept { return payload_size > 0; }

  /// True when the payload is one linear region (copy mode, or a single-chunk
  /// zero-copy frame).
  [[nodiscard]] bool is_contiguous() const noexcept;

  /// Linear view; empty when the frame is chunked.  Call copy_payload_to() or
  /// materialize() for chunked frames.
  [[nodiscard]] std::span<const std::byte> payload() const noexcept;

  /// Gather every chunk into `destination`.  Returns bytes written, or 0 when
  /// the destination is too small.
  [[nodiscard]] std::size_t copy_payload_to(std::span<std::byte> destination) const;

  /// Gather a chunked frame into a freshly allocated linear buffer.
  [[nodiscard]] std::vector<std::byte> materialize() const;

  /// FNV-1a 64 digest over the payload bytes in frame order.
  [[nodiscard]] std::uint64_t payload_digest() const;

  /// Samples `count` evenly spaced payload bytes (deterministic).
  [[nodiscard]] std::vector<std::byte> sample(std::size_t count) const;
};

}  // namespace einstar
