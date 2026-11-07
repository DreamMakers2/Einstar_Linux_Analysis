// SPDX-License-Identifier: MIT
// Command / control channel for the Einstar (EinScan10 / E10).
//
// This is the only code in the backend that speaks the command protocol.  It
// is deliberately independent of the image pipeline: every request leaves on
// the interrupt command pipe (EP 0x01), every reply arrives on EP 0x81, and
// the only bulk traffic is the read-only flash page read (0x02 -> 0x82), which
// is how the vendor's ReadData() works.  The channel never submits streaming
// reads and never touches the image endpoint 0x83.
//
// Wire format (docs/protocol/libeinstar_protocol_spec.md §3,
// docs/protocol/einstar_usb_protocol.md §2, both recovered from the Linux
// driver that ran on real hardware):
//
//   request (EP 0x01, interrupt OUT, zero padded to `wire` bytes)
//     0    1  packet id, rolling 0..253
//     1    1  0x00 (the vendor constant; the device does not validate it)
//     2    1  group   0x00 system, 0x10 camera
//     3    1  opcode
//     4    4  u32 BE argument (the payload length for every command here)
//     8    L  payload
//
//   reply (EP 0x81, interrupt IN)
//     0    1  packet id echo
//     1    1  0x02 marker (observed, never validated by the vendor)
//     2    1  group echo
//     3    1  opcode echo
//     4    1  status, 0 = success, non-zero = device error
//     5    4  u32 BE value length N.  Bytes 5..7 are zero in every observed
//             reply, which is why libeinstar could read only byte 8; the
//             vendor and this channel read the full 32-bit field and clamp it
//             to the bytes actually received.
//     9    N  value, big-endian
//
// Properties:
//   * no exceptions: every fallible call returns Result<T> or Status
//   * no heap allocation on the successful command path: the request/reply
//     buffers are fixed members (10 KiB); only the identity strings (open
//     time) and error diagnostics allocate
//   * whole operations are serialised by an internal mutex, so one channel can
//     be shared by a control plane and a diagnostics thread
//   * the channel never opens or closes the shared Transport and never resets
//     the image endpoint; it claims/releases interface 0 and clears only the
//     two command pipes (and the bulk pair during a page read)
//
// The public surface is deliberately C-ABI friendly: every operation is a
// plain method taking fixed-width scalars or a caller-owned span and returning
// a value or Result<T>, so a thin extern "C" wrapper is a mechanical mapping
// (opaque handle + out-parameters + einstar_error).  No callbacks and no
// ownership transfer are involved.
#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <mutex>
#include <span>
#include <string>
#include <string_view>

#include "einstar/command_io.hpp"
#include "einstar/status.hpp"
#include "einstar/transport.hpp"

namespace einstar {

// ---------------------------------------------------------------------------
// Wire constants
// ---------------------------------------------------------------------------

inline constexpr std::size_t kControlHandshakeSize = 100;
inline constexpr std::uint8_t kControlHandshakeByte = 0xFF;
inline constexpr char kControlHandshakeMarker[] = "Shining3";
inline constexpr std::size_t kControlCommandHeaderSize = 8;
inline constexpr std::size_t kControlReplyHeaderSize = 9;
inline constexpr std::size_t kControlMaxPayload = 8;
inline constexpr std::size_t kControlReplyCapacity = 1024;
inline constexpr std::uint32_t kControlPageSize = 4096;
inline constexpr std::uint32_t kControlPageTransferSize = 0x1400;
inline constexpr std::uint8_t kControlPacketIdModulus = 254;

/// Byte 2 of every request selects the command group.
enum class ControlGroup : std::uint8_t {
  System = 0x00,  // device identity
  Camera = 0x10,  // sensors, trigger, temperature, flash pages
};

/// System-group opcodes implemented here.  Deliberately absent: 0x06
/// (firmware update) and 0x08 (reboot) -- both are destructive and the channel
/// must not be able to express them.
enum class SystemOpcode : std::uint8_t {
  ProductName = 0x01,      // NUL-terminated string value
  Serial = 0x04,           // 8 bytes rendered as 16 uppercase hex digits
  FirmwareVersion = 0x05,  // NUL-terminated string value
};

/// Camera-group opcodes implemented here.  Deliberately absent: 0x58
/// (page write) and every other persistent-flash command.  A page read only
/// asks the device to copy a flash page into a bulk reply; it never writes.
enum class CameraOpcode : std::uint8_t {
  MaxWidth = 0x16,          // payload: camera mask u8, value u16 BE
  MaxHeight = 0x17,         // payload: camera mask u8, value u16 BE
  MaxExposure = 0x20,       // payload: camera mask u8, value u32 BE
  MinExposure = 0x21,       // payload: camera mask u8, value u32 BE
  GetExposure = 0x22,       // payload: camera mask u8, value u32 BE (us)
  SetExposure = 0x23,       // payload: camera mask u8 + u32 BE (us)
  MaxGain = 0x24,           // payload: camera mask u8, value u16 BE
  MinGain = 0x25,           // payload: camera mask u8, value u16 BE
  GetGain = 0x26,           // payload: camera mask u8, value u16 BE
  SetGain = 0x27,           // payload: camera mask u8 + u16 BE
  GetTriggerSwitch = 0x40,  // no payload, value u8 = (rgb << 4) | mono
  SetTriggerSwitch = 0x41,  // payload: one packed u8
  GetTriggerPeriod = 0x48,  // no payload, value u32 BE (us)
  SetTriggerPeriod = 0x49,  // payload: u32 BE (us)
  Temperature = 0x50,       // no payload, value i16 BE, arithmetic >> 7
  SensorCount = 0x51,       // no payload, value u8
  PageRead = 0x57,          // payload: u16 BE page index; 4096 bytes back
};

// ---------------------------------------------------------------------------
// Value types
// ---------------------------------------------------------------------------

/// Parsed firmware string.  The device returns
/// "EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_EN" (hardware-observed).
struct FirmwareVersion {
  std::string raw;          // exactly what the device returned
  std::string model;        // "EinScan10_01"
  std::string sensor;       // "SC130"
  std::string fx3_version;  // "2.10"
  std::string fpga_version; // "3.7"
  std::string locale;       // "EN"

  /// True when the FX3 and FPGA markers were both found.
  [[nodiscard]] bool parsed() const noexcept {
    return !fx3_version.empty() && !fpga_version.empty();
  }
};

/// Splits "EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_EN" into its fields.
/// Never fails: an unrecognised string yields only `raw` (and parsed() ==
/// false), because the firmware string is informational, not a handshake.
[[nodiscard]] FirmwareVersion parse_firmware_version(std::string_view text);

/// Everything the channel caches at open(), in the vendor's connect order:
/// serial, firmware version, sensor count.
struct DeviceIdentity {
  std::string serial;  // 16 uppercase hex digits
  FirmwareVersion firmware;
  std::uint16_t sensor_count = 0;

  [[nodiscard]] bool valid() const noexcept { return !serial.empty(); }
};

/// Frames per trigger, packed into one byte by opcode 0x41.
struct TriggerCounts {
  std::uint8_t mono = 0;  // low nibble: IR frames per trigger
  std::uint8_t rgb = 0;   // high nibble: colour frames per trigger
};

struct ExposureRange {
  std::uint32_t minimum_us = 0;
  std::uint32_t maximum_us = 0;
};

struct GainRange {
  std::uint16_t minimum = 0;
  std::uint16_t maximum = 0;
};

/// Camera selector used by every per-pixel command (cam0=1, cam1=2, cam2=4).
[[nodiscard]] constexpr std::uint8_t camera_mask(unsigned camera_index) noexcept {
  return camera_index < 3U ? static_cast<std::uint8_t>(1U << camera_index)
                           : std::uint8_t{0};
}

[[nodiscard]] constexpr bool is_valid_camera(unsigned camera_index) noexcept {
  return camera_index < 3U;
}

/// (rgb << 4) | mono, each nibble masked to 4 bits.
[[nodiscard]] constexpr std::uint8_t pack_trigger_counts(unsigned mono,
                                                        unsigned rgb) noexcept {
  return static_cast<std::uint8_t>(((rgb & 0x0FU) << 4U) | (mono & 0x0FU));
}

[[nodiscard]] constexpr TriggerCounts unpack_trigger_counts(
    std::uint8_t packed) noexcept {
  return TriggerCounts{static_cast<std::uint8_t>(packed & 0x0FU),
                       static_cast<std::uint8_t>(packed >> 4U)};
}

// ---------------------------------------------------------------------------
// Transport capability
// ---------------------------------------------------------------------------

/// Outbound / synchronous device I/O, implemented by a transport backend
/// alongside Transport.
///
/// `Transport` models the inbound streaming path: it can submit bulk-IN reads,
/// but it has no way to write to the device and no interrupt-IN primitive.
/// The command channel needs both, so a backend that supports control traffic
/// implements this interface too and the channel discovers it with
/// dynamic_cast.  This keeps control_channel.hpp free of libusb and leaves the
/// streaming abstraction untouched.
///
/// Contract:
///   * every call is synchronous and bounded by `timeout_ms`; it never blocks
///     longer than that and never retains the buffers after returning
///   * `ErrorCode::Timeout` means "the device did not answer in time";
///     `Ok` with `transferred == 0` means "an empty packet arrived"
///   * write_bulk() must transmit the whole buffer, splitting it into the
///     device's 1 KiB chunks (the vendor sends 0x400-byte transfers with a
///     1 ms gap; a backend should reproduce that)
///   * read_bulk() must fill the whole destination unless the device errors
///     or the deadline expires; it may need several transfers to do so

// ---------------------------------------------------------------------------
// Configuration and accounting
// ---------------------------------------------------------------------------

struct ControlConfig {
  /// Per-transfer deadline for a command request or reply.
  std::uint32_t timeout_ms = 1000;
  /// Deadline for the 0xFF handshake request and reply.
  std::uint32_t handshake_timeout_ms = 1000;
  /// Per-transfer deadline for a page-read bulk transfer.
  std::uint32_t page_timeout_ms = 2000;
  /// Total attempts per logical command, including the first one.  The vendor
  /// loop runs `for (attempt < 5)`; libeinstar calls the same value `retries`,
  /// which is why the recovered notes say "5 retries".  Five attempts here.
  std::uint32_t attempts = 5;
  /// Delay between attempts.  Zero in tests; 20 ms in production, matching the
  /// vendor and libeinstar.
  std::uint32_t retry_delay_ms = 20;
  /// Stale command replies discarded before the handshake (libeinstar: 5).
  std::uint32_t drain_attempts = 5;
  /// Stale bulk replies discarded before the handshake (libeinstar: 30).
  std::uint32_t bulk_drain_attempts = 30;
  /// Deadline for each drain read; a timeout means "pipe is empty".
  std::uint32_t drain_timeout_ms = 10;
  /// Interface claimed during open() (the Einstar exposes only interface 0).
  std::uint8_t interface_number = 0;
  /// Claim the interface in open() and release it in close().  Set to false
  /// when another component already owns the claim.
  bool claim_interface = true;
};

struct ControlStats {
  std::uint64_t commands = 0;             // logical commands that succeeded
  std::uint64_t attempts = 0;             // transfer attempts incl. retries
  std::uint64_t retries = 0;              // attempts beyond the first
  std::uint64_t timeouts = 0;             // attempts that timed out
  std::uint64_t echo_mismatches = 0;      // replies failing id/group/opcode
  std::uint64_t device_status_errors = 0; // non-zero status bytes
  std::uint64_t pipe_resets = 0;          // clear_halt calls issued
  std::uint64_t truncated_values = 0;     // declared value length > received
  std::uint64_t page_reads = 0;           // successful 4 KiB page reads
  std::uint64_t release_errors = 0;       // failed release_interface() calls
};

// ---------------------------------------------------------------------------
// ControlChannel
// ---------------------------------------------------------------------------

class ControlChannel final {
 public:
  /// `transport` must outlive the channel and must already be open when
  /// open() is called; the channel never opens or closes it.
  explicit ControlChannel(Transport& transport, ControlConfig config = {});
  ~ControlChannel();

  ControlChannel(const ControlChannel&) = delete;
  ControlChannel& operator=(const ControlChannel&) = delete;
  ControlChannel(ControlChannel&&) = delete;
  ControlChannel& operator=(ControlChannel&&) = delete;

  /// Claim interface 0, drain stale replies, run the 100-byte 0xFF handshake,
  /// then read serial, firmware version and sensor count.  On failure the
  /// interface is released again and the channel stays closed.
  [[nodiscard]] Result<DeviceIdentity> open();

  /// Release the interface (if claimed) and forget the identity.  Never closes
  /// the shared Transport.  Idempotent and safe from a destructor.
  void close() noexcept;

  /// Channel state only; the Transport may be closed independently.
  [[nodiscard]] bool is_open() const;

  /// Identity captured by open().  Safe to call from the control thread after
  /// open() returned.
  [[nodiscard]] DeviceIdentity identity() const;

  [[nodiscard]] const ControlConfig& config() const noexcept { return config_; }
  [[nodiscard]] ControlStats stats() const;
  void reset_stats();

  // -- identity (fresh reads; open() caches the first three) -----------------
  [[nodiscard]] Result<std::string> product_name();
  [[nodiscard]] Result<std::string> serial();
  [[nodiscard]] Result<FirmwareVersion> firmware_version();
  [[nodiscard]] Result<std::uint16_t> sensor_count();

  // -- sensor geometry ------------------------------------------------------
  [[nodiscard]] Result<std::uint16_t> max_width(unsigned camera);
  [[nodiscard]] Result<std::uint16_t> max_height(unsigned camera);

  // -- exposure (microseconds, u32 BE) --------------------------------------
  [[nodiscard]] Result<std::uint32_t> exposure_us(unsigned camera);
  [[nodiscard]] Status set_exposure_us(unsigned camera, std::uint32_t microseconds);
  /// Opcodes 0x21/0x20.  Declared in the code-verified opcode table but never
  /// exercised by libeinstar, so a device may answer with a non-zero status;
  /// that is reported as ErrorCode::Protocol.
  [[nodiscard]] Result<ExposureRange> exposure_range(unsigned camera);

  // -- gain (u16 BE) --------------------------------------------------------
  [[nodiscard]] Result<std::uint16_t> gain(unsigned camera);
  [[nodiscard]] Status set_gain(unsigned camera, std::uint16_t gain_value);
  /// Opcodes 0x25/0x24; same caveat as exposure_range().
  [[nodiscard]] Result<GainRange> gain_range(unsigned camera);

  // -- trigger --------------------------------------------------------------
  [[nodiscard]] Result<TriggerCounts> trigger_counts();
  /// mono/rgb above 15 are rejected with InvalidArgument before any I/O.
  [[nodiscard]] Status set_trigger_counts(unsigned mono, unsigned rgb);
  [[nodiscard]] Result<std::uint32_t> trigger_period_us();
  [[nodiscard]] Status set_trigger_period_us(std::uint32_t microseconds);

  // -- temperature ----------------------------------------------------------
  /// Raw value: i16 BE from the reply, arithmetic-shifted right by 7.  The
  /// shift divides out the LM75-style 7 fractional bits; the result is in
  /// 0.5 degC units (inferred from the vendor decode, units unverified).
  [[nodiscard]] Result<std::int16_t> temperature_raw();
  [[nodiscard]] Result<double> temperature_celsius();

  // -- read-only flash page access -----------------------------------------
  /// Reads flash page `index` (4096 bytes) into `destination`, which must hold
  /// at least kControlPageSize bytes.  Uses the bulk pair 0x02 -> 0x82 like
  /// the vendor's ReadData().  Never writes flash.
  [[nodiscard]] Status read_page(std::uint16_t index,
                                 std::span<std::byte> destination);

  // -- command-level stream lifecycle --------------------------------------
  /// Enables the image path the way libeinstar does: period 200000 us, trigger
  /// counts 1 IR + 1 RGB, then the requested period.  This sends commands
  /// only; it never submits a bulk transfer, so the pipeline keeps ownership
  /// of endpoint 0x83.  No vendor "ImageStream" opcode was recovered.
  [[nodiscard]] Status start_streaming(std::uint32_t trigger_period_us = 90'000);
  /// Disables triggering (period 200000 us, counts 0/0).  The 1 s settle sleep
  /// that libeinstar performs is left to the caller on purpose: the channel
  /// never sleeps longer than the configured retry delay.
  [[nodiscard]] Status stop_streaming();

 private:
  struct ReplyView {
    const std::byte* data = nullptr;
    std::size_t size = 0;
  };

  [[nodiscard]] static std::uint16_t wire_size(ControlGroup group,
                                               std::uint8_t opcode) noexcept;
  [[nodiscard]] std::uint8_t next_packet_id() noexcept;

  // The *_locked helpers assume mutex_ is already held.
  [[nodiscard]] Status handshake_locked();
  [[nodiscard]] Status identify_locked(DeviceIdentity& identity);
  [[nodiscard]] Result<ReplyView> transact_locked(ControlGroup group,
                                                  std::uint8_t opcode,
                                                  std::span<const std::byte> payload,
                                                  std::uint32_t timeout_ms);
  [[nodiscard]] Result<std::string> string_locked(ControlGroup group,
                                                  std::uint8_t opcode,
                                                  const char* what);
  [[nodiscard]] Result<std::uint32_t> uint_locked(ControlGroup group,
                                                  std::uint8_t opcode,
                                                  std::span<const std::byte> payload,
                                                  std::size_t max_bytes,
                                                  const char* what);
  [[nodiscard]] Status set_locked(ControlGroup group, std::uint8_t opcode,
                                  std::span<const std::byte> payload);
  [[nodiscard]] Result<std::string> serial_locked();
  [[nodiscard]] Result<FirmwareVersion> firmware_locked();
  [[nodiscard]] Result<std::uint16_t> sensor_count_locked();
  [[nodiscard]] Status set_trigger_period_locked(std::uint32_t microseconds);
  [[nodiscard]] Status set_trigger_counts_locked(unsigned mono, unsigned rgb);

  void reset_command_pipes() noexcept;
  void reset_bulk_pipes() noexcept;
  void sleep_before_retry() const noexcept;
  void teardown_locked() noexcept;

  Transport& transport_;
  const ControlConfig config_;
  CommandIo* io_ = nullptr;
  bool open_ = false;
  bool claimed_ = false;
  mutable std::mutex mutex_;
  DeviceIdentity identity_{};
  ControlStats stats_{};
  std::uint8_t next_id_ = 0;
  // Fixed scratch space: the largest request (page read) and the largest reply
  // (page read) fit exactly.  Commands only use the first wire_size bytes.
  std::array<std::byte, kControlPageTransferSize> request_{};
  std::array<std::byte, kControlPageTransferSize> reply_{};
};

}  // namespace einstar
