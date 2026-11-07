// SPDX-License-Identifier: MIT
// Transport abstraction: the only layer that touches USB (or a simulator).
#pragma once

#include <cstddef>
#include <cstdint>
#include <span>
#include <string_view>

#include "einstar/clock.hpp"
#include "einstar/status.hpp"

namespace einstar {

using TransferId = std::uint64_t;

enum class TransferStatus : std::uint8_t {
  Ok = 0,
  Timeout = 1,
  Cancelled = 2,
  Stall = 3,
  Overflow = 4,
  Error = 5,
};

std::string_view to_string(TransferStatus status) noexcept;

struct TransferCompletion {
  TransferId id = 0;
  TransferStatus status = TransferStatus::Ok;
  std::size_t bytes_transferred = 0;
  /// Clock-domain timestamp taken when the completion was observed.  For the
  /// simulated transport this is the virtual clock, never wall time.
  std::int64_t host_time_ns = 0;
  void* user = nullptr;
};

/// Receives completions.  Implementations must only enqueue the record; all
/// parsing/reassembly happens later on the pipeline thread.
class CompletionSink {
 public:
  CompletionSink() = default;
  CompletionSink(const CompletionSink&) = delete;
  CompletionSink& operator=(const CompletionSink&) = delete;
  virtual ~CompletionSink() = default;
  virtual void on_transfer_complete(const TransferCompletion& completion) noexcept = 0;
};

enum class TransportKind : std::uint8_t { LibUsb = 0, Simulated = 1 };

/// USB endpoints used by the Einstar protocol.
struct Endpoints {
  static constexpr std::uint8_t kCommandOut = 0x01;
  static constexpr std::uint8_t kCommandIn = 0x81;
  static constexpr std::uint8_t kBulkOut = 0x02;
  static constexpr std::uint8_t kBulkIn = 0x82;
  static constexpr std::uint8_t kStreamIn = 0x83;
};

class Transport {
 public:
  Transport() = default;
  Transport(const Transport&) = delete;
  Transport& operator=(const Transport&) = delete;
  virtual ~Transport() = default;

  /// Open the underlying device (or simulator).  Idempotent-safe: returns
  /// AlreadyOpen when called twice.
  virtual Status open() = 0;
  virtual void close() = 0;
  [[nodiscard]] virtual bool is_open() const noexcept = 0;

  /// Claim interface 0.  For libusb this also performs the bInterval=0 -> 7
  /// descriptor workaround before claiming; for the simulator it validates the
  /// scripted descriptor model.
  virtual Status claim_interface(std::uint8_t interface_number) = 0;
  virtual Status release_interface(std::uint8_t interface_number) = 0;

  /// Clear a halted endpoint.  Returns Ok when the endpoint was not halted.
  virtual Status clear_halt(std::uint8_t endpoint) = 0;

  /// Submit one bulk-IN read into `destination`.  The buffer must stay alive and
  /// unmodified until the matching completion is delivered.  Every accepted
  /// submission produces exactly one completion, including on cancel/close.
  virtual TransferId submit_bulk_in(std::uint8_t endpoint,
                                    std::span<std::byte> destination,
                                    CompletionSink& sink, void* user) = 0;

  /// Cancel an in-flight transfer.  Returns false if it already completed.  A
  /// cancelled transfer still produces a completion with status Cancelled.
  virtual bool cancel(TransferId id) = 0;

  /// Process completions, blocking at most `timeout_ms`.  A negative timeout
  /// means "until at least one completion or close".  Returns completions
  /// delivered.
  virtual std::size_t poll(int timeout_ms) = 0;

  [[nodiscard]] virtual std::size_t outstanding() const noexcept = 0;
  [[nodiscard]] virtual const Clock& clock() const noexcept = 0;
  [[nodiscard]] virtual TransportKind kind() const noexcept = 0;

  /// Human-readable backend identity, used in diagnostics and recordings.
  [[nodiscard]] virtual std::string_view name() const noexcept = 0;
};

}  // namespace einstar
