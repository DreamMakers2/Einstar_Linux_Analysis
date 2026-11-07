// SPDX-License-Identifier: MIT
// Optional transport capability: synchronous command-pipe I/O.
//
// The image pipeline only needs asynchronous bulk-IN transfers, so Transport
// stays minimal.  The control channel additionally needs to write interrupt
// requests, read interrupt replies and (for read-only flash page reads) move
// one finite bulk request/reply pair.  A transport implements this interface
// when it can provide those calls; ControlChannel discovers it with a
// dynamic_cast and reports Unsupported otherwise.
#pragma once

#include <cstddef>
#include <cstdint>
#include <span>

#include "einstar/status.hpp"

namespace einstar {

class CommandIo {
 public:
  CommandIo() = default;
  CommandIo(const CommandIo&) = delete;
  CommandIo& operator=(const CommandIo&) = delete;
  virtual ~CommandIo() = default;

  /// Write one already-framed request to an interrupt OUT endpoint.
  virtual Status write_interrupt(std::uint8_t endpoint,
                                 std::span<const std::byte> data,
                                 std::uint32_t timeout_ms) = 0;
  /// Write a finite bulk request (page-read command header).
  virtual Status write_bulk(std::uint8_t endpoint,
                            std::span<const std::byte> data,
                            std::uint32_t timeout_ms) = 0;
  /// Read one interrupt reply; `transferred` receives the actual length.
  virtual Status read_interrupt(std::uint8_t endpoint,
                                std::span<std::byte> destination,
                                std::uint32_t timeout_ms,
                                std::size_t& transferred) = 0;
  /// Read a bulk reply, looping until `destination` is full or the device
  /// returns a short/zero-length packet.
  virtual Status read_bulk(std::uint8_t endpoint,
                           std::span<std::byte> destination,
                           std::uint32_t timeout_ms,
                           std::size_t& transferred) = 0;
};

}  // namespace einstar
