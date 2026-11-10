// SPDX-License-Identifier: MIT
// Implementation of the acquisition-side capture/replay core.
#include "capture_core.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <limits>
#include <map>
#include <set>
#include <string>
#include <system_error>
#include <utility>
#include <vector>

namespace einstar::capture {
namespace {

constexpr std::size_t kPendingGroupWindow = 8;
constexpr std::size_t kMaxReportedAbsentGroups = 64;
constexpr std::uint64_t kFnvOffset = 14695981039346656037ULL;
constexpr std::uint64_t kFnvPrime = 1099511628211ULL;

std::string format_seconds(double value) {
  char buffer[64];
  std::snprintf(buffer, sizeof(buffer), "%.6f", value);
  return std::string(buffer);
}

std::string format_rate(double value) {
  char buffer[64];
  std::snprintf(buffer, sizeof(buffer), "%.3f", value);
  return std::string(buffer);
}

std::string json_string(std::string_view text) {
  std::string out;
  out.reserve(text.size() + 2);
  out.push_back('"');
  for (const char raw : text) {
    const auto byte = static_cast<unsigned char>(raw);
    switch (byte) {
      case '"': out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\b': out += "\\b"; break;
      case '\f': out += "\\f"; break;
      case '\n': out += "\\n"; break;
      case '\r': out += "\\r"; break;
      case '\t': out += "\\t"; break;
      default:
        if (byte < 0x20U) {
          char escape[8];
          std::snprintf(escape, sizeof(escape), "\\u%04X", byte);
          out += escape;
        } else {
          out.push_back(raw);
        }
        break;
    }
  }
  out.push_back('"');
  return out;
}

std::string join_path(const std::string& directory, std::string_view name) {
  if (directory.empty()) {
    return std::string(name);
  }
  if (directory.back() == '/') {
    return directory + std::string(name);
  }
  return directory + "/" + std::string(name);
}

std::string stream_filename(std::uint32_t group, std::uint8_t camera) {
  char buffer[64];
  std::snprintf(buffer, sizeof(buffer), "frame_%06u_cam%u.pgm", group,
                static_cast<unsigned>(camera));
  return std::string(buffer);
}

std::string escaped_for_log(std::string_view text) {
  std::string out;
  out.reserve(text.size());
  for (const char c : text) {
    const auto byte = static_cast<unsigned char>(c);
    out.push_back(byte >= 0x20U && byte < 0x7FU ? c : '?');
  }
  return out;
}

/// Quoted JSON string member.
std::string format_field(std::string_view key, std::string_view value,
                         std::string_view indent) {
  return std::string(indent) + json_string(key) + ": " + json_string(value);
}

/// Member whose value is already valid JSON (number, bool, null, object).
std::string format_raw(std::string_view key, std::string_view value,
                       std::string_view indent) {
  return std::string(indent) + json_string(key) + ": " + std::string(value);
}

std::string format_bool(std::string_view key, bool value, std::string_view indent) {
  return format_raw(key, value ? std::string_view("true")
                               : std::string_view("false"),
                    indent);
}

std::string format_u64(std::string_view key, std::uint64_t value,
                       std::string_view indent) {
  return format_raw(key, std::to_string(value), indent);
}

std::uint64_t digest_bytes(std::uint64_t hash, std::span<const std::uint8_t> bytes) {
  for (const std::uint8_t byte : bytes) {
    hash ^= byte;
    hash *= kFnvPrime;
  }
  return hash;
}

double seconds_between(std::chrono::steady_clock::time_point start,
                       std::chrono::steady_clock::time_point end) {
  const auto ns =
      std::chrono::duration_cast<std::chrono::nanoseconds>(end - start).count();
  return ns <= 0 ? 0.0 : static_cast<double>(ns) / 1e9;
}

double mb_per_second(std::uint64_t bytes, double seconds) {
  if (seconds <= 0.0) {
    return 0.0;
  }
  return static_cast<double>(bytes) / seconds / 1e6;
}

Status write_text_atomic(const std::string& path, std::string_view content,
                         std::uint64_t& written_bytes, std::uint64_t& digest) {
  const std::string temporary = path + ".tmp";
  {
    std::ofstream file(temporary, std::ios::binary | std::ios::trunc);
    if (!file) {
      return Status::error(ErrorCode::IoError,
                           "cannot open " + temporary + " for writing");
    }
    file.write(content.data(), static_cast<std::streamsize>(content.size()));
    file.flush();
    if (!file.good()) {
      return Status::error(ErrorCode::IoError,
                           "write failed: " + temporary);
    }
  }
  std::error_code rename_error;
  std::filesystem::rename(temporary, path, rename_error);
  if (rename_error) {
    std::error_code ignored;
    std::filesystem::remove(temporary, ignored);
    return Status::error(ErrorCode::IoError,
                         "cannot rename " + temporary + " to " + path + ": " +
                             rename_error.message());
  }
  written_bytes = content.size();
  digest = digest_bytes(
      kFnvOffset,
      std::span<const std::uint8_t>(
          reinterpret_cast<const std::uint8_t*>(content.data()), content.size()));
  return Status::success();
}

}  // namespace

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

std::string_view camera_name(std::uint8_t camera) noexcept {
  switch (camera) {
    case 0: return "right_ir";
    case 1: return "left_ir";
    case 2: return "color";
    default: return "unknown";
  }
}

std::string_view stream_key(std::uint8_t camera) noexcept {
  switch (camera) {
    case 0: return "stream0";
    case 1: return "stream1";
    case 2: return "stream2";
    default: return {};
  }
}

std::uint64_t fnv1a64(std::span<const std::uint8_t> bytes) noexcept {
  return digest_bytes(kFnvOffset, bytes);
}

std::string json_quote(std::string_view text) { return json_string(text); }

bool grouping_complete(const GroupingStats& stats) noexcept {
  return stats.groups_incomplete == 0 && stats.groups_absent == 0 &&
         stats.duplicate_frames == 0 && stats.frames_invalid == 0 &&
         stats.late_frames == 0;
}

bool valid_frame(const Frame& frame, std::uint32_t width, std::uint32_t height,
                 std::uint8_t& camera) {
  if (frame.camera_id >= kStreamCount) {
    return false;
  }
  if (frame.payload_size != width * height) {
    return false;
  }
  camera = frame.camera_id;
  return true;
}

// ---------------------------------------------------------------------------
// Recording verification
// ---------------------------------------------------------------------------

Result<recording::Recording> verify_recording(const std::string& path) {
  if (path.empty()) {
    return fail(ErrorCode::InvalidArgument, "recording path is empty");
  }
  auto loaded = recording::read_recording(path);
  if (!loaded) {
    return fail(ErrorCode::Protocol,
                "refusing recording " + path + ": " + loaded.error().message() +
                    " (header CRC, record CRC and layout are validated)");
  }
  if (!loaded->has_trailer) {
    return fail(ErrorCode::Protocol,
                "refusing recording " + path +
                    ": no trailer record, so the stream digest cannot be "
                    "verified (truncated or still being written?)");
  }
  std::uint64_t digest = kFnvOffset;
  std::uint64_t bytes = 0;
  for (const auto& transfer : loaded->transfers) {
    digest = digest_bytes(
        digest, std::span<const std::uint8_t>(
                    reinterpret_cast<const std::uint8_t*>(transfer.bytes.data()),
                    transfer.bytes.size()));
    bytes += transfer.bytes.size();
  }
  if (digest != loaded->trailer_digest || bytes != loaded->trailer_bytes ||
      loaded->transfers.size() != loaded->trailer_transfers) {
    return fail(ErrorCode::Protocol,
                "refusing recording " + path +
                    ": trailer digest/counts do not match the recorded "
                    "transfers");
  }
  return loaded;
}

// ---------------------------------------------------------------------------
// PGM I/O and directories
// ---------------------------------------------------------------------------

Status write_pgm(const std::string& path, std::uint32_t width,
                 std::uint32_t height, std::span<const std::uint8_t> pixels) {
  if (width == 0 || height == 0) {
    return Status::error(ErrorCode::InvalidArgument, "PGM dimensions are zero");
  }
  const std::size_t expected =
      static_cast<std::size_t>(width) * static_cast<std::size_t>(height);
  if (pixels.size() != expected) {
    return Status::error(ErrorCode::InvalidArgument,
                         "PGM payload is " + std::to_string(pixels.size()) +
                             " bytes, expected " + std::to_string(expected));
  }
  std::ofstream file(path, std::ios::binary | std::ios::trunc);
  if (!file) {
    return Status::error(ErrorCode::IoError, "cannot open " + path +
                                                 " for writing");
  }
  char header[64];
  const int header_size =
      std::snprintf(header, sizeof(header), "P5\n%u %u\n255\n", width, height);
  if (header_size <= 0) {
    return Status::error(ErrorCode::Internal, "PGM header formatting failed");
  }
  file.write(header, static_cast<std::streamsize>(header_size));
  file.write(reinterpret_cast<const char*>(pixels.data()),
             static_cast<std::streamsize>(pixels.size()));
  file.flush();
  if (!file.good()) {
    return Status::error(ErrorCode::IoError, "PGM write failed: " + path);
  }
  return Status::success();
}

Result<std::vector<std::uint8_t>> read_pgm(const std::string& path,
                                           std::uint32_t& width,
                                           std::uint32_t& height) {
  std::ifstream file(path, std::ios::binary);
  if (!file) {
    return fail(ErrorCode::IoError, "cannot open " + path);
  }
  std::string magic;
  file >> magic;
  if (magic != "P5") {
    return fail(ErrorCode::Protocol, path + ": not a binary PGM (P5)");
  }
  auto skip_space = [&]() {
    for (;;) {
      const int next = file.peek();
      if (next == '#') {
        std::string line;
        std::getline(file, line);
      } else if (next == ' ' || next == '\t' || next == '\n' || next == '\r') {
        file.get();
      } else {
        break;
      }
    }
  };
  auto read_integer = [&](std::uint32_t& value, const char* what) {
    skip_space();
    unsigned parsed = 0;
    file >> parsed;
    if (!file) {
      return Status::error(ErrorCode::Protocol,
                           path + ": malformed PGM header (" + what + ")");
    }
    value = parsed;
    return Status::success();
  };
  std::uint32_t maxval = 0;
  if (Status status = read_integer(width, "width"); !status.ok()) {
    return fail(status);
  }
  if (Status status = read_integer(height, "height"); !status.ok()) {
    return fail(status);
  }
  if (Status status = read_integer(maxval, "maxval"); !status.ok()) {
    return fail(status);
  }
  if (width == 0 || height == 0 || maxval != 255) {
    return fail(ErrorCode::Protocol,
                path + ": unsupported PGM dimensions/maxval");
  }
  file.get();  // the single whitespace byte after maxval
  const std::size_t count =
      static_cast<std::size_t>(width) * static_cast<std::size_t>(height);
  std::vector<std::uint8_t> pixels(count);
  file.read(reinterpret_cast<char*>(pixels.data()),
            static_cast<std::streamsize>(count));
  if (file.gcount() != static_cast<std::streamsize>(count)) {
    return fail(ErrorCode::Protocol, path + ": truncated PGM payload");
  }
  return pixels;
}

Status ensure_directory(const std::string& directory) {
  if (directory.empty()) {
    return Status::error(ErrorCode::InvalidArgument,
                         "output directory is empty");
  }
  std::error_code error;
  if (std::filesystem::exists(directory, error)) {
    if (error) {
      return Status::error(ErrorCode::IoError,
                           "cannot stat output directory " + directory + ": " +
                               error.message());
    }
    if (!std::filesystem::is_directory(directory, error) || error) {
      return Status::error(ErrorCode::IoError,
                           "output path is not a directory: " + directory);
    }
    return Status::success();
  }
  if (!std::filesystem::create_directories(directory, error) || error) {
    if (!error) {
      error = std::make_error_code(std::errc::file_exists);
    }
    return Status::error(ErrorCode::IoError,
                         "cannot create output directory " + directory + ": " +
                             error.message());
  }
  return Status::success();
}

Result<std::string> prepare_output_directory(const std::string& directory) {
  if (Status status = ensure_directory(directory); !status.ok()) {
    return fail(status);
  }
  std::error_code error;
  const std::filesystem::path absolute =
      std::filesystem::absolute(directory, error);
  if (error || absolute.empty()) {
    return fail(ErrorCode::IoError,
                "cannot resolve absolute path for " + directory + ": " +
                    error.message());
  }
  return absolute.lexically_normal().string();
}

// ---------------------------------------------------------------------------
// GroupWriter
// ---------------------------------------------------------------------------

GroupWriter::GroupWriter(std::string output_directory, std::uint32_t width,
                         std::uint32_t height)
    : directory_(std::move(output_directory)), width_(width), height_(height) {}

Status GroupWriter::prepare() { return ensure_directory(directory_); }

std::string GroupWriter::file_path(std::string_view name) const {
  return join_path(directory_, name);
}

Status GroupWriter::add(const Frame& frame) {
  ++stats_.frames_seen;
  std::uint8_t camera = 0;
  if (!valid_frame(frame, width_, height_, camera)) {
    ++stats_.frames_invalid;
    return Status::success();
  }

  StreamFrame stream;
  stream.camera = camera;
  stream.frame_id = frame.frame_index;
  stream.device_group = frame.device_timestamp;
  stream.host_time_ns = frame.host_timestamp_ns;
  if (frame.is_contiguous()) {
    const std::span<const std::byte> payload = frame.payload();
    if (payload.size() != static_cast<std::size_t>(width_) * height_) {
      ++stats_.frames_invalid;
      return Status::success();
    }
    stream.pixels.resize(payload.size());
    std::memcpy(stream.pixels.data(), payload.data(), payload.size());
  } else {
    stream.pixels.resize(static_cast<std::size_t>(width_) * height_);
    const std::size_t copied = frame.copy_payload_to(
        std::span<std::byte>(reinterpret_cast<std::byte*>(stream.pixels.data()),
                             stream.pixels.size()));
    if (copied != stream.pixels.size()) {
      ++stats_.frames_invalid;
      return Status::success();
    }
  }

  if (written_device_groups_.contains(stream.device_group)) {
    ++stats_.late_frames;
    ++stats_.duplicate_frames;
    duplicates_.push_back(DuplicateFrame{stream.device_group, camera, 0,
                                         stream.frame_id, true});
    return Status::success();
  }

  auto [it, inserted] = pending_.try_emplace(stream.device_group);
  if (inserted) {
    it->second.device_group = stream.device_group;
    seen_device_groups_.push_back(stream.device_group);
  }
  std::optional<StreamFrame>& slot = it->second.streams[camera];
  if (slot.has_value()) {
    ++stats_.duplicate_frames;
    duplicates_.push_back(DuplicateFrame{stream.device_group, camera,
                                         slot->frame_id, stream.frame_id,
                                         false});
    return Status::success();
  }
  slot = std::move(stream);

  const bool complete = std::all_of(
      it->second.streams.begin(), it->second.streams.end(),
      [](const std::optional<StreamFrame>& value) { return value.has_value(); });
  if (complete) {
    Status status = write_group(it->second);
    if (!status.ok()) {
      return status;
    }
    pending_.erase(it);
  }

  // Bound memory: a group that is still missing a camera eight groups after we
  // first saw it is finalised as incomplete.  The wire delivers groups in
  // order, so this only discards genuinely lost frames.
  while (pending_.size() > kPendingGroupWindow) {
    auto oldest = pending_.begin();
    finalize_pending(oldest->second);
    pending_.erase(oldest);
  }
  return Status::success();
}

Status GroupWriter::write_group(PendingGroup& pending) {
  const std::uint32_t group =
      static_cast<std::uint32_t>(groups_.size());
  if (group > kMaxOutputGroups) {
    return Status::error(
        ErrorCode::Exhausted,
        "more than 999999 output groups; six-digit frame names cannot "
        "represent the group id (split the recording)");
  }

  GroupRecord record;
  record.group = group;
  record.device_group = pending.device_group;
  record.host_time_ns = 0;
  bool have_time = false;

  std::array<std::string, kStreamCount> final_names;
  std::array<std::string, kStreamCount> temporary_names;

  for (std::uint8_t camera = 0; camera < kStreamCount; ++camera) {
    const StreamFrame& stream = *pending.streams[camera];
    record.frame_ids[camera] = stream.frame_id;
    record.digests[camera] = digest_bytes(kFnvOffset, stream.pixels);
    if (!have_time || stream.host_time_ns < record.host_time_ns) {
      record.host_time_ns = stream.host_time_ns;
      have_time = true;
    }
    final_names[camera] = stream_filename(group, camera);
    temporary_names[camera] = final_names[camera] + ".tmp";
    Status status = write_pgm(file_path(temporary_names[camera]), width_, height_,
                              stream.pixels);
    if (!status.ok()) {
      for (std::uint8_t cleanup = 0; cleanup <= camera; ++cleanup) {
        std::error_code ignored;
        std::filesystem::remove(file_path(temporary_names[cleanup]), ignored);
      }
      return status;
    }
    record.files[camera] = final_names[camera];
  }

  for (std::uint8_t camera = 0; camera < kStreamCount; ++camera) {
    const std::string& from = file_path(temporary_names[camera]);
    const std::string& to = file_path(final_names[camera]);
    std::error_code error;
    std::filesystem::rename(from, to, error);
    if (error) {
      for (std::uint8_t cleanup = 0; cleanup < kStreamCount; ++cleanup) {
        std::error_code ignored;
        std::filesystem::remove(file_path(temporary_names[cleanup]), ignored);
      }
      return Status::error(ErrorCode::IoError,
                           "cannot rename " + from + " to " + to + ": " +
                               error.message());
    }
  }

  written_device_groups_.insert(record.device_group);
  stats_.frames_written += kStreamCount;
  ++stats_.groups_written;
  stats_.bytes_written += static_cast<std::uint64_t>(width_) * height_ *
                          kStreamCount;
  groups_.push_back(std::move(record));
  return Status::success();
}

void GroupWriter::finalize_pending(PendingGroup& pending) {
  IncompleteGroup group;
  group.device_group = pending.device_group;
  for (std::uint8_t camera = 0; camera < kStreamCount; ++camera) {
    const std::optional<StreamFrame>& stream = pending.streams[camera];
    group.present[camera] = stream.has_value();
    group.frame_ids[camera] = stream.has_value() ? stream->frame_id : 0;
    if (!stream.has_value()) {
      group.missing_mask =
          static_cast<std::uint8_t>(group.missing_mask | (1U << camera));
      ++stats_.missing_camera_frames;
    }
  }
  ++stats_.groups_incomplete;
  incomplete_.push_back(group);
}

Status GroupWriter::flush() {
  for (auto& [key, pending] : pending_) {
    (void)key;
    finalize_pending(pending);
  }
  pending_.clear();
  update_absent_groups();
  return Status::success();
}

void GroupWriter::discard_pending() noexcept { pending_.clear(); }

void GroupWriter::update_absent_groups() {
  std::vector<std::uint64_t> ids = seen_device_groups_;
  std::sort(ids.begin(), ids.end());
  ids.erase(std::unique(ids.begin(), ids.end()), ids.end());
  stats_.groups_absent = 0;
  absent_groups_.clear();
  if (ids.size() < 2) {
    return;
  }
  std::uint64_t stride = std::numeric_limits<std::uint64_t>::max();
  for (std::size_t i = 1; i < ids.size(); ++i) {
    const std::uint64_t delta = ids[i] - ids[i - 1];
    if (delta != 0 && delta < stride) {
      stride = delta;
    }
  }
  if (stride == 0 || stride == std::numeric_limits<std::uint64_t>::max()) {
    return;
  }
  for (std::size_t i = 1; i < ids.size(); ++i) {
    const std::uint64_t delta = ids[i] - ids[i - 1];
    if (delta <= stride || delta % stride != 0) {
      continue;
    }
    const std::uint64_t missing = delta / stride - 1;
    stats_.groups_absent += missing;
    for (std::uint64_t step = 1; step <= missing; ++step) {
      if (absent_groups_.size() >= kMaxReportedAbsentGroups) {
        break;
      }
      absent_groups_.push_back(ids[i - 1] + step * stride);
    }
  }
}

CadenceStats GroupWriter::cadence() const {
  CadenceStats cadence;
  std::int64_t previous = 0;
  bool have_previous = false;
  long double sum = 0.0L;
  for (const GroupRecord& group : groups_) {
    if (group.host_time_ns <= 0) {
      continue;
    }
    if (have_previous) {
      const std::int64_t delta = group.host_time_ns - previous;
      if (delta > 0) {
        if (cadence.intervals == 0 || delta < cadence.min_ns) {
          cadence.min_ns = delta;
        }
        if (delta > cadence.max_ns) {
          cadence.max_ns = delta;
        }
        sum += static_cast<long double>(delta);
        ++cadence.intervals;
      }
    }
    previous = group.host_time_ns;
    have_previous = true;
  }
  if (cadence.intervals > 0) {
    cadence.mean_ns =
        static_cast<std::int64_t>(sum / static_cast<long double>(cadence.intervals));
    if (cadence.mean_ns > 0) {
      cadence.fps = 1e9 / static_cast<double>(cadence.mean_ns);
    }
  }
  return cadence;
}

// ---------------------------------------------------------------------------
// Fault presets
// ---------------------------------------------------------------------------

Result<FaultConfig> fault_preset(std::string_view name) {
  FaultConfig faults;
  if (name == "clean") {
    return faults;
  }
  if (name == "lossy") {
    // SimulatedTransport's `loss_probability` is currently not consulted by
    // the generator (the field exists but no code path drops a packet), so this
    // preset uses the faults that *do* remove stream data: truncated frames
    // (the last-flagged packet never arrives) and corrupted length prefixes
    // (the framer resynchronises and loses packets).  The result is the same
    // observable effect: whole frames do not complete.
    faults.loss_probability = 0.02;
    faults.short_read_probability = 0.25;
    faults.timeout_probability = 0.02;
    faults.truncate_frame_probability = 0.10;
    return faults;
  }
  if (name == "hostile") {
    faults.loss_probability = 0.10;
    faults.short_read_probability = 0.40;
    faults.corrupt_length_probability = 0.05;
    faults.duplicate_header_probability = 0.05;
    faults.stall_probability = 0.05;
    faults.timeout_probability = 0.05;
    faults.truncate_frame_probability = 0.10;
    return faults;
  }
  return fail(ErrorCode::InvalidArgument,
              "unknown fault preset '" + escaped_for_log(name) +
                  "' (expected clean, lossy or hostile)");
}

// ---------------------------------------------------------------------------
// Metrics
// ---------------------------------------------------------------------------

void metrics_from_pipeline(RunMetrics& metrics, const PipelineStats& stats) {
  metrics.bytes_received = stats.bytes_received;
  metrics.frames_completed = stats.frames_completed;
  metrics.frames_dropped = stats.frames_dropped;
  metrics.packets_parsed = stats.packets_parsed;
  metrics.parse_errors = stats.parse_errors;
  metrics.resync_bytes = stats.resync_bytes;
  metrics.staged_packets = stats.staged_packets;
  metrics.duplicate_packets = stats.duplicate_packets;
  metrics.orphan_packets = stats.orphan_packets;
  metrics.frame_restarts = stats.frame_restarts;
  metrics.sequence_gaps = stats.sequence_gaps;
  metrics.partial_timeouts = stats.partial_timeouts;
  metrics.queue_drops = stats.queue_drops;
  metrics.transfer_timeouts = stats.transfer_timeouts;
  metrics.transfer_stalls = stats.transfer_stalls;
  metrics.transfer_errors = stats.transfer_errors;
}

// ---------------------------------------------------------------------------
// Manifest
// ---------------------------------------------------------------------------

namespace {

std::string render_device(const DeviceInfo& device) {
  std::string out = "  \"device\": {\n";
  out += "    " + format_bool("simulated", device.simulated, "") + ",\n";
  out += "    " + format_bool("identified", device.identified, "") + ",\n";
  out += "    " + format_field("vendor_id", device.vendor_id, "") + ",\n";
  out += "    " + format_field("product_id", device.product_id, "") + ",\n";
  out += "    " + format_field("serial", device.serial, "") + ",\n";
  out += "    " + format_bool("serial_redacted", device.serial_redacted, "") + ",\n";
  out += "    " + format_u64("serial_fnv1a64", device.serial_fnv1a64, "") + ",\n";
  out += "    " + format_field("firmware", device.firmware, "") + ",\n";
  if (device.fx3_version.empty() && device.fpga_version.empty()) {
    out += "    \"firmware_parsed\": null,\n";
  } else {
    out += "    \"firmware_parsed\": {\"fx3\": " +
           json_string(device.fx3_version) + ", \"fpga\": " +
           json_string(device.fpga_version) + ", \"locale\": " +
           json_string(device.locale) + "},\n";
  }
  out += "    " + format_u64("sensor_count", device.sensor_count, "") + ",\n";
  if (device.calibration_file.empty()) {
    out += "    \"calibration\": null,\n";
  } else {
    out += "    \"calibration\": {\"file\": " +
           json_string(device.calibration_file) + ", \"bytes\": " +
           std::to_string(device.calibration_bytes) + ", \"fnv1a64\": " +
           std::to_string(device.calibration_fnv1a64) + "},\n";
  }
  out += "    \"volatile_settings\": {\n";
  out += "      " + format_bool("applied", device.settings_applied, "") + ",\n";
  out += "      " + format_u64("trigger_period_us", device.trigger_period_us, "") + ",\n";
  out += "      \"trigger_counts\": {\"mono\": " +
         std::to_string(device.trigger_mono) + ", \"rgb\": " +
         std::to_string(device.trigger_rgb) + "},\n";
  out += "      \"exposure_us\": {";
  for (std::uint8_t camera = 0; camera < kStreamCount; ++camera) {
    out += (camera == 0 ? "" : ", ");
    out += json_string(std::to_string(camera)) + ": " +
           std::to_string(device.exposure_us[camera]);
  }
  out += "},\n";
  out += "      \"gain\": {";
  for (std::uint8_t camera = 0; camera < kStreamCount; ++camera) {
    out += (camera == 0 ? "" : ", ");
    out += json_string(std::to_string(camera)) + ": " +
           std::to_string(device.gain[camera]);
  }
  out += "}\n";
  out += "    }\n";
  out += "  }";
  return out;
}

std::string render_reassembly(const RunMetrics& metrics) {
  std::string out = "  \"reassembly\": {\n";
  out += "    " + format_u64("bytes_received", metrics.bytes_received, "") + ",\n";
  out += "    " + format_u64("frames_completed", metrics.frames_completed, "") + ",\n";
  out += "    " + format_u64("frames_dropped", metrics.frames_dropped, "") + ",\n";
  out += "    " + format_u64("packets_parsed", metrics.packets_parsed, "") + ",\n";
  out += "    " + format_u64("parse_errors", metrics.parse_errors, "") + ",\n";
  out += "    " + format_u64("resync_bytes", metrics.resync_bytes, "") + ",\n";
  out += "    " + format_u64("staged_packets", metrics.staged_packets, "") + ",\n";
  out += "    " + format_u64("duplicate_packets", metrics.duplicate_packets, "") + ",\n";
  out += "    " + format_u64("orphan_packets", metrics.orphan_packets, "") + ",\n";
  out += "    " + format_u64("frame_restarts", metrics.frame_restarts, "") + ",\n";
  out += "    " + format_u64("sequence_gaps", metrics.sequence_gaps, "") + ",\n";
  out += "    " + format_u64("partial_timeouts", metrics.partial_timeouts, "") + ",\n";
  out += "    " + format_u64("queue_drops", metrics.queue_drops, "") + ",\n";
  out += "    " + format_u64("transfer_timeouts", metrics.transfer_timeouts, "") + ",\n";
  out += "    " + format_u64("transfer_stalls", metrics.transfer_stalls, "") + ",\n";
  out += "    " + format_u64("transfer_errors", metrics.transfer_errors, "") + ",\n";
  out += "    " + format_u64("frames_invalid", metrics.frames_invalid, "") + "\n";
  out += "  }";
  return out;
}

std::string render_source(const ManifestInfo& info) {
  std::string out = "  \"source\": {\n";
  if (!info.source_recording.empty()) {
    out += "    " + format_field("kind", std::string_view("recording"), "") + ",\n";
    out += "    " + format_field("path", info.source_recording, "") + ",\n";
    out += "    " + format_u64("transfers", info.source_transfers, "") + ",\n";
    out += "    " + format_u64("bytes", info.source_bytes, "") + ",\n";
    out += "    " + format_u64("digest_fnv1a64", info.source_digest, "") + ",\n";
    out += "    " + format_bool("verified", info.source_verified, "") + "\n";
  } else {
    out += "    " + format_field("kind", std::string_view("simulation"), "") + ",\n";
    out += "    " + format_u64("seed", info.simulation_seed, "") + ",\n";
    out += "    " + format_field("faults", info.simulation_faults, "") + ",\n";
    out += "    " + format_u64("frames", info.simulation_frames, "") + "\n";
  }
  out += "  }";
  return out;
}

}  // namespace

Result<ManifestWriteResult> write_frames_manifest(const std::string& path,
                                                  const ManifestInfo& info,
                                                  const GroupWriter& writer) {
  const GroupingStats& stats = writer.stats();
  const CadenceStats cadence = writer.cadence();
  std::string out = "{\n";
  out += "  " + format_field("schema", std::string_view("einstar-frames-v1"), "") + ",\n";
  out += "  " + format_field("tool", info.tool, "") + ",\n";
  out += "  " + format_field("backend", info.backend, "") + ",\n";
  out += "  " + format_bool("simulated", info.simulated, "") + ",\n";
  out += "  " + format_bool("live", info.live, "") + ",\n";
  out += "  " + format_field("output_directory", info.output_directory, "") + ",\n";
  out += "  \"frame\": {\"width\": " + std::to_string(writer.width()) +
         ", \"height\": " + std::to_string(writer.height()) +
         ", \"bit_depth\": 8, \"format\": \"pgm-p5\", \"bytes_per_frame\": " +
         std::to_string(static_cast<std::uint64_t>(writer.width()) * writer.height()) +
         "},\n";
  out += "  \"camera_map\": {\"0\": \"right_ir\", \"1\": \"left_ir\", \"2\": \"color\"},\n";
  out += "  \"group_id\": {\"source\": \"device_trigger_group_counter\", "
         "\"output_ids\": \"dense_0_based\"},\n";
  out += "  " + format_bool("complete", grouping_complete(stats), "") + ",\n";
  out += "  " + format_bool("stopped_early", info.stopped_early, "") + ",\n";
  out += "  " + format_u64("requested_groups", info.requested_groups, "") + ",\n";
  out += "  " + format_u64("groups_written", stats.groups_written, "") + ",\n";
  out += "  " + format_u64("groups_incomplete", stats.groups_incomplete, "") + ",\n";
  out += "  " + format_u64("groups_absent", stats.groups_absent, "") + ",\n";
  out += "  " + format_u64("duplicate_frames", stats.duplicate_frames, "") + ",\n";
  out += "  " + format_u64("late_frames", stats.late_frames, "") + ",\n";
  out += "  " + format_u64("frames_invalid", stats.frames_invalid, "") + ",\n";
  out += "  " + format_u64("bytes_written", stats.bytes_written, "") + ",\n";
  out += "  " + format_u64("bytes_received", info.metrics.bytes_received, "") + ",\n";
  out += "  " + format_raw("throughput_mbps", format_rate(info.throughput_mbps), "") + ",\n";
  out += "  " + format_raw("virtual_throughput_mbps", format_rate(info.virtual_throughput_mbps), "") + ",\n";
  out += "  " + format_raw("duration_s", format_seconds(info.metrics.wall_seconds), "") + ",\n";
  out += "  " + format_raw("virtual_duration_s", format_seconds(info.metrics.virtual_seconds), "") + ",\n";
  out += "  \"cadence\": {\"intervals\": " + std::to_string(cadence.intervals) +
         ", \"mean_ms\": " +
         format_rate(static_cast<double>(cadence.mean_ns) / 1e6) +
         ", \"min_ms\": " + format_rate(static_cast<double>(cadence.min_ns) / 1e6) +
         ", \"max_ms\": " + format_rate(static_cast<double>(cadence.max_ns) / 1e6) +
         ", \"fps\": " + format_rate(cadence.fps) + "},\n";
  out += "  \"safety\": {\"persistent_writes\": false, \"firmware_updates\": false, "
         "\"reboot\": false},\n";
  out += render_source(info) + ",\n";
  if (!info.recorded_path.empty()) {
    out += "  \"recording\": {\"path\": " + json_string(info.recorded_path) +
           ", \"transfers\": " + std::to_string(info.recorded_transfers) +
           ", \"bytes\": " + std::to_string(info.recorded_bytes) +
           ", \"digest_fnv1a64\": " + std::to_string(info.recorded_digest) + "},\n";
  }
  out += render_device(info.device) + ",\n";
  out += render_reassembly(info.metrics) + ",\n";

  out += "  \"groups\": [";
  for (std::size_t index = 0; index < writer.groups().size(); ++index) {
    const GroupRecord& group = writer.groups()[index];
    out += index == 0 ? "\n" : ",\n";
    out += "    {\"group\": " + std::to_string(group.group) +
           ", \"device_group\": " + std::to_string(group.device_group) +
           ", \"cameras\": 3, \"host_time_ns\": " +
           std::to_string(group.host_time_ns) + ",\n";
    out += "     \"stream0\": " +
           json_string(join_path(info.output_directory, group.files[0])) +
           ", \"stream1\": " +
           json_string(join_path(info.output_directory, group.files[1])) +
           ", \"stream2\": " +
           json_string(join_path(info.output_directory, group.files[2])) + ",\n";
    out += "     \"frame_ids\": {";
    for (std::uint8_t camera = 0; camera < kStreamCount; ++camera) {
      out += (camera == 0 ? "" : ", ");
      out += json_string(std::to_string(camera)) + ": " +
             std::to_string(group.frame_ids[camera]);
    }
    out += "},\n     \"digests\": {";
    for (std::uint8_t camera = 0; camera < kStreamCount; ++camera) {
      out += (camera == 0 ? "" : ", ");
      out += json_string(std::to_string(camera)) + ": " +
             std::to_string(group.digests[camera]);
    }
    out += "}}";
  }
  out += writer.groups().empty() ? "],\n" : "\n  ],\n";

  out += "  \"incomplete_groups\": [";
  for (std::size_t index = 0; index < writer.incomplete().size(); ++index) {
    const IncompleteGroup& group = writer.incomplete()[index];
    out += index == 0 ? "\n" : ",\n";
    out += "    {\"device_group\": " + std::to_string(group.device_group) +
           ", \"missing_mask\": " + std::to_string(group.missing_mask) +
           ", \"missing\": [";
    bool first = true;
    for (std::uint8_t camera = 0; camera < kStreamCount; ++camera) {
      if (group.present[camera]) {
        continue;
      }
      out += first ? "" : ", ";
      out += std::to_string(camera);
      first = false;
    }
    out += "], \"present\": [";
    first = true;
    for (std::uint8_t camera = 0; camera < kStreamCount; ++camera) {
      if (!group.present[camera]) {
        continue;
      }
      out += first ? "" : ", ";
      out += std::to_string(camera);
      first = false;
    }
    out += "], \"frame_ids\": {";
    first = true;
    for (std::uint8_t camera = 0; camera < kStreamCount; ++camera) {
      if (!group.present[camera]) {
        continue;
      }
      out += first ? "" : ", ";
      out += json_string(std::to_string(camera)) + ": " +
             std::to_string(group.frame_ids[camera]);
      first = false;
    }
    out += "}}";
  }
  out += writer.incomplete().empty() ? "],\n" : "\n  ],\n";

  out += "  \"duplicate_group_frames\": [";
  for (std::size_t index = 0; index < writer.duplicates().size(); ++index) {
    const DuplicateFrame& duplicate = writer.duplicates()[index];
    out += index == 0 ? "\n" : ",\n";
    out += "    {\"device_group\": " + std::to_string(duplicate.device_group) +
           ", \"camera\": " + std::to_string(duplicate.camera) +
           ", \"kept_frame_id\": " + std::to_string(duplicate.kept_frame_id) +
           ", \"dropped_frame_id\": " + std::to_string(duplicate.dropped_frame_id) +
           ", \"group_already_written\": " +
           (duplicate.group_already_written ? "true" : "false") + "}";
  }
  out += writer.duplicates().empty() ? "],\n" : "\n  ],\n";

  out += "  \"absent_group_ids\": [";
  for (std::size_t index = 0; index < writer.absent_groups().size(); ++index) {
    out += index == 0 ? "" : ", ";
    out += std::to_string(writer.absent_groups()[index]);
  }
  out += "]\n";
  out += "}\n";

  ManifestWriteResult result;
  result.path = path;
  Status status = write_text_atomic(path, out, result.bytes, result.fnv1a64);
  if (!status.ok()) {
    return fail(status);
  }
  return result;
}

// ---------------------------------------------------------------------------
// Replay
// ---------------------------------------------------------------------------

Result<ReplayReport> replay_recording(const ReplayOptions& options) {
  if (options.input.empty()) {
    return fail(ErrorCode::InvalidArgument, "recording path is empty");
  }
  auto recording = verify_recording(options.input);
  if (!recording) {
    return fail(recording.error());
  }
  std::uint64_t expected_bytes = 0;
  for (const auto& transfer : recording->transfers) {
    expected_bytes += transfer.bytes.size();
  }
  if (recording->transfers.empty() || expected_bytes == 0) {
    return fail(ErrorCode::Protocol,
                "refusing recording " + options.input +
                    ": it contains no transfer bytes to replay");
  }

  auto directory = prepare_output_directory(options.output_directory);
  if (!directory) {
    return fail(directory.error());
  }
  GroupWriter writer(*directory);
  if (Status status = writer.prepare(); !status.ok()) {
    return fail(status);
  }

  SimConfig sim;
  sim.expect_interval_quirk = false;
  sim.frames = 0;  // replay mode never generates
  sim.replay = recording::make_replay_source(*recording);

  PipelineConfig config;
  config.queue_depth = options.queue_depth == 0 ? 8 : options.queue_depth;
  config.copy_mode = CopyMode::Copy;
  config.frame_queue_capacity =
      options.frame_queue_capacity == 0 ? 32 : options.frame_queue_capacity;
  config.partial_frame_timeout_ns = options.partial_frame_timeout_ns;

  auto transport = std::make_unique<SimulatedTransport>(sim);
  SimulatedTransport* replay_transport = transport.get();
  auto pipeline =
      std::make_unique<AcquisitionPipeline>(std::move(transport), config);
  const auto wall_start = std::chrono::steady_clock::now();
  if (Status status = pipeline->start(); !status.ok()) {
    return fail(status);
  }
  const std::int64_t start_ns = pipeline->start_time_ns();

  bool truncated = false;
  bool stalled = false;
  bool exhausted_loop = false;
  std::size_t iterations = 0;
  std::size_t tail = 0;
  std::size_t no_progress = 0;
  std::uint64_t last_bytes = 0;
  const std::size_t max_iterations = 2'000'000 + expected_bytes / 2048;
  Frame frame;
  while (iterations++ < max_iterations) {
    (void)pipeline->poll(2);
    bool popped = false;
    while (pipeline->try_pop_frame(frame)) {
      popped = true;
      Status status = writer.add(frame);
      frame = Frame{};
      if (!status.ok()) {
        (void)pipeline->stop();
        return fail(status);
      }
      if (options.max_groups != 0 &&
          writer.stats().groups_written >= options.max_groups) {
        truncated = true;
        break;
      }
    }
    if (truncated) {
      break;
    }
    const PipelineStats stats = pipeline->stats();
    if (stats.bytes_received >= expected_bytes) {
      tail = popped ? 0 : tail + 1;
      if (tail >= 4) {
        break;
      }
    } else {
      if (stats.bytes_received == last_bytes && !popped) {
        if (++no_progress > 200'000) {
          stalled = true;
          break;
        }
      } else {
        no_progress = 0;
        last_bytes = stats.bytes_received;
      }
    }
  }
  if (iterations >= max_iterations) {
    exhausted_loop = true;
  }

  const auto wall_end = std::chrono::steady_clock::now();
  const std::int64_t end_ns = replay_transport->virtual_clock().now_ns();
  const Status stop_status = pipeline->stop();
  if (!stop_status.ok()) {
    return fail(stop_status);
  }
  if (stalled) {
    return fail(ErrorCode::Timeout,
                "replay stalled after " + std::to_string(writer.stats().groups_written) +
                    " group(s): the simulated transport stopped making progress");
  }
  if (exhausted_loop) {
    return fail(ErrorCode::Timeout,
                "replay exceeded its iteration budget after " +
                    std::to_string(writer.stats().groups_written) + " group(s)");
  }
  if (!truncated && pipeline->stats().bytes_received < expected_bytes) {
    return fail(ErrorCode::Protocol,
                "replay consumed " +
                    std::to_string(pipeline->stats().bytes_received) + " of " +
                    std::to_string(expected_bytes) +
                    " recorded bytes; the recording is truncated or has "
                    "unsupported transfer statuses");
  }

  if (truncated) {
    writer.discard_pending();
  } else {
    (void)writer.flush();
  }

  const PipelineStats stats = pipeline->stats();
  ReplayReport report;
  report.grouping = writer.stats();
  report.cadence = writer.cadence();
  report.groups = writer.groups();
  report.incomplete_groups = writer.incomplete();
  report.duplicates = writer.duplicates();
  report.absent_group_ids = writer.absent_groups();
  report.manifest.tool = "einstar-replay-frames";
  report.manifest.backend = "simulated";
  report.manifest.simulated = true;
  report.manifest.live = false;
  report.manifest.output_directory = *directory;
  report.manifest.requested_groups = options.max_groups;
  report.manifest.stopped_early = truncated;
  report.manifest.source_recording = options.input;
  report.manifest.source_transfers = recording->transfers.size();
  report.manifest.source_bytes = expected_bytes;
  report.manifest.source_digest = recording->trailer_digest;
  report.manifest.source_verified = true;
  report.manifest.device.simulated = true;
  report.manifest.device.identified = false;
  report.manifest.device.serial = "SIMULATED";
  report.manifest.device.serial_redacted = false;
  report.manifest.device.firmware = "simulated";
  report.manifest.device.sensor_count = kStreamCount;
  metrics_from_pipeline(report.manifest.metrics, stats);
  report.manifest.metrics.frames_invalid = writer.stats().frames_invalid;
  report.manifest.metrics.wall_seconds =
      seconds_between(wall_start, wall_end);
  report.manifest.metrics.virtual_seconds =
      end_ns > start_ns
          ? static_cast<double>(end_ns - start_ns) / 1e9
          : 0.0;
  report.manifest.throughput_mbps =
      mb_per_second(stats.bytes_received, report.manifest.metrics.wall_seconds);
  report.manifest.virtual_throughput_mbps = mb_per_second(
      stats.bytes_received, report.manifest.metrics.virtual_seconds);

  if (options.write_manifest) {
    const std::string path =
        join_path(*directory, "frames_manifest.json");
    auto written = write_frames_manifest(path, report.manifest, writer);
    if (!written) {
      return fail(written.error());
    }
    report.manifest_path = written->path;
  }
  if (writer.stats().groups_written == 0) {
    return fail(ErrorCode::Protocol,
                "replay verified " + options.input +
                    " but no complete three-camera group was reassembled "
                    "(all frames were lost or incomplete)");
  }
  return report;
}

// ---------------------------------------------------------------------------
// Simulated capture
// ---------------------------------------------------------------------------

Result<SimCaptureReport> capture_simulated(const SimCaptureOptions& options) {
  if (options.groups == 0) {
    return fail(ErrorCode::InvalidArgument, "--groups must be at least 1");
  }
  auto faults = fault_preset(options.faults);
  if (!faults) {
    return fail(faults.error());
  }
  if (options.trigger_period_us == 0) {
    return fail(ErrorCode::InvalidArgument, "--trigger period must be non-zero");
  }
  auto directory = prepare_output_directory(options.output_directory);
  if (!directory) {
    return fail(directory.error());
  }
  GroupWriter writer(*directory);
  if (Status status = writer.prepare(); !status.ok()) {
    return fail(status);
  }

  SimConfig sim;
  sim.seed = options.seed;
  sim.frames = options.groups * kStreamCount;
  sim.camera_count = kStreamCount;
  sim.first_camera_index = 0;
  sim.expect_interval_quirk = false;
  sim.trigger_period_ns = static_cast<std::int64_t>(options.trigger_period_us) * 1000;
  sim.bandwidth_bytes_per_sec = options.bandwidth_bytes_per_sec;
  sim.faults = *faults;

  PipelineConfig config;
  config.queue_depth = options.queue_depth == 0 ? 8 : options.queue_depth;
  config.copy_mode = CopyMode::Copy;
  config.frame_queue_capacity =
      options.frame_queue_capacity == 0 ? 32 : options.frame_queue_capacity;
  config.partial_frame_timeout_ns = options.partial_frame_timeout_ns;
  if (!options.recording_path.empty()) {
    config.enable_recording = true;
    config.recording_path = options.recording_path;
    config.recording_metadata =
        "backend=simulated;seed=" + std::to_string(options.seed) +
        ";faults=" + options.faults +
        ";groups=" + std::to_string(options.groups);
  }

  auto transport = std::make_unique<SimulatedTransport>(sim);
  SimulatedTransport* generator = transport.get();
  auto pipeline =
      std::make_unique<AcquisitionPipeline>(std::move(transport), config);
  const auto wall_start = std::chrono::steady_clock::now();
  if (Status status = pipeline->start(); !status.ok()) {
    return fail(status);
  }
  const std::int64_t start_ns = pipeline->start_time_ns();

  bool target_reached = false;
  bool exhausted_loop = false;
  std::size_t iterations = 0;
  std::size_t idle = 0;
  std::uint64_t last_bytes = 0;
  const std::size_t max_iterations =
      2'000'000 + static_cast<std::size_t>(options.groups) * 8192;
  Frame frame;
  while (iterations++ < max_iterations) {
    (void)pipeline->poll(2);
    bool popped = false;
    while (pipeline->try_pop_frame(frame)) {
      popped = true;
      Status status = writer.add(frame);
      frame = Frame{};
      if (!status.ok()) {
        (void)pipeline->stop();
        return fail(status);
      }
      if (writer.stats().groups_written >= options.groups) {
        target_reached = true;
        break;
      }
    }
    if (target_reached) {
      break;
    }
    if (generator->generated_frames() >= sim.frames) {
      const PipelineStats stats = pipeline->stats();
      if (stats.bytes_received == last_bytes && !popped) {
        ++idle;
      } else {
        idle = 0;
      }
      last_bytes = stats.bytes_received;
      if (idle >= 500) {
        break;
      }
    }
  }
  if (iterations >= max_iterations) {
    exhausted_loop = true;
  }

  const auto wall_end = std::chrono::steady_clock::now();
  const std::int64_t end_ns = generator->virtual_clock().now_ns();
  const Status stop_status = pipeline->stop();
  if (!stop_status.ok()) {
    return fail(stop_status);
  }
  if (exhausted_loop) {
    return fail(ErrorCode::Timeout,
                "simulated capture exceeded its iteration budget after " +
                    std::to_string(writer.stats().groups_written) + " group(s)");
  }

  (void)writer.flush();
  const PipelineStats stats = pipeline->stats();

  SimCaptureReport report;
  report.grouping = writer.stats();
  report.cadence = writer.cadence();
  report.groups = writer.groups();
  report.incomplete_groups = writer.incomplete();
  report.duplicates = writer.duplicates();
  report.absent_group_ids = writer.absent_groups();
  report.manifest.tool = "einstar-capture-frames";
  report.manifest.backend = "simulated";
  report.manifest.simulated = true;
  report.manifest.live = false;
  report.manifest.output_directory = *directory;
  report.manifest.requested_groups = options.groups;
  report.manifest.stopped_early = false;
  report.manifest.simulation_seed = options.seed;
  report.manifest.simulation_faults = options.faults;
  report.manifest.simulation_frames = sim.frames;
  report.manifest.device.simulated = true;
  report.manifest.device.identified = false;
  report.manifest.device.serial = "SIMULATED";
  report.manifest.device.serial_redacted = false;
  report.manifest.device.firmware = "simulated";
  report.manifest.device.sensor_count = kStreamCount;
  report.manifest.device.settings_applied = false;
  report.manifest.device.trigger_period_us = options.trigger_period_us;
  report.manifest.device.trigger_mono = 1;
  report.manifest.device.trigger_rgb = 1;
  metrics_from_pipeline(report.manifest.metrics, stats);
  report.manifest.metrics.frames_invalid = writer.stats().frames_invalid;
  report.manifest.metrics.wall_seconds = seconds_between(wall_start, wall_end);
  report.manifest.metrics.virtual_seconds =
      end_ns > start_ns ? static_cast<double>(end_ns - start_ns) / 1e9 : 0.0;
  report.manifest.throughput_mbps =
      mb_per_second(stats.bytes_received, report.manifest.metrics.wall_seconds);
  report.manifest.virtual_throughput_mbps = mb_per_second(
      stats.bytes_received, report.manifest.metrics.virtual_seconds);

  if (!options.recording_path.empty()) {
    auto recorded = verify_recording(options.recording_path);
    if (!recorded) {
      return fail(recorded.error());
    }
    std::uint64_t recorded_bytes = 0;
    for (const auto& transfer : recorded->transfers) {
      recorded_bytes += transfer.bytes.size();
    }
    report.manifest.recorded_path = options.recording_path;
    report.manifest.recorded_transfers = recorded->transfers.size();
    report.manifest.recorded_bytes = recorded_bytes;
    report.manifest.recorded_digest = recorded->trailer_digest;
  }

  const std::string manifest_path = join_path(*directory, "frames_manifest.json");
  auto written = write_frames_manifest(manifest_path, report.manifest, writer);
  if (!written) {
    return fail(written.error());
  }
  report.manifest_path = written->path;

  if (report.grouping.groups_written == 0) {
    return fail(ErrorCode::Exhausted,
                "simulated capture wrote no complete three-camera group");
  }
  if (options.faults == "clean" && !target_reached) {
    return fail(ErrorCode::Internal,
                "clean simulated capture stopped after " +
                    std::to_string(report.grouping.groups_written) + " of " +
                    std::to_string(options.groups) +
                    " requested group(s); this is a bug, not a fault");
  }
  return report;
}

}  // namespace einstar::capture
