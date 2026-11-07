// SPDX-License-Identifier: MIT
// Offline tests for the Einstar command / control channel.
//
// FakeDevice is an in-memory FX3 model: it decodes requests with the same
// offsets and wire sizes as the firmware (docs/protocol/libeinstar_protocol_spec.md
// §3, docs/protocol/einstar_usb_protocol.md §2) and answers with realistic
// replies.  It implements Transport (the streaming abstraction) and CommandIo
// (the outbound capability) so the channel can bind to it exactly as it binds
// to a real backend.
#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <initializer_list>
#include <map>
#include <span>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include "einstar/clock.hpp"
#include "einstar/control_channel.hpp"
#include "einstar/transport.hpp"
#include "test_util.hpp"

using namespace einstar;

namespace {

// ---------------------------------------------------------------------------
// Independent protocol helpers (deliberately not reusing the implementation)
// ---------------------------------------------------------------------------

constexpr std::uint8_t kFx3System = 0x00;
constexpr std::uint8_t kFx3Camera = 0x10;

std::uint16_t fx3_be16(const std::uint8_t* p) {
  return static_cast<std::uint16_t>((static_cast<unsigned>(p[0]) << 8U) |
                                    static_cast<unsigned>(p[1]));
}

std::uint32_t fx3_be32(const std::uint8_t* p) {
  return (static_cast<std::uint32_t>(p[0]) << 24U) |
         (static_cast<std::uint32_t>(p[1]) << 16U) |
         (static_cast<std::uint32_t>(p[2]) << 8U) |
         static_cast<std::uint32_t>(p[3]);
}

void fx3_put_be32(std::vector<std::uint8_t>& out, std::size_t offset,
                  std::uint32_t value) {
  out[offset + 0] = static_cast<std::uint8_t>((value >> 24U) & 0xFFU);
  out[offset + 1] = static_cast<std::uint8_t>((value >> 16U) & 0xFFU);
  out[offset + 2] = static_cast<std::uint8_t>((value >> 8U) & 0xFFU);
  out[offset + 3] = static_cast<std::uint8_t>(value & 0xFFU);
}

std::vector<std::uint8_t> ascii(std::string_view text, bool nul_terminate) {
  std::vector<std::uint8_t> out;
  out.reserve(text.size() + (nul_terminate ? 1U : 0U));
  for (const char c : text) {
    out.push_back(static_cast<std::uint8_t>(c));
  }
  if (nul_terminate) {
    out.push_back(0);
  }
  return out;
}

std::vector<std::uint8_t> to_bytes(std::span<const std::byte> data) {
  std::vector<std::uint8_t> out;
  out.reserve(data.size());
  for (const std::byte value : data) {
    out.push_back(std::to_integer<std::uint8_t>(value));
  }
  return out;
}

std::string hex_of(const std::vector<std::uint8_t>& bytes) {
  static constexpr char kDigits[] = "0123456789abcdef";
  std::string text;
  text.reserve(bytes.size() * 2U);
  for (const std::uint8_t value : bytes) {
    text.push_back(kDigits[(value >> 4U) & 0x0FU]);
    text.push_back(kDigits[value & 0x0FU]);
  }
  return text;
}

std::string hex_prefix(const std::vector<std::uint8_t>& bytes,
                       std::size_t count) {
  return hex_of(std::vector<std::uint8_t>(
      bytes.begin(), bytes.begin() + static_cast<std::ptrdiff_t>(count)));
}

/// `prefix` is a space-free lowercase hex string; pad with zero bytes to `wire`.
std::string padded_hex(std::string_view prefix, std::size_t wire) {
  std::string text(prefix);
  text.append((wire - prefix.size() / 2U) * 2U, '0');
  return text;
}

std::size_t wire_size_for(std::uint8_t group, std::uint8_t opcode) {
  if (group == kFx3System) {
    if (opcode == 0x01) {
      return 100;
    }
    if (opcode == 0x04 || opcode == 0x05) {
      return 64;
    }
    return 0;
  }
  if (group != kFx3Camera) {
    return 0;
  }
  switch (opcode) {
    case 0x40:
    case 0x41:
      return 10;
    case 0x48:
    case 0x49:
      return 50;
    case 0x16:
    case 0x17:
    case 0x20:
    case 0x21:
    case 0x22:
    case 0x23:
    case 0x24:
    case 0x25:
    case 0x26:
    case 0x27:
    case 0x50:
    case 0x51:
    case 0x57:
      return 16;
    default:
      return 0;
  }
}

std::size_t payload_size_for(std::uint8_t group, std::uint8_t opcode) {
  if (group == kFx3System) {
    return 0;
  }
  switch (opcode) {
    case 0x16:
    case 0x17:
    case 0x20:
    case 0x21:
    case 0x22:
    case 0x24:
    case 0x25:
    case 0x26:
      return 1;
    case 0x23:
      return 5;
    case 0x27:
      return 3;
    case 0x41:
      return 1;
    case 0x49:
      return 4;
    case 0x48:
    case 0x40:
    case 0x50:
    case 0x51:
      return 0;
    default:
      return 0;
  }
}

std::uint8_t camera_from_mask(std::uint8_t mask) {
  switch (mask) {
    case 0x01:
      return 0;
    case 0x02:
      return 1;
    case 0x04:
      return 2;
    default:
      return 0xFF;
  }
}

// ---------------------------------------------------------------------------
// FakeDevice: in-memory FX3
// ---------------------------------------------------------------------------

struct Request {
  std::uint8_t id = 0;
  std::uint8_t group = 0;
  std::uint8_t opcode = 0;
  std::uint32_t argument = 0;
  std::vector<std::uint8_t> payload;
  std::size_t wire = 0;
};

class FakeDevice final : public Transport, public CommandIo {
 public:
  // ---- Transport ---------------------------------------------------------
  Status open() override {
    open_ = true;
    return Status::success();
  }
  void close() override {
    open_ = false;
    claimed_ = false;
    command_reply_.clear();
    page_reply_.clear();
  }
  [[nodiscard]] bool is_open() const noexcept override { return open_; }

  Status claim_interface(std::uint8_t interface_number) override {
    if (!open_) {
      return Status::error(ErrorCode::NotOpen, "fake: device is closed");
    }
    if (interface_number != 0) {
      return Status::error(ErrorCode::InvalidArgument,
                           "fake: only interface 0 exists");
    }
    claimed_ = true;
    ++claim_calls_;
    return Status::success();
  }

  Status release_interface(std::uint8_t interface_number) override {
    if (interface_number != 0) {
      return Status::error(ErrorCode::InvalidArgument,
                           "fake: only interface 0 exists");
    }
    claimed_ = false;
    ++release_calls_;
    return Status::success();
  }

  Status clear_halt(std::uint8_t endpoint) override {
    ++halt_calls_[endpoint];
    return Status::success();
  }

  TransferId submit_bulk_in(std::uint8_t /*endpoint*/,
                            std::span<std::byte> /*destination*/,
                            CompletionSink& /*sink*/, void* /*user*/) override {
    ++unexpected_submits_;
    return 0;
  }
  bool cancel(TransferId /*id*/) override { return false; }
  std::size_t poll(int /*timeout_ms*/) override { return 0; }
  [[nodiscard]] std::size_t outstanding() const noexcept override { return 0; }
  [[nodiscard]] const Clock& clock() const noexcept override { return clock_; }
  [[nodiscard]] TransportKind kind() const noexcept override {
    return TransportKind::Simulated;
  }
  [[nodiscard]] std::string_view name() const noexcept override {
    return "fake-fx3";
  }

  // ---- CommandIo ---------------------------------------------------------
  Status write_interrupt(std::uint8_t endpoint,
                         std::span<const std::byte> data,
                         std::uint32_t /*timeout_ms*/) override {
    if (endpoint != Endpoints::kCommandOut) {
      return Status::error(ErrorCode::InvalidArgument,
                           "fake: commands use EP 0x01");
    }
    ++interrupt_writes_;
    std::vector<std::uint8_t> frame = to_bytes(data);
    frames_.push_back(frame);
    if (frame.size() == kHandshakeSize &&
        frame[0] == kHandshakeByte) {
      enqueue_handshake_reply();
      return Status::success();
    }
    if (frame.size() < 8) {
      ++wire_size_mismatches_;
      return Status::success();  // the device stays silent
    }
    Request request;
    request.id = frame[0];
    request.group = frame[2];
    request.opcode = frame[3];
    request.argument = fx3_be32(frame.data() + 4);
    request.wire = frame.size();
    if (wire_size_for(request.group, request.opcode) != frame.size()) {
      ++wire_size_mismatches_;
    }
    if (request.argument > frame.size() - 8U) {
      ++argument_mismatches_;
      return Status::success();
    }
    request.payload.assign(
        frame.begin() + 8,
        frame.begin() + 8 + static_cast<std::ptrdiff_t>(request.argument));
    requests_.push_back(request);
    if (drop_replies_ > 0) {
      --drop_replies_;
      return Status::success();  // dropped on the floor
    }
    enqueue_command_reply(request);
    return Status::success();
  }

  Status write_bulk(std::uint8_t endpoint, std::span<const std::byte> data,
                    std::uint32_t /*timeout_ms*/) override {
    if (endpoint != Endpoints::kBulkOut) {
      return Status::error(ErrorCode::InvalidArgument,
                           "fake: page requests use EP 0x02");
    }
    ++bulk_writes_;
    const std::vector<std::uint8_t> frame = to_bytes(data);
    if (frame.size() != kPageTransfer) {
      ++wire_size_mismatches_;
      return Status::success();
    }
    page_frames_.push_back(frame);
    if (frame[2] != kFx3Camera || frame[3] != 0x57) {
      ++unknown_commands_;
      return Status::success();
    }
    Request request;
    request.id = frame[0];
    request.group = frame[2];
    request.opcode = frame[3];
    request.argument = fx3_be32(frame.data() + 4);
    request.wire = frame.size();
    if (request.argument != 2) {
      ++argument_mismatches_;
      return Status::success();
    }
    last_page_ = fx3_be16(frame.data() + 8);
    if (drop_replies_ > 0) {
      --drop_replies_;
      return Status::success();
    }
    std::vector<std::uint8_t> reply(kPageTransfer, 0);
    reply[0] = request.id;
    reply[1] = 0x02;
    reply[2] = kFx3Camera;
    reply[3] = 0x57;
    std::uint8_t status = 0;
    if (status_value_ != 0 && status_group_ == kFx3Camera &&
        status_opcode_ == 0x57) {
      status = status_value_;
    }
    if (corrupt_echoes_ > 0) {
      --corrupt_echoes_;
      reply[3] = static_cast<std::uint8_t>(reply[3] ^ 0xFFU);
    }
    reply[4] = status;
    if (status == 0) {
      for (std::size_t i = 0; i < 4096U; ++i) {
        reply[9U + i] = static_cast<std::uint8_t>(
            (static_cast<unsigned>(last_page_) * 31U + i) & 0xFFU);
      }
    }
    page_reply_ = std::move(reply);
    return Status::success();
  }

  Status read_interrupt(std::uint8_t endpoint,
                        std::span<std::byte> destination,
                        std::uint32_t /*timeout_ms*/,
                        std::size_t& transferred) override {
    if (endpoint != Endpoints::kCommandIn) {
      return Status::error(ErrorCode::InvalidArgument,
                           "fake: command replies use EP 0x81");
    }
    ++interrupt_reads_;
    transferred = 0;
    if (command_reply_.empty()) {
      return Status::error(ErrorCode::Timeout, "fake: no command reply");
    }
    transferred = copy_out(destination, command_reply_);
    command_reply_.clear();
    return Status::success();
  }

  Status read_bulk(std::uint8_t endpoint, std::span<std::byte> destination,
                   std::uint32_t /*timeout_ms*/,
                   std::size_t& transferred) override {
    if (endpoint != Endpoints::kBulkIn) {
      return Status::error(ErrorCode::InvalidArgument,
                           "fake: page replies use EP 0x82");
    }
    ++bulk_reads_;
    transferred = 0;
    if (page_reply_.empty()) {
      return Status::error(ErrorCode::Timeout, "fake: no page reply");
    }
    transferred = copy_out(destination, page_reply_);
    page_reply_.clear();
    return Status::success();
  }

  // ---- fault injection / state ------------------------------------------
  void set_bad_handshake(bool value) { bad_handshake_ = value; }
  void drop_replies(std::uint32_t count) { drop_replies_ = count; }
  void corrupt_echoes(std::uint32_t count) { corrupt_echoes_ = count; }
  void fail_status(std::uint8_t group, std::uint8_t opcode,
                   std::uint8_t status) {
    status_group_ = group;
    status_opcode_ = opcode;
    status_value_ = status;
  }
  void clear_status_failure() { status_value_ = 0; }

  void clear_traffic() {
    frames_.clear();
    page_frames_.clear();
    requests_.clear();
    halt_calls_.clear();
    interrupt_writes_ = 0;
    interrupt_reads_ = 0;
    bulk_writes_ = 0;
    bulk_reads_ = 0;
    unexpected_submits_ = 0;
    wire_size_mismatches_ = 0;
    argument_mismatches_ = 0;
    payload_mismatches_ = 0;
    unknown_commands_ = 0;
  }

  [[nodiscard]] const std::vector<std::vector<std::uint8_t>>& frames() const {
    return frames_;
  }
  [[nodiscard]] const std::vector<Request>& requests() const {
    return requests_;
  }
  [[nodiscard]] const std::vector<std::vector<std::uint8_t>>& page_frames()
      const {
    return page_frames_;
  }
  [[nodiscard]] std::size_t claim_calls() const { return claim_calls_; }
  [[nodiscard]] std::size_t release_calls() const { return release_calls_; }
  [[nodiscard]] std::size_t halt_calls(std::uint8_t endpoint) const {
    const auto it = halt_calls_.find(endpoint);
    return it == halt_calls_.end() ? 0U : it->second;
  }
  [[nodiscard]] std::size_t interrupt_writes() const { return interrupt_writes_; }
  [[nodiscard]] std::size_t bulk_writes() const { return bulk_writes_; }
  [[nodiscard]] std::size_t bulk_reads() const { return bulk_reads_; }
  [[nodiscard]] std::size_t unexpected_submits() const {
    return unexpected_submits_;
  }
  [[nodiscard]] std::size_t wire_size_mismatches() const {
    return wire_size_mismatches_;
  }
  [[nodiscard]] std::size_t argument_mismatches() const {
    return argument_mismatches_;
  }
  [[nodiscard]] std::size_t payload_mismatches() const {
    return payload_mismatches_;
  }
  [[nodiscard]] std::size_t unknown_commands() const { return unknown_commands_; }
  [[nodiscard]] std::uint16_t last_page() const { return last_page_; }
  [[nodiscard]] bool claimed() const { return claimed_; }
  /// The image path is enabled while at least one frame per trigger is asked
  /// for; there is no separate stream-enable opcode in the recovered protocol.
  [[nodiscard]] bool streaming() const { return trigger != 0; }

  // Device state the tests inspect.
  std::array<std::uint32_t, 3> exposure{{2184, 2184, 2184}};
  std::array<std::uint16_t, 3> gain{{500, 500, 500}};
  std::uint8_t trigger = 0x11;
  std::uint32_t period = 90'000;
  std::uint16_t sensors = 3;

 private:
  static constexpr std::size_t kHandshakeSize = 100;
  static constexpr std::uint8_t kHandshakeByte = 0xFF;
  static constexpr std::size_t kPageTransfer = 0x1400;

  static std::size_t copy_out(std::span<std::byte> destination,
                              const std::vector<std::uint8_t>& source) {
    const std::size_t count =
        std::min(destination.size(), source.size());
    for (std::size_t i = 0; i < count; ++i) {
      destination[i] = static_cast<std::byte>(source[i]);
    }
    return count;
  }

  void enqueue_handshake_reply() {
    std::vector<std::uint8_t> reply(kHandshakeSize, 0);
    reply[0] = 0xFF;
    reply[1] = 0x02;
    const std::string_view marker = bad_handshake_ ? "ShinyXX3" : "Shining3";
    std::copy(marker.begin(), marker.end(),
              reply.begin() + 9);
    command_reply_ = std::move(reply);
  }

  void enqueue_command_reply(const Request& request) {
    std::uint8_t status = 0;
    if (status_value_ != 0 && status_group_ == request.group &&
        status_opcode_ == request.opcode) {
      status = status_value_;
    }
    if (payload_size_for(request.group, request.opcode) !=
        request.payload.size()) {
      ++payload_mismatches_;
      status = 2;  // the firmware rejects a malformed payload
    }
    std::uint8_t camera = 0;
    if (status == 0 && request.group == kFx3Camera) {
      switch (request.opcode) {
        case 0x16:
        case 0x17:
        case 0x20:
        case 0x21:
        case 0x22:
        case 0x23:
        case 0x24:
        case 0x25:
        case 0x26:
        case 0x27:
          camera = camera_from_mask(request.payload.empty()
                                        ? std::uint8_t{0}
                                        : request.payload[0]);
          if (camera == 0xFF) {
            status = 2;
          }
          break;
        default:
          break;
      }
    }

    std::vector<std::uint8_t> value;
    if (status == 0) {
      if (request.group == kFx3System) {
        switch (request.opcode) {
          case 0x01:
            value = ascii("EinScan10_01", false);
            break;
          case 0x04:
            // Synthetic serial: never a real device identifier.
            value = {0x01, 0x23, 0x45, 0x67, 0x89,
                     0xAB, 0xCD, 0xEF, 0x44, 0x00, 0x00, 0x00};
            break;
          case 0x05:
            value =
                ascii("EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_EN", true);
            break;
          default:
            ++unknown_commands_;
            status = 1;
            break;
        }
      } else {
        switch (request.opcode) {
          case 0x16:
            value = {0x05, 0x00};  // 1280
            break;
          case 0x17:
            value = {0x04, 0x00};  // 1024
            break;
          case 0x20:
            value = {0x00, 0x01, 0x86, 0xA0};  // 100000
            break;
          case 0x21:
            value = {0x00, 0x00, 0x00, 0x64};  // 100
            break;
          case 0x22: {
            value.resize(4);
            fx3_put_be32(value, 0, exposure[camera]);
            break;
          }
          case 0x23:
            exposure[camera] = fx3_be32(request.payload.data() + 1);
            break;
          case 0x24:
            value = {0x05, 0xDC};  // 1500
            break;
          case 0x25:
            value = {0x00, 0x00};
            break;
          case 0x26:
            value = {static_cast<std::uint8_t>(gain[camera] >> 8U),
                     static_cast<std::uint8_t>(gain[camera] & 0xFFU)};
            break;
          case 0x27:
            gain[camera] =
                fx3_be16(request.payload.data() + 1);
            break;
          case 0x40:
            value = {trigger};
            break;
          case 0x41:
            trigger = request.payload[0];
            break;
          case 0x48:
            value.resize(4);
            fx3_put_be32(value, 0, period);
            break;
          case 0x49:
            period = fx3_be32(request.payload.data());
            break;
          case 0x50:
            value = {0x19, 0x00};  // 6400 >> 7 == 50 (25.0 degC)
            break;
          case 0x51:
            value = {static_cast<std::uint8_t>(sensors & 0xFFU)};
            break;
          default:
            ++unknown_commands_;
            status = 1;
            break;
        }
      }
    }
    if (status != 0) {
      value.clear();
    }

    const std::size_t wire = wire_size_for(request.group, request.opcode);
    const std::size_t reply_size = std::max(wire == 0 ? 16U : wire,
                                            9U + value.size());
    std::vector<std::uint8_t> reply(reply_size, 0);
    reply[0] = request.id;
    reply[1] = 0x02;
    reply[2] = request.group;
    reply[3] = request.opcode;
    if (corrupt_echoes_ > 0) {
      --corrupt_echoes_;
      reply[3] = static_cast<std::uint8_t>(reply[3] ^ 0xFFU);
    }
    reply[4] = status;
    fx3_put_be32(reply, 5, static_cast<std::uint32_t>(value.size()));
    std::copy(value.begin(), value.end(), reply.begin() + 9);
    command_reply_ = std::move(reply);
  }

  VirtualClock clock_;
  bool open_ = false;
  bool claimed_ = false;
  bool bad_handshake_ = false;
  std::uint32_t drop_replies_ = 0;
  std::uint32_t corrupt_echoes_ = 0;
  std::uint8_t status_group_ = 0;
  std::uint8_t status_opcode_ = 0;
  std::uint8_t status_value_ = 0;
  std::uint16_t last_page_ = 0;
  std::vector<std::uint8_t> command_reply_;
  std::vector<std::uint8_t> page_reply_;
  std::vector<std::vector<std::uint8_t>> frames_;
  std::vector<std::vector<std::uint8_t>> page_frames_;
  std::vector<Request> requests_;
  std::map<std::uint8_t, std::size_t> halt_calls_;
  std::size_t claim_calls_ = 0;
  std::size_t release_calls_ = 0;
  std::size_t interrupt_writes_ = 0;
  std::size_t interrupt_reads_ = 0;
  std::size_t bulk_writes_ = 0;
  std::size_t bulk_reads_ = 0;
  std::size_t unexpected_submits_ = 0;
  std::size_t wire_size_mismatches_ = 0;
  std::size_t argument_mismatches_ = 0;
  std::size_t payload_mismatches_ = 0;
  std::size_t unknown_commands_ = 0;
};

/// A transport with no outbound path: proves capability detection.
class PlainTransport final : public Transport {
 public:
  Status open() override {
    open_ = true;
    return Status::success();
  }
  void close() override { open_ = false; }
  [[nodiscard]] bool is_open() const noexcept override { return open_; }
  Status claim_interface(std::uint8_t) override {
    return Status::success();
  }
  Status release_interface(std::uint8_t) override {
    return Status::success();
  }
  Status clear_halt(std::uint8_t) override { return Status::success(); }
  TransferId submit_bulk_in(std::uint8_t, std::span<std::byte>,
                            CompletionSink&, void*) override {
    return 0;
  }
  bool cancel(TransferId) override { return false; }
  std::size_t poll(int) override { return 0; }
  [[nodiscard]] std::size_t outstanding() const noexcept override { return 0; }
  [[nodiscard]] const Clock& clock() const noexcept override { return clock_; }
  [[nodiscard]] TransportKind kind() const noexcept override {
    return TransportKind::Simulated;
  }
  [[nodiscard]] std::string_view name() const noexcept override {
    return "plain";
  }

 private:
  SteadyClock clock_;
  bool open_ = false;
};

ControlConfig fast_config() {
  ControlConfig config;
  config.retry_delay_ms = 0;
  return config;
}

bool open_ok(FakeDevice& fake, ControlChannel& channel) {
  if (!fake.open().ok()) {
    return false;
  }
  auto result = channel.open();
  if (!result) {
    std::printf("  open failed: %s\n", result.error().message().c_str());
    return false;
  }
  return true;
}

}  // namespace

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

EINSTAR_TEST(control_open_claims_and_handshakes) {
  FakeDevice fake;
  ControlChannel channel(fake, fast_config());
  REQUIRE(fake.open().ok());
  auto identity = channel.open();
  if (!identity) {
    std::printf("  open failed: %s\n", identity.error().message().c_str());
  }
  REQUIRE(identity.has_value());
  CHECK(channel.is_open());
  CHECK(fake.claimed());
  CHECK_EQ(fake.claim_calls(), std::size_t{1});

  // Connect order: handshake, serial, firmware, sensor count.
  REQUIRE(fake.frames().size() == 4U);
  CHECK_EQ(hex_of(fake.frames()[0]), padded_hex("ff", 100));
  CHECK_EQ(hex_of(fake.frames()[1]), padded_hex("0000000400000000", 64));
  CHECK_EQ(hex_of(fake.frames()[2]), padded_hex("0100000500000000", 64));
  CHECK_EQ(hex_of(fake.frames()[3]), padded_hex("0200105100000000", 16));

  CHECK_EQ(identity->serial, std::string("0123456789ABCDEF"));
  CHECK_EQ(identity->sensor_count, std::uint16_t{3});
  CHECK_EQ(identity->firmware.raw,
           std::string("EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_EN"));
  CHECK_EQ(identity->firmware.model, std::string("EinScan10_01"));
  CHECK_EQ(identity->firmware.sensor, std::string("SC130"));
  CHECK_EQ(identity->firmware.fx3_version, std::string("2.10"));
  CHECK_EQ(identity->firmware.fpga_version, std::string("3.7"));
  CHECK_EQ(identity->firmware.locale, std::string("EN"));
  CHECK(identity->firmware.parsed());
  const DeviceIdentity cached = channel.identity();
  CHECK_EQ(cached.serial, std::string("0123456789ABCDEF"));

  CHECK_EQ(fake.wire_size_mismatches(), std::size_t{0});
  CHECK_EQ(fake.argument_mismatches(), std::size_t{0});
  CHECK_EQ(fake.unexpected_submits(), std::size_t{0});
}

EINSTAR_TEST(control_golden_request_vectors) {
  FakeDevice fake;
  ControlChannel channel(fake, fast_config());
  REQUIRE(open_ok(fake, channel));
  fake.clear_traffic();

  REQUIRE(channel.set_exposure_us(1, 5000).ok());
  auto exposure = channel.exposure_us(0);
  REQUIRE(exposure.has_value());
  CHECK_EQ(*exposure, std::uint32_t{2184});
  REQUIRE(channel.set_gain(2, 400).ok());
  auto counts = channel.trigger_counts();
  REQUIRE(counts.has_value());
  CHECK_EQ(static_cast<unsigned>(counts->mono), 1U);
  CHECK_EQ(static_cast<unsigned>(counts->rgb), 1U);
  REQUIRE(channel.set_trigger_counts(1, 1).ok());
  REQUIRE(channel.set_trigger_period_us(90'000).ok());
  auto width = channel.max_width(0);
  REQUIRE(width.has_value());
  CHECK_EQ(*width, std::uint16_t{1280});
  auto temperature = channel.temperature_raw();
  REQUIRE(temperature.has_value());
  CHECK_EQ(*temperature, std::int16_t{50});

  REQUIRE(fake.frames().size() == 8U);
  CHECK_EQ(hex_of(fake.frames()[0]),
           std::string("03001023000000050200001388000000"));
  CHECK_EQ(hex_of(fake.frames()[1]),
           std::string("04001022000000010100000000000000"));
  CHECK_EQ(hex_of(fake.frames()[2]),
           std::string("05001027000000030401900000000000"));
  CHECK_EQ(hex_of(fake.frames()[3]), std::string("06001040000000000000"));
  CHECK_EQ(hex_of(fake.frames()[4]), std::string("07001041000000011100"));
  CHECK_EQ(hex_of(fake.frames()[5]),
           padded_hex("080010490000000400015f90", 50));
  CHECK_EQ(hex_of(fake.frames()[6]), padded_hex("090010160000000101", 16));
  CHECK_EQ(hex_of(fake.frames()[7]), padded_hex("0a00105000000000", 16));

  CHECK_EQ(fake.wire_size_mismatches(), std::size_t{0});
  CHECK_EQ(fake.argument_mismatches(), std::size_t{0});
  CHECK_EQ(fake.payload_mismatches(), std::size_t{0});
  CHECK_EQ(fake.unknown_commands(), std::size_t{0});
  CHECK_EQ(channel.stats().echo_mismatches, std::uint64_t{0});
}

EINSTAR_TEST(control_identity_queries) {
  FakeDevice fake;
  ControlChannel channel(fake, fast_config());
  REQUIRE(open_ok(fake, channel));
  fake.clear_traffic();

  auto product = channel.product_name();
  REQUIRE(product.has_value());
  CHECK_EQ(*product, std::string("EinScan10_01"));
  auto serial = channel.serial();
  REQUIRE(serial.has_value());
  CHECK_EQ(*serial, std::string("0123456789ABCDEF"));
  auto firmware = channel.firmware_version();
  REQUIRE(firmware.has_value());
  CHECK_EQ(firmware->fpga_version, std::string("3.7"));
  auto sensors = channel.sensor_count();
  REQUIRE(sensors.has_value());
  CHECK_EQ(*sensors, std::uint16_t{3});

  REQUIRE(fake.frames().size() == 4U);
  CHECK_EQ(fake.frames()[0].size(), std::size_t{100});
  CHECK_EQ(hex_prefix(fake.frames()[0], 8),
           std::string("0300000100000000"));
  CHECK_EQ(fake.frames()[1].size(), std::size_t{64});
  CHECK_EQ(hex_prefix(fake.frames()[1], 8),
           std::string("0400000400000000"));
  CHECK_EQ(fake.frames()[2].size(), std::size_t{64});
  CHECK_EQ(hex_prefix(fake.frames()[2], 8),
           std::string("0500000500000000"));
  CHECK_EQ(fake.frames()[3].size(), std::size_t{16});
  CHECK_EQ(fake.wire_size_mismatches(), std::size_t{0});
}

EINSTAR_TEST(control_firmware_version_parsing) {
  const auto exact =
      parse_firmware_version("EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_EN");
  CHECK(exact.parsed());
  CHECK_EQ(exact.raw,
           std::string("EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_EN"));
  CHECK_EQ(exact.model, std::string("EinScan10_01"));
  CHECK_EQ(exact.sensor, std::string("SC130"));
  CHECK_EQ(exact.fx3_version, std::string("2.10"));
  CHECK_EQ(exact.fpga_version, std::string("3.7"));
  CHECK_EQ(exact.locale, std::string("EN"));

  const auto no_locale =
      parse_firmware_version("EinScan10_SC130_FX3_V2.10_FPGA_V3.7");
  CHECK(no_locale.parsed());
  CHECK_EQ(no_locale.model, std::string("EinScan10"));
  CHECK_EQ(no_locale.sensor, std::string("SC130"));
  CHECK_EQ(no_locale.locale, std::string(""));

  const auto other =
      parse_firmware_version("EinScan10_01_SC130_FX3_V2.14_FPGA_V3.7_CH");
  CHECK_EQ(other.fx3_version, std::string("2.14"));
  CHECK_EQ(other.locale, std::string("CH"));

  const auto garbage = parse_firmware_version("not-a-firmware-string");
  CHECK(!garbage.parsed());
  CHECK_EQ(garbage.raw, std::string("not-a-firmware-string"));
  CHECK(garbage.model.empty());
  CHECK(garbage.fx3_version.empty());
}

EINSTAR_TEST(control_trigger_packing) {
  CHECK_EQ(static_cast<unsigned>(pack_trigger_counts(1, 1)), 0x11U);
  CHECK_EQ(static_cast<unsigned>(pack_trigger_counts(0x13, 0x02)), 0x23U);
  const auto counts = unpack_trigger_counts(0xA5);
  CHECK_EQ(static_cast<unsigned>(counts.mono), 5U);
  CHECK_EQ(static_cast<unsigned>(counts.rgb), 10U);

  FakeDevice fake;
  ControlChannel channel(fake, fast_config());
  REQUIRE(open_ok(fake, channel));
  fake.clear_traffic();

  REQUIRE(channel.set_trigger_counts(2, 3).ok());
  CHECK_EQ(static_cast<unsigned>(fake.trigger), 0x32U);
  auto read_back = channel.trigger_counts();
  REQUIRE(read_back.has_value());
  CHECK_EQ(static_cast<unsigned>(read_back->mono), 2U);
  CHECK_EQ(static_cast<unsigned>(read_back->rgb), 3U);
  CHECK_EQ(hex_of(fake.frames()[0]), std::string("03001041000000013200"));

  const std::size_t before = fake.frames().size();
  CHECK_EQ(channel.set_trigger_counts(16, 0).code(),
           ErrorCode::InvalidArgument);
  CHECK_EQ(channel.set_trigger_counts(0, 16).code(),
           ErrorCode::InvalidArgument);
  CHECK_EQ(fake.frames().size(), before);
}

EINSTAR_TEST(control_exposure_round_trip) {
  FakeDevice fake;
  ControlChannel channel(fake, fast_config());
  REQUIRE(open_ok(fake, channel));
  fake.clear_traffic();

  REQUIRE(channel.set_exposure_us(0, 1000).ok());
  REQUIRE(channel.set_exposure_us(1, 2000).ok());
  REQUIRE(channel.set_exposure_us(2, 3000).ok());
  CHECK_EQ(fake.exposure[0], std::uint32_t{1000});
  CHECK_EQ(fake.exposure[1], std::uint32_t{2000});
  CHECK_EQ(fake.exposure[2], std::uint32_t{3000});
  auto c0 = channel.exposure_us(0);
  auto c1 = channel.exposure_us(1);
  auto c2 = channel.exposure_us(2);
  REQUIRE(c0.has_value());
  REQUIRE(c1.has_value());
  REQUIRE(c2.has_value());
  CHECK_EQ(*c0, std::uint32_t{1000});
  CHECK_EQ(*c1, std::uint32_t{2000});
  CHECK_EQ(*c2, std::uint32_t{3000});

  auto range = channel.exposure_range(1);
  REQUIRE(range.has_value());
  CHECK_EQ(range->minimum_us, std::uint32_t{100});
  CHECK_EQ(range->maximum_us, std::uint32_t{100000});

  const std::size_t before = fake.frames().size();
  CHECK_EQ(channel.exposure_us(3).error().code(),
           ErrorCode::InvalidArgument);
  CHECK_EQ(channel.set_exposure_us(3, 1).code(),
           ErrorCode::InvalidArgument);
  CHECK_EQ(channel.exposure_range(3).error().code(),
           ErrorCode::InvalidArgument);
  CHECK_EQ(fake.frames().size(), before);
  CHECK_EQ(fake.wire_size_mismatches(), std::size_t{0});
  CHECK_EQ(fake.payload_mismatches(), std::size_t{0});
}

EINSTAR_TEST(control_gain_round_trip) {
  FakeDevice fake;
  ControlChannel channel(fake, fast_config());
  REQUIRE(open_ok(fake, channel));
  fake.clear_traffic();

  REQUIRE(channel.set_gain(0, 100).ok());
  REQUIRE(channel.set_gain(1, 250).ok());
  REQUIRE(channel.set_gain(2, 500).ok());
  CHECK_EQ(fake.gain[0], std::uint16_t{100});
  CHECK_EQ(fake.gain[1], std::uint16_t{250});
  CHECK_EQ(fake.gain[2], std::uint16_t{500});
  auto g0 = channel.gain(0);
  auto g1 = channel.gain(1);
  auto g2 = channel.gain(2);
  REQUIRE(g0.has_value());
  REQUIRE(g1.has_value());
  REQUIRE(g2.has_value());
  CHECK_EQ(*g0, std::uint16_t{100});
  CHECK_EQ(*g1, std::uint16_t{250});
  CHECK_EQ(*g2, std::uint16_t{500});

  auto range = channel.gain_range(0);
  REQUIRE(range.has_value());
  CHECK_EQ(range->minimum, std::uint16_t{0});
  CHECK_EQ(range->maximum, std::uint16_t{1500});
  CHECK_EQ(fake.payload_mismatches(), std::size_t{0});
}

EINSTAR_TEST(control_retry_on_dropped_reply) {
  FakeDevice fake;
  ControlChannel channel(fake, fast_config());
  REQUIRE(open_ok(fake, channel));
  channel.reset_stats();
  fake.clear_traffic();

  fake.drop_replies(1);
  auto value = channel.exposure_us(0);
  REQUIRE(value.has_value());
  CHECK_EQ(*value, std::uint32_t{2184});

  const auto stats = channel.stats();
  CHECK_EQ(stats.commands, std::uint64_t{1});
  CHECK_EQ(stats.attempts, std::uint64_t{2});
  CHECK_EQ(stats.retries, std::uint64_t{1});
  CHECK_EQ(stats.timeouts, std::uint64_t{1});
  CHECK_EQ(stats.echo_mismatches, std::uint64_t{0});
  REQUIRE(fake.frames().size() == 2U);
  CHECK_EQ(static_cast<unsigned>(fake.frames()[0][3]), 0x22U);
  CHECK_EQ(static_cast<unsigned>(fake.frames()[1][3]), 0x22U);
  CHECK(fake.frames()[0][0] != fake.frames()[1][0]);
  CHECK_EQ(fake.halt_calls(0x01), std::size_t{1});
  CHECK_EQ(fake.halt_calls(0x81), std::size_t{1});
}

EINSTAR_TEST(control_timeout_is_bounded) {
  FakeDevice fake;
  ControlConfig config = fast_config();
  config.attempts = 3;
  ControlChannel channel(fake, config);
  REQUIRE(open_ok(fake, channel));
  channel.reset_stats();
  fake.clear_traffic();

  fake.drop_replies(100);
  auto value = channel.exposure_us(0);
  REQUIRE(!value.has_value());
  CHECK_EQ(value.error().code(), ErrorCode::Timeout);
  CHECK_EQ(fake.requests().size(), std::size_t{3});
  const auto stats = channel.stats();
  CHECK_EQ(stats.attempts, std::uint64_t{3});
  CHECK_EQ(stats.retries, std::uint64_t{2});
  CHECK_EQ(stats.timeouts, std::uint64_t{3});
  CHECK_EQ(stats.commands, std::uint64_t{0});
  CHECK_EQ(fake.halt_calls(0x01), std::size_t{3});
  CHECK_EQ(fake.halt_calls(0x81), std::size_t{3});
}

EINSTAR_TEST(control_bad_status_is_fatal) {
  FakeDevice fake;
  ControlChannel channel(fake, fast_config());
  REQUIRE(open_ok(fake, channel));
  channel.reset_stats();
  fake.clear_traffic();

  fake.fail_status(kFx3Camera, 0x22, 0x05);
  auto value = channel.exposure_us(0);
  REQUIRE(!value.has_value());
  CHECK_EQ(value.error().code(), ErrorCode::Protocol);
  CHECK(value.error().message().find("device status 5") !=
        std::string::npos);
  CHECK_EQ(fake.requests().size(), std::size_t{1});
  const auto stats = channel.stats();
  CHECK_EQ(stats.attempts, std::uint64_t{1});
  CHECK_EQ(stats.retries, std::uint64_t{0});
  CHECK_EQ(stats.device_status_errors, std::uint64_t{1});
  fake.clear_status_failure();
  auto recovered = channel.exposure_us(0);
  REQUIRE(recovered.has_value());
}

EINSTAR_TEST(control_corrupted_echo_retries) {
  FakeDevice fake;
  ControlChannel channel(fake, fast_config());
  REQUIRE(open_ok(fake, channel));
  channel.reset_stats();
  fake.clear_traffic();

  fake.corrupt_echoes(2);
  auto value = channel.gain(1);
  REQUIRE(value.has_value());
  CHECK_EQ(*value, std::uint16_t{500});
  const auto stats = channel.stats();
  CHECK_EQ(stats.attempts, std::uint64_t{3});
  CHECK_EQ(stats.retries, std::uint64_t{2});
  CHECK_EQ(stats.echo_mismatches, std::uint64_t{2});
  CHECK_EQ(stats.commands, std::uint64_t{1});
  REQUIRE(fake.frames().size() == 3U);
  CHECK_EQ(static_cast<unsigned>(fake.frames()[2][3]), 0x26U);
  CHECK(fake.frames()[0][0] != fake.frames()[1][0]);
  CHECK(fake.frames()[1][0] != fake.frames()[2][0]);
}

EINSTAR_TEST(control_page_read_bounds_and_content) {
  FakeDevice fake;
  ControlChannel channel(fake, fast_config());
  REQUIRE(open_ok(fake, channel));
  channel.reset_stats();
  fake.clear_traffic();

  std::array<std::byte, 16> too_small{};
  CHECK_EQ(channel.read_page(0, too_small).code(),
           ErrorCode::InvalidArgument);
  CHECK_EQ(fake.bulk_writes(), std::size_t{0});

  std::array<std::byte, kControlPageSize> page{};
  REQUIRE(channel.read_page(0x1234, page).ok());
  CHECK_EQ(fake.last_page(), std::uint16_t{0x1234});
  CHECK_EQ(fake.bulk_writes(), std::size_t{1});
  CHECK_EQ(fake.bulk_reads(), std::size_t{1});
  REQUIRE(fake.page_frames().size() == 1U);
  CHECK_EQ(fake.page_frames()[0].size(),
           std::size_t{kControlPageTransferSize});
  CHECK_EQ(hex_prefix(fake.page_frames()[0], 10),
           std::string("03001057000000021234"));
  for (std::size_t i = 0; i < kControlPageSize; ++i) {
    const auto expected = static_cast<std::uint8_t>(
        (0x1234U * 31U + i) & 0xFFU);
    CHECK_EQ(std::to_integer<unsigned>(page[i]),
             static_cast<unsigned>(expected));
  }
  const auto stats = channel.stats();
  CHECK_EQ(stats.page_reads, std::uint64_t{1});
  CHECK_EQ(stats.commands, std::uint64_t{1});

  // 0xFFFF is the largest index the wire format can express.
  REQUIRE(channel.read_page(0xFFFF, page).ok());
  CHECK_EQ(fake.last_page(), std::uint16_t{0xFFFF});
  CHECK_EQ(std::to_integer<unsigned>(page[0]),
           static_cast<unsigned>((0xFFFFU * 31U) & 0xFFU));
  CHECK_EQ(std::to_integer<unsigned>(page[kControlPageSize - 1]),
           static_cast<unsigned>((0xFFFFU * 31U + kControlPageSize - 1U) &
                                 0xFFU));
  CHECK_EQ(fake.wire_size_mismatches(), std::size_t{0});
  CHECK_EQ(fake.argument_mismatches(), std::size_t{0});
}

EINSTAR_TEST(control_page_read_status_error) {
  FakeDevice fake;
  ControlChannel channel(fake, fast_config());
  REQUIRE(open_ok(fake, channel));
  channel.reset_stats();
  fake.clear_traffic();

  fake.fail_status(kFx3Camera, 0x57, 7);
  std::array<std::byte, kControlPageSize> page{};
  const Status status = channel.read_page(1, page);
  CHECK_EQ(status.code(), ErrorCode::Protocol);
  CHECK_EQ(fake.bulk_writes(), std::size_t{1});
  const auto stats = channel.stats();
  CHECK_EQ(stats.device_status_errors, std::uint64_t{1});
  CHECK_EQ(stats.page_reads, std::uint64_t{0});
  CHECK_EQ(stats.retries, std::uint64_t{0});
}

EINSTAR_TEST(control_page_read_retries_echo_mismatch) {
  FakeDevice fake;
  ControlChannel channel(fake, fast_config());
  REQUIRE(open_ok(fake, channel));
  channel.reset_stats();
  fake.clear_traffic();

  fake.corrupt_echoes(1);
  std::array<std::byte, kControlPageSize> page{};
  REQUIRE(channel.read_page(2, page).ok());
  CHECK_EQ(fake.bulk_writes(), std::size_t{2});
  const auto stats = channel.stats();
  CHECK_EQ(stats.echo_mismatches, std::uint64_t{1});
  CHECK_EQ(stats.retries, std::uint64_t{1});
  CHECK_EQ(stats.page_reads, std::uint64_t{1});
  CHECK_EQ(fake.halt_calls(0x82), std::size_t{1});
  CHECK_EQ(fake.halt_calls(0x02), std::size_t{1});
}

EINSTAR_TEST(control_close_releases_and_reopens) {
  FakeDevice fake;
  ControlChannel channel(fake, fast_config());
  REQUIRE(open_ok(fake, channel));
  REQUIRE(channel.exposure_us(0).has_value());
  CHECK(fake.claimed());

  channel.close();
  CHECK(!channel.is_open());
  CHECK(!fake.claimed());
  CHECK_EQ(fake.release_calls(), std::size_t{1});
  CHECK_EQ(fake.outstanding(), std::size_t{0});
  CHECK_EQ(fake.unexpected_submits(), std::size_t{0});
  CHECK_EQ(channel.exposure_us(0).error().code(), ErrorCode::NotOpen);

  channel.close();  // idempotent
  CHECK_EQ(fake.release_calls(), std::size_t{1});

  auto reopened = channel.open();
  REQUIRE(reopened.has_value());
  CHECK(channel.is_open());
  CHECK(fake.claimed());
  CHECK_EQ(fake.claim_calls(), std::size_t{2});
  auto value = channel.exposure_us(0);
  REQUIRE(value.has_value());
  CHECK_EQ(*value, std::uint32_t{2184});
}

EINSTAR_TEST(control_open_rejects_zero_deadlines) {
  FakeDevice fake;
  ControlConfig config = fast_config();
  config.timeout_ms = 0;
  ControlChannel channel(fake, config);
  auto result = channel.open();
  REQUIRE(!result.has_value());
  CHECK_EQ(result.error().code(), ErrorCode::InvalidArgument);
  CHECK_EQ(fake.frames().size(), std::size_t{0});
  CHECK_EQ(fake.claim_calls(), std::size_t{0});
}

EINSTAR_TEST(control_open_twice_is_already_open) {
  FakeDevice fake;
  ControlChannel channel(fake, fast_config());
  REQUIRE(open_ok(fake, channel));
  auto second = channel.open();
  REQUIRE(!second.has_value());
  CHECK_EQ(second.error().code(), ErrorCode::AlreadyOpen);
  CHECK_EQ(fake.claim_calls(), std::size_t{1});
}

EINSTAR_TEST(control_open_requires_open_transport) {
  FakeDevice fake;
  ControlChannel channel(fake, fast_config());
  auto result = channel.open();
  REQUIRE(!result.has_value());
  CHECK_EQ(result.error().code(), ErrorCode::NotOpen);
  CHECK(!channel.is_open());
  CHECK(!fake.claimed());
}

EINSTAR_TEST(control_open_requires_command_io) {
  PlainTransport transport;
  REQUIRE(transport.open().ok());
  ControlChannel channel(transport, fast_config());
  auto result = channel.open();
  REQUIRE(!result.has_value());
  CHECK_EQ(result.error().code(), ErrorCode::Unsupported);
  CHECK(!channel.is_open());
}

EINSTAR_TEST(control_bad_handshake_rejected) {
  FakeDevice fake;
  fake.set_bad_handshake(true);
  ControlChannel channel(fake, fast_config());
  REQUIRE(fake.open().ok());
  auto result = channel.open();
  REQUIRE(!result.has_value());
  CHECK_EQ(result.error().code(), ErrorCode::Protocol);
  CHECK(result.error().message().find("Shining3") != std::string::npos);
  CHECK(!channel.is_open());
  CHECK(!fake.claimed());
  CHECK_EQ(fake.release_calls(), std::size_t{1});
  // No identity command may be sent when the probe is not understood.
  CHECK_EQ(fake.frames().size(), std::size_t{1});

  fake.set_bad_handshake(false);
  auto recovered = channel.open();
  REQUIRE(recovered.has_value());
  CHECK(channel.is_open());
}

EINSTAR_TEST(control_stream_lifecycle_commands) {
  FakeDevice fake;
  ControlChannel channel(fake, fast_config());
  REQUIRE(open_ok(fake, channel));
  channel.reset_stats();
  fake.clear_traffic();

  REQUIRE(channel.start_streaming(90'000).ok());
  REQUIRE(fake.frames().size() == 3U);
  CHECK_EQ(hex_of(fake.frames()[0]),
           padded_hex("030010490000000400030d40", 50));
  CHECK_EQ(hex_of(fake.frames()[1]), std::string("04001041000000011100"));
  CHECK_EQ(hex_of(fake.frames()[2]),
           padded_hex("050010490000000400015f90", 50));
  CHECK_EQ(fake.period, std::uint32_t{90'000});
  CHECK_EQ(static_cast<unsigned>(fake.trigger), 0x11U);
  CHECK(fake.streaming());
  CHECK_EQ(fake.bulk_writes(), std::size_t{0});
  CHECK_EQ(fake.unexpected_submits(), std::size_t{0});

  REQUIRE(channel.stop_streaming().ok());
  REQUIRE(fake.frames().size() == 5U);
  CHECK_EQ(hex_of(fake.frames()[3]),
           padded_hex("060010490000000400030d40", 50));
  CHECK_EQ(hex_of(fake.frames()[4]), std::string("07001041000000010000"));
  CHECK_EQ(fake.period, std::uint32_t{200'000});
  CHECK_EQ(static_cast<unsigned>(fake.trigger), 0U);
  CHECK(!fake.streaming());
  CHECK_EQ(channel.stats().commands, std::uint64_t{5});
}

EINSTAR_TEST(control_temperature_decoding) {
  FakeDevice fake;
  ControlChannel channel(fake, fast_config());
  REQUIRE(open_ok(fake, channel));
  auto raw = channel.temperature_raw();
  REQUIRE(raw.has_value());
  CHECK_EQ(*raw, std::int16_t{50});
  auto celsius = channel.temperature_celsius();
  REQUIRE(celsius.has_value());
  CHECK_NEAR(*celsius, 25.0, 1e-9);

  fake.fail_status(kFx3Camera, 0x50, 9);
  auto failed = channel.temperature_raw();
  REQUIRE(!failed.has_value());
  CHECK_EQ(failed.error().code(), ErrorCode::Protocol);
}

EINSTAR_TEST_MAIN()
