// SPDX-License-Identifier: MIT
// USB configuration-descriptor parsing and the Einstar bInterval quirk.
//
// The Einstar (3267:0003) declares bInterval = 0 on its interrupt endpoints.
// bInterval 0 is invalid per USB 2.0 (valid range 1..16 for full speed; xHCI
// computes 1 << (bInterval - 1)), so Linux hosts must present 7 instead.
//
// This module is a pure byte-level codec: it parses raw configuration
// descriptors, detects the quirk, and produces a patched descriptor.  It is
// unit-tested offline and is used by LibUsbTransport to (a) validate the
// descriptor it received and (b) publish the corrected interval.  libusb-1.0
// has no public API to install a modified descriptor into the kernel, so the
// transport reports the patch and refuses to claim the interface unless the
// quirk is resolved; README documents the kernel/udev side of the workaround.
#pragma once

#include <cstddef>
#include <cstdint>
#include <span>
#include <string>
#include <vector>

#include "einstar/status.hpp"

namespace einstar {

struct EndpointInfo {
  std::uint8_t address = 0;          // bEndpointAddress
  std::uint8_t attributes = 0;       // bmAttributes
  std::uint16_t max_packet_size = 0; // wMaxPacketSize
  std::uint8_t interval = 0;         // bInterval
  std::size_t descriptor_offset = 0; // offset in the raw descriptor blob

  [[nodiscard]] bool is_interrupt() const noexcept {
    return (attributes & 0x03U) == 0x03U;
  }
  [[nodiscard]] bool is_bulk() const noexcept {
    return (attributes & 0x03U) == 0x02U;
  }
  [[nodiscard]] bool is_in() const noexcept { return (address & 0x80U) != 0; }
};

struct InterfaceInfo {
  std::uint8_t number = 0;
  std::uint8_t alternate_setting = 0;
  std::uint8_t class_code = 0;    // bInterfaceClass
  std::uint8_t subclass = 0;      // bInterfaceSubClass
  std::uint8_t protocol = 0;      // bInterfaceProtocol
  std::vector<EndpointInfo> endpoints;
};

struct ConfigDescriptorInfo {
  std::uint16_t total_length = 0;
  std::uint8_t configuration_value = 0;
  std::vector<InterfaceInfo> interfaces;
};

class UsbConfigDescriptor {
 public:
  UsbConfigDescriptor() = default;

  /// Parse a raw configuration descriptor (the bytes returned for
  /// GET_DESCRIPTOR(CONFIGURATION), including wTotalLength's worth of data).
  static Result<UsbConfigDescriptor> parse(std::span<const std::byte> raw);

  [[nodiscard]] const ConfigDescriptorInfo& info() const noexcept { return info_; }
  [[nodiscard]] const std::vector<std::byte>& raw() const noexcept { return raw_; }
  [[nodiscard]] bool valid() const noexcept { return valid_; }
  [[nodiscard]] bool was_patched() const noexcept { return patched_; }

  /// Set bInterval on one endpoint inside this descriptor copy.  Only interrupt
  /// endpoints may be patched; the new interval must be 1..16.  Idempotent.
  Result<bool> patch_interrupt_interval(std::uint8_t endpoint_address,
                                        std::uint8_t new_interval);

  /// Patch every interrupt endpoint whose bInterval is 0 to `new_interval`.
  /// Returns the number of endpoints changed.
  Result<std::size_t> patch_zero_intervals(std::uint8_t new_interval = 7);

  /// All interrupt endpoints that still declare bInterval == 0.
  [[nodiscard]] std::vector<std::uint8_t> zero_interval_interrupt_endpoints() const;

  /// Human readable quirk report (empty when no quirk is present).
  [[nodiscard]] std::vector<std::string> quirk_notes() const;

  /// Serialize the (possibly patched) descriptor.
  [[nodiscard]] std::vector<std::byte> serialize() const;

  [[nodiscard]] const EndpointInfo* find_endpoint(std::uint8_t address) const;
  [[nodiscard]] const InterfaceInfo* find_interface(std::uint8_t number,
                                                    std::uint8_t alt = 0) const;

  /// Build a synthetic descriptor for tests / simulator diagnostics.
  static UsbConfigDescriptor make_synthetic(std::uint8_t configuration_value,
                                            std::vector<InterfaceInfo> interfaces,
                                            bool zero_intervals = true);

 private:
  void reparse();

  std::vector<std::byte> raw_;
  ConfigDescriptorInfo info_;
  bool valid_ = false;
  bool patched_ = false;
};

}  // namespace einstar
