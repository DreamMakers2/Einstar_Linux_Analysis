// SPDX-License-Identifier: MIT
#include "einstar/recorder.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <cstring>
#include <fstream>
#include <memory>
#include <mutex>
#include <thread>
#include <utility>

#include "einstar/recorder.hpp"

namespace einstar::recording {
namespace {

constexpr char kMagic[8] = {'E', 'I', 'N', 'S', 'T', 'A', 'R', '1'};

const std::array<std::uint32_t, 256>& crc_table() {
  static const std::array<std::uint32_t, 256> table = [] {
    std::array<std::uint32_t, 256> result{};
    for (std::uint32_t i = 0; i < 256; ++i) {
      std::uint32_t crc = i;
      for (int bit = 0; bit < 8; ++bit) {
        crc = (crc & 1U) != 0U ? (crc >> 1U) ^ 0xEDB88320U : crc >> 1U;
      }
      result[i] = crc;
    }
    return result;
  }();
  return table;
}

void put_u32(std::vector<std::byte>& out, std::uint32_t value) {
  out.push_back(static_cast<std::byte>(value & 0xFFU));
  out.push_back(static_cast<std::byte>((value >> 8U) & 0xFFU));
  out.push_back(static_cast<std::byte>((value >> 16U) & 0xFFU));
  out.push_back(static_cast<std::byte>((value >> 24U) & 0xFFU));
}

void put_u64(std::vector<std::byte>& out, std::uint64_t value) {
  put_u32(out, static_cast<std::uint32_t>(value & 0xFFFFFFFFULL));
  put_u32(out, static_cast<std::uint32_t>(value >> 32U));
}

void write_u16(std::vector<std::byte>& out, std::size_t offset,
               std::uint16_t value) {
  out[offset] = static_cast<std::byte>(value & 0xFFU);
  out[offset + 1] = static_cast<std::byte>((value >> 8U) & 0xFFU);
}

void write_u32(std::vector<std::byte>& out, std::size_t offset,
               std::uint32_t value) {
  out[offset] = static_cast<std::byte>(value & 0xFFU);
  out[offset + 1] = static_cast<std::byte>((value >> 8U) & 0xFFU);
  out[offset + 2] = static_cast<std::byte>((value >> 16U) & 0xFFU);
  out[offset + 3] = static_cast<std::byte>((value >> 24U) & 0xFFU);
}

void write_u64(std::vector<std::byte>& out, std::size_t offset,
               std::uint64_t value) {
  write_u32(out, offset, static_cast<std::uint32_t>(value & 0xFFFFFFFFULL));
  write_u32(out, offset + 4, static_cast<std::uint32_t>(value >> 32U));
}

std::uint16_t load_u16(const std::byte* data) {
  return static_cast<std::uint16_t>(std::to_integer<std::uint8_t>(data[0])) |
         static_cast<std::uint16_t>(
             static_cast<std::uint16_t>(std::to_integer<std::uint8_t>(data[1]))
             << 8U);
}

std::uint32_t load_u32(const std::byte* data) {
  return static_cast<std::uint32_t>(std::to_integer<std::uint8_t>(data[0])) |
         (static_cast<std::uint32_t>(std::to_integer<std::uint8_t>(data[1]))
          << 8U) |
         (static_cast<std::uint32_t>(std::to_integer<std::uint8_t>(data[2]))
          << 16U) |
         (static_cast<std::uint32_t>(std::to_integer<std::uint8_t>(data[3]))
          << 24U);
}

std::uint64_t load_u64(const std::byte* data) {
  return static_cast<std::uint64_t>(load_u32(data)) |
         (static_cast<std::uint64_t>(load_u32(data + 4)) << 32U);
}

std::vector<std::byte> make_file_header(std::uint64_t start_host_ns,
                                        std::uint32_t flags) {
  std::vector<std::byte> header(kFileHeaderSize, std::byte{0});
  std::memcpy(header.data(), kMagic, sizeof(kMagic));
  write_u16(header, 8, kFormatVersion);
  write_u16(header, 10, static_cast<std::uint16_t>(kFileHeaderSize));
  write_u32(header, 12, flags);
  write_u64(header, 16, start_host_ns);
  const std::span<const std::byte> prefix(header.data(), 32);
  write_u32(header, 32, RawRecorder::crc32(prefix));
  return header;
}

std::vector<std::byte> make_record(RecordType type, TransferStatus status,
                                   std::uint16_t flags, std::uint32_t sequence,
                                   std::uint64_t host_time_ns,
                                   std::uint64_t device_timestamp,
                                   std::span<const std::byte> payload) {
  const std::size_t total = kRecordHeaderSize + payload.size() + 4;
  std::vector<std::byte> record(total, std::byte{0});
  write_u32(record, 0, kRecordMagic);
  write_u32(record, 4, static_cast<std::uint32_t>(total));
  record[8] = static_cast<std::byte>(type);
  record[9] = static_cast<std::byte>(status);
  write_u16(record, 10, flags);
  write_u32(record, 12, sequence);
  write_u64(record, 16, host_time_ns);
  write_u64(record, 24, device_timestamp);
  write_u32(record, 32, static_cast<std::uint32_t>(payload.size()));
  if (!payload.empty()) {
    std::memcpy(record.data() + kRecordHeaderSize, payload.data(),
                payload.size());
  }
  const std::span<const std::byte> covered(record.data(),
                                           kRecordHeaderSize + payload.size());
  write_u32(record, kRecordHeaderSize + payload.size(),
            RawRecorder::crc32(covered));
  return record;
}

std::uint64_t digest_update(std::uint64_t hash, std::span<const std::byte> data) {
  for (const std::byte value : data) {
    hash ^= static_cast<std::uint64_t>(std::to_integer<std::uint8_t>(value));
    hash *= 1099511628211ULL;
  }
  return hash;
}

constexpr std::uint64_t kFnvOffset = 14695981039346656037ULL;

}  // namespace

std::uint32_t RawRecorder::crc32(std::span<const std::byte> data) noexcept {
  std::uint32_t crc = 0xFFFFFFFFU;
  const auto& table = crc_table();
  for (const std::byte value : data) {
    crc = table[(crc ^ std::to_integer<std::uint8_t>(value)) & 0xFFU] ^
          (crc >> 8U);
  }
  return crc ^ 0xFFFFFFFFU;
}

std::uint64_t RawRecorder::fnv1a64(std::span<const std::byte> data) noexcept {
  return digest_update(kFnvOffset, data);
}

class RawRecorder::Impl {
 public:
  Impl(std::string path_in, RecorderConfig config_in)
      : path(std::move(path_in)),
        config(std::move(config_in)),
        queue(std::max<std::size_t>(1, config.queue_capacity), config.policy) {}

  Status fail(Status status) {
    {
      std::lock_guard lock(error_mutex);
      if (error.ok()) {
        error = status;
      }
    }
    failed.store(true, std::memory_order_release);
    queue.close();
    return status;
  }

  Status error_status() const {
    std::lock_guard lock(error_mutex);
    return error;
  }

  bool write_record(const std::vector<std::byte>& record) {
    file.write(reinterpret_cast<const char*>(record.data()),
               static_cast<std::streamsize>(record.size()));
    if (!file) {
      fail(Status::error(ErrorCode::IoError,
                         "failed to write recording: " + path));
      return false;
    }
    return true;
  }

  void write_metadata() {
    if (config.metadata.empty()) {
      return;
    }
    const std::span<const std::byte> payload(
        reinterpret_cast<const std::byte*>(config.metadata.data()),
        config.metadata.size());
    const auto record = make_record(RecordType::Metadata, TransferStatus::Ok, 0,
                                    0, config.start_host_ns, 0, payload);
    write_record(record);
  }

  void write_transfer(const RecordedTransfer& transfer) {
    const std::span<const std::byte> payload(transfer.bytes.data(),
                                             transfer.bytes.size());
    const auto record =
        make_record(RecordType::Transfer, transfer.status, 0,
                    static_cast<std::uint32_t>(transfer.sequence),
                    static_cast<std::uint64_t>(transfer.host_time_ns),
                    transfer.device_timestamp, payload);
    if (!write_record(record)) {
      return;
    }
    records.fetch_add(1, std::memory_order_relaxed);
    bytes.fetch_add(transfer.bytes.size(), std::memory_order_relaxed);
    digest.store(digest_update(digest.load(std::memory_order_relaxed), payload),
                 std::memory_order_relaxed);
  }

  void write_trailer() {
    std::vector<std::byte> payload;
    payload.reserve(kTrailerPayloadSize);
    put_u64(payload, digest.load(std::memory_order_relaxed));
    put_u64(payload, records.load(std::memory_order_relaxed));
    put_u64(payload, bytes.load(std::memory_order_relaxed));
    put_u64(payload, 0);
    const auto record =
        make_record(RecordType::Trailer, TransferStatus::Ok, 0, 0, 0, 0,
                    std::span<const std::byte>(payload));
    write_record(record);
  }

  void run() {
    RecordedTransfer transfer;
    while (true) {
      if (queue.pop(transfer, std::chrono::milliseconds(50))) {
        write_transfer(transfer);
        if (failed.load(std::memory_order_acquire)) {
          break;
        }
        continue;
      }
      if (queue.closed() && queue.size() == 0) {
        break;
      }
    }
    if (!failed.load(std::memory_order_acquire)) {
      write_trailer();
    }
    file.flush();
    if (config.fsync_on_stop) {
      // Not all libstdc++ versions expose a portable fd from ofstream; the
      // flush above is the portable guarantee.  fsync is intentionally not
      // faked.
    }
    file.close();
  }

  std::string path;
  RecorderConfig config;
  std::ofstream file;
  BoundedQueue<RecordedTransfer> queue;
  std::thread writer;
  std::atomic<bool> started{false};
  std::atomic<bool> stopping{false};
  std::atomic<bool> failed{false};
  std::atomic<std::uint64_t> records{0};
  std::atomic<std::uint64_t> bytes{0};
  std::atomic<std::uint64_t> dropped{0};
  std::atomic<std::uint64_t> digest{kFnvOffset};
  mutable std::mutex error_mutex;
  Status error;
};

RawRecorder::RawRecorder(std::string path, RecorderConfig config)
    : impl_(std::make_unique<Impl>(std::move(path), std::move(config))) {}

RawRecorder::~RawRecorder() {
  if (impl_ != nullptr) {
    (void)stop();
  }
}

Status RawRecorder::start() {
  if (impl_->started.load(std::memory_order_acquire)) {
    return Status::error(ErrorCode::AlreadyOpen, "recorder already started");
  }
  impl_->file.open(impl_->path, std::ios::binary | std::ios::trunc);
  if (!impl_->file) {
    return impl_->fail(Status::error(
        ErrorCode::IoError, "cannot open recording file: " + impl_->path));
  }
  const auto header = make_file_header(impl_->config.start_host_ns, 0);
  if (!impl_->write_record(header)) {
    return impl_->error_status();
  }
  impl_->write_metadata();
  if (impl_->failed.load(std::memory_order_acquire)) {
    return impl_->error_status();
  }
  impl_->started.store(true, std::memory_order_release);
  impl_->writer = std::thread([impl = impl_.get()] { impl->run(); });
  return Status::success();
}

Status RawRecorder::record(const RecordedTransfer& transfer) {
  if (!impl_->started.load(std::memory_order_acquire)) {
    return Status::error(ErrorCode::NotOpen, "recorder is not running");
  }
  if (impl_->failed.load(std::memory_order_acquire)) {
    return impl_->error_status();
  }
  const PushOutcome outcome = impl_->queue.push(transfer);
  if (outcome == PushOutcome::Closed) {
    return Status::error(ErrorCode::Closed, "recording queue is closed");
  }
  if (outcome == PushOutcome::DroppedNewest ||
      outcome == PushOutcome::DroppedOldest) {
    impl_->dropped.fetch_add(1, std::memory_order_relaxed);
  }
  return Status::success();
}

Status RawRecorder::stop() {
  if (impl_ == nullptr ||
      !impl_->started.load(std::memory_order_acquire)) {
    return Status::success();
  }
  if (impl_->stopping.exchange(true, std::memory_order_acq_rel)) {
    return Status::success();
  }
  impl_->queue.close();
  if (impl_->writer.joinable()) {
    impl_->writer.join();
  }
  impl_->started.store(false, std::memory_order_release);
  impl_->stopping.store(false, std::memory_order_release);
  return impl_->error_status();
}

bool RawRecorder::running() const noexcept {
  return impl_ != nullptr &&
         impl_->started.load(std::memory_order_acquire) &&
         !impl_->failed.load(std::memory_order_acquire);
}

std::uint64_t RawRecorder::recorded_transfers() const noexcept {
  return impl_ == nullptr ? 0
                          : impl_->records.load(std::memory_order_relaxed);
}

std::uint64_t RawRecorder::recorded_bytes() const noexcept {
  return impl_ == nullptr ? 0 : impl_->bytes.load(std::memory_order_relaxed);
}

std::uint64_t RawRecorder::dropped_records() const noexcept {
  return impl_ == nullptr ? 0 : impl_->dropped.load(std::memory_order_relaxed);
}

std::uint64_t RawRecorder::digest() const noexcept {
  return impl_ == nullptr ? 0 : impl_->digest.load(std::memory_order_relaxed);
}

Status RawRecorder::last_error() const {
  return impl_ == nullptr ? Status::success() : impl_->error_status();
}

const std::string& RawRecorder::path() const noexcept {
  static const std::string empty;
  return impl_ == nullptr ? empty : impl_->path;
}

Result<Recording> read_recording(const std::string& path) {
  std::ifstream file(path, std::ios::binary);
  if (!file) {
    return fail(ErrorCode::IoError, "cannot open recording file: " + path);
  }
  std::array<std::byte, kFileHeaderSize> raw_header{};
  file.read(reinterpret_cast<char*>(raw_header.data()),
            static_cast<std::streamsize>(raw_header.size()));
  if (file.gcount() != static_cast<std::streamsize>(raw_header.size())) {
    return fail(ErrorCode::IoError, "recording header is truncated: " + path);
  }
  if (std::memcmp(raw_header.data(), kMagic, sizeof(kMagic)) != 0) {
    return fail(ErrorCode::Protocol, "bad recording magic: " + path);
  }
  const std::uint16_t version = load_u16(raw_header.data() + 8);
  const std::uint16_t header_size = load_u16(raw_header.data() + 10);
  if (version != kFormatVersion || header_size != kFileHeaderSize) {
    return fail(ErrorCode::Unsupported,
                "unsupported recording version/header size");
  }
  const std::span<const std::byte> header_prefix(raw_header.data(), 32);
  if (RawRecorder::crc32(header_prefix) != load_u32(raw_header.data() + 32)) {
    return fail(ErrorCode::Protocol, "recording header CRC mismatch");
  }

  Recording recording;
  recording.source_path = path;
  recording.header.version = version;
  recording.header.flags = load_u32(raw_header.data() + 12);
  recording.header.start_host_ns = load_u64(raw_header.data() + 16);

  std::uint64_t computed_digest = kFnvOffset;
  std::uint64_t computed_bytes = 0;
  bool saw_trailer = false;

  while (true) {
    std::array<std::byte, kRecordHeaderSize> raw_record{};
    file.read(reinterpret_cast<char*>(raw_record.data()),
              static_cast<std::streamsize>(raw_record.size()));
    const std::streamsize got = file.gcount();
    if (got == 0) {
      break;
    }
    if (got != static_cast<std::streamsize>(raw_record.size())) {
      return fail(ErrorCode::Protocol, "truncated record header");
    }
    if (load_u32(raw_record.data()) != kRecordMagic) {
      return fail(ErrorCode::Protocol, "bad record magic");
    }
    const std::uint32_t total = load_u32(raw_record.data() + 4);
    if (total < kRecordHeaderSize + 4 || total > 4U * 1024U * 1024U) {
      return fail(ErrorCode::Protocol, "invalid record length");
    }
    const std::uint32_t payload_len = load_u32(raw_record.data() + 32);
    if (payload_len != total - kRecordHeaderSize - 4) {
      return fail(ErrorCode::Protocol, "record payload length mismatch");
    }
    std::vector<std::byte> payload(payload_len);
    if (payload_len > 0) {
      file.read(reinterpret_cast<char*>(payload.data()),
                static_cast<std::streamsize>(payload_len));
      if (file.gcount() != static_cast<std::streamsize>(payload_len)) {
        return fail(ErrorCode::Protocol, "truncated record payload");
      }
    }
    std::array<std::byte, 4> raw_crc{};
    file.read(reinterpret_cast<char*>(raw_crc.data()),
              static_cast<std::streamsize>(raw_crc.size()));
    if (file.gcount() != static_cast<std::streamsize>(raw_crc.size())) {
      return fail(ErrorCode::Protocol, "truncated record CRC");
    }
    std::vector<std::byte> covered(raw_record.begin(), raw_record.end());
    covered.insert(covered.end(), payload.begin(), payload.end());
    const std::uint32_t expected = RawRecorder::crc32(covered);
    if (expected != load_u32(raw_crc.data())) {
      return fail(ErrorCode::Protocol, "record CRC mismatch");
    }
    const auto type = static_cast<RecordType>(
        std::to_integer<std::uint8_t>(raw_record[8]));
    const auto status = static_cast<TransferStatus>(
        std::to_integer<std::uint8_t>(raw_record[9]));
    const std::uint32_t sequence = load_u32(raw_record.data() + 12);
    const std::uint64_t host_time = load_u64(raw_record.data() + 16);
    const std::uint64_t device_timestamp = load_u64(raw_record.data() + 24);
    switch (type) {
      case RecordType::Metadata:
        recording.header.metadata.assign(
            reinterpret_cast<const char*>(payload.data()), payload.size());
        break;
      case RecordType::Transfer: {
        if (status > TransferStatus::Error) {
          return fail(ErrorCode::Protocol, "invalid transfer status in record");
        }
        RecordedTransfer transfer;
        transfer.sequence = sequence;
        transfer.host_time_ns = static_cast<std::int64_t>(host_time);
        transfer.device_timestamp = device_timestamp;
        transfer.status = status;
        transfer.bytes = std::move(payload);
        computed_digest =
            digest_update(computed_digest, transfer.bytes);
        computed_bytes += transfer.bytes.size();
        recording.transfers.push_back(std::move(transfer));
        break;
      }
      case RecordType::Trailer: {
        if (payload.size() != kTrailerPayloadSize || saw_trailer) {
          return fail(ErrorCode::Protocol, "invalid trailer record");
        }
        recording.trailer_digest = load_u64(payload.data());
        recording.trailer_transfers = load_u64(payload.data() + 8);
        recording.trailer_bytes = load_u64(payload.data() + 16);
        recording.has_trailer = true;
        saw_trailer = true;
        break;
      }
      default:
        return fail(ErrorCode::Protocol, "unknown record type");
    }
  }

  if (saw_trailer) {
    if (recording.trailer_digest != computed_digest ||
        recording.trailer_transfers != recording.transfers.size() ||
        recording.trailer_bytes != computed_bytes) {
      return fail(ErrorCode::Protocol,
                  "trailer does not match recorded content");
    }
  }
  return recording;
}

Status write_recording(const std::string& path, const Recording& recording) {
  RecorderConfig config;
  config.metadata = recording.header.metadata;
  config.start_host_ns = recording.header.start_host_ns;
  config.policy = BackpressurePolicy::Block;
  RawRecorder recorder(path, config);
  Status status = recorder.start();
  if (!status.ok()) {
    return status;
  }
  for (const auto& transfer : recording.transfers) {
    status = recorder.record(transfer);
    if (!status.ok()) {
      (void)recorder.stop();
      return status;
    }
  }
  return recorder.stop();
}

ReplaySource make_replay_source(const Recording& recording) {
  auto transfers = std::make_shared<std::vector<ReplayTransfer>>();
  transfers->reserve(recording.transfers.size());
  for (const auto& transfer : recording.transfers) {
    ReplayTransfer replay;
    replay.status = transfer.status;
    replay.bytes = transfer.bytes;
    transfers->push_back(std::move(replay));
  }
  return ReplaySource{std::move(transfers)};
}

}  // namespace einstar::recording
