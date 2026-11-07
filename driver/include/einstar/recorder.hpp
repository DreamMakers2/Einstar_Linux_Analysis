// SPDX-License-Identifier: MIT
// Lossless raw recording and replay of the exact USB byte stream.
//
// File layout (all integers little-endian):
//
//   FileHeader (64 bytes)
//     0   char magic[8]      "EINSTAR1"
//     8   u16 version        1
//     10  u16 header_size    64
//     12  u32 flags
//     16  u64 start_host_ns  acquisition clock at start (0 = not set)
//     24  u64 reserved
//     32  u32 header_crc     CRC-32 over bytes [0, 32)
//     36  u32 reserved
//     40  u64 reserved
//     48  u64 reserved
//     56  u64 reserved
//
//   Record (36-byte header + payload + CRC)
//     0   u32 magic          0x52315345 ("ES1R")
//     4   u32 record_bytes   header + payload + crc
//     8   u8  type           1 = metadata, 2 = transfer, 3 = trailer
//     9   u8  status         TransferStatus value (0 for metadata/trailer)
//     10  u16 flags
//     12  u32 sequence
//     16  u64 host_time_ns
//     24  u64 device_timestamp
//     32  u32 payload_len
//     36  payload
//     36+payload_len   u32 crc32 over the whole record except this field
//
// The trailer payload is 32 bytes: u64 digest (FNV-1a 64 over every recorded
// transfer payload byte, in record order), u64 transfer_count, u64 byte_count,
// u64 reserved.
#pragma once

#include <cstddef>
#include <cstdint>
#include <memory>
#include <span>
#include <string>
#include <string_view>
#include <vector>

#include "einstar/queue.hpp"
#include "einstar/status.hpp"
#include "einstar/transport.hpp"

namespace einstar::recording {

inline constexpr std::uint32_t kRecordMagic = 0x52315345U;  // "ES1R"
inline constexpr std::uint16_t kFormatVersion = 1;
inline constexpr std::size_t kFileHeaderSize = 64;
inline constexpr std::size_t kRecordHeaderSize = 36;
inline constexpr std::size_t kTrailerPayloadSize = 32;

enum class RecordType : std::uint8_t {
  Metadata = 1,
  Transfer = 2,
  Trailer = 3,
};

struct RecordedTransfer {
  std::uint64_t sequence = 0;
  std::int64_t host_time_ns = 0;
  std::uint64_t device_timestamp = 0;
  TransferStatus status = TransferStatus::Ok;
  std::vector<std::byte> bytes;
};

struct RecordingHeader {
  std::uint16_t version = kFormatVersion;
  std::uint32_t flags = 0;
  std::uint64_t start_host_ns = 0;
  std::string metadata;
};

struct Recording {
  RecordingHeader header;
  std::vector<RecordedTransfer> transfers;
  std::uint64_t trailer_digest = 0;
  std::uint64_t trailer_transfers = 0;
  std::uint64_t trailer_bytes = 0;
  bool has_trailer = false;
  std::string source_path;
};

struct RecorderConfig {
  std::size_t queue_capacity = 256;
  BackpressurePolicy policy = BackpressurePolicy::Block;  // lossless by default
  std::string metadata;
  std::uint64_t start_host_ns = 0;
  bool fsync_on_stop = false;
};

/// Append-only recorder with a dedicated writer thread.  `record()` copies the
/// transfer bytes into the writer queue; Block keeps the recording lossless.
class RawRecorder {
 public:
  RawRecorder(std::string path, RecorderConfig config = {});
  RawRecorder(const RawRecorder&) = delete;
  RawRecorder& operator=(const RawRecorder&) = delete;
  ~RawRecorder();

  Status start();
  Status record(const RecordedTransfer& transfer);
  Status stop();

  [[nodiscard]] bool running() const noexcept;
  [[nodiscard]] std::uint64_t recorded_transfers() const noexcept;
  [[nodiscard]] std::uint64_t recorded_bytes() const noexcept;
  [[nodiscard]] std::uint64_t dropped_records() const noexcept;
  [[nodiscard]] std::uint64_t digest() const noexcept;
  [[nodiscard]] Status last_error() const;
  [[nodiscard]] const std::string& path() const noexcept;

  // CRC helpers are public so tests and tools can validate independently.
  static std::uint32_t crc32(std::span<const std::byte> data) noexcept;
  static std::uint64_t fnv1a64(std::span<const std::byte> data) noexcept;

 private:
  class Impl;
  std::unique_ptr<Impl> impl_;
};

/// Read and validate a whole recording.  Fails on magic/CRC/length corruption.
Result<Recording> read_recording(const std::string& path);

/// Write a recording (used by tests and by replay export).
Status write_recording(const std::string& path, const Recording& recording);

/// Replay chunk fed to SimulatedTransport.
struct ReplayTransfer {
  TransferStatus status = TransferStatus::Ok;
  std::vector<std::byte> bytes;
};

struct ReplaySource {
  std::shared_ptr<const std::vector<ReplayTransfer>> transfers;
};

ReplaySource make_replay_source(const Recording& recording);

}  // namespace einstar::recording
