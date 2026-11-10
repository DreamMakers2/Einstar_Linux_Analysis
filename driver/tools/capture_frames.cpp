// SPDX-License-Identifier: MIT
// Safe capture of synchronised three-camera groups.
//
//   einstar-capture-frames --out DIR --groups N [--backend simulated|libusb]
//       [--record FILE] [--exposure-us [CAM:]US ...] [--gain [CAM:]V ...]
//       [--trigger US|off] [--trigger-counts MONO:RGB] [--faults clean|lossy|hostile]
//       [--seed S] [--queue-depth N] [--timeout-seconds S] [--json]
//       [--show-serial] [--probe-only]
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

using einstar::capture::CadenceStats;
using einstar::ErrorCode;
using einstar::fail;
using einstar::Result;
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
  bool json = false;
  bool show_serial = false;
  bool probe_only = false;
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
      "\n"
      "  --out DIR            output directory (frame_%%06u_cam%%u.pgm +\n"
      "                       frames_manifest.json)\n"
      "  --groups N           synchronised three-camera groups to capture\n"
      "  --backend NAME       simulated (default, deterministic) or libusb\n"
      "  --record FILE        also record the exact raw byte stream (RawRecorder)\n"
      "  --exposure-us SPEC   volatile exposure in us; SPEC = US or CAM:US\n"
      "                       (repeatable; libusb only)\n"
      "  --gain SPEC          volatile gain; SPEC = VALUE or CAM:VALUE\n"
      "                       (repeatable; libusb only)\n"
      "  --trigger US         volatile trigger period in us (default 68000 libusb,\n"
      "                       40000 simulated) or 'off' to leave the trigger alone\n"
      "  --trigger-counts A:B volatile mono:rgb frames per trigger (libusb only)\n"
      "  --faults NAME        simulated fault preset: clean, lossy, hostile\n"
      "  --seed S             simulated seed (default 1)\n"
      "  --timeout-seconds S  wall-clock capture deadline (default 30)\n"
      "  --json               print a machine-readable summary on stdout\n"
      "  --show-serial        do not redact the device serial in logs/manifest\n"
      "  --probe-only         libusb: read metadata + calibration, change nothing\n"
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

[[maybe_unused]] double megabytes_per_second(std::uint64_t bytes, double seconds) {
  return seconds > 0.0 ? static_cast<double>(bytes) / seconds / 1e6 : 0.0;
}

std::uint64_t string_digest(std::string_view text) {
  return einstar::capture::fnv1a64(std::span<const std::uint8_t>(
      reinterpret_cast<const std::uint8_t*>(text.data()), text.size()));
}

[[maybe_unused]] std::string join_path(const std::string& directory, std::string_view name) {
  if (!directory.empty() && directory.back() == '/') {
    return directory + std::string(name);
  }
  return directory + "/" + std::string(name);
}

void print_grouping(const ManifestInfo& manifest, const GroupingStats& grouping,
                    const CadenceStats& cadence, const std::string& manifest_path) {
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
  std::printf("  frames         : %llu written, %llu completed, %llu dropped, %llu invalid\n",
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
  std::printf("  cadence        : %.2f fps, %.3f ms mean (%.3f..%.3f ms), %llu interval(s)\n",
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
    std::printf("  recording      : %s (%llu bytes, %llu transfers, digest 0x%016llX)\n",
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
  std::printf("\"output_directory\": %s, ", quote(manifest.output_directory).c_str());
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
  std::printf("\"virtual_duration_s\": %.6f, ", manifest.metrics.virtual_seconds);
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
  std::printf("\"device_serial\": %s, ",
              quote(manifest.device.serial).c_str());
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
    return usage_error("--out is required");
  }
  if (options.groups == 0) {
    return usage_error("--groups must be at least 1");
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
  print_grouping(result.manifest, result.grouping, result.cadence,
                 result.manifest_path);
  if (options.json) {
    print_json_summary(result.manifest, result.grouping, result.cadence,
                       result.manifest_path);
  }
  return result.grouping.groups_written == 0 ? 1 : 0;
}

// ---------------------------------------------------------------------------
// libusb backend
// ---------------------------------------------------------------------------

#if defined(EINSTAR_WITH_LIBUSB)

struct LiveResult {
  ManifestInfo manifest;
  GroupingStats grouping;
  CadenceStats cadence;
  std::string manifest_path;
  bool timed_out = false;
  bool probe_only = false;
};

using einstar::AcquisitionPipeline;
using einstar::ControlChannel;
using einstar::ControlConfig;
using einstar::CopyMode;
using einstar::Frame;
using einstar::LibUsbTransport;
using einstar::PipelineConfig;
using einstar::Status;

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
    const std::size_t offset = static_cast<std::size_t>(index) * kCalibrationPageSize;
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

/// Reads everything the capture tools need from a live device: identity
/// (already read by open()), the informational getters, and the factory
/// calibration blob.  Read-only apart from optionally writing the local copy
/// of the blob into the output directory.
Result<DeviceInfo> read_device_metadata(
    ControlChannel& channel, const einstar::DeviceIdentity& identity,
    bool show_serial, const std::optional<std::string>& directory) {
  auto device_result = read_device_metadata(*channel, *identity,
                                            options.show_serial, directory);
  if (!device_result) {
    std::fprintf(stderr, "metadata read failed: %s\n",
                 device_result.error().message().c_str());
    channel->close();
    (void)pipeline->stop();
    return 1;
  }
  DeviceInfo device = *device_result;

  if (options.probe_only) {
    std::printf("  probe-only     : no streaming, no volatile settings changed\n");
    channel->close();
    (void)pipeline->stop();
    return 0;
  }

  // ---- volatile settings ---------------------------------------------------
  bool applied = false;
  std::array<std::optional<std::uint32_t>, 3> exposures;
  for (const std::string& spec : options.exposure_specs) {
    std::uint32_t camera = 0;
    std::uint32_t value = 0;
    if (!parse_camera_value(spec, camera, value)) {
      std::fprintf(stderr, "invalid --exposure-us value: %s\n", spec.c_str());
      channel->close();
      (void)pipeline->stop();
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
      channel->close();
      (void)pipeline->stop();
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
      channel->close();
      (void)pipeline->stop();
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
      std::fprintf(stderr, "volatile gain for camera %u was refused: %s\n"
                           "aborting without changing anything else\n",
                   camera, status.message().c_str());
      channel->close();
      (void)pipeline->stop();
      return 1;
    }
    device.gain[camera] = *gains[camera];
    applied = true;
    std::printf("  gain cam%u      : %u (volatile)\n", camera,
                static_cast<unsigned>(*gains[camera]));
  }

  const std::uint32_t trigger_period =
      options.trigger_mode == TriggerMode::Period ? options.trigger_period_us
                                                  : 68'000;
  bool streaming = false;
  if (options.trigger_mode != TriggerMode::Off) {
    if (trigger_period == 0) {
      std::fprintf(stderr, "--trigger period must be non-zero\n");
      channel->close();
      (void)pipeline->stop();
      return 2;
    }
    const Status status = channel->start_streaming(trigger_period);
    if (!status.ok()) {
      std::fprintf(stderr, "volatile start_streaming(%u us) was refused: %s\n"
                           "aborting; the trigger was not left running\n",
                   trigger_period, status.message().c_str());
      channel->close();
      (void)pipeline->stop();
      return 1;
    }
    streaming = true;
    applied = true;
    device.trigger_period_us = trigger_period;
    device.trigger_mono = 1;
    device.trigger_rgb = 1;
    std::printf("  trigger        : %u us, 1 IR + 1 colour per trigger "
                "(volatile)\n", trigger_period);
    if (options.trigger_counts_set) {
      const Status counts = channel->set_trigger_counts(options.trigger_mono,
                                                        options.trigger_rgb);
      if (!counts.ok()) {
        std::fprintf(stderr, "volatile trigger counts were refused: %s\n",
                     counts.message().c_str());
        (void)channel->stop_streaming();
        channel->close();
        (void)pipeline->stop();
        return 1;
      }
      device.trigger_mono = options.trigger_mono;
      device.trigger_rgb = options.trigger_rgb;
      std::printf("  trigger counts : mono %u, rgb %u (volatile)\n",
                  static_cast<unsigned>(options.trigger_mono),
                  static_cast<unsigned>(options.trigger_rgb));
    }
  } else {
    std::printf("  trigger        : left untouched (--trigger off)\n");
  }
  device.settings_applied = applied;

  // ---- capture -------------------------------------------------------------
  GroupWriter writer(*directory);
  if (Status status = writer.prepare(); !status.ok()) {
    std::fprintf(stderr, "%s\n", status.message().c_str());
    if (streaming) {
      (void)channel->stop_streaming();
    }
    channel->close();
    (void)pipeline->stop();
    return 1;
  }

  const auto wall_start = std::chrono::steady_clock::now();
  const auto deadline =
      wall_start + std::chrono::seconds(options.timeout_seconds == 0
                                            ? 30
                                            : options.timeout_seconds);
  bool target_reached = false;
  bool timed_out = false;
  Frame frame;
  while (!target_reached) {
    (void)pipeline->poll(5);
    while (pipeline->try_pop_frame(frame)) {
      const Status status = writer.add(frame);
      frame = Frame{};
      if (!status.ok()) {
        std::fprintf(stderr, "%s\n", status.message().c_str());
        if (streaming) {
          (void)channel->stop_streaming();
        }
        channel->close();
        (void)pipeline->stop();
        return 1;
      }
      if (writer.stats().groups_written >= options.groups) {
        target_reached = true;
        break;
      }
    }
    if (!target_reached && std::chrono::steady_clock::now() >= deadline) {
      timed_out = true;
      break;
    }
  }
  const auto wall_end = std::chrono::steady_clock::now();

  if (streaming) {
    const Status status = channel->stop_streaming();
    if (!status.ok()) {
      std::fprintf(stderr, "warning: stop_streaming failed: %s\n",
                   status.message().c_str());
    }
  }
  channel->close();
  const Status stop_status = pipeline->stop();
  if (!stop_status.ok()) {
    std::fprintf(stderr, "pipeline stop failed: %s\n",
                 stop_status.message().c_str());
    return 1;
  }
  (void)writer.flush();
  const einstar::PipelineStats stats = pipeline->stats();

  LiveResult result;
  result.timed_out = timed_out;
  result.grouping = writer.stats();
  result.cadence = writer.cadence();
  result.manifest.tool = "einstar-capture-frames";
  result.manifest.backend = "libusb";
  result.manifest.simulated = false;
  result.manifest.live = true;
  result.manifest.output_directory = *directory;
  result.manifest.requested_groups = options.groups;
  result.manifest.stopped_early = false;
  result.manifest.device = device;
  einstar::capture::metrics_from_pipeline(result.manifest.metrics, stats);
  result.manifest.metrics.frames_invalid = writer.stats().frames_invalid;
  result.manifest.metrics.wall_seconds =
      std::chrono::duration<double>(wall_end - wall_start).count();
  result.manifest.metrics.virtual_seconds = 0.0;
  result.manifest.throughput_mbps =
      megabytes_per_second(stats.bytes_received,
                           result.manifest.metrics.wall_seconds);
  result.manifest.virtual_throughput_mbps = 0.0;

  if (!options.record_path.empty()) {
    auto recorded = einstar::capture::verify_recording(options.record_path);
    if (!recorded) {
      std::fprintf(stderr, "recording verification failed: %s\n",
                   recorded.error().message().c_str());
      return 1;
    }
    std::uint64_t bytes = 0;
    for (const auto& transfer : recorded->transfers) {
      bytes += transfer.bytes.size();
    }
    result.manifest.recorded_path = options.record_path;
    result.manifest.recorded_transfers = recorded->transfers.size();
    result.manifest.recorded_bytes = bytes;
    result.manifest.recorded_digest = recorded->trailer_digest;
  }

  const std::string manifest_path =
      join_path(*directory, "frames_manifest.json");
  auto written =
      einstar::capture::write_frames_manifest(manifest_path, result.manifest, writer);
  if (!written) {
    std::fprintf(stderr, "manifest: %s\n", written.error().message().c_str());
    return 1;
  }
  result.manifest_path = written->path;

  std::printf("capture (libusb) - UNTESTED PATH, no scanner was attached\n");
  std::printf("  out dir        : %s\n", result.manifest.output_directory.c_str());
  print_grouping(result.manifest, result.grouping, result.cadence,
                 result.manifest_path);
  if (options.json) {
    print_json_summary(result.manifest, result.grouping, result.cadence,
                       result.manifest_path);
  }
  if (timed_out) {
    std::fprintf(stderr,
                 "capture timed out after %u s with %llu of %u group(s); the "
                 "manifest records the partial result\n",
                 options.timeout_seconds,
                 static_cast<unsigned long long>(result.grouping.groups_written),
                 options.groups);
    return 1;
  }
  return result.grouping.groups_written == 0 ? 1 : 0;
}

#endif  // EINSTAR_WITH_LIBUSB

}  // namespace

int main(int argc, char** argv) {
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
  if (show_help) {
    print_usage();
    return 0;
  }
  if (options.trigger_mode == TriggerMode::Period &&
      options.backend == Backend::Simulated && options.trigger_period_us == 0) {
    return usage_error("--trigger period must be positive");
  }
  if (options.probe_only && options.backend == Backend::Simulated) {
    return run_simulated(options);
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
}
