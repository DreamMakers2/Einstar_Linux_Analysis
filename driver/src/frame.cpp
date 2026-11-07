// SPDX-License-Identifier: MIT
#include "einstar/frame.hpp"

#include <algorithm>
#include <atomic>
#include <cstring>
#include <mutex>
#include <utility>
#include <vector>

namespace einstar {

std::string_view to_string(CopyMode mode) noexcept {
  return mode == CopyMode::ZeroCopy ? "zero-copy" : "copy";
}

struct BufferBlock {
  std::vector<std::byte> storage;
  BufferPool* owner = nullptr;
  std::uint32_t index = 0;
  std::atomic<std::uint32_t> references{0};
};

BufferHandle::BufferHandle(const BufferHandle& other) noexcept
    : block_(other.block_) {
  if (block_ != nullptr) {
    block_->references.fetch_add(1, std::memory_order_relaxed);
  }
}

BufferHandle::BufferHandle(BufferHandle&& other) noexcept
    : block_(std::exchange(other.block_, nullptr)) {}

BufferHandle& BufferHandle::operator=(const BufferHandle& other) noexcept {
  if (this != &other) {
    BufferHandle copy(other);
    *this = std::move(copy);
  }
  return *this;
}

BufferHandle& BufferHandle::operator=(BufferHandle&& other) noexcept {
  if (this != &other) {
    release();
    block_ = std::exchange(other.block_, nullptr);
  }
  return *this;
}

BufferHandle::~BufferHandle() { release(); }

void BufferHandle::release() noexcept {
  if (block_ == nullptr) {
    return;
  }
  BufferBlock* block = std::exchange(block_, nullptr);
  if (block->references.fetch_sub(1, std::memory_order_acq_rel) == 1) {
    block->owner->return_block(block);
  }
}

std::span<std::byte> BufferHandle::bytes() noexcept {
  if (block_ == nullptr) {
    return {};
  }
  return std::span<std::byte>(block_->storage.data(), block_->storage.size());
}

std::span<const std::byte> BufferHandle::bytes() const noexcept {
  if (block_ == nullptr) {
    return {};
  }
  return std::span<const std::byte>(block_->storage.data(),
                                    block_->storage.size());
}

std::byte* BufferHandle::data() noexcept {
  return block_ == nullptr ? nullptr : block_->storage.data();
}

const std::byte* BufferHandle::data() const noexcept {
  return block_ == nullptr ? nullptr : block_->storage.data();
}

std::size_t BufferHandle::capacity() const noexcept {
  return block_ == nullptr ? 0 : block_->storage.size();
}

void BufferHandle::reset() noexcept { release(); }

BufferPool::BufferPool(std::size_t block_size, std::size_t block_count)
    : block_size_(block_size) {
  if (block_size_ == 0 || block_count == 0) {
    return;
  }
  blocks_.reserve(block_count);
  free_.reserve(block_count);
  for (std::size_t i = 0; i < block_count; ++i) {
    auto block = std::make_unique<BufferBlock>();
    block->storage.resize(block_size_);
    block->owner = this;
    block->index = static_cast<std::uint32_t>(i);
    free_.push_back(block.get());
    blocks_.push_back(std::move(block));
  }
  capacity_ = blocks_.size();
}

BufferPool::~BufferPool() = default;

BufferHandle BufferPool::acquire() noexcept {
  std::lock_guard lock(mutex_);
  if (free_.empty()) {
    return {};
  }
  BufferBlock* block = free_.back();
  free_.pop_back();
  block->references.store(1, std::memory_order_relaxed);
  in_use_++;
  acquires_++;
  if (in_use_ > peak_) {
    peak_ = in_use_;
  }
  return BufferHandle(block);
}

void BufferPool::return_block(BufferBlock* block) noexcept {
  std::lock_guard lock(mutex_);
  if (in_use_ > 0) {
    in_use_--;
  }
  free_.push_back(block);
}

std::size_t BufferPool::in_use() const noexcept {
  std::lock_guard lock(mutex_);
  return in_use_;
}

std::size_t BufferPool::available() const noexcept {
  std::lock_guard lock(mutex_);
  return free_.size();
}

std::size_t BufferPool::peak_in_use() const noexcept {
  std::lock_guard lock(mutex_);
  return peak_;
}

std::size_t BufferPool::total_acquires() const noexcept {
  std::lock_guard lock(mutex_);
  return static_cast<std::size_t>(acquires_);
}

bool Frame::is_contiguous() const noexcept {
  return chunks.size() == 1 && chunks[0].offset == 0 &&
         chunks[0].size == payload_size && payload_size > 0;
}

std::span<const std::byte> Frame::payload() const noexcept {
  if (!is_contiguous()) {
    return {};
  }
  const auto bytes = chunks[0].buffer.bytes();
  if (bytes.size() < chunks[0].offset + chunks[0].size) {
    return {};
  }
  return bytes.subspan(chunks[0].offset, chunks[0].size);
}

std::size_t Frame::copy_payload_to(std::span<std::byte> destination) const {
  if (destination.size() < payload_size) {
    return 0;
  }
  std::size_t written = 0;
  for (const auto& chunk : chunks) {
    const auto bytes = chunk.buffer.bytes();
    if (bytes.size() < chunk.offset + chunk.size) {
      return 0;
    }
    std::memcpy(destination.data() + written,
                bytes.data() + chunk.offset, chunk.size);
    written += chunk.size;
  }
  return written;
}

std::vector<std::byte> Frame::materialize() const {
  std::vector<std::byte> result(payload_size);
  if (payload_size == 0) {
    return result;
  }
  if (copy_payload_to(result) != payload_size) {
    result.clear();
  }
  return result;
}

std::uint64_t Frame::payload_digest() const {
  std::uint64_t hash = 14695981039346656037ULL;
  for (const auto& chunk : chunks) {
    const auto bytes = chunk.buffer.bytes();
    if (bytes.size() < chunk.offset + chunk.size) {
      return 0;
    }
    const std::byte* data = bytes.data() + chunk.offset;
    for (std::size_t i = 0; i < chunk.size; ++i) {
      hash ^= static_cast<std::uint64_t>(std::to_integer<std::uint8_t>(data[i]));
      hash *= 1099511628211ULL;
    }
  }
  return hash;
}

std::vector<std::byte> Frame::sample(std::size_t count) const {
  std::vector<std::byte> result;
  if (payload_size == 0 || count == 0) {
    return result;
  }
  result.reserve(count);
  std::size_t chunk_start = 0;
  std::size_t chunk_index = 0;
  for (std::size_t i = 0; i < count; ++i) {
    const std::size_t position =
        (i * static_cast<std::size_t>(payload_size)) / count;
    while (chunk_index + 1 < chunks.size() &&
           position >= chunk_start + chunks[chunk_index].size) {
      chunk_start += chunks[chunk_index].size;
      ++chunk_index;
    }
    if (chunk_index >= chunks.size()) {
      break;
    }
    const auto bytes = chunks[chunk_index].buffer.bytes();
    const std::size_t offset = chunks[chunk_index].offset +
                               (position - chunk_start);
    if (offset < bytes.size()) {
      result.push_back(bytes[offset]);
    }
  }
  return result;
}

}  // namespace einstar
