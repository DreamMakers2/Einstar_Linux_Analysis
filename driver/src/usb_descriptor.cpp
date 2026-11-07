// SPDX-License-Identifier: MIT
#include "einstar/usb_descriptor.hpp"

#include <algorithm>
#include <array>
#include <cstdio>
#include <cstring>
#include <string>

namespace einstar {
namespace {

constexpr std::uint8_t kDescriptorConfig = 0x02;
constexpr std::uint8_t kDescriptorInterface = 0x04;
constexpr std::uint8_t kDescriptorEndpoint = 0x05;
constexpr std::size_t kConfigHeaderSize = 9;
constexpr std::size_t kInterfaceHeaderSize = 9;
constexpr std::size_t kEndpointHeaderSize = 7;

std::uint16_t read_le16(const std::byte* p) {
  return static_cast<std::uint16_t>(std::to_integer<std::uint8_t>(p[0])) |
         static_cast<std::uint16_t>(
             static_cast<std::uint16_t>(std::to_integer<std::uint8_t>(p[1])) << 8);
}

void write_le16(std::byte* p, std::uint16_t value) {
  p[0] = static_cast<std::byte>(value & 0xFFU);
  p[1] = static_cast<std::byte>((value >> 8) & 0xFFU);
}

}  // namespace

Result<UsbConfigDescriptor> UsbConfigDescriptor::parse(
    std::span<const std::byte> raw) {
  if (raw.size() < kConfigHeaderSize) {
    return fail(ErrorCode::Protocol, "configuration descriptor is shorter than 9 bytes");
  }
  if (std::to_integer<std::uint8_t>(raw[0]) < kConfigHeaderSize) {
    return fail(ErrorCode::Protocol, "configuration descriptor bLength is invalid");
  }
  if (std::to_integer<std::uint8_t>(raw[1]) != kDescriptorConfig) {
    return fail(ErrorCode::Protocol, "descriptor type is not CONFIGURATION");
  }
  const std::uint16_t total = read_le16(raw.data() + 2);
  if (total < kConfigHeaderSize) {
    return fail(ErrorCode::Protocol, "configuration wTotalLength is invalid");
  }
  if (raw.size() < total) {
    return fail(ErrorCode::Protocol,
                "configuration descriptor is truncated (expected " +
                    std::to_string(total) + " bytes, got " +
                    std::to_string(raw.size()) + ")");
  }

  UsbConfigDescriptor descriptor;
  descriptor.raw_.assign(raw.begin(), raw.begin() + total);

  std::size_t offset = 0;
  int current_interface = -1;
  while (offset + 2 <= descriptor.raw_.size()) {
    const std::uint8_t length =
        std::to_integer<std::uint8_t>(descriptor.raw_[offset]);
    const std::uint8_t type =
        std::to_integer<std::uint8_t>(descriptor.raw_[offset + 1]);
    if (length < 2 || offset + length > descriptor.raw_.size()) {
      return fail(ErrorCode::Protocol, "malformed descriptor at offset " +
                                           std::to_string(offset));
    }
    if (type == kDescriptorInterface) {
      if (length < kInterfaceHeaderSize) {
        return fail(ErrorCode::Protocol, "short interface descriptor");
      }
      InterfaceInfo interface_info;
      interface_info.number =
          std::to_integer<std::uint8_t>(descriptor.raw_[offset + 2]);
      interface_info.alternate_setting =
          std::to_integer<std::uint8_t>(descriptor.raw_[offset + 3]);
      interface_info.endpoints.clear();
      // Interface class/subclass/protocol live at offsets 5/6/7.
      interface_info.class_code =
          std::to_integer<std::uint8_t>(descriptor.raw_[offset + 5]);
      interface_info.subclass =
          std::to_integer<std::uint8_t>(descriptor.raw_[offset + 6]);
      interface_info.protocol =
          std::to_integer<std::uint8_t>(descriptor.raw_[offset + 7]);
      descriptor.info_.interfaces.push_back(std::move(interface_info));
      current_interface =
          static_cast<int>(descriptor.info_.interfaces.size()) - 1;
    } else if (type == kDescriptorEndpoint) {
      if (length < kEndpointHeaderSize) {
        return fail(ErrorCode::Protocol, "short endpoint descriptor");
      }
      if (current_interface < 0) {
        return fail(ErrorCode::Protocol, "endpoint outside an interface");
      }
      EndpointInfo endpoint;
      endpoint.address =
          std::to_integer<std::uint8_t>(descriptor.raw_[offset + 2]);
      endpoint.attributes =
          std::to_integer<std::uint8_t>(descriptor.raw_[offset + 3]);
      endpoint.max_packet_size = read_le16(descriptor.raw_.data() + offset + 4);
      endpoint.interval =
          std::to_integer<std::uint8_t>(descriptor.raw_[offset + 6]);
      endpoint.descriptor_offset = offset;
      descriptor.info_.interfaces[static_cast<std::size_t>(current_interface)]
          .endpoints.push_back(endpoint);
    }
    offset += length;
  }

  descriptor.info_.total_length = total;
  descriptor.info_.configuration_value =
      std::to_integer<std::uint8_t>(descriptor.raw_[5]);
  descriptor.valid_ = true;
  return descriptor;
}

void UsbConfigDescriptor::reparse() {
  auto reparsed = parse(raw_);
  if (!reparsed.has_value()) {
    valid_ = false;
    return;
  }
  info_ = std::move(reparsed->info_);
  raw_ = std::move(reparsed->raw_);
  valid_ = true;
  // Keep the patched flag owned by this object; parse() returns a fresh one.
}

Result<bool> UsbConfigDescriptor::patch_interrupt_interval(
    std::uint8_t endpoint_address, std::uint8_t new_interval) {
  if (!valid_) {
    return fail(ErrorCode::InvalidArgument, "descriptor is not valid");
  }
  if (new_interval < 1 || new_interval > 16) {
    return fail(ErrorCode::InvalidArgument,
                "bInterval must be in 1..16, got " +
                    std::to_string(static_cast<unsigned>(new_interval)));
  }
  for (auto& interface_info : info_.interfaces) {
    for (auto& endpoint : interface_info.endpoints) {
      if (endpoint.address != endpoint_address) {
        continue;
      }
      if (!endpoint.is_interrupt()) {
        return fail(ErrorCode::InvalidArgument,
                    "endpoint is not an interrupt endpoint");
      }
      if (endpoint.interval == new_interval) {
        return false;  // already patched / no change
      }
      raw_[endpoint.descriptor_offset + 6] =
          static_cast<std::byte>(new_interval);
      endpoint.interval = new_interval;
      patched_ = true;
      return true;
    }
  }
  return fail(ErrorCode::InvalidArgument, "endpoint not present in descriptor");
}

Result<std::size_t> UsbConfigDescriptor::patch_zero_intervals(
    std::uint8_t new_interval) {
  if (!valid_) {
    return fail(ErrorCode::InvalidArgument, "descriptor is not valid");
  }
  std::size_t patched = 0;
  std::vector<std::uint8_t> addresses;
  for (const auto& interface_info : info_.interfaces) {
    for (const auto& endpoint : interface_info.endpoints) {
      if (endpoint.is_interrupt() && endpoint.interval == 0) {
        addresses.push_back(endpoint.address);
      }
    }
  }
  for (const std::uint8_t address : addresses) {
    auto result = patch_interrupt_interval(address, new_interval);
    if (!result.has_value()) {
      return fail(result.error());
    }
    if (*result) {
      patched++;
    }
  }
  return patched;
}

std::vector<std::uint8_t> UsbConfigDescriptor::zero_interval_interrupt_endpoints()
    const {
  std::vector<std::uint8_t> result;
  for (const auto& interface_info : info_.interfaces) {
    for (const auto& endpoint : interface_info.endpoints) {
      if (endpoint.is_interrupt() && endpoint.interval == 0) {
        result.push_back(endpoint.address);
      }
    }
  }
  return result;
}

std::vector<std::string> UsbConfigDescriptor::quirk_notes() const {
  std::vector<std::string> notes;
  for (const auto& interface_info : info_.interfaces) {
    for (const auto& endpoint : interface_info.endpoints) {
      if (endpoint.is_interrupt() && endpoint.interval == 0) {
        char buffer[160];
        std::snprintf(buffer, sizeof(buffer),
                      "interrupt endpoint 0x%02X declares bInterval=0 (invalid); "
                      "Linux requires 7",
                      static_cast<unsigned>(endpoint.address));
        notes.emplace_back(buffer);
      }
    }
  }
  return notes;
}

std::vector<std::byte> UsbConfigDescriptor::serialize() const { return raw_; }

const EndpointInfo* UsbConfigDescriptor::find_endpoint(
    std::uint8_t address) const {
  for (const auto& interface_info : info_.interfaces) {
    for (const auto& endpoint : interface_info.endpoints) {
      if (endpoint.address == address) {
        return &endpoint;
      }
    }
  }
  return nullptr;
}

const InterfaceInfo* UsbConfigDescriptor::find_interface(
    std::uint8_t number, std::uint8_t alt) const {
  for (const auto& interface_info : info_.interfaces) {
    if (interface_info.number == number &&
        interface_info.alternate_setting == alt) {
      return &interface_info;
    }
  }
  return nullptr;
}

UsbConfigDescriptor UsbConfigDescriptor::make_synthetic(
    std::uint8_t configuration_value, std::vector<InterfaceInfo> interfaces,
    bool zero_intervals) {
  std::size_t total = kConfigHeaderSize;
  for (auto& interface_info : interfaces) {
    total += kInterfaceHeaderSize;
    total += kEndpointHeaderSize * interface_info.endpoints.size();
  }
  std::vector<std::byte> raw(total, std::byte{0});
  raw[0] = static_cast<std::byte>(kConfigHeaderSize);
  raw[1] = static_cast<std::byte>(kDescriptorConfig);
  write_le16(raw.data() + 2, static_cast<std::uint16_t>(total));
  raw[4] = static_cast<std::byte>(interfaces.size() > 0 ? interfaces.size() : 0);
  raw[5] = static_cast<std::byte>(configuration_value);
  raw[6] = static_cast<std::byte>(0);  // bmAttributes
  raw[7] = static_cast<std::byte>(50);

  std::size_t offset = kConfigHeaderSize;
  for (const auto& interface_info : interfaces) {
    raw[offset] = static_cast<std::byte>(kInterfaceHeaderSize);
    raw[offset + 1] = static_cast<std::byte>(kDescriptorInterface);
    raw[offset + 2] = static_cast<std::byte>(interface_info.number);
    raw[offset + 3] = static_cast<std::byte>(interface_info.alternate_setting);
    raw[offset + 4] = static_cast<std::byte>(interface_info.endpoints.size());
    raw[offset + 5] = static_cast<std::byte>(interface_info.class_code);
    raw[offset + 6] = static_cast<std::byte>(interface_info.subclass);
    raw[offset + 7] = static_cast<std::byte>(interface_info.protocol);
    raw[offset + 8] = static_cast<std::byte>(0);
    offset += kInterfaceHeaderSize;
    for (const auto& endpoint : interface_info.endpoints) {
      raw[offset] = static_cast<std::byte>(kEndpointHeaderSize);
      raw[offset + 1] = static_cast<std::byte>(kDescriptorEndpoint);
      raw[offset + 2] = static_cast<std::byte>(endpoint.address);
      raw[offset + 3] = static_cast<std::byte>(endpoint.attributes);
      write_le16(raw.data() + offset + 4, endpoint.max_packet_size);
      raw[offset + 6] =
          static_cast<std::byte>(zero_intervals && endpoint.is_interrupt()
                                     ? 0
                                     : endpoint.interval);
      offset += kEndpointHeaderSize;
    }
  }

  auto parsed = parse(raw);
  if (!parsed.has_value()) {
    return UsbConfigDescriptor{};
  }
  return std::move(*parsed);
}

}  // namespace einstar
