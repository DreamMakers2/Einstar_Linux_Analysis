// SPDX-License-Identifier: MIT
// Implementation of the acquisition-side capture/replay core.
#include "capture_core.hpp"

#include <algorithm>
#include <array>
#include <cerrno>
#include <chrono>
#include <charconv>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <limits>
#include <map>
#include <set>
#include <string>
#include <system_error>
#include <thread>
#include <utility>
#include <vector>

#include <sys/stat.h>
#include <sys/statvfs.h>
#include <fcntl.h>
#include <poll.h>
#include <unistd.h>
#ifdef __linux__
#include <sys/prctl.h>
#endif

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

/// Strips spaces and tabs (not newlines) from both ends.
std::string_view trim_controls(std::string_view text) {
  while (!text.empty() && (text.front() == ' ' || text.front() == '\t' ||
                           text.front() == '\r')) {
    text.remove_prefix(1);
  }
  while (!text.empty() && (text.back() == ' ' || text.back() == '\t' ||
                           text.back() == '\r')) {
    text.remove_suffix(1);
  }
  return text;
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
         stats.slot_id_mismatches == 0 &&
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

  // Cross-camera sync check: the three frames of one trigger group must carry
  // the same per-exposure slot id.  The u64 group counter is the grouping key;
  // this is an independent consistency check on it.
  const std::uint32_t reference_slot = pending.streams[0]->frame_id;
  for (std::uint8_t camera = 1; camera < kStreamCount; ++camera) {
    if (pending.streams[camera]->frame_id != reference_slot) {
      stats_.slot_id_mismatches++;
      break;
    }
  }

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
  evict_old_groups();
  return Status::success();
}

void GroupWriter::evict_old_groups() {
  if (ring_size_ == 0) {
    return;
  }
  // `groups_` keeps every record for the manifest; only the oldest files that
  // are still on disk are removed.  The newest group (index size()-1) is never
  // selected because the loop stops once `groups_on_disk() == ring_size_`.
  while (groups_on_disk() > ring_size_) {
    const GroupRecord& oldest =
        groups_[static_cast<std::size_t>(groups_evicted_)];
    for (std::uint8_t camera = 0; camera < kStreamCount; ++camera) {
      const std::string& name = oldest.files[camera];
      if (!safe_frame_filename(name)) {
        continue;
      }
      std::error_code ignored;
      std::filesystem::remove(file_path(name), ignored);
      // The triplet is complete before this runs, so a .tmp should not exist;
      // remove it anyway so an interrupted earlier attempt cannot leak one.
      std::filesystem::remove(file_path(name + ".tmp"), ignored);
    }
    ++groups_evicted_;
  }
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

void GroupWriter::discard_pending() noexcept {
  pending_.clear();
  update_absent_groups();
}

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
  metrics.dropped_incomplete = stats.dropped_incomplete;
  metrics.dropped_overflow = stats.dropped_overflow;
  metrics.dropped_stale = stats.dropped_stale;
  metrics.dropped_capacity = stats.dropped_capacity;
  metrics.dropped_pool = stats.dropped_pool;
  metrics.dropped_timeout = stats.dropped_timeout;
  metrics.dropped_flush = stats.dropped_flush;
  metrics.packet_index_gaps = stats.packet_index_gaps;
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
  out += "},\n";
  out += "      \"illuminator\": {\"laser_percent\": " +
         (device.laser_percent ? std::to_string(*device.laser_percent)
                               : std::string("null")) +
         ", \"laser_applied\": " + (device.laser_set ? "true" : "false") +
         ", \"strobe\": {\"route0\": " +
         (device.strobe[0] ? std::to_string(*device.strobe[0])
                           : std::string("null")) +
         ", \"route1\": " +
         (device.strobe[1] ? std::to_string(*device.strobe[1])
                           : std::string("null")) +
         "}, \"applied\": " +
         ((device.laser_set || device.strobe_set[0] || device.strobe_set[1])
              ? "true"
              : "false") +
         "}\n";
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
  out += "    " + format_u64("frames_invalid", metrics.frames_invalid, "") + ",\n";
  // Per-reason drop counters: an actionable live diagnostic rather than a
  // single opaque `frames_dropped`.
  out += "    \"drop_reasons\": {";
  out += "\"incomplete\": " + std::to_string(metrics.dropped_incomplete);
  out += ", \"overflow\": " + std::to_string(metrics.dropped_overflow);
  out += ", \"stale\": " + std::to_string(metrics.dropped_stale);
  out += ", \"capacity\": " + std::to_string(metrics.dropped_capacity);
  out += ", \"pool\": " + std::to_string(metrics.dropped_pool);
  out += ", \"timeout\": " + std::to_string(metrics.dropped_timeout);
  out += ", \"flush\": " + std::to_string(metrics.dropped_flush);
  out += "},\n";
  out += "    " + format_u64("packet_index_gaps", metrics.packet_index_gaps, "") + "\n";
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
  } else if (!info.simulated) {
    out += "    " + format_field("kind", std::string_view("live-device"), "") + ",\n";
    out += "    " + format_field("framing", std::string_view("raw"), "") + "\n";
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
  out += "  " + format_u64("slot_id_mismatches", stats.slot_id_mismatches, "") + ",\n";
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
// Control file, ring eviction and session state
// ---------------------------------------------------------------------------

std::string_view control_command_name(ControlCommand command) noexcept {
  switch (command) {
    case ControlCommand::None: return "none";
    case ControlCommand::Start: return "start";
    case ControlCommand::Pause: return "pause";
    case ControlCommand::Resume: return "resume";
    case ControlCommand::Stop: return "stop";
  }
  return "none";
}

ControlCommand parse_control_text(std::string_view text) {
  ControlCommand command = ControlCommand::None;
  while (!text.empty()) {
    const std::size_t newline = text.find('\n');
    std::string_view line = text.substr(0, newline);
    if (newline == std::string_view::npos) {
      text = {};
    } else {
      text.remove_prefix(newline + 1);
    }
    line = trim_controls(line);
    if (line.empty()) {
      continue;
    }
    // Unknown lines are ignored: they neither change the command nor undo the
    // last recognised one.  This makes a trailing comment harmless.
    if (line == "start") {
      command = ControlCommand::Start;
    } else if (line == "pause") {
      command = ControlCommand::Pause;
    } else if (line == "resume") {
      command = ControlCommand::Resume;
    } else if (line == "stop") {
      command = ControlCommand::Stop;
    }
  }
  return command;
}

namespace {

bool parse_token_uint(std::string_view token, unsigned base, std::uint64_t& value) {
  if (token.empty()) {
    return false;
  }
  std::string text(token);
  char* end = nullptr;
  errno = 0;
  const unsigned long long parsed = std::strtoull(text.c_str(), &end, base);
  if (errno != 0 || end == text.c_str() || *end != '\0') {
    return false;
  }
  value = static_cast<std::uint64_t>(parsed);
  return true;
}

bool parse_camera_uint(std::string_view token, unsigned& camera,
                       std::uint64_t& value) {
  const std::size_t colon = token.find(':');
  if (colon == std::string_view::npos) {
    return false;
  }
  std::uint64_t cam = 0;
  if (!parse_token_uint(token.substr(0, colon), 10, cam) || cam > 2) {
    return false;
  }
  if (!parse_token_uint(token.substr(colon + 1), 10, value)) {
    return false;
  }
  camera = static_cast<unsigned>(cam);
  return true;
}

}  // namespace

/// Poll a tiny volatile-settings file.  The wizard rewrites it atomically
/// (write + rename), so a partial file is never observed.  Unknown lines and
/// out-of-range values are reported once and otherwise ignored.
class VolatileSettingsFile {
 public:
  explicit VolatileSettingsFile(std::string path) : path_(std::move(path)) {}

  std::optional<VolatileSettings> poll() {
    if (path_.empty()) {
      return std::nullopt;
    }
    struct stat info {};
    if (::stat(path_.c_str(), &info) != 0) {
      have_stat_ = false;
      return std::nullopt;
    }
    const auto device = static_cast<std::uint64_t>(info.st_dev);
    const auto inode = static_cast<std::uint64_t>(info.st_ino);
    const auto size = static_cast<std::uint64_t>(info.st_size);
    const auto mtime_sec = static_cast<std::int64_t>(info.st_mtim.tv_sec);
    const auto mtime_nsec = static_cast<std::int64_t>(info.st_mtim.tv_nsec);
    if (have_stat_ && device == device_ && inode == inode_ && size == size_ &&
        mtime_sec == mtime_sec_ && mtime_nsec == mtime_nsec_) {
      return std::nullopt;
    }
    std::ifstream file(path_, std::ios::binary);
    if (!file) {
      have_stat_ = false;
      return std::nullopt;
    }
    std::string text((std::istreambuf_iterator<char>(file)),
                     std::istreambuf_iterator<char>());
    if (text.size() > 64 * 1024) {
      text.resize(64 * 1024);
    }
    have_stat_ = true;
    device_ = device;
    inode_ = inode;
    size_ = size;
    mtime_sec_ = mtime_sec;
    mtime_nsec_ = mtime_nsec;

    VolatileSettings out;
    std::array<bool, 3> exposure_seen{};
    std::array<bool, 3> gain_seen{};
    std::size_t pos = 0;
    bool bad = false;
    while (pos < text.size()) {
      const std::size_t end = text.find('\n', pos);
      std::string line = text.substr(pos, end == std::string::npos
                                             ? std::string::npos : end - pos);
      pos = end == std::string::npos ? text.size() : end + 1;
      line = std::string(trim_controls(line));
      if (line.empty() || line[0] == '#') {
        continue;
      }
      std::size_t sep = line.find_first_of(" \t");
      const std::string key = line.substr(0, sep);
      std::size_t cursor = sep;
      while (cursor < line.size()) {
        while (cursor < line.size() &&
               (line[cursor] == ' ' || line[cursor] == '\t')) {
          ++cursor;
        }
        const std::size_t token_end = line.find_first_of(" \t", cursor);
        const std::string_view token(line.data() + cursor,
                                     (token_end == std::string::npos
                                          ? line.size() : token_end) - cursor);
        cursor = token_end == std::string::npos ? line.size() : token_end;
        if (key == "exposure" || key == "gain") {
          unsigned camera = 0;
          std::uint64_t value = 0;
          if (!parse_camera_uint(token, camera, value)) {
            bad = true;
            continue;
          }
          if (key == "exposure") {
            if (value < 10 || value > 1000000) {
              bad = true;
              continue;
            }
            out.exposure_us[camera] = static_cast<std::uint32_t>(value);
            exposure_seen[camera] = true;
          } else {
            if (value > 65535) {
              bad = true;
              continue;
            }
            out.gain[camera] = static_cast<std::uint16_t>(value);
            gain_seen[camera] = true;
          }
        } else if (key == "laser") {
          std::uint64_t value = 0;
          if (!parse_token_uint(token, 10, value) || value > 100) {
            bad = true;
            continue;
          }
          out.laser_percent = static_cast<std::uint8_t>(value);
        }
      }
    }
    if (bad && !warned_) {
      warned_ = true;
      std::fprintf(stderr, "warning: ignoring malformed volatile settings line\n");
    }
    // An exposure/gain block must name all three cameras.  A partial line
    // would otherwise zero the cameras it omitted and the live driver would
    // try to write an invalid value.
    out.has_exposure = exposure_seen[0] && exposure_seen[1] && exposure_seen[2];
    out.has_gain = gain_seen[0] && gain_seen[1] && gain_seen[2];
    if (!out.has_exposure && !out.has_gain && !out.laser_percent) {
      return std::nullopt;
    }
    return out;
  }

 private:
  std::string path_;
  bool have_stat_ = false;
  bool warned_ = false;
  std::uint64_t device_ = 0;
  std::uint64_t inode_ = 0;
  std::uint64_t size_ = 0;
  std::int64_t mtime_sec_ = 0;
  std::int64_t mtime_nsec_ = 0;
};

ControlFile::ControlFile(std::string path) : path_(std::move(path)) {}

ControlCommand ControlFile::poll() {
  if (path_.empty()) {
    command_ = ControlCommand::None;
    return command_;
  }
  struct stat info {};
  if (::stat(path_.c_str(), &info) != 0) {
    // A missing control file means "keep running": it never changes state and
    // is not an error.  The next appearance is read because have_stat_ is off.
    have_stat_ = false;
    command_ = ControlCommand::None;
    return command_;
  }
  const auto device = static_cast<std::uint64_t>(info.st_dev);
  const auto inode = static_cast<std::uint64_t>(info.st_ino);
  const auto size = static_cast<std::uint64_t>(info.st_size);
  const auto mtime_sec = static_cast<std::int64_t>(info.st_mtim.tv_sec);
  const auto mtime_nsec = static_cast<std::int64_t>(info.st_mtim.tv_nsec);
  if (have_stat_ && device == device_ && inode == inode_ && size == size_ &&
      mtime_sec == mtime_sec_ && mtime_nsec == mtime_nsec_) {
    return command_;  // stat only: no read, negligible cost
  }

  // A GUI writes a tiny command file atomically.  Read at most the last 64 KiB
  // so a hostile/absurd file cannot stall the capture loop.
  constexpr std::uint64_t kMaxControlBytes = 64 * 1024;
  std::string text;
  {
    std::ifstream file(path_, std::ios::binary);
    if (!file) {
      have_stat_ = false;
      command_ = ControlCommand::None;
      return command_;
    }
    if (size > kMaxControlBytes) {
      file.seekg(static_cast<std::streamoff>(size - kMaxControlBytes),
                 std::ios::beg);
    }
    text.assign(std::istreambuf_iterator<char>(file),
                std::istreambuf_iterator<char>());
  }
  ++reads_;
  command_ = parse_control_text(text);
  have_stat_ = true;
  device_ = device;
  inode_ = inode;
  size_ = size;
  mtime_sec_ = mtime_sec;
  mtime_nsec_ = mtime_nsec;
  return command_;
}

std::vector<std::uint32_t> ring_eviction_set(
    std::span<const std::uint32_t> written_groups, std::uint32_t ring_size) {
  if (ring_size == 0 || written_groups.size() <= ring_size) {
    return {};
  }
  const std::size_t remove = written_groups.size() - ring_size;
  return std::vector<std::uint32_t>(written_groups.begin(),
                                    written_groups.begin() +
                                        static_cast<std::ptrdiff_t>(remove));
}

bool safe_frame_filename(std::string_view name) noexcept {
  if (name.empty() || name == "." || name == "..") {
    return false;
  }
  return name.find('/') == std::string_view::npos &&
         name.find('\\') == std::string_view::npos &&
         name.find('\0') == std::string_view::npos;
}

std::string_view session_state_name(SessionState state) noexcept {
  switch (state) {
    case SessionState::Starting: return "starting";
    case SessionState::Running: return "running";
    case SessionState::Paused: return "paused";
    case SessionState::Stopping: return "stopping";
    case SessionState::Stopped: return "stopped";
  }
  return "stopped";
}

std::string_view stop_reason_name(StopReason reason) noexcept {
  switch (reason) {
    case StopReason::None: return {};
    case StopReason::Groups: return "groups";
    case StopReason::MaxSeconds: return "max_seconds";
    case StopReason::SignalInt: return "signal:SIGINT";
    case StopReason::SignalTerm: return "signal:SIGTERM";
    case StopReason::ControlStop: return "control:stop";
    case StopReason::Error: return "error";
  }
  return "error";
}

std::string build_live_status_json(const LiveStatus& status) {
  std::string out = "{\"schema\":\"einstar-live-status-v1\",";
  out += "\"state\":";
  out += json_string(session_state_name(status.state));
  out += ",\"stop_reason\":";
  const std::string_view reason = stop_reason_name(status.stop_reason);
  out += reason.empty() ? "null" : json_string(reason);
  out += ",\"elapsed_s\":";
  {
    char buffer[64];
    std::snprintf(buffer, sizeof(buffer), "%.6f", status.elapsed_s);
    out += buffer;
  }
  out += ",\"groups_written\":" + std::to_string(status.groups_written);
  out += ",\"groups_requested\":" + std::to_string(status.groups_requested);
  out += ",\"latest_group\":" + std::to_string(status.latest_group);
  out += ",\"last_group\":" + std::to_string(status.last_group);
  out += ",\"ring_size\":" + std::to_string(status.ring_size);
  out += ",\"groups_on_disk\":" + std::to_string(status.groups_on_disk);
  out += ",\"groups_evicted\":" + std::to_string(status.groups_evicted);
  out += ",\"frames_written\":" + std::to_string(status.frames_written);
  out += ",\"bytes_written\":" + std::to_string(status.bytes_written);
  out += ",\"disk_free_bytes\":" + std::to_string(status.disk_free_bytes);
  out += ",\"frames_completed\":" + std::to_string(status.frames_completed);
  out += ",\"frames_dropped\":" + std::to_string(status.frames_dropped);
  out += ",\"bytes_received\":" + std::to_string(status.bytes_received);
  out += ",\"throughput_mbps\":";
  {
    char buffer[64];
    std::snprintf(buffer, sizeof(buffer), "%.3f", status.throughput_mbps);
    out += buffer;
  }
  out += ",\"transfer_timeouts\":" + std::to_string(status.transfer_timeouts);
  out += ",\"frames_invalid\":" + std::to_string(status.frames_invalid);
  out += ",\"output_directory\":" + json_string(status.output_directory);
  out += ",\"trigger_period_us\":" + std::to_string(status.trigger_period_us);
  out += ",\"exposure_us\":[";
  for (std::uint8_t camera = 0; camera < kStreamCount; ++camera) {
    out += (camera == 0 ? "" : ",") + std::to_string(status.exposure_us[camera]);
  }
  out += "],\"gain\":[";
  for (std::uint8_t camera = 0; camera < kStreamCount; ++camera) {
    out += (camera == 0 ? "" : ",") + std::to_string(status.gain[camera]);
  }
  out += "],\"laser_percent\":";
  out += status.laser_percent ? std::to_string(*status.laser_percent) : "null";
  out += ",\"cleanup_attempted\":";
  out += status.cleanup_attempted ? "true" : "false";
  out += ",\"cleanup_verified\":";
  out += status.cleanup_verified ? "true" : "false";
  if (status.trigger_counts) {
    out += ",\"trigger_counts\":{\"mono\":" +
           std::to_string((*status.trigger_counts)[0]) + ",\"rgb\":" +
           std::to_string((*status.trigger_counts)[1]) + "}";
  }
  out += ",\"simulated\":";
  out += status.simulated ? "true" : "false";
  out += ",\"live\":";
  out += status.live ? "true" : "false";
  out += "}\n";
  return out;
}

Status write_status_file(const std::string& path, std::string_view json) {
  if (path.empty()) {
    return Status::success();
  }
  const std::filesystem::path target(path);
  if (target.has_parent_path()) {
    const std::string parent = target.parent_path().string();
    if (!parent.empty()) {
      if (Status status = ensure_directory(parent); !status.ok()) {
        return status;
      }
    }
  }
  std::uint64_t bytes = 0;
  std::uint64_t digest = 0;
  return write_text_atomic(path, json, bytes, digest);
}

std::uint64_t disk_free_bytes(const std::string& path) noexcept {
  struct statvfs info {};
  if (path.empty() || ::statvfs(path.c_str(), &info) != 0) {
    return 0;
  }
  return static_cast<std::uint64_t>(info.f_bavail) *
         static_cast<std::uint64_t>(info.f_frsize);
}

// ---------------------------------------------------------------------------
// Signal flag
// ---------------------------------------------------------------------------

namespace {
static_assert(std::atomic<int>::is_always_lock_free);
std::atomic<int> g_stop_signal{0};
std::atomic<int> g_shutdown_requested{0};
}  // namespace

void notify_stop_signal(int signum) noexcept {
  if (signum != SIGINT && signum != SIGTERM) {
    return;
  }
  int expected = 0;
  (void)g_stop_signal.compare_exchange_strong(expected, signum);
  g_shutdown_requested.store(1);
}

int take_stop_signal() noexcept {
  return g_stop_signal.exchange(0);
}

Status install_signal_handlers() {
  struct sigaction action {};
  action.sa_handler = [](int signum) { notify_stop_signal(signum); };
  sigemptyset(&action.sa_mask);
  action.sa_flags = 0;  // the loop polls with a 5 ms timeout
  if (::sigaction(SIGINT, &action, nullptr) != 0 ||
      ::sigaction(SIGTERM, &action, nullptr) != 0) {
    return Status::error(ErrorCode::IoError,
                         "cannot install capture signal handlers");
  }
  return Status::success();
}

ProcessSafetyGuard::~ProcessSafetyGuard() {
  finished_.store(true);
  if (watchdog_.joinable()) {
    watchdog_.join();
  }
  if (heartbeat_fd_ >= 0) {
    (void)::close(heartbeat_fd_);
  }
}

Status ProcessSafetyGuard::arm_from_environment() {
  if (watchdog_.joinable()) {
    return Status::error(ErrorCode::InvalidArgument, "process safety already armed");
  }
  // Never clear a pending signal: one may have arrived during process startup.
  if (const Status handlers = install_signal_handlers(); !handlers.ok()) {
    return handlers;
  }
  const auto parse_env = [](const char* name, int& value) {
    const char* text = std::getenv(name);
    if (text == nullptr) {
      return true;
    }
    const char* end = text + std::strlen(text);
    const auto parsed = std::from_chars(text, end, value);
    return parsed.ec == std::errc{} && parsed.ptr == end && value > 0;
  };
  int parent = static_cast<int>(::getppid());
  int fd = -1;
  if (!parse_env("EINSTAR_CAPTURE_PARENT_PID", parent) ||
      !parse_env("EINSTAR_CAPTURE_HEARTBEAT_FD", fd)) {
    return Status::error(ErrorCode::InvalidArgument, "invalid process safety environment");
  }
#ifdef __linux__
  if (::prctl(PR_SET_PDEATHSIG, SIGTERM) != 0) {
    return Status::error(ErrorCode::IoError, "PR_SET_PDEATHSIG failed");
  }
#else
  if (std::getenv("EINSTAR_CAPTURE_PARENT_PID") != nullptr) {
    return Status::error(ErrorCode::InvalidArgument, "wizard parent safety requires Linux");
  }
#endif
  // Covers parent death before exec/main and between getppid and prctl.
  if (parent <= 0 || ::getppid() != parent) {
    return Status::error(ErrorCode::IoError, "capture parent already exited (startup race)");
  }
  if (fd >= 0) {
    struct stat metadata {};
    const int flags = ::fcntl(fd, F_GETFL);
    if (fd < 3 || flags < 0 || ::fstat(fd, &metadata) != 0 ||
        !S_ISFIFO(metadata.st_mode) ||
        ::fcntl(fd, F_SETFL, flags | O_NONBLOCK) != 0) {
      return Status::error(ErrorCode::InvalidArgument, "invalid heartbeat pipe");
    }
    heartbeat_fd_ = fd;
  }
  watchdog_ = std::thread([this, parent] {
    using Clock = std::chrono::steady_clock;
    auto heartbeat = Clock::now();
    auto deadline = Clock::time_point::max();
    while (!finished_.load()) {
      struct pollfd descriptor {heartbeat_fd_, POLLIN, 0};
      const int ready = ::poll(&descriptor, heartbeat_fd_ >= 0 ? 1U : 0U, 50);
      const auto now = Clock::now();
      const char* reason = nullptr;
      if (::getppid() != parent) {
        reason = "process safety: parent exited; requesting cleanup\n";
      } else if (heartbeat_fd_ >= 0) {
        if (ready > 0 && (descriptor.revents & POLLIN) != 0) {
          char bytes[256];
          const auto count = ::read(heartbeat_fd_, bytes, sizeof(bytes));
          if (count > 0) {
            heartbeat = now;
          } else if (count == 0 || (errno != EAGAIN && errno != EINTR)) {
            reason = "process safety: heartbeat pipe closed; requesting cleanup\n";
          }
        }
        if (ready > 0 && (descriptor.revents & (POLLHUP | POLLERR | POLLNVAL)) != 0) {
          reason = "process safety: heartbeat pipe closed; requesting cleanup\n";
        } else if (now - heartbeat >= std::chrono::seconds(2)) {
          reason = "process safety: heartbeat expired; requesting cleanup\n";
        }
      }
      if (reason != nullptr && deadline == Clock::time_point::max()) {
        notify_stop_signal(SIGTERM);
        // Avoid stdio locks held by an acquisition thread during teardown.
        (void)::write(STDERR_FILENO, reason, std::strlen(reason));
      }
      if (g_shutdown_requested.load() != 0 && deadline == Clock::time_point::max()) {
        deadline = now + std::chrono::seconds(5);
      }
      if (now >= deadline) {
        constexpr char message[] = "process safety: cleanup deadline exceeded; illuminator state UNVERIFIED; forcing SIGKILL\n";
        (void)::write(STDERR_FILENO, message, sizeof(message) - 1);
        (void)::kill(::getpid(), SIGKILL);
        ::_exit(128 + SIGKILL);
      }
    }
  });
  return Status::success();
}

// ---------------------------------------------------------------------------
// Capture session loop
// ---------------------------------------------------------------------------

Result<CaptureSessionResult> run_capture_session(
    const CaptureSessionOptions& options, CaptureSessionDriver& driver) {
  // Includes preparation failures and exception unwinding before the loop.
  struct StopOnExit {
    CaptureSessionDriver& driver;
    ~StopOnExit() { (void)driver.stop(); }
  } stop_on_exit{driver};
  auto directory = prepare_output_directory(options.output_directory);
  if (!directory) {
    return fail(directory.error());
  }
  GroupWriter writer(*directory);
  writer.set_ring_size(options.ring_size);
  if (Status status = writer.prepare(); !status.ok()) {
    return fail(status);
  }

  const bool has_control = !options.control_path.empty();
  ControlFile control(options.control_path);
  VolatileSettingsFile volatile_settings(options.volatility_path);
  bool volatility_warned = false;
  if (const Status handlers = install_signal_handlers(); !handlers.ok()) {
    return fail(handlers);
  }

  ManifestInfo manifest = options.manifest;
  manifest.output_directory = *directory;
  manifest.requested_groups = options.groups;

  const auto start = std::chrono::steady_clock::now();
  const auto status_interval = std::chrono::milliseconds(500);
  auto next_status = start;
  auto next_progress = start;

  SessionState state = SessionState::Starting;
  StopReason reason = StopReason::None;
  bool timed_out = false;
  bool target_reached = false;
  int signal_number = 0;
  bool status_warned = false;
  Status failure = Status::success();

  // Freeze the reported capture duration when the loop ends so the final
  // status does not grow while the recording is verified and the manifest is
  // written.  While streaming, this is simply the live wall clock.
  double frozen_elapsed = -1.0;
  const auto elapsed = [&]() {
    if (frozen_elapsed >= 0.0) {
      return frozen_elapsed;
    }
    return std::chrono::duration<double>(std::chrono::steady_clock::now() -
                                        start)
        .count();
  };

  const auto publish = [&](SessionState current) {
    if (options.status_file.empty()) {
      return;
    }
    const PipelineStats live = driver.stats();
    const DeviceInfo& device = driver.device();
    LiveStatus status;
    status.state = current;
    status.stop_reason = reason;
    status.elapsed_s = elapsed();
    status.groups_written = writer.stats().groups_written;
    status.groups_requested = options.groups;
    status.latest_group =
        writer.stats().groups_written == 0
            ? 0
            : writer.stats().groups_written - 1;
    if (!writer.groups().empty()) {
      status.last_group = writer.groups().back().device_group;
    }
    status.ring_size = options.ring_size;
    status.groups_on_disk = writer.groups_on_disk();
    status.groups_evicted = writer.groups_evicted();
    status.frames_written = writer.stats().frames_written;
    status.bytes_written = writer.stats().bytes_written;
    status.disk_free_bytes = disk_free_bytes(*directory);
    status.frames_completed = live.frames_completed;
    status.frames_dropped = live.frames_dropped;
    status.bytes_received = live.bytes_received;
    status.throughput_mbps =
        live.bytes_received / 1.0e6 / (status.elapsed_s > 0.0 ? status.elapsed_s : 1.0);
    status.transfer_timeouts = live.transfer_timeouts;
    status.frames_invalid = writer.stats().frames_invalid;
    status.output_directory = *directory;
    status.trigger_period_us = device.trigger_period_us;
    status.exposure_us = device.exposure_us;
    status.gain = device.gain;
    status.laser_percent = device.laser_percent;
    status.cleanup_attempted = device.cleanup_attempted;
    status.cleanup_verified = device.cleanup_verified;
    if (device.cleanup_verified) {
      status.trigger_counts = std::array<std::uint8_t, 2>{device.trigger_mono,
                                                       device.trigger_rgb};
    }
    status.simulated = manifest.simulated;
    status.live = manifest.live;
    const Status written = write_status_file(
        options.status_file, build_live_status_json(status));
    if (!written.ok() && !status_warned) {
      status_warned = true;
      std::fprintf(stderr, "warning: cannot publish status file %s: %s\n",
                   options.status_file.c_str(),
                   written.message().c_str());
    }
  };

  publish(SessionState::Starting);
  if (const int pending = take_stop_signal(); pending != 0) {
    signal_number = pending;
    reason = pending == SIGINT ? StopReason::SignalInt : StopReason::SignalTerm;
  } else if (Status started = driver.start(); !started.ok()) {
    failure = started;
    reason = StopReason::Error;
  } else {
    state = SessionState::Running;
    publish(state);
  }

  Frame frame;
  while (reason == StopReason::None) {
    const auto now = std::chrono::steady_clock::now();

    // -- signals: the handler only sets a flag; all teardown happens below ---
    if (const int pending = take_stop_signal(); pending != 0) {
      signal_number = pending;
      reason = pending == SIGINT ? StopReason::SignalInt : StopReason::SignalTerm;
      break;
    }

    // -- control file ------------------------------------------------------
    if (has_control) {
      switch (control.poll()) {
        case ControlCommand::Stop:
          reason = StopReason::ControlStop;
          break;
        case ControlCommand::Pause:
          if (state == SessionState::Running) {
            if (Status paused = driver.pause(); !paused.ok()) {
              failure = paused;
              reason = StopReason::Error;
              break;
            }
            state = SessionState::Paused;
            publish(state);
          }
          break;
        case ControlCommand::Resume:
          if (state == SessionState::Paused) {
            if (Status resumed = driver.resume(); !resumed.ok()) {
              failure = resumed;
              reason = StopReason::Error;
              break;
            }
            state = SessionState::Running;
            publish(state);
          }
          break;
        case ControlCommand::Start:
        case ControlCommand::None:
          break;
      }
      if (reason != StopReason::None) {
        break;
      }
    }

    // -- volatile exposure/gain/laser changes ------------------------------
    if (auto requested = volatile_settings.poll(); requested.has_value()) {
      if (state == SessionState::Running) {
        const Status applied = driver.apply_volatile(*requested);
        if (!applied.ok() && !volatility_warned) {
          volatility_warned = true;
          std::fprintf(stderr,
                       "warning: volatile settings change rejected: %s\n",
                       applied.message().c_str());
        } else if (applied.ok()) {
          publish(state);
        }
      }
    }

    // -- limits ------------------------------------------------------------
    if (options.groups != 0 &&
        writer.stats().groups_written >= options.groups) {
      target_reached = true;
      reason = StopReason::Groups;
      break;
    }
    if (options.max_seconds > 0.0 && elapsed() >= options.max_seconds) {
      reason = StopReason::MaxSeconds;
      break;
    }
    if (options.timeout_seconds_set && options.timeout_seconds > 0 &&
        now - start >= std::chrono::seconds(options.timeout_seconds)) {
      timed_out = true;
      reason = StopReason::Error;
      break;
    }

    // -- pump --------------------------------------------------------------
    if (state == SessionState::Paused) {
      // Live keeps draining in-flight USB transfers here; the simulator uses
      // it to sleep without generating new frames.
      (void)driver.pump_paused(5);
    } else {
      (void)driver.poll(5);
      bool popped_any = false;
      while (driver.try_pop_frame(frame)) {
        popped_any = true;
        const Status added = writer.add(frame);
        frame = Frame{};
        if (!added.ok()) {
          failure = added;
          reason = StopReason::Error;
          break;
        }
        if (options.groups != 0 &&
            writer.stats().groups_written >= options.groups) {
          target_reached = true;
          reason = StopReason::Groups;
          break;
        }
      }
      if (reason != StopReason::None) {
        break;
      }
      if (!popped_any && driver.source_exhausted()) {
        // A finite simulated source (with injected loss) ended before the
        // requested group count.  Stop now instead of idling until the wall
        // clock safety limit; the manifest reports what was actually written.
        target_reached =
            options.groups != 0 &&
            writer.stats().groups_written >= options.groups;
        reason = StopReason::Groups;
        break;
      }
    }

    // -- periodic status (~2 Hz) and progress ------------------------------
    const auto after = std::chrono::steady_clock::now();
    if (!options.status_file.empty() && after >= next_status) {
      next_status = after + status_interval;
      publish(state);
    }
    if (options.progress_seconds > 0.0 && after >= next_progress) {
      const PipelineStats live = driver.stats();
      std::fprintf(
          stderr,
          "[progress] t=%.1fs state=%s groups=%llu on_disk=%llu evicted=%llu "
          "completed=%llu dropped=%llu bytes=%llu timeouts=%llu stalls=%llu "
          "pool_in_use=%zu/%zu starved=%llu queues=%zu\n",
          elapsed(), std::string(session_state_name(state)).c_str(),
          static_cast<unsigned long long>(writer.stats().groups_written),
          static_cast<unsigned long long>(writer.groups_on_disk()),
          static_cast<unsigned long long>(writer.groups_evicted()),
          static_cast<unsigned long long>(live.frames_completed),
          static_cast<unsigned long long>(live.frames_dropped),
          static_cast<unsigned long long>(live.bytes_received),
          static_cast<unsigned long long>(live.transfer_timeouts),
          static_cast<unsigned long long>(live.transfer_stalls),
          live.transfer_pool_in_use, live.transfer_pool_capacity,
          static_cast<unsigned long long>(live.submission_starved),
          live.frame_queue_depth);
      next_progress =
          after +
          std::chrono::milliseconds(
              static_cast<std::int64_t>(options.progress_seconds * 1000.0));
    }
  }

  // -- graceful teardown, on every path -------------------------------------
  if (reason == StopReason::None) {
    // Defensive: the loop only exits with a reason above.
    reason = StopReason::Error;
  }
  frozen_elapsed = elapsed();
  state = SessionState::Stopping;
  publish(state);

  // Stop triggering, turn the illuminator off and close the pipeline BEFORE
  // finalising the writer/recording, exactly like the historical path.
  if (Status stopped = driver.stop(); !stopped.ok()) {
    std::fprintf(stderr, "warning: device/pipeline stop failed: %s\n",
                 stopped.message().c_str());
    if (failure.ok()) {
      failure = stopped;
      reason = StopReason::Error;
    }
  }
  const double virtual_seconds = driver.virtual_duration_seconds();
  const auto end = std::chrono::steady_clock::now();

  (void)writer.flush();
  // Publish the final counters while the recording is verified and the
  // manifest is written; the GUI should not wait without progress information.
  publish(state);
  const PipelineStats stats = driver.stats();

  manifest.stopped_early =
      options.groups != 0 &&
      writer.stats().groups_written < options.groups;
  manifest.metrics = RunMetrics{};
  metrics_from_pipeline(manifest.metrics, stats);
  manifest.metrics.frames_invalid = writer.stats().frames_invalid;
  manifest.metrics.wall_seconds = seconds_between(start, end);
  manifest.metrics.virtual_seconds = virtual_seconds;
  manifest.throughput_mbps =
      mb_per_second(stats.bytes_received, manifest.metrics.wall_seconds);
  manifest.virtual_throughput_mbps =
      mb_per_second(stats.bytes_received, manifest.metrics.virtual_seconds);
  if (!options.record_path.empty()) {
    auto recorded = verify_recording(options.record_path);
    if (!recorded) {
      std::fprintf(stderr, "recording verification failed: %s\n",
                   recorded.error().message().c_str());
      if (failure.ok()) {
        failure = recorded.error();
        reason = StopReason::Error;
      }
    } else {
      std::uint64_t recorded_bytes = 0;
      for (const auto& transfer : recorded->transfers) {
        recorded_bytes += transfer.bytes.size();
      }
      manifest.recorded_path = options.record_path;
      manifest.recorded_transfers = recorded->transfers.size();
      manifest.recorded_bytes = recorded_bytes;
      manifest.recorded_digest = recorded->trailer_digest;
    }
  }

  const std::string manifest_path =
      join_path(*directory, "frames_manifest.json");
  auto written = write_frames_manifest(manifest_path, manifest, writer);
  if (!written) {
    std::fprintf(stderr, "manifest: %s\n", written.error().message().c_str());
    if (failure.ok()) {
      failure = written.error();
      reason = StopReason::Error;
    }
  }

  CaptureSessionResult result;
  result.manifest = std::move(manifest);
  result.stopped_early = result.manifest.stopped_early;
  result.grouping = writer.stats();
  result.cadence = writer.cadence();
  result.groups = writer.groups();
  result.incomplete_groups = writer.incomplete();
  result.duplicates = writer.duplicates();
  result.absent_group_ids = writer.absent_groups();
  if (written) {
    result.manifest_path = written->path;
  }
  result.state = SessionState::Stopped;
  result.stop_reason = reason;
  result.signal_number = signal_number;
  result.timed_out = timed_out;
  result.target_reached = target_reached;
  result.ring_size = options.ring_size;
  result.groups_evicted = writer.groups_evicted();
  result.groups_on_disk = writer.groups_on_disk();

  publish(SessionState::Stopped);
  if (!failure.ok()) {
    return fail(failure);
  }
  return result;
}

// ---------------------------------------------------------------------------
// Replay
// ---------------------------------------------------------------------------

/// Recover the wire framing of a recording: explicit metadata first, then a
/// structural probe on the first transfer.  The two formats are unambiguous
/// because a length-prefixed packet starts with a plausible u32 length followed
/// by a plausible header, while a raw packet starts with the header itself.
FramingMode detect_recording_framing(const recording::Recording& recording) {
  const std::string& metadata = recording.header.metadata;
  const std::size_t key = metadata.find("framing=");
  if (key != std::string::npos) {
    std::string value = metadata.substr(key + 8);
    const std::size_t end = value.find(';');
    if (end != std::string::npos) {
      value = value.substr(0, end);
    }
    return framing_mode_from_string(value);
  }
  for (const auto& transfer : recording.transfers) {
    if (transfer.bytes.size() < kStreamHeaderSize) {
      continue;
    }
    const auto* bytes = transfer.bytes.data();
    if (PacketFramer::header_is_plausible(bytes)) {
      return FramingMode::Raw;
    }
    if (transfer.bytes.size() >= kStreamHeaderSize + kStreamLengthSize) {
      const std::uint32_t declared = load_be32(bytes);
      if (declared >= kStreamHeaderSize &&
          PacketFramer::header_is_plausible(bytes + kStreamLengthSize)) {
        return FramingMode::LengthPrefixed;
      }
    }
  }
  return FramingMode::Raw;
}

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
  config.framing =
      options.framing ? *options.framing : detect_recording_framing(*recording);

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

namespace {

/// Simulated session driver.  poll() advances the deterministic generator;
/// while paused the loop calls pump_paused() instead, which sleeps without
/// polling the transport, so no new frames are generated.  The pipeline, the
/// writer and the recording stay open across a pause.
class SimulatedSessionDriver final : public CaptureSessionDriver {
 public:
  SimulatedSessionDriver(SimConfig sim, PipelineConfig config, DeviceInfo device)
      : sim_(std::move(sim)), config_(std::move(config)),
        device_(std::move(device)) {}

  Status start() override {
    transport_ = std::make_unique<SimulatedTransport>(sim_);
    generator_ = transport_.get();
    pipeline_ =
        std::make_unique<AcquisitionPipeline>(std::move(transport_), config_);
    if (Status status = pipeline_->start(); !status.ok()) {
      return status;
    }
    started_ns_ = pipeline_->start_time_ns();
    return Status::success();
  }

  Status poll(int timeout_ms) override {
    if (pipeline_) {
      (void)pipeline_->poll(timeout_ms);
    }
    return Status::success();
  }

  bool try_pop_frame(Frame& frame) override {
    return pipeline_ && pipeline_->try_pop_frame(frame);
  }

  [[nodiscard]] PipelineStats stats() const override {
    return pipeline_ ? pipeline_->stats() : PipelineStats{};
  }

  [[nodiscard]] const DeviceInfo& device() const override { return device_; }

  Status apply_volatile(const VolatileSettings& settings) override {
    if (settings.has_exposure) {
      device_.exposure_us = settings.exposure_us;
    }
    if (settings.has_gain) {
      device_.gain = settings.gain;
    }
    if (settings.laser_percent) {
      device_.laser_percent = *settings.laser_percent;
    }
    return Status::success();
  }

  Status pause() override { return Status::success(); }
  Status resume() override { return Status::success(); }

  Status pump_paused(int timeout_ms) override {
    if (timeout_ms > 0) {
      std::this_thread::sleep_for(std::chrono::milliseconds(timeout_ms));
    }
    return Status::success();
  }

  Status stop() override {
    device_.cleanup_attempted = true;
    const Status stopped = pipeline_ ? pipeline_->stop() : Status::success();
    device_.cleanup_verified = stopped.ok();  // Simulator state only.
    if (stopped.ok()) {
      device_.trigger_mono = 0;
      device_.trigger_rgb = 0;
      device_.laser_percent = 0;
    }
    // Retain the stopped pipeline so final status/manifest keep its counters.
    return stopped;
  }

  [[nodiscard]] double virtual_duration_seconds() const override {
    if (generator_ == nullptr) {
      return 0.0;
    }
    const std::int64_t end = generator_->virtual_clock().now_ns();
    return end > started_ns_
               ? static_cast<double>(end - started_ns_) / 1e9
               : 0.0;
  }

  [[nodiscard]] std::uint64_t frames_generated() const override {
    return generator_ ? generator_->generated_frames() : 0;
  }

  bool source_exhausted() override {
    if (!generator_ || generator_->generated_frames() < sim_.frames) {
      exhaustion_idle_ = 0;
      exhaustion_bytes_ = 0;
      return false;
    }
    const PipelineStats stats = pipeline_ ? pipeline_->stats() : PipelineStats{};
    if (stats.bytes_received != exhaustion_bytes_) {
      exhaustion_bytes_ = stats.bytes_received;
      exhaustion_idle_ = 0;
      return false;
    }
    return ++exhaustion_idle_ >= 50;
  }

 private:
  SimConfig sim_;
  PipelineConfig config_;
  DeviceInfo device_;
  std::unique_ptr<SimulatedTransport> transport_;
  std::unique_ptr<AcquisitionPipeline> pipeline_;
  SimulatedTransport* generator_ = nullptr;
  std::int64_t started_ns_ = 0;
  std::uint64_t exhaustion_bytes_ = 0;
  std::size_t exhaustion_idle_ = 0;
};

}  // namespace

Result<SimCaptureReport> capture_simulated(const SimCaptureOptions& options) {
  const bool has_control = !options.control_path.empty();
  if (options.groups == 0 && !has_control && options.max_seconds <= 0.0) {
    return fail(ErrorCode::InvalidArgument,
                "--groups must be at least 1 (or use --control/--max-seconds)");
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

  SimConfig sim;
  sim.seed = options.seed;
  if (options.groups == 0) {
    // Unbounded: the session loop stops the generator via the control file,
    // signals or --max-seconds long before this budget is reached.
    sim.frames = std::numeric_limits<std::uint32_t>::max();
  } else {
    sim.frames = static_cast<std::uint32_t>(
        std::min<std::uint64_t>(static_cast<std::uint64_t>(options.groups) *
                                    kStreamCount,
                                std::numeric_limits<std::uint32_t>::max()));
  }
  sim.camera_count = kStreamCount;
  sim.first_camera_index = 0;
  sim.expect_interval_quirk = false;
  sim.trigger_period_ns =
      static_cast<std::int64_t>(options.trigger_period_us) * 1000;
  sim.bandwidth_bytes_per_sec = options.bandwidth_bytes_per_sec;
  sim.faults = *faults;

  PipelineConfig config;
  config.queue_depth = options.queue_depth == 0 ? 8 : options.queue_depth;
  config.copy_mode = CopyMode::Copy;
  config.frame_queue_capacity =
      options.frame_queue_capacity == 0 ? 32 : options.frame_queue_capacity;
  config.partial_frame_timeout_ns = options.partial_frame_timeout_ns;
  config.framing = sim.framing;
  if (!options.recording_path.empty()) {
    config.enable_recording = true;
    config.recording_path = options.recording_path;
    config.recording_metadata =
        "backend=simulated;seed=" + std::to_string(options.seed) +
        ";faults=" + options.faults +
        ";groups=" + std::to_string(options.groups);
  }

  DeviceInfo device;
  device.simulated = true;
  device.identified = false;
  device.serial = "SIMULATED";
  device.serial_redacted = false;
  device.firmware = "simulated";
  device.sensor_count = kStreamCount;
  device.settings_applied = false;
  device.trigger_period_us = options.trigger_period_us;
  device.trigger_mono = 1;
  device.trigger_rgb = 1;

  SimulatedSessionDriver driver(sim, config, device);

  CaptureSessionOptions session;
  session.output_directory = *directory;
  session.control_path = options.control_path;
  session.status_file = options.status_file;
  session.volatility_path = options.volatility_path;
  session.groups = options.groups;
  session.ring_size = options.ring_size;
  session.max_seconds = options.max_seconds;
  session.timeout_seconds = options.timeout_seconds;
  // A fixed-count run keeps the historical 30 s default; a control session
  // only gets a safety limit when the caller explicitly asked for one.
  session.timeout_seconds_set =
      options.timeout_seconds_set || !has_control;
  session.progress_seconds = options.progress_seconds;
  session.record_path = options.recording_path;
  session.manifest.tool = "einstar-capture-frames";
  session.manifest.backend = "simulated";
  session.manifest.simulated = true;
  session.manifest.live = false;
  session.manifest.simulation_seed = options.seed;
  session.manifest.simulation_faults = options.faults;
  session.manifest.simulation_frames =
      options.groups == 0 ? 0U : sim.frames;
  session.manifest.device = device;

  auto session_result = run_capture_session(session, driver);
  if (!session_result) {
    return fail(session_result.error());
  }

  SimCaptureReport report;
  report.manifest = session_result->manifest;
  report.grouping = session_result->grouping;
  report.cadence = session_result->cadence;
  report.groups = session_result->groups;
  report.incomplete_groups = session_result->incomplete_groups;
  report.duplicates = session_result->duplicates;
  report.absent_group_ids = session_result->absent_group_ids;
  report.manifest_path = session_result->manifest_path;
  report.ring_size = session_result->ring_size;
  report.groups_evicted = session_result->groups_evicted;
  report.groups_on_disk = session_result->groups_on_disk;
  report.state = session_result->state;
  report.stop_reason = session_result->stop_reason;

  if (report.grouping.groups_written == 0 && options.groups > 0) {
    return fail(ErrorCode::Exhausted,
                "simulated capture wrote no complete three-camera group");
  }
  if (options.faults == "clean" && !has_control &&
      options.max_seconds <= 0.0 && !session_result->target_reached) {
    return fail(ErrorCode::Internal,
                "clean simulated capture stopped after " +
                    std::to_string(report.grouping.groups_written) + " of " +
                    std::to_string(options.groups) +
                    " requested group(s); this is a bug, not a fault");
  }
  return report;
}

}  // namespace einstar::capture
