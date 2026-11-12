// SPDX-License-Identifier: MIT
// Safe capture of synchronised three-camera groups.
//
//   einstar-capture-frames --out DIR --groups N [--backend simulated|libusb]
//       [--record FILE] [--exposure-us [CAM:]US ...] [--gain [CAM:]V ...]
//       [--trigger US|off] [--trigger-counts MONO:RGB] [--faults clean|lossy|hostile]
//       [--seed S] [--queue-depth N] [--timeout-seconds S] [--json]
//       [--show-serial] [--probe-only]
//   einstar-capture-frames --session-dir DIR --control PATH --groups 0
//       [--ring N] [--max-seconds S] [--backend simulated|libusb]
//
// The wizard session mode (docs/calibration/wizard_architecture.md §5) polls a
// command file (start|pause|resume|stop), keeps a bounded ring of the newest N
// groups, reports state/stop_reason/ring counters in the live status JSON and
// turns SIGINT/SIGTERM into the same graceful stop as `stop`.
//
// `simulated` uses SimulatedTransport (deterministic, no hardware).  `libusb`
// uses the real transport, identifies the device over the control channel,
// reads serial / firmware / sensor count / the factory calibration blob, and
// applies only VOLATILE exposure, gain and trigger settings.  It never writes
// flash, never updates firmware and never reboots the device.  The libusb path
// is compiled and linked but has NOT been run against a scanner.
#include <algorithm>
#include <array>
#include <charconv>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <memory>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "capture_core.hpp"

#if defined(EINSTAR_WITH_LIBUSB)
#include "einstar/control_channel.hpp"
#include "einstar/libusb_transport.hpp"
#endif

namespace {

using einstar::ErrorCode;
using einstar::Result;
using einstar::capture::CadenceStats;
using einstar::capture::DeviceInfo;
using einstar::capture::GroupingStats;
using einstar::capture::GroupWriter;
using einstar::capture::ManifestInfo;
using einstar::capture::SimCaptureOptions;
using einstar::capture::SimCaptureReport;

[[maybe_unused]] constexpr std::uint32_t kCalibrationBlobSize = 6568;
[[maybe_unused]] constexpr std::uint32_t kCalibrationPageSize = 4096;

enum class Backend { Simulated, LibUsb };
enum class TriggerMode { Default, Period, Off };

struct Options {
  std::string output_directory;
  std::uint32_t groups = 0;
  Backend backend = Backend::Simulated;
  std::string record_path;
  std::vector<std::string> exposure_specs;
  std::vector<std::string> gain_specs;
  TriggerMode trigger_mode = TriggerMode::Default;
  std::uint32_t trigger_period_us = 0;
  bool trigger_counts_set = false;
  std::uint8_t trigger_mono = 1;
  std::uint8_t trigger_rgb = 1;
  std::string faults = "clean";
  std::uint64_t seed = 1;
  std::uint32_t queue_depth = 8;
  std::uint32_t timeout_seconds = 30;
  bool timeout_seconds_set = false;
  bool json = false;
  bool show_serial = false;
  bool probe_only = false;
  std::optional<unsigned> laser_percent;
  double progress_seconds = 0.0;
  std::string status_file;
  std::vector<std::pair<unsigned, unsigned>> strobe_specs;

  // Wizard session protocol (docs/calibration/wizard_architecture.md §5).
  std::string control_path;
  std::string exposure_control_path;
  std::uint32_t ring_size = 0;
  double max_seconds = 0.0;
  std::string session_dir;
};

bool parse_u64(std::string_view text, std::uint64_t& value) {
  if (text.empty()) {
    return false;
  }
  const char* first = text.data();
  const char* last = text.data() + text.size();
  const auto result = std::from_chars(first, last, value);
  return result.ec == std::errc() && result.ptr == last;
}

bool parse_u32(std::string_view text, std::uint32_t& value) {
  std::uint64_t wide = 0;
  if (!parse_u64(text, wide) || wide > 0xFFFFFFFFULL) {
    return false;
  }
  value = static_cast<std::uint32_t>(wide);
  return true;
}

/// Accepts "VALUE" (camera = 3, meaning all cameras) or "CAM:VALUE".
[[maybe_unused]] bool parse_camera_value(std::string_view text,
                                         std::uint32_t& camera,
                                         std::uint32_t& value) {
  const std::size_t colon = text.find(':');
  if (colon == std::string_view::npos) {
    camera = 3;
    return parse_u32(text, value);
  }
  std::uint32_t parsed_camera = 0;
  if (!parse_u32(text.substr(0, colon), parsed_camera) || parsed_camera > 2 ||
      !parse_u32(text.substr(colon + 1), value)) {
    return false;
  }
  camera = parsed_camera;
  return true;
}

void print_usage() {
  std::printf(
      "usage:\n"
      "  einstar-capture-frames --out DIR --groups N [--backend simulated|libusb]\n"
      "      [--record FILE] [--exposure-us [CAM:]US ...] [--gain [CAM:]V ...]\n"
      "      [--trigger US|off] [--trigger-counts MONO:RGB]\n"
      "      [--faults clean|lossy|hostile] [--seed S] [--queue-depth N]\n"
      "      [--timeout-seconds S] [--json] [--show-serial] [--probe-only]\n"
      "  einstar-capture-frames --session-dir DIR --control PATH --groups 0\n"
      "      [--ring N] [--max-seconds S] [--backend simulated|libusb]\n"
      "\n"
      "  --out DIR            output directory (frame_%%06u_cam%%u.pgm +\n"
      "                       frames_manifest.json)\n"
      "  --session-dir DIR    standard wizard layout: equivalent to\n"
      "                       --out DIR/frames --record DIR/raw.e1r\n"
      "                       --status-file DIR/status.json\n"
      "  --groups N           synchronised three-camera groups to capture;\n"
      "                       with --control, 0 (the default) is unbounded\n"
      "  --backend NAME       simulated (default, deterministic) or libusb\n"
      "  --record FILE        also record the exact raw byte stream (RawRecorder)\n"
      "  --control PATH       poll a command file (start|pause|resume|stop; last\n"
      "                       line wins) and run until told to stop\n"
      "  --ring N             keep only the newest N complete groups in OUT;\n"
      "                       0 = keep everything (default).  The manifest and\n"
      "                       the raw recording still keep every group\n"
      "  --max-seconds S      graceful stop after S seconds of session time\n"
      "  --exposure-us SPEC   volatile exposure in us; SPEC = US or CAM:US\n"
      "                       (repeatable; libusb only)\n"
      "  --gain SPEC          volatile gain; SPEC = VALUE or CAM:VALUE\n"
      "                       (repeatable; libusb only)\n"
      "  --laser PERCENT      volatile speckle-projector brightness 0..100\n"
      "                       (libusb only; 0 after a client called lights off)\n"
      "  --strobe ROUTE:VALUE volatile strobe route 0/1 brightness\n"
      "                       (repeatable; libusb only)\n"
      "  --trigger US         volatile trigger period in us (default 68000 libusb,\n"
      "                       40000 simulated) or 'off' to leave the trigger alone\n"
      "  --trigger-counts A:B volatile mono:rgb frames per trigger (libusb only)\n"
      "  --faults NAME        simulated fault preset: clean, lossy, hostile\n"
      "  --seed S             simulated seed (default 1)\n"
      "  --timeout-seconds S  wall-clock safety deadline (default 30; with\n"
      "                       --control only when explicitly given)\n"
      "  --status-file PATH   rewrite a live status JSON twice per second\n"
      "                       (einstar-live-status-v1; consumed by tools/liveview)\n"
      "  --progress SECONDS   print live pipeline counters to stderr\n"
      "  --json               print a machine-readable summary on stdout\n"
      "  --show-serial        do not redact the device serial in logs/manifest\n"
      "  --probe-only         libusb: read metadata + calibration, change nothing\n"
      "\n"
      "SIGINT/SIGTERM perform the same graceful stop as the 'stop' command: the\n"
      "trigger is stopped, the illuminator is set to 0 and the recording and\n"
      "manifest are finalised.  Every deliberate stop exits with status 0.\n"
      "\n"
      "Safety: only volatile exposure/gain/trigger commands are sent.  This tool\n"
      "cannot flash firmware, write persistent calibration or reboot the scanner.\n");
}

int usage_error(const char* message) {
  std::fprintf(stderr, "%s\n", message);
  print_usage();
  return 2;
}

[[maybe_unused]] std::string redact_serial(const std::string& serial) {
  if (serial.empty()) {
    return "unknown";
  }
  if (serial.size() <= 4) {
    return std::string(serial.size(), '*');
  }
  if (serial.size() <= 8) {
    return serial.substr(0, 2) + std::string(serial.size() - 4, '*') +
           serial.substr(serial.size() - 2);
  }
  return serial.substr(0, 4) + std::string(serial.size() - 8, '*') +
         serial.substr(serial.size() - 4);
}

[[maybe_unused]] double megabytes_per_second(std::uint64_t bytes,
                                             double seconds) {
  return seconds > 0.0 ? static_cast<double>(bytes) / seconds / 1e6 : 0.0;
}

[[maybe_unused]] std::uint64_t string_digest(std::string_view text) {
  return einstar::capture::fnv1a64(std::span<const std::uint8_t>(
      reinterpret_cast<const std::uint8_t*>(text.data()), text.size()));
}

[[maybe_unused]] std::string join_path(const std::string& directory,
                                       std::string_view name) {
  if (!directory.empty() && directory.back() == '/') {
    return directory + std::string(name);
  }
  return directory + "/" + std::string(name);
}

void print_grouping(const ManifestInfo& manifest, const GroupingStats& grouping,
                    const CadenceStats& cadence,
                    const std::string& manifest_path) {
  std::printf("  groups         : %llu requested, %llu written\n",
              static_cast<unsigned long long>(manifest.requested_groups),
              static_cast<unsigned long long>(grouping.groups_written));
  std::printf("  cameras/group  : 3 (0=right IR, 1=left IR, 2=colour)\n");
  std::printf("  incomplete     : %llu group(s), %llu missing camera frame(s)\n",
              static_cast<unsigned long long>(grouping.groups_incomplete),
              static_cast<unsigned long long>(grouping.missing_camera_frames));
  std::printf("  absent groups  : %llu\n",
              static_cast<unsigned long long>(grouping.groups_absent));
  std::printf("  duplicates     : %llu duplicate frame(s), %llu late frame(s)\n",
              static_cast<unsigned long long>(grouping.duplicate_frames),
              static_cast<unsigned long long>(grouping.late_frames));
  std::printf("  frames         : %llu written, %llu completed, %llu dropped, "
              "%llu invalid\n",
              static_cast<unsigned long long>(grouping.frames_written),
              static_cast<unsigned long long>(manifest.metrics.frames_completed),
              static_cast<unsigned long long>(manifest.metrics.frames_dropped),
              static_cast<unsigned long long>(grouping.frames_invalid));
  std::printf("  bytes          : %llu received, %llu written\n",
              static_cast<unsigned long long>(manifest.metrics.bytes_received),
              static_cast<unsigned long long>(grouping.bytes_written));
  std::printf("  duration       : %.3f s wall%s\n", manifest.metrics.wall_seconds,
              manifest.metrics.virtual_seconds > 0.0
                  ? " (+ virtual clock for the simulated link)"
                  : "");
  if (manifest.metrics.virtual_seconds > 0.0) {
    std::printf("  virtual time   : %.3f s\n", manifest.metrics.virtual_seconds);
  }
  std::printf("  throughput     : %.1f MB/s wall, %.1f MB/s virtual\n",
              manifest.throughput_mbps, manifest.virtual_throughput_mbps);
  std::printf("  cadence        : %.2f fps, %.3f ms mean (%.3f..%.3f ms), "
              "%llu interval(s)\n",
              cadence.fps, static_cast<double>(cadence.mean_ns) / 1e6,
              static_cast<double>(cadence.min_ns) / 1e6,
              static_cast<double>(cadence.max_ns) / 1e6,
              static_cast<unsigned long long>(cadence.intervals));
  std::printf("  loss           : %llu sequence gap(s), %llu partial timeout(s), "
              "%llu queue drop(s)\n",
              static_cast<unsigned long long>(manifest.metrics.sequence_gaps),
              static_cast<unsigned long long>(manifest.metrics.partial_timeouts),
              static_cast<unsigned long long>(manifest.metrics.queue_drops));
  std::printf("  result         : %s\n",
              einstar::capture::grouping_complete(grouping) ? "complete" : "LOSSY");
  std::printf("  manifest       : %s\n", manifest_path.c_str());
  if (!manifest.recorded_path.empty()) {
    std::printf("  recording      : %s (%llu bytes, %llu transfers, "
                "digest 0x%016llX)\n",
                manifest.recorded_path.c_str(),
                static_cast<unsigned long long>(manifest.recorded_bytes),
                static_cast<unsigned long long>(manifest.recorded_transfers),
                static_cast<unsigned long long>(manifest.recorded_digest));
  }
}

void print_json_summary(const ManifestInfo& manifest, const GroupingStats& grouping,
                        const CadenceStats& cadence,
                        const std::string& manifest_path) {
  const auto quote = [](std::string_view text) {
    return einstar::capture::json_quote(text);
  };
  std::printf("{");
  std::printf("\"schema\": \"einstar-capture-summary-v1\", ");
  std::printf("\"tool\": %s, ", quote(manifest.tool).c_str());
  std::printf("\"backend\": %s, ", quote(manifest.backend).c_str());
  std::printf("\"simulated\": %s, ", manifest.simulated ? "true" : "false");
  std::printf("\"live\": %s, ", manifest.live ? "true" : "false");
  std::printf("\"output_directory\": %s, ",
              quote(manifest.output_directory).c_str());
  std::printf("\"requested_groups\": %u, ", manifest.requested_groups);
  std::printf("\"groups_written\": %llu, ",
              static_cast<unsigned long long>(grouping.groups_written));
  std::printf("\"groups_incomplete\": %llu, ",
              static_cast<unsigned long long>(grouping.groups_incomplete));
  std::printf("\"groups_absent\": %llu, ",
              static_cast<unsigned long long>(grouping.groups_absent));
  std::printf("\"duplicate_frames\": %llu, ",
              static_cast<unsigned long long>(grouping.duplicate_frames));
  std::printf("\"frames_completed\": %llu, ",
              static_cast<unsigned long long>(manifest.metrics.frames_completed));
  std::printf("\"frames_dropped\": %llu, ",
              static_cast<unsigned long long>(manifest.metrics.frames_dropped));
  std::printf("\"bytes_received\": %llu, ",
              static_cast<unsigned long long>(manifest.metrics.bytes_received));
  std::printf("\"bytes_written\": %llu, ",
              static_cast<unsigned long long>(grouping.bytes_written));
  std::printf("\"duration_s\": %.6f, ", manifest.metrics.wall_seconds);
  std::printf("\"virtual_duration_s\": %.6f, ",
              manifest.metrics.virtual_seconds);
  std::printf("\"throughput_mbps\": %.3f, ", manifest.throughput_mbps);
  std::printf("\"virtual_throughput_mbps\": %.3f, ",
              manifest.virtual_throughput_mbps);
  std::printf("\"fps\": %.3f, ", cadence.fps);
  std::printf("\"complete\": %s, ",
              einstar::capture::grouping_complete(grouping) ? "true" : "false");
  std::printf("\"manifest\": %s, ", quote(manifest_path).c_str());
  std::printf("\"recording\": %s, ",
              manifest.recorded_path.empty()
                  ? "\"\""
                  : quote(manifest.recorded_path).c_str());
  std::printf("\"device_serial\": %s, ", quote(manifest.device.serial).c_str());
  std::printf("\"serial_redacted\": %s, ",
              manifest.device.serial_redacted ? "true" : "false");
  std::printf("\"firmware\": %s, ", quote(manifest.device.firmware).c_str());
  std::printf("\"calibration_bytes\": %llu, ",
              static_cast<unsigned long long>(manifest.device.calibration_bytes));
  std::printf("\"calibration_fnv1a64\": %llu, ",
              static_cast<unsigned long long>(manifest.device.calibration_fnv1a64));
  std::printf("\"volatile_settings_applied\": %s",
              manifest.device.settings_applied ? "true" : "false");
  std::printf("}\n");
}

// ---------------------------------------------------------------------------
// Simulated backend
// ---------------------------------------------------------------------------

int run_simulated(const Options& options) {
  if (options.trigger_mode == TriggerMode::Off) {
    return usage_error(
        "--trigger off requires --backend libusb; the simulator always emits "
        "one frame per camera per trigger");
  }
  if (!options.exposure_specs.empty() || !options.gain_specs.empty() ||
      options.trigger_counts_set) {
    return usage_error(
        "--exposure-us/--gain/--trigger-counts require --backend libusb: the "
        "simulator has no command channel");
  }
  if (options.probe_only) {
    std::printf("simulated device (no hardware)\n");
    std::printf("  vendor/product : 3267:0003\n");
    std::printf("  serial         : SIMULATED\n");
    std::printf("  firmware       : simulated\n");
    std::printf("  sensor count   : 3\n");
    std::printf("  cameras        : 3 (0=right IR, 1=left IR, 2=colour)\n");
    std::printf("  probe-only     : nothing was captured or changed\n");
    return 0;
  }
  if (options.output_directory.empty()) {
    return usage_error("--out or --session-dir is required");
  }
  if (options.groups == 0 && options.control_path.empty() &&
      options.max_seconds <= 0.0) {
    return usage_error(
        "--groups must be at least 1 unless --control or --max-seconds makes "
        "the run unbounded");
  }

  SimCaptureOptions capture;
  capture.output_directory = options.output_directory;
  capture.groups = options.groups;
  capture.seed = options.seed;
  capture.faults = options.faults;
  capture.recording_path = options.record_path;
  capture.queue_depth = options.queue_depth;
  capture.trigger_period_us =
      options.trigger_mode == TriggerMode::Period ? options.trigger_period_us
                                                  : 40'000;
  capture.partial_frame_timeout_ns = 500'000'000;
  capture.control_path = options.control_path;
  capture.status_file = options.status_file;
  capture.volatility_path = options.exposure_control_path;
  capture.ring_size = options.ring_size;
  capture.max_seconds = options.max_seconds;
  capture.timeout_seconds = options.timeout_seconds;
  capture.timeout_seconds_set =
      options.timeout_seconds_set || options.control_path.empty();
  capture.progress_seconds = options.progress_seconds;

  auto report = einstar::capture::capture_simulated(capture);
  if (!report) {
    std::fprintf(stderr, "simulated capture failed: %s\n",
                 report.error().message().c_str());
    return 1;
  }
  const SimCaptureReport& result = *report;
  std::printf("capture (simulated, deterministic)\n");
  std::printf("  out dir        : %s\n", result.manifest.output_directory.c_str());
  std::printf("  seed / faults  : %llu / %s\n",
              static_cast<unsigned long long>(result.manifest.simulation_seed),
              result.manifest.simulation_faults.c_str());
  std::printf("  trigger period : %u us\n",
              result.manifest.device.trigger_period_us);
  std::printf("  session        : %s, stop reason %s, ring %u (%llu on disk, "
              "%llu evicted)\n",
              std::string(einstar::capture::session_state_name(result.state))
                  .c_str(),
              result.stop_reason == einstar::capture::StopReason::None
                  ? "none"
                  : std::string(
                        einstar::capture::stop_reason_name(result.stop_reason))
                        .c_str(),
              result.ring_size,
              static_cast<unsigned long long>(result.groups_on_disk),
              static_cast<unsigned long long>(result.groups_evicted));
  print_grouping(result.manifest, result.grouping, result.cadence,
                 result.manifest_path);
  if (options.json) {
    print_json_summary(result.manifest, result.grouping, result.cadence,
                       result.manifest_path);
  }
  if (result.stop_reason == einstar::capture::StopReason::SignalInt ||
      result.stop_reason == einstar::capture::StopReason::SignalTerm) {
    std::printf("stopped by signal %s\n",
                result.stop_reason == einstar::capture::StopReason::SignalInt
                    ? "SIGINT"
                    : "SIGTERM");
  }
  return result.stop_reason == einstar::capture::StopReason::Error ? 1 : 0;
}

// ---------------------------------------------------------------------------
// libusb backend (compiled but untested: no scanner is attached)
// ---------------------------------------------------------------------------

#if defined(EINSTAR_WITH_LIBUSB)

using einstar::AcquisitionPipeline;
using einstar::ControlChannel;
using einstar::ControlConfig;
using einstar::CopyMode;
using einstar::Frame;
using einstar::LibUsbTransport;
using einstar::PipelineConfig;
using einstar::PipelineStats;
using einstar::Status;
using einstar::capture::CaptureSessionDriver;
using einstar::capture::CaptureSessionOptions;
using einstar::capture::VolatileSettings;
using einstar::capture::SessionState;
using einstar::capture::StopReason;

void print_metadata(const einstar::DeviceIdentity& identity, bool show_serial) {
  std::printf("device\n");
  std::printf("  vendor/product : 3267:0003\n");
  const std::string serial =
      show_serial ? identity.serial : redact_serial(identity.serial);
  std::printf("  serial         : %s%s\n", serial.c_str(),
              show_serial ? "" : " (redacted; --show-serial prints it)");
  std::printf("  firmware       : %s\n",
              identity.firmware.raw.empty() ? "(not reported)"
                                            : identity.firmware.raw.c_str());
  if (identity.firmware.parsed()) {
    std::printf("  parsed         : FX3 %s, FPGA %s, locale %s\n",
                identity.firmware.fx3_version.c_str(),
                identity.firmware.fpga_version.c_str(),
                identity.firmware.locale.c_str());
  }
  std::printf("  sensor count   : %u\n", identity.sensor_count);
}

Result<std::vector<std::uint8_t>> read_calibration_blob(ControlChannel& channel) {
  std::vector<std::uint8_t> blob(kCalibrationBlobSize);
  std::array<std::byte, kCalibrationPageSize> page{};
  const std::uint32_t pages =
      (kCalibrationBlobSize + kCalibrationPageSize - 1) / kCalibrationPageSize;
  for (std::uint32_t index = 0; index < pages; ++index) {
    Status status = channel.read_page(static_cast<std::uint16_t>(index), page);
    if (!status.ok()) {
      return fail(ErrorCode::Protocol,
                  "read-only calibration page read " + std::to_string(index) +
                      " failed: " + status.message() +
                      " (the scanner may be held by EXStar or not answering "
                      "page reads)");
    }
    const std::size_t offset =
        static_cast<std::size_t>(index) * kCalibrationPageSize;
    const std::size_t take = std::min<std::size_t>(
        kCalibrationPageSize, kCalibrationBlobSize - offset);
    std::memcpy(blob.data() + offset, page.data(), take);
  }
  return blob;
}

Status write_calibration_file(const std::string& directory,
                              const std::vector<std::uint8_t>& blob) {
  const std::string path = join_path(directory, "calibration.bin");
  std::ofstream file(path, std::ios::binary | std::ios::trunc);
  if (!file) {
    return Status::error(ErrorCode::IoError,
                         "cannot open " + path + " for writing");
  }
  file.write(reinterpret_cast<const char*>(blob.data()),
             static_cast<std::streamsize>(blob.size()));
  file.flush();
  if (!file.good()) {
    return Status::error(ErrorCode::IoError, "calibration write failed: " + path);
  }
  return Status::success();
}

/// Reads everything the capture tools need from a live device: the identity
/// (already read by open()), the informational getters, and the factory
/// calibration blob.  Read-only apart from optionally writing the local copy
/// of the blob into the output directory.
Result<DeviceInfo> read_device_metadata(
    ControlChannel& channel, const einstar::DeviceIdentity& identity,
    bool show_serial, const std::optional<std::string>& directory) {
  DeviceInfo device;
  device.simulated = false;
  device.identified = true;
  device.vendor_id = "3267";
  device.product_id = "0003";
  device.serial = show_serial ? identity.serial : redact_serial(identity.serial);
  device.serial_redacted = !show_serial;
  device.serial_fnv1a64 = string_digest(identity.serial);
  device.firmware = identity.firmware.raw;
  device.fx3_version = identity.firmware.fx3_version;
  device.fpga_version = identity.firmware.fpga_version;
  device.locale = identity.firmware.locale;
  device.sensor_count = identity.sensor_count;

  // Informational getters: failures are reported, never fatal.
  for (unsigned camera = 0; camera < 3; ++camera) {
    if (auto exposure = channel.exposure_us(camera)) {
      device.exposure_us[camera] = *exposure;
    }
    if (auto gain = channel.gain(camera)) {
      device.gain[camera] = *gain;
    }
    if (auto width = channel.max_width(camera)) {
      device.max_width[camera] = *width;
    }
    if (auto height = channel.max_height(camera)) {
      device.max_height[camera] = *height;
    }
  }
  if (auto counts = channel.trigger_counts()) {
    device.trigger_mono = counts->mono;
    device.trigger_rgb = counts->rgb;
  }
  if (auto period = channel.trigger_period_us()) {
    device.trigger_period_us = *period;
  }
  // Illuminator state: best effort, and the most common cause of a dark IR
  // stream after a client that calls lights_off() was killed.
  if (auto laser = channel.laser_brightness()) {
    device.laser_percent = *laser;
  }
  for (unsigned route = 0; route < 2; ++route) {
    if (auto strobe = channel.strobe_brightness(route)) {
      device.strobe[route] = *strobe;
    }
  }
  if (auto temperature = channel.temperature_raw()) {
    device.temperature_raw = static_cast<std::uint16_t>(*temperature);
    device.temperature_read = true;
  }

  auto blob = read_calibration_blob(channel);
  if (!blob) {
    return fail(blob.error());
  }
  device.calibration_bytes = blob->size();
  device.calibration_fnv1a64 = einstar::capture::fnv1a64(*blob);
  if (directory) {
    if (Status status = write_calibration_file(*directory, *blob);
        !status.ok()) {
      return fail(status);
    }
    device.calibration_file = "calibration.bin";
  }
  std::printf("  calibration    : %zu bytes (read-only pages 0..%u), "
              "fnv1a64 0x%016llX%s\n",
              blob->size(),
              static_cast<unsigned>((kCalibrationBlobSize - 1) /
                                    kCalibrationPageSize),
              static_cast<unsigned long long>(device.calibration_fnv1a64),
              device.calibration_file.empty()
                  ? ""
                  : (", written to " + device.calibration_file).c_str());
  return device;
}

/// Applies one volatile exposure value, validating it against the device's
/// reported range when that query is supported.
Status apply_exposure(ControlChannel& channel, unsigned camera, std::uint32_t us) {
  auto range = channel.exposure_range(camera);
  if (range && (us < range->minimum_us || us > range->maximum_us)) {
    return Status::error(
        ErrorCode::InvalidArgument,
        "exposure " + std::to_string(us) + " us for camera " +
            std::to_string(camera) + " is outside the device range " +
            std::to_string(range->minimum_us) + ".." +
            std::to_string(range->maximum_us) + " us");
  }
  return channel.set_exposure_us(camera, us);
}

Status apply_gain(ControlChannel& channel, unsigned camera, std::uint16_t value) {
  auto range = channel.gain_range(camera);
  if (range && (value < range->minimum || value > range->maximum)) {
    return Status::error(
        ErrorCode::InvalidArgument,
        "gain " + std::to_string(value) + " for camera " +
            std::to_string(camera) + " is outside the device range " +
            std::to_string(range->minimum) + ".." +
            std::to_string(range->maximum));
  }
  return channel.set_gain(camera, value);
}

/// Read-only probe: open, identify, read metadata and calibration, close.
int run_libusb_probe(const Options& options,
                     const std::optional<std::string>& directory) {
  LibUsbTransport transport{LibUsbTransport::Config{}};
  const Status open_status = transport.open();
  std::optional<std::string> prepared_directory = directory;
  if (open_status.ok() && !prepared_directory && !options.output_directory.empty()) {
    auto prepared =
        einstar::capture::prepare_output_directory(options.output_directory);
    if (!prepared) {
      std::fprintf(stderr, "output directory: %s\n",
                   prepared.error().message().c_str());
      transport.close();
      return 1;
    }
    prepared_directory = *prepared;
  }
  if (!open_status.ok()) {
    std::fprintf(stderr,
                 "cannot open the scanner: %s\n  hint: close EXStar, check the "
                 "cable/power and the udev rule (see "
                 "docs/reconstruction/acquisition.md)\n",
                 open_status.message().c_str());
    return 1;
  }
  std::printf("device\n");
  std::printf("  opened         : 3267:0003 via libusb\n");
  for (const std::string& note : transport.quirk_notes()) {
    std::printf("  descriptor     : %s\n", note.c_str());
  }
  ControlConfig probe_config;
  ControlChannel channel(transport, probe_config);
  auto identity = channel.open();
  if (!identity) {
    std::fprintf(stderr, "control channel failed: %s\n",
                 identity.error().message().c_str());
    channel.close();
    transport.close();
    return 1;
  }
  print_metadata(*identity, options.show_serial);
  auto device =
      read_device_metadata(channel, *identity, options.show_serial,
                           prepared_directory);
  if (!device) {
    std::fprintf(stderr, "metadata read failed: %s\n",
                 device.error().message().c_str());
    channel.close();
    transport.close();
    return 1;
  }
  const DeviceInfo& probe = *device;
  for (unsigned camera = 0; camera < 3; ++camera) {
    std::printf("  camera %u       : %ux%u, exposure %u us, gain %u\n", camera,
                static_cast<unsigned>(probe.max_width[camera]),
                static_cast<unsigned>(probe.max_height[camera]),
                probe.exposure_us[camera],
                static_cast<unsigned>(probe.gain[camera]));
  }
  std::printf("  trigger        : period %u us, counts mono %u rgb %u\n",
              probe.trigger_period_us, static_cast<unsigned>(probe.trigger_mono),
              static_cast<unsigned>(probe.trigger_rgb));
  std::printf("  illuminator    : laser %s, strobe route0 %s, route1 %s\n",
              probe.laser_percent
                  ? (std::to_string(*probe.laser_percent) + " %").c_str()
                  : "not reported",
              probe.strobe[0] ? std::to_string(*probe.strobe[0]).c_str()
                              : "not reported",
              probe.strobe[1] ? std::to_string(*probe.strobe[1]).c_str()
                              : "not reported");
  std::printf("  temperature    : %s\n",
              probe.temperature_read
                  ? (std::to_string(probe.temperature_raw) + " raw").c_str()
                  : "not reported");
  std::printf("  probe-only     : no streaming, no volatile settings changed\n");
  channel.close();
  transport.close();
  return 0;
}

/// Leave the projector/LEDs dark.  Volatile only (no flash write), and the
/// safe state for a device that will be left unattended: the vendor's own
/// `lights_off()` does the same. Failures are reported and returned.
Status turn_illuminator_off(ControlChannel& channel, DeviceInfo* device = nullptr) {
  bool ok = true;
  // Try every off command, then read back; never infer zero from a write.
  if (device != nullptr) {
    device->laser_percent.reset();
    for (auto& value : device->strobe) { value.reset(); }
  }
  if (const Status status = channel.set_laser_brightness(0); !status.ok()) {
    ok = false;
    std::fprintf(stderr, "warning: laser off failed: %s\n", status.message().c_str());
  }
  for (unsigned route = 0; route < 2; ++route) {
    if (const Status status = channel.set_strobe_brightness(route, 0); !status.ok()) {
      ok = false;
      std::fprintf(stderr, "warning: strobe %u off failed: %s\n", route,
                   status.message().c_str());
    }
  }
  return ok ? Status::success()
            : Status::error(ErrorCode::TransportError, "illuminator off command failed");
}

Status verify_illuminator_off(ControlChannel& channel, DeviceInfo* device = nullptr) {
  bool ok = true;
  const auto laser = channel.laser_brightness();
  if (device != nullptr && laser) { device->laser_percent = *laser; }
  ok = laser && *laser == 0 && ok;
  for (unsigned route = 0; route < 2; ++route) {
    const auto strobe = channel.strobe_brightness(route);
    if (device != nullptr && strobe) { device->strobe[route] = *strobe; }
    ok = strobe && *strobe == 0 && ok;
  }
  if (!ok) {
    std::fprintf(stderr, "warning: illuminator off UNVERIFIED (nonzero/missing readback)\n");
    return Status::error(ErrorCode::TransportError, "illuminator off unverified");
  }
  std::printf("  illuminator    : off (volatile readback; optical state unverified)\n");
  return Status::success();
}

// Covers setup errors and unwinding before the session driver takes over.
struct SetupSafetyCleanup {
  ControlChannel& channel;
  AcquisitionPipeline& pipeline;
  bool armed = true;
  ~SetupSafetyCleanup() {
    if (!armed) { return; }
    (void)turn_illuminator_off(channel);
    if (const Status stopped = channel.stop_streaming(); !stopped.ok()) {
      (void)channel.set_trigger_counts(0, 0);
      std::fprintf(stderr, "warning: setup trigger cleanup UNVERIFIED: %s\n",
                   stopped.message().c_str());
    }
    (void)verify_illuminator_off(channel);
    channel.close();
    (void)pipeline.stop();
  }
};

/// Live session driver: owns the device-side pause/resume/stop transitions.
/// The pipeline and control channel are opened before the session starts; the
/// trigger is started by start() and always stopped by stop().
class LiveSessionDriver final : public CaptureSessionDriver {
 public:
  LiveSessionDriver(AcquisitionPipeline& pipeline, ControlChannel& channel,
                    const Options& options, DeviceInfo device,
                    std::uint32_t trigger_period_us)
      : pipeline_(pipeline),
        channel_(channel),
        options_(options),
        device_(std::move(device)),
        trigger_period_us_(trigger_period_us),
        trigger_enabled_(options.trigger_mode != TriggerMode::Off) {
    // Values to restore on resume: the capture arguments override the
    // illuminator state observed before the trigger was started.
    if (options.laser_percent) {
      laser_on_ = *options.laser_percent;
    } else {
      laser_on_ = device_.laser_percent;
    }
    for (unsigned route = 0; route < 2; ++route) {
      strobe_on_[route] = device_.strobe[route];
    }
    for (const auto& [route, value] : options.strobe_specs) {
      if (route < 2) {
        strobe_on_[route] = value;
      }
    }
  }

  ~LiveSessionDriver() override { (void)stop(); }

  Status start() override {
    if (!trigger_enabled_ || streaming_) {
      return Status::success();
    }
    const Status streamed = channel_.start_streaming(trigger_period_us_);
    if (!streamed.ok()) {
      return Status::error(ErrorCode::TransportError,
                           "volatile start_streaming(" +
                               std::to_string(trigger_period_us_) +
                               " us) was refused: " + streamed.message());
    }
    streaming_ = true;
    device_.trigger_period_us = trigger_period_us_;
    device_.trigger_mono = 1;
    device_.trigger_rgb = 1;
    std::printf("  trigger        : %u us, 1 IR + 1 colour per trigger "
                "(volatile)\n",
                trigger_period_us_);
    if (Status counts = apply_trigger_counts(); !counts.ok()) {
      (void)channel_.stop_streaming();
      streaming_ = false;
      return counts;
    }
    return Status::success();
  }

  Status poll(int timeout_ms) override {
    (void)pipeline_.poll(timeout_ms);
    return Status::success();
  }

  bool try_pop_frame(Frame& frame) override {
    return pipeline_.try_pop_frame(frame);
  }

  [[nodiscard]] PipelineStats stats() const override {
    return pipeline_.stats();
  }

  [[nodiscard]] const DeviceInfo& device() const override { return device_; }

  Status apply_volatile(const VolatileSettings& settings) override {
    bool ok = true;
    std::string error;
    if (settings.has_exposure) {
      for (unsigned camera = 0; camera < 3; ++camera) {
        const Status status =
            channel_.set_exposure_us(camera, settings.exposure_us[camera]);
        if (!status.ok()) {
          ok = false;
          error = "exposure camera " + std::to_string(camera) + ": " +
                  status.message();
          break;
        }
        device_.exposure_us[camera] = settings.exposure_us[camera];
      }
    }
    if (ok && settings.has_gain) {
      for (unsigned camera = 0; camera < 3; ++camera) {
        const Status status = channel_.set_gain(camera, settings.gain[camera]);
        if (!status.ok()) {
          ok = false;
          error = "gain camera " + std::to_string(camera) + ": " +
                  status.message();
          break;
        }
        device_.gain[camera] = settings.gain[camera];
      }
    }
    if (ok && settings.laser_percent) {
      const Status status = channel_.set_laser_brightness(*settings.laser_percent);
      if (!status.ok()) {
        ok = false;
        error = "laser: " + status.message();
      } else {
        device_.laser_percent = *settings.laser_percent;
      }
    }
    return ok ? Status::success()
              : Status::error(ErrorCode::TransportError,
                              "volatile settings: " + error);
  }

  Status pause() override {
    const Status off = turn_illuminator_off(channel_, &device_);
    const Status stopped = channel_.stop_streaming();
    streaming_ = false;
    if (!stopped.ok()) {
      (void)channel_.set_trigger_counts(0, 0);
      return Status::error(ErrorCode::TransportError,
                           "pause could not stop trigger: " + stopped.message());
    }
    const Status readback = verify_illuminator_off(channel_, &device_);
    return off.ok() ? readback : off;
  }

  Status resume() override {
    bool ok = true;
    if (laser_on_) {
      const Status status = channel_.set_laser_brightness(*laser_on_);
      if (!status.ok()) {
        ok = false;
        std::fprintf(stderr, "warning: laser restore failed: %s\n",
                     status.message().c_str());
      } else {
        device_.laser_percent = *laser_on_;
      }
    }
    for (unsigned route = 0; route < 2; ++route) {
      if (!strobe_on_[route]) {
        continue;
      }
      const Status status = channel_.set_strobe_brightness(
          route, static_cast<std::uint16_t>(*strobe_on_[route]));
      if (!status.ok()) {
        ok = false;
        std::fprintf(stderr, "warning: strobe %u restore failed: %s\n", route,
                     status.message().c_str());
      } else {
        device_.strobe[route] = *strobe_on_[route];
      }
    }
    if (!trigger_enabled_) {
      return ok ? Status::success()
                : Status::error(ErrorCode::TransportError,
                                "resume could not restore the illuminator");
    }
    const Status streamed = channel_.start_streaming(trigger_period_us_);
    if (!streamed.ok()) {
      return Status::error(ErrorCode::TransportError,
                           "resume could not restart the trigger: " +
                               streamed.message());
    }
    streaming_ = true;
    device_.trigger_period_us = trigger_period_us_;
    device_.trigger_mono = 1;
    device_.trigger_rgb = 1;
    // start_streaming() resets the counts to 1/1, so re-apply an explicit
    // --trigger-counts value after every pause/resume cycle.
    if (Status counts = apply_trigger_counts(); !counts.ok()) {
      (void)channel_.stop_streaming();
      streaming_ = false;
      return counts;
    }
    if (!ok) {
      return Status::error(ErrorCode::TransportError,
                           "resume could not restore the illuminator");
    }
    return Status::success();
  }

  Status pump_paused(int timeout_ms) override {
    // Keep the pipeline alive: in-flight USB transfers are reaped and the
    // recording stays open, but the device is not triggering.
    (void)pipeline_.poll(timeout_ms);
    return Status::success();
  }

  Status stop() override {
    if (stopped_) { return stop_status_; }
    stopped_ = true;
    device_.cleanup_attempted = true;
    // Darkness first, including partial-start and trigger-off sessions.
    const Status off = turn_illuminator_off(channel_, &device_);
    const Status stopped = channel_.stop_streaming();
    // stop_streaming returns early if its period write fails.
    if (!stopped.ok()) { (void)channel_.set_trigger_counts(0, 0); }
    streaming_ = false;
    const Status dark = verify_illuminator_off(channel_, &device_);
    const auto counts = channel_.trigger_counts();
    if (counts) {
      device_.trigger_mono = counts->mono;
      device_.trigger_rgb = counts->rgb;
    }
    device_.cleanup_verified = off.ok() && dark.ok() && counts &&
                              counts->mono == 0 && counts->rgb == 0;
    if (!device_.cleanup_verified) {
      std::fprintf(stderr, "warning: device cleanup UNVERIFIED\n");
    }
    channel_.close();
    const Status pipeline_stopped = pipeline_.stop();
    if (!device_.cleanup_verified) {
      stop_status_ = Status::error(ErrorCode::TransportError, "device cleanup unverified");
    } else if (!stopped.ok()) {
      stop_status_ = stopped;
    } else {
      stop_status_ = pipeline_stopped;
    }
    return stop_status_;
  }

  [[nodiscard]] double virtual_duration_seconds() const override { return 0.0; }

 private:
  Status stop_status_ = Status::success();
  Status apply_trigger_counts() {
    if (!options_.trigger_counts_set) {
      return Status::success();
    }
    const Status counts = channel_.set_trigger_counts(options_.trigger_mono,
                                                      options_.trigger_rgb);
    if (!counts.ok()) {
      return Status::error(
          ErrorCode::TransportError,
          "volatile trigger counts were refused: " + counts.message());
    }
    device_.trigger_mono = options_.trigger_mono;
    device_.trigger_rgb = options_.trigger_rgb;
    std::printf("  trigger counts : mono %u, rgb %u (volatile)\n",
                static_cast<unsigned>(options_.trigger_mono),
                static_cast<unsigned>(options_.trigger_rgb));
    return Status::success();
  }

  AcquisitionPipeline& pipeline_;
  ControlChannel& channel_;
  const Options& options_;
  DeviceInfo device_;
  std::uint32_t trigger_period_us_ = 0;
  bool trigger_enabled_ = true;
  bool streaming_ = false;
  bool stopped_ = false;
  std::optional<std::uint32_t> laser_on_;
  std::array<std::optional<std::uint32_t>, 2> strobe_on_{};
};

int run_libusb(const Options& options) {
  if (!options.probe_only && options.groups == 0 &&
      options.control_path.empty() && options.max_seconds <= 0.0) {
    return usage_error(
        "--groups must be at least 1 unless --control or --max-seconds makes "
        "the run unbounded");
  }
  if (!options.probe_only && options.output_directory.empty()) {
    return usage_error("--out or --session-dir is required");
  }

  std::optional<std::string> directory;

  if (options.probe_only) {
    return run_libusb_probe(options, std::nullopt);
  }

  auto transport = std::make_unique<LibUsbTransport>(LibUsbTransport::Config{});
  LibUsbTransport* usb = transport.get();

  PipelineConfig config;
  config.queue_depth = options.queue_depth == 0 ? 8 : options.queue_depth;
  config.copy_mode = CopyMode::Copy;
  config.frame_queue_capacity = 32;
  config.partial_frame_timeout_ns = 500'000'000;
  if (!options.record_path.empty()) {
    config.enable_recording = true;
    config.recording_path = options.record_path;
    config.recording_metadata =
        "backend=libusb;groups=" + std::to_string(options.groups);
  }
  auto pipeline =
      std::make_unique<AcquisitionPipeline>(std::move(transport), config);
  const Status start_status = pipeline->start();
  if (!start_status.ok()) {
    std::fprintf(stderr,
                 "cannot open the scanner: %s\n  hint: close EXStar, check the "
                 "cable/power and the udev rule (see "
                 "docs/reconstruction/acquisition.md)\n",
                 start_status.message().c_str());
    return 1;
  }

  std::printf("device\n");
  std::printf("  opened         : 3267:0003 via libusb\n");
  for (const std::string& note : usb->quirk_notes()) {
    std::printf("  descriptor     : %s\n", note.c_str());
  }
  if (!options.output_directory.empty()) {
    auto prepared =
        einstar::capture::prepare_output_directory(options.output_directory);
    if (!prepared) {
      std::fprintf(stderr, "output directory: %s\n",
                   prepared.error().message().c_str());
      (void)pipeline->stop();
      return 1;
    }
    directory = *prepared;
  }
  const std::string& output_directory = *directory;

  ControlConfig control_config;
  control_config.claim_interface = false;  // the pipeline already owns the claim
  auto channel = std::make_unique<ControlChannel>(*usb, control_config);
  auto identity = channel->open();
  if (!identity) {
    std::fprintf(stderr, "control channel failed: %s\n",
                 identity.error().message().c_str());
    channel->close();
    (void)pipeline->stop();
    return 1;
  }
  SetupSafetyCleanup setup_cleanup{*channel, *pipeline};
  print_metadata(*identity, options.show_serial);

  auto device_result = read_device_metadata(*channel, *identity,
                                            options.show_serial, directory);
  if (!device_result) {
    std::fprintf(stderr, "metadata read failed: %s\n",
                 device_result.error().message().c_str());
    return 1;
  }
  DeviceInfo device = *device_result;

  // ---- volatile settings ---------------------------------------------------
  bool applied = false;
  std::array<std::optional<std::uint32_t>, 3> exposures;
  for (const std::string& spec : options.exposure_specs) {
    std::uint32_t camera = 0;
    std::uint32_t value = 0;
    if (!parse_camera_value(spec, camera, value)) {
      std::fprintf(stderr, "invalid --exposure-us value: %s\n", spec.c_str());

      return 2;
    }
    for (unsigned index = 0; index < 3; ++index) {
      if (camera == 3 || camera == index) {
        exposures[index] = value;
      }
    }
  }
  for (unsigned camera = 0; camera < 3; ++camera) {
    if (!exposures[camera]) {
      continue;
    }
    const Status status = apply_exposure(*channel, camera, *exposures[camera]);
    if (!status.ok()) {
      std::fprintf(stderr,
                   "volatile exposure for camera %u was refused: %s\n"
                   "aborting without changing anything else\n",
                   camera, status.message().c_str());
      return 1;
    }
    device.exposure_us[camera] = *exposures[camera];
    applied = true;
    std::printf("  exposure cam%u  : %u us (volatile)\n", camera,
                *exposures[camera]);
  }

  std::array<std::optional<std::uint16_t>, 3> gains;
  for (const std::string& spec : options.gain_specs) {
    std::uint32_t camera = 0;
    std::uint32_t value = 0;
    if (!parse_camera_value(spec, camera, value) || value > 0xFFFFU) {
      std::fprintf(stderr, "invalid --gain value: %s\n", spec.c_str());

      return 2;
    }
    for (unsigned index = 0; index < 3; ++index) {
      if (camera == 3 || camera == index) {
        gains[index] = static_cast<std::uint16_t>(value);
      }
    }
  }
  for (unsigned camera = 0; camera < 3; ++camera) {
    if (!gains[camera]) {
      continue;
    }
    const Status status = apply_gain(*channel, camera, *gains[camera]);
    if (!status.ok()) {
      std::fprintf(stderr,
                   "volatile gain for camera %u was refused: %s\n"
                   "aborting without changing anything else\n",
                   camera, status.message().c_str());
      return 1;
    }
    device.gain[camera] = *gains[camera];
    applied = true;
    std::printf("  gain cam%u      : %u (volatile)\n", camera,
                static_cast<unsigned>(*gains[camera]));
  }

  if (options.laser_percent) {
    const Status status = channel->set_laser_brightness(*options.laser_percent);
    if (!status.ok()) {
      std::fprintf(stderr,
                   "volatile laser brightness %u%% was refused: %s\n"
                   "aborting; the trigger was not started\n",
                   *options.laser_percent, status.message().c_str());
      return 1;
    }
    device.laser_percent = *options.laser_percent;
    device.laser_set = true;
    applied = true;
    std::printf("  laser          : %u %% (volatile)\n", *options.laser_percent);
  }
  for (const auto& [route, value] : options.strobe_specs) {
    const Status status = channel->set_strobe_brightness(
        route, static_cast<std::uint16_t>(value));
    if (!status.ok()) {
      std::fprintf(stderr,
                   "volatile strobe route %u = %u was refused: %s\n"
                   "aborting; the trigger was not started\n",
                   route, value, status.message().c_str());
      // The setup guard also handles a previously enabled laser.
      return 1;
    }
    device.strobe[route] = value;
    device.strobe_set[route] = true;
    applied = true;
    std::printf("  strobe route%u  : %u (volatile)\n", route, value);
  }

  const std::uint32_t trigger_period =
      options.trigger_mode == TriggerMode::Period ? options.trigger_period_us
                                                  : 68'000;
  if (options.trigger_mode == TriggerMode::Off) {
    std::printf("  trigger        : left untouched (--trigger off)\n");
  }
  device.settings_applied = applied;

  LiveSessionDriver driver(*pipeline, *channel, options, device,
                           trigger_period);

  setup_cleanup.armed = false;
  CaptureSessionOptions session;
  session.output_directory = output_directory;
  session.control_path = options.control_path;
  session.status_file = options.status_file;
  session.volatility_path = options.exposure_control_path;
  session.groups = options.groups;
  session.ring_size = options.ring_size;
  session.max_seconds = options.max_seconds;
  session.timeout_seconds = options.timeout_seconds;
  session.timeout_seconds_set =
      options.timeout_seconds_set || options.control_path.empty();
  session.progress_seconds = options.progress_seconds;
  session.record_path = options.record_path;
  session.manifest.tool = "einstar-capture-frames";
  session.manifest.backend = "libusb";
  session.manifest.simulated = false;
  session.manifest.live = true;
  session.manifest.device = device;

  auto session_result = einstar::capture::run_capture_session(session, driver);
  if (!session_result) {
    // The session loop stops the device on every path it enters, but setup can
    // fail before that (for example if the frames directory disappears).  Stop
    // is idempotent: trigger off, illuminator off, pipeline closed.
    (void)driver.stop();
    std::fprintf(stderr, "%s\n", session_result.error().message().c_str());
    return 1;
  }
  const einstar::capture::CaptureSessionResult& result = *session_result;

  std::printf("capture (libusb) - UNTESTED PATH, no scanner was attached\n");
  std::printf("  out dir        : %s\n",
              result.manifest.output_directory.c_str());
  std::printf("  session        : %s, stop reason %s, ring %u (%llu on disk, "
              "%llu evicted)\n",
              std::string(einstar::capture::session_state_name(result.state))
                  .c_str(),
              result.stop_reason == StopReason::None
                  ? "none"
                  : std::string(
                        einstar::capture::stop_reason_name(result.stop_reason))
                        .c_str(),
              result.ring_size,
              static_cast<unsigned long long>(result.groups_on_disk),
              static_cast<unsigned long long>(result.groups_evicted));
  print_grouping(result.manifest, result.grouping, result.cadence,
                 result.manifest_path);
  if (options.json) {
    print_json_summary(result.manifest, result.grouping, result.cadence,
                       result.manifest_path);
  }
  if (result.timed_out) {
    std::fprintf(stderr,
                 "capture timed out after %u s with %llu group(s) written; the "
                 "manifest records the partial result\n",
                 options.timeout_seconds,
                 static_cast<unsigned long long>(result.grouping.groups_written));
    return 1;
  }
  if (result.stop_reason == StopReason::SignalInt ||
      result.stop_reason == StopReason::SignalTerm) {
    std::printf("stopped by signal %s\n",
                result.stop_reason == StopReason::SignalInt ? "SIGINT"
                                                            : "SIGTERM");
  }
  return result.stop_reason == StopReason::Error ? 1 : 0;
}

#endif  // EINSTAR_WITH_LIBUSB

}  // namespace

int main(int argc, char** argv) try {
  einstar::capture::ProcessSafetyGuard safety;
  if (const auto armed = safety.arm_from_environment(); !armed.ok()) {
    std::fprintf(stderr, "process safety: %s; capture refused\n", armed.message().c_str());
    return 1;
  }
  Options options;
  bool show_help = false;
  for (int i = 1; i < argc; ++i) {
    const std::string_view argument = argv[i];
    if (argument == "--out" && i + 1 < argc) {
      options.output_directory = argv[++i];
    } else if (argument == "--groups" && i + 1 < argc) {
      std::uint32_t value = 0;
      if (!parse_u32(argv[++i], value)) {
        return usage_error("--groups needs a non-negative integer");
      }
      options.groups = value;
    } else if (argument == "--backend" && i + 1 < argc) {
      const std::string_view backend = argv[++i];
      if (backend == "simulated") {
        options.backend = Backend::Simulated;
      } else if (backend == "libusb") {
        options.backend = Backend::LibUsb;
      } else {
        return usage_error("--backend must be simulated or libusb");
      }
    } else if (argument == "--record" && i + 1 < argc) {
      options.record_path = argv[++i];
    } else if (argument == "--exposure-us" && i + 1 < argc) {
      options.exposure_specs.emplace_back(argv[++i]);
    } else if (argument == "--gain" && i + 1 < argc) {
      options.gain_specs.emplace_back(argv[++i]);
    } else if (argument == "--status-file" && i + 1 < argc) {
      options.status_file = argv[++i];
    } else if (argument == "--control" && i + 1 < argc) {
      options.control_path = argv[++i];
    } else if (argument == "--exposure-control" && i + 1 < argc) {
      options.exposure_control_path = argv[++i];
    } else if (argument == "--ring" && i + 1 < argc) {
      std::uint32_t value = 0;
      if (!parse_u32(argv[++i], value)) {
        return usage_error("--ring needs a non-negative integer");
      }
      options.ring_size = value;
    } else if (argument == "--max-seconds" && i + 1 < argc) {
      char* end = nullptr;
      const double seconds = std::strtod(argv[++i], &end);
      if (end == argv[i] || !(seconds > 0.0) || !(seconds < 1.0e9)) {
        return usage_error("--max-seconds takes a positive number of seconds");
      }
      options.max_seconds = seconds;
    } else if (argument == "--session-dir" && i + 1 < argc) {
      options.session_dir = argv[++i];
    } else if (argument == "--progress" && i + 1 < argc) {
      char* end = nullptr;
      const double seconds = std::strtod(argv[++i], &end);
      if (end == argv[i] || !(seconds > 0.0)) {
        std::fprintf(stderr, "--progress takes a positive number of seconds\n");
        return 2;
      }
      options.progress_seconds = seconds;
    } else if (argument == "--laser" && i + 1 < argc) {
      std::uint32_t percent = 0;
      if (!parse_u32(argv[++i], percent) || percent > 100) {
        std::fprintf(stderr, "--laser takes 0..100 percent\n");
        return 2;
      }
      options.laser_percent = percent;
    } else if (argument == "--strobe" && i + 1 < argc) {
      const std::string_view value = argv[++i];
      const std::size_t colon = value.find(':');
      std::uint32_t route = 0;
      std::uint32_t amount = 0;
      if (colon == std::string_view::npos ||
          !parse_u32(value.substr(0, colon), route) || route > 1 ||
          !parse_u32(value.substr(colon + 1), amount) || amount > 0xFFFF) {
        std::fprintf(stderr, "--strobe takes ROUTE:VALUE with route 0 or 1\n");
        return 2;
      }
      options.strobe_specs.emplace_back(route, amount);
    } else if (argument == "--trigger" && i + 1 < argc) {
      const std::string_view value = argv[++i];
      if (value == "off") {
        options.trigger_mode = TriggerMode::Off;
      } else {
        std::uint32_t period = 0;
        if (!parse_u32(value, period) || period == 0) {
          return usage_error("--trigger needs a positive period in us or 'off'");
        }
        options.trigger_mode = TriggerMode::Period;
        options.trigger_period_us = period;
      }
    } else if (argument == "--trigger-counts" && i + 1 < argc) {
      const std::string_view value = argv[++i];
      const std::size_t colon = value.find(':');
      std::uint32_t mono = 0;
      std::uint32_t rgb = 0;
      if (colon == std::string_view::npos ||
          !parse_u32(value.substr(0, colon), mono) ||
          !parse_u32(value.substr(colon + 1), rgb) || mono > 15 || rgb > 15) {
        return usage_error("--trigger-counts needs MONO:RGB (0..15 each)");
      }
      options.trigger_counts_set = true;
      options.trigger_mono = static_cast<std::uint8_t>(mono);
      options.trigger_rgb = static_cast<std::uint8_t>(rgb);
    } else if (argument == "--faults" && i + 1 < argc) {
      options.faults = argv[++i];
    } else if (argument == "--seed" && i + 1 < argc) {
      if (!parse_u64(argv[++i], options.seed)) {
        return usage_error("--seed needs an unsigned integer");
      }
    } else if (argument == "--queue-depth" && i + 1 < argc) {
      std::uint32_t value = 0;
      if (!parse_u32(argv[++i], value) || value == 0) {
        return usage_error("--queue-depth must be at least 1");
      }
      options.queue_depth = value;
    } else if (argument == "--timeout-seconds" && i + 1 < argc) {
      std::uint32_t value = 0;
      if (!parse_u32(argv[++i], value) || value == 0) {
        return usage_error("--timeout-seconds must be at least 1");
      }
      options.timeout_seconds = value;
      options.timeout_seconds_set = true;
    } else if (argument == "--json") {
      options.json = true;
    } else if (argument == "--show-serial") {
      options.show_serial = true;
    } else if (argument == "--probe-only") {
      options.probe_only = true;
    } else if (argument == "--help" || argument == "-h") {
      show_help = true;
    } else {
      std::fprintf(stderr, "unknown argument: %s\n", argv[i]);
      print_usage();
      return 2;
    }
  }
  if (!options.session_dir.empty()) {
    if (!options.output_directory.empty() || !options.record_path.empty() ||
        !options.status_file.empty()) {
      return usage_error(
          "--session-dir cannot be combined with --out, --record or "
          "--status-file (it supplies all three)");
    }
    options.output_directory = join_path(options.session_dir, "frames");
    options.record_path = join_path(options.session_dir, "raw.e1r");
    options.status_file = join_path(options.session_dir, "status.json");
  }
  if (!options.control_path.empty() && !options.timeout_seconds_set) {
    // A wizard session runs until the GUI says stop; the 30 s default safety
    // deadline would otherwise end a perfectly healthy multi-minute capture.
    options.timeout_seconds = 0;
  }
  if (show_help) {
    print_usage();
    return 0;
  }
  switch (options.backend) {
    case Backend::Simulated:
      return run_simulated(options);
    case Backend::LibUsb:
#if defined(EINSTAR_WITH_LIBUSB)
      return run_libusb(options);
#else
      std::fprintf(stderr,
                   "this build has no libusb support (EINSTAR_WITH_LIBUSB=OFF); "
                   "use --backend simulated\n");
      return 1;
#endif
  }
  return 2;
} catch (const std::exception& error) {
  std::fprintf(stderr, "capture failed: %s; consult cleanup diagnostics\n", error.what());
  return 1;
}
