// SPDX-License-Identifier: MIT
#include "einstar/status.hpp"

#include "einstar/transport.hpp"

namespace einstar {

std::string_view to_string(ErrorCode code) noexcept {
  switch (code) {
    case ErrorCode::Ok:
      return "ok";
    case ErrorCode::InvalidArgument:
      return "invalid_argument";
    case ErrorCode::NotOpen:
      return "not_open";
    case ErrorCode::AlreadyOpen:
      return "already_open";
    case ErrorCode::Timeout:
      return "timeout";
    case ErrorCode::Cancelled:
      return "cancelled";
    case ErrorCode::Stall:
      return "stall";
    case ErrorCode::Overflow:
      return "overflow";
    case ErrorCode::TransportError:
      return "transport_error";
    case ErrorCode::DescriptorQuirk:
      return "descriptor_quirk";
    case ErrorCode::Backpressure:
      return "backpressure";
    case ErrorCode::Protocol:
      return "protocol";
    case ErrorCode::IoError:
      return "io_error";
    case ErrorCode::Closed:
      return "closed";
    case ErrorCode::Exhausted:
      return "exhausted";
    case ErrorCode::Unsupported:
      return "unsupported";
    case ErrorCode::Internal:
      return "internal";
    case ErrorCode::NotFound:
      return "not_found";
  }
  return "unknown";
}

std::string_view to_string(TransferStatus status) noexcept {
  switch (status) {
    case TransferStatus::Ok:
      return "ok";
    case TransferStatus::Timeout:
      return "timeout";
    case TransferStatus::Cancelled:
      return "cancelled";
    case TransferStatus::Stall:
      return "stall";
    case TransferStatus::Overflow:
      return "overflow";
    case TransferStatus::Error:
      return "error";
  }
  return "unknown";
}

}  // namespace einstar
