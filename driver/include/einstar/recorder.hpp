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
//
// Streaming reads
// ---------------
// `RecordingStreamReader` validates the same structure incrementally while it
// serves payload bytes: RAM is O(read destination) + O(largest metadata record),
// never O(file).  It is the recommended way to replay a multi-gigabyte
// recording; `read_recording` remains for small files and unit tests.
#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <fstream>
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

// ---------------------------------------------------------------------------
// Streaming verification / replay
// ---------------------------------------------------------------------------

/// Result of validating a recording incrementally, including the trailer
/// comparison.  `trailer_verified` is the only field that may be treated as
/// "the stream digest was checked": it is true only when a trailer record was
/// present *and* its digest, transfer count and byte count all matched.
struct StreamingVerifyStats {
  std::uint64_t record_count = 0;         // metadata + transfer + trailer records
  std::uint64_t metadata_records = 0;
  std::uint64_t metadata_bytes = 0;
  std::uint64_t transfer_records = 0;     // includes non-Ok and zero-length
  std::uint64_t non_ok_transfers = 0;
  std::uint64_t zero_length_transfers = 0;
  std::uint64_t payload_bytes = 0;        // sum of transfer payload lengths
  std::uint64_t digest = 14695981039346656037ULL;  // FNV-1a 64 over payloads
  std::uint64_t file_size = 0;
  std::uint64_t bytes_consumed = 0;
  bool has_trailer = false;
  std::uint64_t trailer_digest = 0;
  std::uint64_t trailer_transfers = 0;
  std::uint64_t trailer_bytes = 0;
  bool digest_matches = false;
  bool counts_match = false;
  bool trailer_verified = false;
  /// First up to 64 payload bytes of the first non-empty Ok transfer.  Used to
  /// detect legacy framing without retaining the recording.
  std::array<std::byte, 64> first_payload_prefix{};
  std::uint32_t first_payload_prefix_size = 0;
};

/// Pull-based replay source consumed by `SimulatedTransport`.  One call returns
/// payload bytes from the next recorded transfer.  A transfer whose recorded
/// status is not Ok is reported exactly once as zero bytes plus that status;
/// end of stream reports zero bytes plus `TransferStatus::Timeout`.
class ReplayReader {
 public:
  ReplayReader() = default;
  ReplayReader(const ReplayReader&) = delete;
  ReplayReader& operator=(const ReplayReader&) = delete;
  virtual ~ReplayReader() = default;

  virtual std::size_t read(std::span<std::byte> destination,
                           TransferStatus& status) = 0;

  /// Capture-clock timestamp of the transfer record currently being served
  /// (0 when the source has none).  `SimulatedTransport` uses it so replayed
  /// frames carry the real capture timing instead of the virtual replay clock.
  [[nodiscard]] virtual std::int64_t last_host_time_ns() const noexcept {
    return 0;
  }
  [[nodiscard]] virtual std::uint64_t last_device_timestamp() const noexcept {
    return 0;
  }
};

/// Incremental, memory-bounded reader for one `.e1r` recording.
///
/// Validates the file header (magic/version/header size/header CRC), every
/// record magic/length/payload length/CRC, the transfer status range, the
/// metadata/trailer payloads, the FNV-1a payload digest, the transfer/byte
/// counts against the trailer, and rejects trailing bytes after the trailer.
/// A missing or truncated trailer is a hard failure: `read()` reports
/// `TransferStatus::Error` and `error()` carries the precise `Protocol` status.
/// Every resource is bounded by one record (max 4 MiB payload by format).
class RecordingStreamReader final : public ReplayReader {
 public:
  /// Open and validate the file header, then skip/validate leading metadata
  /// records up to the first transfer record.  Never loads transfer payloads.
  static Result<std::unique_ptr<RecordingStreamReader>> open(
      const std::string& path);

  ~RecordingStreamReader() override;

  std::size_t read(std::span<std::byte> destination,
                   TransferStatus& status) override;
  [[nodiscard]] std::int64_t last_host_time_ns() const noexcept override {
    return current_host_time_ns_;
  }
  [[nodiscard]] std::uint64_t last_device_timestamp() const noexcept override {
    return current_device_timestamp_;
  }

  [[nodiscard]] bool at_end() const noexcept { return at_end_; }
  [[nodiscard]] bool failed() const noexcept { return failed_; }
  [[nodiscard]] Status error() const { return error_; }
  [[nodiscard]] const RecordingHeader& header() const noexcept {
    return header_;
  }
  [[nodiscard]] std::uint64_t file_size() const noexcept { return file_size_; }
  [[nodiscard]] std::uint64_t bytes_consumed() const noexcept {
    return offset_;
  }
  /// File offset of the record currently being read (diagnostics).
  [[nodiscard]] std::uint64_t record_offset() const noexcept {
    return record_offset_;
  }
  /// Up to 64 payload bytes of the first non-empty Ok transfer; used to detect
  /// legacy length-prefixed framing without keeping the recording in memory.
  [[nodiscard]] std::span<const std::byte> first_transfer_prefix() const
      noexcept {
    return std::span<const std::byte>(first_prefix_.data(), first_prefix_size_);
  }
  /// Running statistics; complete once `at_end()` is true.
  [[nodiscard]] const StreamingVerifyStats& stats() const noexcept {
    return stats_;
  }
  /// Final validation.  Only succeeds after a clean `at_end()`; returns the
  /// statistics (with `trailer_verified`) or the first hard failure.
  Result<StreamingVerifyStats> finish();

 private:
  explicit RecordingStreamReader(std::string path);

  Status open_impl();
  void fail_reader(Status status);
  bool read_exact(void* destination, std::size_t bytes);
  bool advance();
  bool read_record_header();
  bool finish_transfer_record();
  bool consume_payload(std::span<std::byte> scratch);
  bool validate_trailer(std::span<const std::byte> payload);

  std::string path_;
  std::ifstream file_;
  std::uint64_t file_size_ = 0;
  std::uint64_t offset_ = 0;
  std::uint64_t record_offset_ = 0;
  RecordingHeader header_;
  StreamingVerifyStats stats_;
  Status error_;
  bool failed_ = false;
  bool at_end_ = false;
  bool saw_trailer_ = false;

  // Current transfer record state.
  std::array<std::byte, kRecordHeaderSize> record_header_{};
  std::uint32_t payload_remaining_ = 0;
  TransferStatus transfer_status_ = TransferStatus::Ok;
  std::uint32_t transfer_sequence_ = 0;
  std::uint64_t transfer_host_ns_ = 0;
  std::uint64_t transfer_device_timestamp_ = 0;
  std::int64_t current_host_time_ns_ = 0;
  std::uint64_t current_device_timestamp_ = 0;
  std::uint32_t running_crc_ = 0xFFFFFFFFU;
  bool in_transfer_ = false;

  std::array<std::byte, 64> first_prefix_{};
  std::size_t first_prefix_size_ = 0;
};

/// Stream a recording end-to-end and verify it, without retaining payloads.
Result<StreamingVerifyStats> verify_recording_stream(const std::string& path);

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
  ReplaySource() = default;
  explicit ReplaySource(
      std::shared_ptr<const std::vector<ReplayTransfer>> in_memory)
      : transfers(std::move(in_memory)) {}

  /// In-memory replay (small recordings and tests).
  std::shared_ptr<const std::vector<ReplayTransfer>> transfers;
  /// Streaming replay: when set, `SimulatedTransport` pulls payload bytes from
  /// this reader instead of the in-memory vector.
  std::shared_ptr<ReplayReader> reader;
};

ReplaySource make_replay_source(const Recording& recording);

}  // namespace einstar::recording
