// SPDX-License-Identifier: MIT
// Error / status types for the Einstar acquisition backend.
#pragma once

#include <cstdint>
#include <expected>
#include <string>
#include <string_view>
#include <utility>

namespace einstar {

enum class ErrorCode : std::int32_t {
  Ok = 0,
  InvalidArgument = 1,
  NotOpen = 2,
  AlreadyOpen = 3,
  Timeout = 4,
  Cancelled = 5,
  Stall = 6,
  Overflow = 7,
  TransportError = 8,
  DescriptorQuirk = 9,
  Backpressure = 10,
  Protocol = 11,
  IoError = 12,
  Closed = 13,
  Exhausted = 14,
  Unsupported = 15,
  Internal = 16,
  NotFound = 17,
};

std::string_view to_string(ErrorCode code) noexcept;

/// Lightweight error value.  `Status` is copyable and cheap in the success case.
class Status {
 public:
  Status() = default;
  Status(ErrorCode code, std::string message)
      : code_(code), message_(std::move(message)) {}

  static Status success() noexcept { return Status{}; }
  static Status error(ErrorCode code, std::string message) {
    return Status(code, std::move(message));
  }

  [[nodiscard]] bool ok() const noexcept { return code_ == ErrorCode::Ok; }
  explicit operator bool() const noexcept { return ok(); }
  [[nodiscard]] ErrorCode code() const noexcept { return code_; }
  [[nodiscard]] const std::string& message() const noexcept { return message_; }

  bool operator==(const Status& other) const noexcept {
    return code_ == other.code_ && message_ == other.message_;
  }

 private:
  ErrorCode code_ = ErrorCode::Ok;
  std::string message_;
};

template <class T>
using Result = std::expected<T, Status>;

inline std::unexpected<Status> fail(ErrorCode code, std::string message) {
  return std::unexpected(Status(code, std::move(message)));
}

inline std::unexpected<Status> fail(Status status) {
  return std::unexpected(std::move(status));
}

}  // namespace einstar
