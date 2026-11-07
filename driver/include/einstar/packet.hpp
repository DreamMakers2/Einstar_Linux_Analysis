// SPDX-License-Identifier: MIT
// Stream packet framing for EP 0x83.
//
// Wire format (verified in einscan10.2.0.0.dll ImageStreamThread::run):
//
//   [u32 BE len][32-byte header][payload] ...
//
// `len` covers header + payload (len = 32 + payload_len).  Header byte 8 == 1
// marks the first packet of a frame; bytes 12..15 are a u32 BE frame/index
// field; bytes 16..23 are a u64 BE device timestamp; byte 2 carries a 3-bit
// camera id in bits 0..2 and 3 auxiliary bits in bits 3..5.
//
// A USB transfer may contain several complete packets, one packet, or (with
// short reads) part of a packet.  The framer keeps a bounded carry buffer for
// split packets; complete packets that live entirely inside the transfer are
// exposed in place (zero copy).
#pragma once

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <span>
#include <vector>

namespace einstar {

// Header field offsets, verified in einscan10.2.0.0.dll::ImageStreamThread::run and
// independently in libeinstar (which captured images from real hardware):
//   [2] bits 3..5 : one-hot camera selector (1, 2, 4); bits 0..2: sub-index/chunk counter
//   [8]           : 1 on the LAST packet of a frame (frame is complete only then)
//   [12..15]      : u32 BE frame id  -- valid on the last packet only
//   [16..23]      : u64 BE device timestamp -- latched from the first packet of a frame
inline constexpr std::size_t kStreamHeaderSize = 32;
inline constexpr std::size_t kHdrCameraByte = 2;
inline constexpr std::size_t kHdrLastFlag = 8;
inline constexpr std::size_t kHdrFrameId = 12;
inline constexpr std::size_t kHdrTimestamp = 16;
inline constexpr std::uint8_t kInvalidCamera = 0xFF;
inline constexpr std::uint8_t kCameraCount = 3;
inline constexpr std::size_t kStreamLengthSize = 4;
inline constexpr std::uint32_t kStreamChunkPayload = 0xA000;  // 40,960
inline constexpr std::uint32_t kStreamPacketLen = 0xA020;     // 32 + 0xA000
inline constexpr std::uint32_t kDefaultMaxPacketBytes = 0xA400;
inline constexpr std::uint32_t kDefaultFrameSize = 1280U * 1024U;

inline std::uint32_t load_be32(const std::byte* p) noexcept {
  return (static_cast<std::uint32_t>(std::to_integer<std::uint8_t>(p[0])) << 24) |
         (static_cast<std::uint32_t>(std::to_integer<std::uint8_t>(p[1])) << 16) |
         (static_cast<std::uint32_t>(std::to_integer<std::uint8_t>(p[2])) << 8) |
         static_cast<std::uint32_t>(std::to_integer<std::uint8_t>(p[3]));
}

inline std::uint64_t load_be64(const std::byte* p) noexcept {
  return (static_cast<std::uint64_t>(load_be32(p)) << 32) | load_be32(p + 4);
}

inline void store_be32(std::byte* p, std::uint32_t value) noexcept {
  p[0] = static_cast<std::byte>((value >> 24) & 0xFFU);
  p[1] = static_cast<std::byte>((value >> 16) & 0xFFU);
  p[2] = static_cast<std::byte>((value >> 8) & 0xFFU);
  p[3] = static_cast<std::byte>(value & 0xFFU);
}

inline void store_be64(std::byte* p, std::uint64_t value) noexcept {
  store_be32(p, static_cast<std::uint32_t>(value >> 32));
  store_be32(p + 4, static_cast<std::uint32_t>(value & 0xFFFFFFFFULL));
}

/// Map the one-hot camera selector to a 0-based index (1->0, 2->1, 4->2).
[[nodiscard]] inline constexpr std::uint8_t camera_index_from_field(
    std::uint8_t field) noexcept {
  return field == 1U ? 0U : field == 2U ? 1U : field == 4U ? 2U : kInvalidCamera;
}

/// Inverse of camera_index_from_field (0->1, 1->2, 2->4).
[[nodiscard]] inline constexpr std::uint8_t camera_field_from_index(
    std::uint8_t index) noexcept {
  return index == 0U ? 1U : index == 1U ? 2U : index == 2U ? 4U : 0U;
}

struct StreamPacket {
  std::uint32_t declared_length = 0;
  std::uint8_t raw_camera_byte = 0;
  /// One-hot selector from header byte 2 bits 3..5 (1, 2 or 4).
  std::uint8_t camera_field = 0;
  /// 0-based camera index derived from camera_field (0, 1, 2) or kInvalidCamera.
  std::uint8_t camera_index = kInvalidCamera;
  /// Sub-index/chunk counter from header byte 2 bits 0..2.
  std::uint8_t sub_index = 0;
  /// True when header byte 8 == 1: this packet completes the frame.
  bool last_of_frame = false;
  /// Frame id (u32 BE at header 12).  Only meaningful when last_of_frame is true.
  std::uint32_t frame_index = 0;
  /// Device timestamp (u64 BE at header 16).  Only meaningful on the first
  /// packet of a frame; the reassembler latches it there.
  std::uint64_t device_timestamp = 0;
  const std::byte* payload = nullptr;
  std::size_t payload_size = 0;
  /// True when the packet had to be stitched from a previous short read and
  /// therefore points into the framer's internal staging buffer.
  bool staged = false;
  std::array<std::byte, kStreamHeaderSize> header{};
};

struct FramerStats {
  std::uint64_t packets = 0;
  std::uint64_t protocol_errors = 0;
  std::uint64_t resync_bytes = 0;
  std::uint64_t staged_packets = 0;
  std::uint64_t oversized_lengths = 0;
  std::uint64_t invalid_headers = 0;
};

class PacketFramer {
 public:
  explicit PacketFramer(std::uint32_t max_packet_bytes = kDefaultMaxPacketBytes)
      : max_packet_bytes_(max_packet_bytes == 0 ? kDefaultMaxPacketBytes
                                                : max_packet_bytes) {}

  /// Feed one transfer's bytes and invoke `visitor(const StreamPacket&)` for
  /// every complete packet.  Returns the number of packets emitted.  Bytes of
  /// an incomplete packet are retained (bounded by max_packet_bytes + 4).
  template <typename Visitor>
  std::size_t consume(std::span<const std::byte> input, Visitor&& visitor,
                      FramerStats& stats) {
    std::size_t emitted = 0;

    if (!carry_.empty()) {
      carry_.insert(carry_.end(), input.begin(), input.end());
      std::size_t offset = 0;
      while (offset + kStreamLengthSize <= carry_.size()) {
        const std::uint32_t length = load_be32(carry_.data() + offset);
        if (length < kStreamHeaderSize || length > max_packet_bytes_) {
          stats.protocol_errors++;
          if (length > max_packet_bytes_) {
            stats.oversized_lengths++;
          }
          offset += 1;
          stats.resync_bytes++;
          continue;
        }
        const std::size_t total = kStreamLengthSize + length;
        if (carry_.size() - offset < kStreamLengthSize + kStreamHeaderSize) {
          break;
        }
        if (carry_.size() - offset < total) {
          break;
        }
        emit(carry_.data() + offset, stats, visitor, emitted, /*staged=*/true);
        offset += total;
      }
      if (offset > 0) {
        carry_.erase(carry_.begin(),
                     carry_.begin() + static_cast<std::ptrdiff_t>(offset));
      }
      return emitted;
    }

    std::size_t offset = 0;
    while (offset < input.size()) {
      const std::span<const std::byte> rest = input.subspan(offset);
      if (rest.size() < kStreamLengthSize) {
        stash(rest);
        break;
      }
      const std::uint32_t length = load_be32(rest.data());
      const bool length_plausible =
          length >= kStreamHeaderSize && length <= max_packet_bytes_;
      if (!length_plausible) {
        stats.protocol_errors++;
        if (length > max_packet_bytes_) {
          stats.oversized_lengths++;
        }
        offset += 1;
        stats.resync_bytes++;
        continue;
      }
      const std::size_t total = kStreamLengthSize + length;
      if (rest.size() < kStreamLengthSize + kStreamHeaderSize) {
        stash(rest);
        break;
      }
      if (rest.size() < total) {
        stash(rest);
        break;
      }
      emit(rest.data(), stats, visitor, emitted, /*staged=*/false);
      offset += total;
    }
    return emitted;
  }

  void reset() noexcept { carry_.clear(); }

  [[nodiscard]] std::size_t buffered_bytes() const noexcept {
    return carry_.size();
  }

  [[nodiscard]] std::uint32_t max_packet_bytes() const noexcept {
    return max_packet_bytes_;
  }

 private:
  void stash(std::span<const std::byte> fragment) {
    if (fragment.size() > max_packet_bytes_ + kStreamLengthSize) {
      carry_.assign(fragment.end() - static_cast<std::ptrdiff_t>(
                                       std::min<std::size_t>(
                                           fragment.size(),
                                           kStreamLengthSize - 1)),
                    fragment.end());
      return;
    }
    carry_.assign(fragment.begin(), fragment.end());
  }

  template <typename Visitor>
  void emit(const std::byte* base, FramerStats& stats, Visitor& visitor,
            std::size_t& emitted, bool staged) {
    const std::uint32_t length = load_be32(base);
    const std::byte* header_bytes = base + kStreamLengthSize;
    StreamPacket packet;
    packet.declared_length = length;
    std::memcpy(packet.header.data(), header_bytes, kStreamHeaderSize);
    packet.raw_camera_byte = std::to_integer<std::uint8_t>(packet.header[kHdrCameraByte]);
    packet.camera_field = static_cast<std::uint8_t>((packet.raw_camera_byte >> 3) & 0x07U);
    packet.camera_index = camera_index_from_field(packet.camera_field);
    packet.sub_index = static_cast<std::uint8_t>(packet.raw_camera_byte & 0x07U);
    packet.last_of_frame =
        std::to_integer<std::uint8_t>(packet.header[kHdrLastFlag]) == 1U;
    packet.frame_index = load_be32(packet.header.data() + kHdrFrameId);
    packet.device_timestamp = load_be64(packet.header.data() + kHdrTimestamp);
    packet.payload = header_bytes + kStreamHeaderSize;
    packet.payload_size = length - kStreamHeaderSize;
    packet.staged = staged;
    stats.packets++;
    if (staged) {
      stats.staged_packets++;
    }
    visitor(packet);
    emitted++;
  }

  std::uint32_t max_packet_bytes_;
  std::vector<std::byte> carry_;
};

}  // namespace einstar
