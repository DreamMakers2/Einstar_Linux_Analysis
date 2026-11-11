// SPDX-License-Identifier: MIT
// Control channel implementation.  See control_channel.hpp for the wire
// format and docs/protocol/libeinstar_protocol_spec.md for the provenance of
// every opcode.
#include "einstar/control_channel.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <string>
#include <string_view>
#include <thread>
#include <utility>

namespace einstar {
namespace {

// Request offsets (control_channel.hpp).
constexpr std::size_t kOffId = 0;
constexpr std::size_t kOffGroup = 2;
constexpr std::size_t kOffOpcode = 3;
constexpr std::size_t kOffArgument = 4;
// Reply offsets.
constexpr std::size_t kOffStatus = 4;
constexpr std::size_t kOffValueLength = 5;

constexpr std::size_t kPayloadOffset = kControlCommandHeaderSize;  // 8
constexpr std::size_t kValueOffset = kControlReplyHeaderSize;      // 9

void ctl_store_be16(std::byte* out, std::uint16_t value) noexcept {
  out[0] = static_cast<std::byte>((value >> 8U) & 0xFFU);
  out[1] = static_cast<std::byte>(value & 0xFFU);
}

void ctl_store_be32(std::byte* out, std::uint32_t value) noexcept {
  out[0] = static_cast<std::byte>((value >> 24U) & 0xFFU);
  out[1] = static_cast<std::byte>((value >> 16U) & 0xFFU);
  out[2] = static_cast<std::byte>((value >> 8U) & 0xFFU);
  out[3] = static_cast<std::byte>(value & 0xFFU);
}

std::uint16_t ctl_load_be16(const std::byte* in) noexcept {
  return static_cast<std::uint16_t>(
      (static_cast<std::uint16_t>(std::to_integer<std::uint8_t>(in[0])) << 8U) |
      static_cast<std::uint16_t>(std::to_integer<std::uint8_t>(in[1])));
}

std::uint32_t ctl_load_be32(const std::byte* in) noexcept {
  return (static_cast<std::uint32_t>(std::to_integer<std::uint8_t>(in[0])) << 24U) |
         (static_cast<std::uint32_t>(std::to_integer<std::uint8_t>(in[1])) << 16U) |
         (static_cast<std::uint32_t>(std::to_integer<std::uint8_t>(in[2])) << 8U) |
         static_cast<std::uint32_t>(std::to_integer<std::uint8_t>(in[3]));
}

std::string opcode_tag(ControlGroup group, std::uint8_t opcode) {
  static constexpr char kHexDigits[] = "0123456789ABCDEF";
  std::string tag = group == ControlGroup::System ? "system/0x" : "camera/0x";
  tag.push_back(kHexDigits[(opcode >> 4U) & 0x0FU]);
  tag.push_back(kHexDigits[opcode & 0x0FU]);
  return tag;
}

/// The serial reply carries 8 identity bytes followed by four trailer bytes
/// that the vendor ignores; render only the first 8, as EXStar does.
std::string hex_serial(const std::byte* data, std::size_t size) {
  static constexpr char kHexDigits[] = "0123456789ABCDEF";
  std::string text;
  text.reserve(size * 2U);
  for (std::size_t i = 0; i < size; ++i) {
    const auto byte = std::to_integer<std::uint8_t>(data[i]);
    text.push_back(kHexDigits[(byte >> 4U) & 0x0FU]);
    text.push_back(kHexDigits[byte & 0x0FU]);
  }
  return text;
}

std::string quoted_length(std::size_t value) {
  return std::to_string(value) + " bytes";
}

}  // namespace

// ---------------------------------------------------------------------------
// Firmware string
// ---------------------------------------------------------------------------

FirmwareVersion parse_firmware_version(std::string_view text) {
  // Format: "<model>_<sensor>_FX3_V<fx3>_FPGA_V<fpga>_<locale>", e.g.
  // "EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_EN".  Markers are matched on the
  // whole string because the version itself contains a dot, not an underscore;
  // splitting on '_' would separate "FX3" from "V2.10".
  FirmwareVersion version;
  version.raw.assign(text);

  constexpr std::string_view kFx3Marker = "FX3_V";
  constexpr std::string_view kFpgaMarker = "FPGA_V";
  const std::size_t fx3_pos = text.find(kFx3Marker);
  const std::size_t fpga_pos = text.find(kFpgaMarker);
  if (fx3_pos == std::string_view::npos || fpga_pos == std::string_view::npos ||
      fpga_pos <= fx3_pos) {
    return version;
  }

  const auto value_after = [&](std::size_t pos, std::string_view marker) {
    const std::size_t value_begin = pos + marker.size();
    const std::size_t value_end = text.find('_', value_begin);
    return text.substr(value_begin, (value_end == std::string_view::npos
                                         ? text.size()
                                         : value_end) -
                                        value_begin);
  };
  version.fx3_version.assign(value_after(fx3_pos, kFx3Marker));
  version.fpga_version.assign(value_after(fpga_pos, kFpgaMarker));
  if (version.fx3_version.empty() || version.fpga_version.empty()) {
    return version;
  }

  // Locale: everything after the FPGA version.
  const std::size_t fpga_value_end =
      fpga_pos + kFpgaMarker.size() + version.fpga_version.size();
  if (fpga_value_end < text.size() && text[fpga_value_end] == '_') {
    version.locale.assign(text.substr(fpga_value_end + 1U));
  }

  // Prefix before the FX3 marker is "<model...>_<sensor>_".
  std::string_view prefix = text.substr(0, fx3_pos);
  if (!prefix.empty() && prefix.back() == '_') {
    prefix.remove_suffix(1);
  }
  const std::size_t last_separator = prefix.rfind('_');
  if (last_separator == std::string_view::npos) {
    version.model.assign(prefix);
  } else {
    version.model.assign(prefix.substr(0, last_separator));
    version.sensor.assign(prefix.substr(last_separator + 1U));
  }
  return version;
}

// ---------------------------------------------------------------------------
// Construction / lifecycle
// ---------------------------------------------------------------------------

ControlChannel::ControlChannel(Transport& transport, ControlConfig config)
    : transport_(transport), config_(config) {}

ControlChannel::~ControlChannel() { close(); }

Result<DeviceIdentity> ControlChannel::open() {
  std::lock_guard<std::mutex> lock(mutex_);
  if (open_) {
    return fail(ErrorCode::AlreadyOpen, "control channel is already open");
  }
  if (config_.attempts == 0) {
    return fail(ErrorCode::InvalidArgument,
                "ControlConfig::attempts must be at least 1");
  }
  if (config_.timeout_ms == 0 || config_.handshake_timeout_ms == 0 ||
      config_.page_timeout_ms == 0 || config_.drain_timeout_ms == 0) {
    return fail(ErrorCode::InvalidArgument,
                "ControlConfig deadlines must all be non-zero");
  }
  if (config_.interface_number != 0) {
    return fail(ErrorCode::InvalidArgument,
                "the Einstar exposes only interface 0");
  }
  if (!transport_.is_open()) {
    return fail(ErrorCode::NotOpen,
                "transport is not open; open it before the control channel");
  }

  CommandIo* io = dynamic_cast<CommandIo*>(&transport_);
  if (io == nullptr) {
    return fail(ErrorCode::Unsupported,
                "transport does not implement CommandIo: there is no outbound "
                "command path");
  }
  io_ = io;
  if (config_.claim_interface) {
    Status claimed = transport_.claim_interface(config_.interface_number);
    if (!claimed.ok()) {
      io_ = nullptr;
      return fail(std::move(claimed));
    }
    claimed_ = true;
  }

  next_id_ = 0;
  Status status = handshake_locked();
  if (!status.ok()) {
    teardown_locked();
    return fail(std::move(status));
  }
  open_ = true;  // transact_locked() is live from here on.

  DeviceIdentity identity;
  status = identify_locked(identity);
  if (!status.ok()) {
    teardown_locked();
    return fail(std::move(status));
  }
  identity_ = std::move(identity);
  return identity_;
}

void ControlChannel::close() noexcept {
  std::lock_guard<std::mutex> lock(mutex_);
  teardown_locked();
}

bool ControlChannel::is_open() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return open_;
}

DeviceIdentity ControlChannel::identity() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return identity_;
}

ControlStats ControlChannel::stats() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return stats_;
}

void ControlChannel::reset_stats() {
  std::lock_guard<std::mutex> lock(mutex_);
  stats_ = ControlStats{};
}

void ControlChannel::teardown_locked() noexcept {
  if (claimed_) {
    const Status released = transport_.release_interface(config_.interface_number);
    if (!released.ok()) {
      ++stats_.release_errors;
    }
    claimed_ = false;
  }
  open_ = false;
  io_ = nullptr;
  identity_ = DeviceIdentity{};
  next_id_ = 0;
}

// ---------------------------------------------------------------------------
// Open sequence
// ---------------------------------------------------------------------------

Status ControlChannel::handshake_locked() {
  // Drain stale replies exactly like CCommunicateManager::attach: a handful of
  // short reads on each inbound pipe, stopping at the first timeout.
  std::size_t got = 0;
  for (std::uint32_t i = 0; i < config_.drain_attempts; ++i) {
    const Status status = io_->read_interrupt(
        Endpoints::kCommandIn,
        std::span<std::byte>(reply_.data(), kControlReplyCapacity),
        config_.drain_timeout_ms, got);
    if (!status.ok()) {
      break;
    }
  }
  for (std::uint32_t i = 0; i < config_.bulk_drain_attempts; ++i) {
    const Status status = io_->read_bulk(
        Endpoints::kBulkIn,
        std::span<std::byte>(reply_.data(), kControlPageTransferSize),
        config_.drain_timeout_ms, got);
    if (!status.ok()) {
      break;
    }
  }
  // Clear both inbound pipes before the probe (libeinstar: 0x81 then 0x82).
  (void)transport_.clear_halt(Endpoints::kCommandIn);
  (void)transport_.clear_halt(Endpoints::kBulkIn);
  stats_.pipe_resets += 2;

  std::fill_n(request_.begin(), kControlHandshakeSize, std::byte{0});
  request_[0] = static_cast<std::byte>(kControlHandshakeByte);
  Status status = io_->write_interrupt(
      Endpoints::kCommandOut,
      std::span<const std::byte>(request_.data(), kControlHandshakeSize),
      config_.handshake_timeout_ms);
  if (!status.ok()) {
    return status;
  }
  got = 0;
  status = io_->read_interrupt(
      Endpoints::kCommandIn,
      std::span<std::byte>(reply_.data(), kControlReplyCapacity),
      config_.handshake_timeout_ms, got);
  if (!status.ok()) {
    return status;
  }
  constexpr std::string_view kMarker = kControlHandshakeMarker;
  if (got < kValueOffset + kMarker.size()) {
    return Status::error(ErrorCode::Protocol,
                         "handshake reply shorter than 17 bytes");
  }
  for (std::size_t i = 0; i < kMarker.size(); ++i) {
    if (std::to_integer<char>(reply_[kValueOffset + i]) != kMarker[i]) {
      return Status::error(
          ErrorCode::Protocol,
          "handshake reply lacks the \"Shining3\" marker");
    }
  }
  return Status::success();
}

Status ControlChannel::identify_locked(DeviceIdentity& identity) {
  auto serial_text = serial_locked();
  if (!serial_text) {
    return serial_text.error();
  }
  auto firmware = firmware_locked();
  if (!firmware) {
    return firmware.error();
  }
  if (firmware->raw.empty()) {
    return Status::error(ErrorCode::Protocol,
                         "firmware version reply carried no text");
  }
  auto sensors = sensor_count_locked();
  if (!sensors) {
    return sensors.error();
  }
  identity.serial = std::move(*serial_text);
  identity.firmware = std::move(*firmware);
  identity.sensor_count = *sensors;
  return Status::success();
}

// ---------------------------------------------------------------------------
// Request / reply engine
// ---------------------------------------------------------------------------

std::uint16_t ControlChannel::wire_size(ControlGroup group,
                                        std::uint8_t opcode) noexcept {
  if (group == ControlGroup::System) {
    switch (static_cast<SystemOpcode>(opcode)) {
      case SystemOpcode::ProductName:
        return 100;
      case SystemOpcode::Serial:
      case SystemOpcode::FirmwareVersion:
        return 64;
    }
    return 16;
  }
  switch (static_cast<CameraOpcode>(opcode)) {
    case CameraOpcode::GetTriggerSwitch:
    case CameraOpcode::SetTriggerSwitch:
      return 10;
    case CameraOpcode::GetTriggerPeriod:
    case CameraOpcode::SetTriggerPeriod:
    case CameraOpcode::GetLaserBrightness:
    case CameraOpcode::SetLaserBrightness:
    case CameraOpcode::GetStrobeBrightness:
    case CameraOpcode::SetStrobeBrightness:
      return 50;
    default:
      return 16;
  }
}

std::uint8_t ControlChannel::next_packet_id() noexcept {
  const std::uint8_t id = next_id_;
  next_id_ = static_cast<std::uint8_t>((next_id_ + 1U) % kControlPacketIdModulus);
  return id;
}

void ControlChannel::reset_command_pipes() noexcept {
  (void)transport_.clear_halt(Endpoints::kCommandOut);
  (void)transport_.clear_halt(Endpoints::kCommandIn);
  stats_.pipe_resets += 2;
}

void ControlChannel::reset_bulk_pipes() noexcept {
  // libeinstar resets only 0x82; clearing 0x02 as well also recovers a stalled
  // page request after a failed write.
  (void)transport_.clear_halt(Endpoints::kBulkOut);
  (void)transport_.clear_halt(Endpoints::kBulkIn);
  stats_.pipe_resets += 2;
}

void ControlChannel::sleep_before_retry() const noexcept {
  if (config_.retry_delay_ms == 0) {
    return;
  }
  std::this_thread::sleep_for(
      std::chrono::milliseconds(config_.retry_delay_ms));
}

Result<ControlChannel::ReplyView> ControlChannel::transact_locked(
    ControlGroup group, std::uint8_t opcode, std::span<const std::byte> payload,
    std::uint32_t timeout_ms) {
  if (!open_ || io_ == nullptr) {
    return fail(ErrorCode::NotOpen, "control channel is not open");
  }
  if (!transport_.is_open()) {
    return fail(ErrorCode::NotOpen, "transport is not open");
  }
  if (payload.size() > kControlMaxPayload) {
    return fail(ErrorCode::InvalidArgument,
                "command payload does not fit the 8-byte wire slot");
  }
  const std::size_t wire = wire_size(group, opcode);
  if (kPayloadOffset + payload.size() > wire) {
    return fail(ErrorCode::InvalidArgument,
                "payload exceeds the opcode wire size");
  }

  Status last = Status::error(ErrorCode::Timeout, "command was never attempted");
  for (std::uint32_t attempt = 0; attempt < config_.attempts; ++attempt) {
    if (attempt > 0) {
      ++stats_.retries;
      sleep_before_retry();
    }

    std::fill_n(request_.begin(), wire, std::byte{0});
    request_[kOffId] = static_cast<std::byte>(next_packet_id());
    request_[1] = std::byte{0x00};  // vendor constant, not validated
    request_[kOffGroup] = static_cast<std::byte>(
        static_cast<std::uint8_t>(group));
    request_[kOffOpcode] = static_cast<std::byte>(opcode);
    ctl_store_be32(request_.data() + kOffArgument,
                   static_cast<std::uint32_t>(payload.size()));
    std::copy(payload.begin(), payload.end(),
              request_.begin() + static_cast<std::ptrdiff_t>(kPayloadOffset));

    ++stats_.attempts;
    Status status = io_->write_interrupt(
        Endpoints::kCommandOut,
        std::span<const std::byte>(request_.data(), wire), timeout_ms);
    if (!status.ok()) {
      if (status.code() == ErrorCode::Timeout) {
        ++stats_.timeouts;
      }
      last = std::move(status);
      reset_command_pipes();
      continue;
    }

    std::size_t got = 0;
    status = io_->read_interrupt(
        Endpoints::kCommandIn,
        std::span<std::byte>(reply_.data(), kControlReplyCapacity), timeout_ms,
        got);
    if (!status.ok()) {
      if (status.code() == ErrorCode::Timeout) {
        ++stats_.timeouts;
      }
      last = std::move(status);
      reset_command_pipes();
      continue;
    }
    if (got < kValueOffset) {
      last = Status::error(ErrorCode::Protocol,
                           "reply shorter than the 9-byte header");
      reset_command_pipes();
      continue;
    }

    // Echo validation: id, group and opcode must come back unchanged.
    if (reply_[kOffId] != request_[kOffId] ||
        reply_[kOffGroup] != request_[kOffGroup] ||
        reply_[kOffOpcode] != request_[kOffOpcode]) {
      ++stats_.echo_mismatches;
      last = Status::error(ErrorCode::Protocol,
                           "reply does not echo the request id/group/opcode");
      reset_command_pipes();
      continue;
    }

    // A non-zero status is a device decision, not a transport fault: retrying
    // would only repeat the same error, so propagate it immediately.
    if (reply_[kOffStatus] != std::byte{0}) {
      const auto code = std::to_integer<unsigned>(reply_[kOffStatus]);
      ++stats_.device_status_errors;
      return fail(ErrorCode::Protocol,
                  "device status " + std::to_string(code) + " for " +
                      opcode_tag(group, opcode));
    }

    const std::uint32_t declared =
        ctl_load_be32(reply_.data() + kOffValueLength);
    std::size_t value_size = declared;
    const std::size_t available = got - kValueOffset;
    if (value_size > available) {
      value_size = available;
      ++stats_.truncated_values;
    }
    ++stats_.commands;
    return ReplyView{reply_.data() + kValueOffset, value_size};
  }
  return fail(std::move(last));
}

// ---------------------------------------------------------------------------
// Typed command helpers
// ---------------------------------------------------------------------------

Result<std::string> ControlChannel::string_locked(ControlGroup group,
                                                  std::uint8_t opcode,
                                                  const char* what) {
  auto reply = transact_locked(group, opcode, {}, config_.timeout_ms);
  if (!reply) {
    return fail(reply.error());
  }
  std::string text;
  text.reserve(reply->size);
  for (std::size_t i = 0; i < reply->size; ++i) {
    const char c = std::to_integer<char>(reply->data[i]);
    if (c == '\0') {
      break;
    }
    text.push_back(c);
  }
  if (text.empty()) {
    return fail(ErrorCode::Protocol,
                std::string(what) + ": empty string value");
  }
  return text;
}

Result<std::uint32_t> ControlChannel::uint_locked(
    ControlGroup group, std::uint8_t opcode, std::span<const std::byte> payload,
    std::size_t max_bytes, const char* what) {
  auto reply = transact_locked(group, opcode, payload, config_.timeout_ms);
  if (!reply) {
    return fail(reply.error());
  }
  if (reply->size == 0 || reply->size > max_bytes) {
    return fail(ErrorCode::Protocol,
                std::string(what) + ": reply value has " +
                    quoted_length(reply->size));
  }
  std::uint32_t value = 0;
  for (std::size_t i = 0; i < reply->size; ++i) {
    value = (value << 8U) | std::to_integer<std::uint8_t>(reply->data[i]);
  }
  return value;
}

Status ControlChannel::set_locked(ControlGroup group, std::uint8_t opcode,
                                  std::span<const std::byte> payload) {
  auto reply = transact_locked(group, opcode, payload, config_.timeout_ms);
  if (!reply) {
    return reply.error();
  }
  // Setters answer with an empty value; a stray value is tolerated because the
  // vendor ignores it too.
  return Status::success();
}

Result<std::string> ControlChannel::serial_locked() {
  auto reply = transact_locked(ControlGroup::System,
                               static_cast<std::uint8_t>(SystemOpcode::Serial),
                               {}, config_.timeout_ms);
  if (!reply) {
    return fail(reply.error());
  }
  if (reply->size < 8) {
    return fail(ErrorCode::Protocol,
                "serial reply carries fewer than 8 identity bytes");
  }
  return hex_serial(reply->data, 8);
}

Result<FirmwareVersion> ControlChannel::firmware_locked() {
  auto reply = transact_locked(
      ControlGroup::System,
      static_cast<std::uint8_t>(SystemOpcode::FirmwareVersion), {},
      config_.timeout_ms);
  if (!reply) {
    return fail(reply.error());
  }
  std::string text;
  text.reserve(reply->size);
  for (std::size_t i = 0; i < reply->size; ++i) {
    const char c = std::to_integer<char>(reply->data[i]);
    if (c == '\0') {
      break;
    }
    text.push_back(c);
  }
  if (text.empty()) {
    return fail(ErrorCode::Protocol, "firmware version reply carried no text");
  }
  return parse_firmware_version(text);
}

Result<std::uint16_t> ControlChannel::sensor_count_locked() {
  auto value = uint_locked(ControlGroup::Camera,
                           static_cast<std::uint8_t>(CameraOpcode::SensorCount),
                           {}, 2, "sensor count");
  if (!value) {
    return fail(value.error());
  }
  return static_cast<std::uint16_t>(*value);
}

Result<std::uint8_t> ControlChannel::laser_brightness() {
  std::lock_guard<std::mutex> lock(mutex_);
  auto value = uint_locked(ControlGroup::Camera,
                           static_cast<std::uint8_t>(
                               CameraOpcode::GetLaserBrightness),
                           {}, 1, "laser brightness");
  if (!value) {
    return fail(value.error());
  }
  return static_cast<std::uint8_t>(std::min<std::uint32_t>(*value, 0xFFU));
}

Status ControlChannel::set_laser_brightness(unsigned percent) {
  if (percent > 100U) {
    return Status::error(ErrorCode::InvalidArgument,
                         "laser brightness must be 0..100 percent");
  }
  std::lock_guard<std::mutex> lock(mutex_);
  const std::array<std::byte, 1> payload{
      static_cast<std::byte>(percent & 0xFFU)};
  return set_locked(ControlGroup::Camera,
                    static_cast<std::uint8_t>(CameraOpcode::SetLaserBrightness),
                    payload);
}

Result<std::uint16_t> ControlChannel::strobe_brightness(unsigned route) {
  if (route > 1U) {
    return fail(ErrorCode::InvalidArgument, "strobe route must be 0 or 1");
  }
  std::lock_guard<std::mutex> lock(mutex_);
  const std::array<std::byte, 1> payload{static_cast<std::byte>(route)};
  auto value = uint_locked(
      ControlGroup::Camera,
      static_cast<std::uint8_t>(CameraOpcode::GetStrobeBrightness), payload, 2,
      "strobe brightness");
  if (!value) {
    return fail(value.error());
  }
  return static_cast<std::uint16_t>(std::min<std::uint32_t>(*value, 0xFFFFU));
}

Status ControlChannel::set_strobe_brightness(unsigned route,
                                             std::uint16_t value) {
  if (route > 1U) {
    return Status::error(ErrorCode::InvalidArgument,
                         "strobe route must be 0 or 1");
  }
  std::lock_guard<std::mutex> lock(mutex_);
  std::array<std::byte, 3> payload{};
  payload[0] = static_cast<std::byte>(route);
  ctl_store_be16(payload.data() + 1, value);
  return set_locked(ControlGroup::Camera,
                    static_cast<std::uint8_t>(CameraOpcode::SetStrobeBrightness),
                    payload);
}

Status ControlChannel::set_trigger_period_locked(std::uint32_t microseconds) {
  std::array<std::byte, 4> payload{};
  ctl_store_be32(payload.data(), microseconds);
  return set_locked(ControlGroup::Camera,
                    static_cast<std::uint8_t>(CameraOpcode::SetTriggerPeriod),
                    payload);
}

Status ControlChannel::set_trigger_counts_locked(unsigned mono, unsigned rgb) {
  std::array<std::byte, 1> payload{
      static_cast<std::byte>(pack_trigger_counts(mono, rgb))};
  return set_locked(ControlGroup::Camera,
                    static_cast<std::uint8_t>(CameraOpcode::SetTriggerSwitch),
                    payload);
}

// ---------------------------------------------------------------------------
// Public commands
// ---------------------------------------------------------------------------

Result<std::string> ControlChannel::product_name() {
  std::lock_guard<std::mutex> lock(mutex_);
  return string_locked(ControlGroup::System,
                       static_cast<std::uint8_t>(SystemOpcode::ProductName),
                       "product name");
}

Result<std::string> ControlChannel::serial() {
  std::lock_guard<std::mutex> lock(mutex_);
  return serial_locked();
}

Result<FirmwareVersion> ControlChannel::firmware_version() {
  std::lock_guard<std::mutex> lock(mutex_);
  return firmware_locked();
}

Result<std::uint16_t> ControlChannel::sensor_count() {
  std::lock_guard<std::mutex> lock(mutex_);
  return sensor_count_locked();
}

Result<std::uint16_t> ControlChannel::max_width(unsigned camera) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (!is_valid_camera(camera)) {
    return fail(ErrorCode::InvalidArgument, "camera index must be 0, 1 or 2");
  }
  const std::array<std::byte, 1> payload{
      static_cast<std::byte>(camera_mask(camera))};
  auto value = uint_locked(ControlGroup::Camera,
                           static_cast<std::uint8_t>(CameraOpcode::MaxWidth),
                           payload, 2, "max width");
  if (!value) {
    return fail(value.error());
  }
  return static_cast<std::uint16_t>(*value);
}

Result<std::uint16_t> ControlChannel::max_height(unsigned camera) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (!is_valid_camera(camera)) {
    return fail(ErrorCode::InvalidArgument, "camera index must be 0, 1 or 2");
  }
  const std::array<std::byte, 1> payload{
      static_cast<std::byte>(camera_mask(camera))};
  auto value = uint_locked(ControlGroup::Camera,
                           static_cast<std::uint8_t>(CameraOpcode::MaxHeight),
                           payload, 2, "max height");
  if (!value) {
    return fail(value.error());
  }
  return static_cast<std::uint16_t>(*value);
}

Result<std::uint32_t> ControlChannel::exposure_us(unsigned camera) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (!is_valid_camera(camera)) {
    return fail(ErrorCode::InvalidArgument, "camera index must be 0, 1 or 2");
  }
  const std::array<std::byte, 1> payload{
      static_cast<std::byte>(camera_mask(camera))};
  return uint_locked(ControlGroup::Camera,
                     static_cast<std::uint8_t>(CameraOpcode::GetExposure),
                     payload, 4, "exposure");
}

Status ControlChannel::set_exposure_us(unsigned camera,
                                       std::uint32_t microseconds) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (!is_valid_camera(camera)) {
    return Status::error(ErrorCode::InvalidArgument,
                         "camera index must be 0, 1 or 2");
  }
  std::array<std::byte, 5> payload{};
  payload[0] = static_cast<std::byte>(camera_mask(camera));
  ctl_store_be32(payload.data() + 1, microseconds);
  return set_locked(ControlGroup::Camera,
                    static_cast<std::uint8_t>(CameraOpcode::SetExposure),
                    payload);
}

Result<ExposureRange> ControlChannel::exposure_range(unsigned camera) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (!is_valid_camera(camera)) {
    return fail(ErrorCode::InvalidArgument, "camera index must be 0, 1 or 2");
  }
  const std::array<std::byte, 1> payload{
      static_cast<std::byte>(camera_mask(camera))};
  auto minimum = uint_locked(ControlGroup::Camera,
                             static_cast<std::uint8_t>(CameraOpcode::MinExposure),
                             payload, 4, "minimum exposure");
  if (!minimum) {
    return fail(minimum.error());
  }
  auto maximum = uint_locked(ControlGroup::Camera,
                             static_cast<std::uint8_t>(CameraOpcode::MaxExposure),
                             payload, 4, "maximum exposure");
  if (!maximum) {
    return fail(maximum.error());
  }
  return ExposureRange{*minimum, *maximum};
}

Result<std::uint16_t> ControlChannel::gain(unsigned camera) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (!is_valid_camera(camera)) {
    return fail(ErrorCode::InvalidArgument, "camera index must be 0, 1 or 2");
  }
  const std::array<std::byte, 1> payload{
      static_cast<std::byte>(camera_mask(camera))};
  auto value = uint_locked(ControlGroup::Camera,
                           static_cast<std::uint8_t>(CameraOpcode::GetGain),
                           payload, 2, "gain");
  if (!value) {
    return fail(value.error());
  }
  return static_cast<std::uint16_t>(*value);
}

Status ControlChannel::set_gain(unsigned camera, std::uint16_t gain_value) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (!is_valid_camera(camera)) {
    return Status::error(ErrorCode::InvalidArgument,
                         "camera index must be 0, 1 or 2");
  }
  std::array<std::byte, 3> payload{};
  payload[0] = static_cast<std::byte>(camera_mask(camera));
  ctl_store_be16(payload.data() + 1, gain_value);
  return set_locked(ControlGroup::Camera,
                    static_cast<std::uint8_t>(CameraOpcode::SetGain), payload);
}

Result<GainRange> ControlChannel::gain_range(unsigned camera) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (!is_valid_camera(camera)) {
    return fail(ErrorCode::InvalidArgument, "camera index must be 0, 1 or 2");
  }
  const std::array<std::byte, 1> payload{
      static_cast<std::byte>(camera_mask(camera))};
  auto minimum = uint_locked(ControlGroup::Camera,
                             static_cast<std::uint8_t>(CameraOpcode::MinGain),
                             payload, 2, "minimum gain");
  if (!minimum) {
    return fail(minimum.error());
  }
  auto maximum = uint_locked(ControlGroup::Camera,
                             static_cast<std::uint8_t>(CameraOpcode::MaxGain),
                             payload, 2, "maximum gain");
  if (!maximum) {
    return fail(maximum.error());
  }
  return GainRange{static_cast<std::uint16_t>(*minimum),
                   static_cast<std::uint16_t>(*maximum)};
}

Result<TriggerCounts> ControlChannel::trigger_counts() {
  std::lock_guard<std::mutex> lock(mutex_);
  auto value = uint_locked(
      ControlGroup::Camera,
      static_cast<std::uint8_t>(CameraOpcode::GetTriggerSwitch), {}, 1,
      "trigger switch");
  if (!value) {
    return fail(value.error());
  }
  return unpack_trigger_counts(static_cast<std::uint8_t>(*value));
}

Status ControlChannel::set_trigger_counts(unsigned mono, unsigned rgb) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (mono > 15U || rgb > 15U) {
    return Status::error(ErrorCode::InvalidArgument,
                         "trigger counts must fit in a 4-bit nibble");
  }
  return set_trigger_counts_locked(mono, rgb);
}

Result<std::uint32_t> ControlChannel::trigger_period_us() {
  std::lock_guard<std::mutex> lock(mutex_);
  return uint_locked(
      ControlGroup::Camera,
      static_cast<std::uint8_t>(CameraOpcode::GetTriggerPeriod), {}, 4,
      "trigger period");
}

Status ControlChannel::set_trigger_period_us(std::uint32_t microseconds) {
  std::lock_guard<std::mutex> lock(mutex_);
  return set_trigger_period_locked(microseconds);
}

Result<std::int16_t> ControlChannel::temperature_raw() {
  std::lock_guard<std::mutex> lock(mutex_);
  auto reply = transact_locked(ControlGroup::Camera,
                               static_cast<std::uint8_t>(
                                   CameraOpcode::Temperature),
                               {}, config_.timeout_ms);
  if (!reply) {
    return fail(reply.error());
  }
  if (reply->size != 2) {
    return fail(ErrorCode::Protocol,
                "temperature reply is not a 16-bit value");
  }
  const auto raw = static_cast<std::int16_t>(ctl_load_be16(reply->data));
  // Arithmetic shift: C++20 guarantees it for signed types.  The vendor stores
  // the shifted integer; the 0.5 degC unit is inferred, not verified.
  return static_cast<std::int16_t>(raw >> 7);
}

Result<double> ControlChannel::temperature_celsius() {
  auto raw = temperature_raw();
  if (!raw) {
    return fail(raw.error());
  }
  return static_cast<double>(*raw) * 0.5;
}

Status ControlChannel::read_page(std::uint16_t index,
                                 std::span<std::byte> destination) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (!open_ || io_ == nullptr) {
    return Status::error(ErrorCode::NotOpen, "control channel is not open");
  }
  if (!transport_.is_open()) {
    return Status::error(ErrorCode::NotOpen, "transport is not open");
  }
  if (destination.size() < kControlPageSize) {
    return Status::error(ErrorCode::InvalidArgument,
                         "page destination must hold at least 4096 bytes");
  }

  Status last = Status::error(ErrorCode::Timeout,
                              "page read was never attempted");
  for (std::uint32_t attempt = 0; attempt < config_.attempts; ++attempt) {
    if (attempt > 0) {
      ++stats_.retries;
      sleep_before_retry();
    }
    std::fill(request_.begin(), request_.end(), std::byte{0});
    request_[kOffId] = static_cast<std::byte>(next_packet_id());
    request_[1] = std::byte{0x00};
    request_[kOffGroup] = static_cast<std::byte>(
        static_cast<std::uint8_t>(ControlGroup::Camera));
    request_[kOffOpcode] = static_cast<std::byte>(
        static_cast<std::uint8_t>(CameraOpcode::PageRead));
    ctl_store_be32(request_.data() + kOffArgument, 2U);
    ctl_store_be16(request_.data() + kPayloadOffset, index);

    ++stats_.attempts;
    Status status = io_->write_bulk(
        Endpoints::kBulkOut,
        std::span<const std::byte>(request_.data(), request_.size()),
        config_.page_timeout_ms);
    if (!status.ok()) {
      if (status.code() == ErrorCode::Timeout) {
        ++stats_.timeouts;
      }
      last = std::move(status);
      reset_bulk_pipes();
      continue;
    }

    std::size_t got = 0;
    status = io_->read_bulk(
        Endpoints::kBulkIn,
        std::span<std::byte>(reply_.data(), reply_.size()),
        config_.page_timeout_ms, got);
    if (!status.ok()) {
      if (status.code() == ErrorCode::Timeout) {
        ++stats_.timeouts;
      }
      last = std::move(status);
      reset_bulk_pipes();
      continue;
    }
    if (got < kValueOffset + kControlPageSize) {
      last = Status::error(ErrorCode::Protocol,
                           "page reply shorter than 9 + 4096 bytes");
      reset_bulk_pipes();
      continue;
    }
    if (reply_[kOffId] != request_[kOffId] ||
        reply_[kOffGroup] != request_[kOffGroup] ||
        reply_[kOffOpcode] != request_[kOffOpcode]) {
      ++stats_.echo_mismatches;
      last = Status::error(ErrorCode::Protocol,
                           "page reply does not echo the request");
      reset_bulk_pipes();
      continue;
    }
    if (reply_[kOffStatus] != std::byte{0}) {
      const auto code = std::to_integer<unsigned>(reply_[kOffStatus]);
      ++stats_.device_status_errors;
      return Status::error(ErrorCode::Protocol,
                           "device status " + std::to_string(code) +
                               " for camera/0x57");
    }
    std::copy_n(reply_.data() + kValueOffset, kControlPageSize,
                destination.data());
    ++stats_.commands;
    ++stats_.page_reads;
    return Status::success();
  }
  return last;
}

Status ControlChannel::start_streaming(std::uint32_t trigger_period_us) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (trigger_period_us == 0) {
    return Status::error(ErrorCode::InvalidArgument,
                         "trigger period must be non-zero");
  }
  // The vendor/library sequence: force a slow period, enable 1 IR + 1 RGB
  // frame per trigger, then apply the requested period.
  if (Status status = set_trigger_period_locked(200'000); !status.ok()) {
    return status;
  }
  if (Status status = set_trigger_counts_locked(1, 1); !status.ok()) {
    return status;
  }
  return set_trigger_period_locked(trigger_period_us);
}

Status ControlChannel::stop_streaming() {
  std::lock_guard<std::mutex> lock(mutex_);
  // No 1 s settle: the channel must not hide a long sleep.  The caller (or the
  // pipeline draining its last frames) is responsible for that.
  if (Status status = set_trigger_period_locked(200'000); !status.ok()) {
    return status;
  }
  return set_trigger_counts_locked(0, 0);
}

}  // namespace einstar
