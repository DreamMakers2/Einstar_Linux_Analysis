// SPDX-License-Identifier: MIT
// Shared core for the acquisition-side capture tools.
//
// This is deliberately NOT part of the driver library: it turns completed
// `einstar::Frame`s into the on-disk layout the reconstruction CLI consumes
// (`frame_%06u_cam%u.pgm` triplets plus `frames_manifest.json`) and it drives
// the existing pipeline/reassembler for replay and for the deterministic
// simulator.  `einstar-replay-frames`, `einstar-capture-frames` and
// `test_replay_frames` all use these functions, so the tested path is the path
// the tools run.
//
// Grouping rule (docs/reconstruction/architecture.md, "the 8-byte header
// counter is a trigger-group counter, not a timestamp"): a group is identified
// by `Frame::device_timestamp`, which the reassembler latches from header bytes
// 16..23.  That counter is identical for every camera frame of one trigger
// group and increments once per group.  `Frame::frame_index` (header bytes
// 12..15, valid only on the last packet) is a per-exposure slot counter and is
// recorded for provenance, never used as the group key.
//
// Output ids are dense, zero-based (`%06u`) because the reconstruction loader
// matches exactly six digits and the raw device counter is a u64.  The manifest
// keeps the mapping: `group` (output id) -> `device_group` (raw counter) plus
// each stream's device `frame_id`.
#pragma once

#include <array>
#include <cstdint>
#include <map>
#include <optional>
#include <set>
#include <span>
#include <string>
#include <string_view>
#include <vector>

#include "einstar/frame.hpp"
#include "einstar/pipeline.hpp"
#include "einstar/recorder.hpp"
#include "einstar/sim.hpp"
#include "einstar/status.hpp"

namespace einstar::capture {

inline constexpr std::uint32_t kFrameWidth = 1280;
inline constexpr std::uint32_t kFrameHeight = 1024;
inline constexpr std::uint32_t kFrameBytes = kFrameWidth * kFrameHeight;
inline constexpr std::uint8_t kStreamCount = 3;
inline constexpr std::uint32_t kMaxOutputGroups = 999999;  // six decimal digits

/// USB stream index -> role (hardware-observed camera association).
std::string_view camera_name(std::uint8_t camera) noexcept;
/// "stream0" ... "stream2"; out-of-range cameras return "".
std::string_view stream_key(std::uint8_t camera) noexcept;

// ---------------------------------------------------------------------------
// Recording verification
// ---------------------------------------------------------------------------

/// Read a recording through `recording::read_recording` (header CRC, per-record
/// CRC, record structure) and additionally require a trailer whose digest,
/// transfer count and byte count match the content.  Returns
/// `ErrorCode::Protocol` with a precise message on corruption.
Result<recording::Recording> verify_recording(const std::string& path);

// ---------------------------------------------------------------------------
// Grouping and PGM output
// ---------------------------------------------------------------------------

struct StreamFrame {
  std::uint8_t camera = 0;
  std::uint32_t frame_id = 0;        // device slot counter (last packet only)
  std::uint64_t device_group = 0;    // trigger-group counter (header 16..23)
  std::int64_t host_time_ns = 0;     // transport clock at the frame's first packet
  std::vector<std::uint8_t> pixels;  // width * height, 8-bit
};

/// One complete, written group: three camera frames and their manifest data.
struct GroupRecord {
  std::uint32_t group = 0;                 // dense output id (file name id)
  std::uint64_t device_group = 0;          // raw trigger-group counter
  std::array<std::uint32_t, kStreamCount> frame_ids{};
  std::array<std::uint64_t, kStreamCount> digests{};  // FNV-1a 64 of pixels
  std::int64_t host_time_ns = 0;           // earliest frame of the group
  std::array<std::string, kStreamCount> files;  // file names inside the directory
};

/// A group that was seen but never reached one frame per camera before the
/// grouping window closed (or the stream ended).
struct IncompleteGroup {
  std::uint64_t device_group = 0;
  std::array<bool, kStreamCount> present{};
  std::array<std::uint32_t, kStreamCount> frame_ids{};
  std::uint8_t missing_mask = 0;  // bit c set when camera c is missing
};

/// A second frame for a camera that already had one in the same group.
struct DuplicateFrame {
  std::uint64_t device_group = 0;
  std::uint8_t camera = 0;
  std::uint32_t kept_frame_id = 0;
  std::uint32_t dropped_frame_id = 0;
  bool group_already_written = false;
};

struct GroupingStats {
  std::uint64_t frames_seen = 0;
  std::uint64_t frames_written = 0;
  std::uint64_t frames_invalid = 0;
  std::uint64_t groups_written = 0;
  std::uint64_t groups_incomplete = 0;
  std::uint64_t groups_absent = 0;      // inferred from the counter stride
  std::uint64_t duplicate_frames = 0;
  std::uint64_t late_frames = 0;        // frame for an already-written group
  std::uint64_t bytes_written = 0;
  std::uint64_t missing_camera_frames = 0;  // sum over incomplete groups
};

struct CadenceStats {
  std::uint64_t intervals = 0;
  std::int64_t min_ns = 0;
  std::int64_t max_ns = 0;
  std::int64_t mean_ns = 0;
  double fps = 0.0;
};

/// Writes PGM triplets as groups complete.  Every file is written to a
/// temporary name and renamed only after all three streams of the group are on
/// disk, so a partially written group never looks like a valid triplet.
class GroupWriter {
 public:
  explicit GroupWriter(std::string output_directory,
                       std::uint32_t width = kFrameWidth,
                       std::uint32_t height = kFrameHeight);
  GroupWriter(const GroupWriter&) = delete;
  GroupWriter& operator=(const GroupWriter&) = delete;
  GroupWriter(GroupWriter&&) = delete;
  GroupWriter& operator=(GroupWriter&&) = delete;

  /// Create the output directory (recursively).
  Status prepare();

  /// Consume one completed frame.  Writes a triplet when the group completes.
  /// Frames with an invalid camera or size are counted, not written.
  Status add(const Frame& frame);

  /// Finalise every pending group as incomplete (end of a full stream).
  Status flush();

  /// Forget pending groups without reporting them (deliberate early stop).
  void discard_pending() noexcept;

  [[nodiscard]] const std::vector<GroupRecord>& groups() const noexcept {
    return groups_;
  }
  [[nodiscard]] const std::vector<IncompleteGroup>& incomplete() const noexcept {
    return incomplete_;
  }
  [[nodiscard]] const std::vector<DuplicateFrame>& duplicates() const noexcept {
    return duplicates_;
  }
  [[nodiscard]] const std::vector<std::uint64_t>& absent_groups() const noexcept {
    return absent_groups_;
  }
  [[nodiscard]] const GroupingStats& stats() const noexcept { return stats_; }
  [[nodiscard]] const std::string& output_directory() const noexcept {
    return directory_;
  }
  [[nodiscard]] std::uint32_t width() const noexcept { return width_; }
  [[nodiscard]] std::uint32_t height() const noexcept { return height_; }
  [[nodiscard]] CadenceStats cadence() const;

 private:
  struct PendingGroup {
    std::uint64_t device_group = 0;
    std::array<std::optional<StreamFrame>, kStreamCount> streams;
  };

  Status write_group(PendingGroup& pending);
  void finalize_pending(PendingGroup& pending);
  void update_absent_groups();
  [[nodiscard]] std::string file_path(std::string_view name) const;

  std::string directory_;
  std::uint32_t width_ = kFrameWidth;
  std::uint32_t height_ = kFrameHeight;
  std::map<std::uint64_t, PendingGroup> pending_;
  std::set<std::uint64_t> written_device_groups_;
  std::vector<std::uint64_t> seen_device_groups_;
  std::vector<GroupRecord> groups_;
  std::vector<IncompleteGroup> incomplete_;
  std::vector<DuplicateFrame> duplicates_;
  std::vector<std::uint64_t> absent_groups_;
  GroupingStats stats_;
};

/// True when every camera and the frame size are usable.
bool valid_frame(const Frame& frame, std::uint32_t width, std::uint32_t height,
                 std::uint8_t& camera);

/// Binary PGM (P5, maxval 255).  `pixels` must be width * height bytes.
Status write_pgm(const std::string& path, std::uint32_t width,
                 std::uint32_t height, std::span<const std::uint8_t> pixels);

/// Reads a binary PGM, validating dimensions and payload length.
Result<std::vector<std::uint8_t>> read_pgm(const std::string& path,
                                           std::uint32_t& width,
                                           std::uint32_t& height);

/// Recursively create `directory`.  Empty is an error.
Status ensure_directory(const std::string& directory);

/// Create `directory` and return its absolute path (the manifest uses absolute
/// stream paths so it can be consumed from any working directory).
Result<std::string> prepare_output_directory(const std::string& directory);

// ---------------------------------------------------------------------------
// Manifest
// ---------------------------------------------------------------------------

struct DeviceInfo {
  bool simulated = true;
  bool identified = false;
  std::string vendor_id = "3267";
  std::string product_id = "0003";
  /// Redacted unless the caller passed --show-serial.
  std::string serial;
  bool serial_redacted = true;
  std::uint64_t serial_fnv1a64 = 0;
  std::string firmware;  // raw device string
  std::string fx3_version;
  std::string fpga_version;
  std::string locale;
  std::uint16_t sensor_count = 0;
  std::string calibration_file;  // file name inside the output directory
  std::uint64_t calibration_bytes = 0;
  std::uint64_t calibration_fnv1a64 = 0;
  bool settings_applied = false;  // all writes are volatile
  std::uint32_t trigger_period_us = 0;
  std::uint8_t trigger_mono = 0;
  std::uint8_t trigger_rgb = 0;
  std::array<std::uint32_t, kStreamCount> exposure_us{};
  std::array<std::uint16_t, kStreamCount> gain{};
};

struct RunMetrics {
  std::uint64_t bytes_received = 0;
  std::uint64_t frames_completed = 0;
  std::uint64_t frames_dropped = 0;
  std::uint64_t packets_parsed = 0;
  std::uint64_t parse_errors = 0;
  std::uint64_t resync_bytes = 0;
  std::uint64_t staged_packets = 0;
  std::uint64_t duplicate_packets = 0;
  std::uint64_t orphan_packets = 0;
  std::uint64_t frame_restarts = 0;
  std::uint64_t sequence_gaps = 0;
  std::uint64_t partial_timeouts = 0;
  std::uint64_t queue_drops = 0;
  std::uint64_t transfer_timeouts = 0;
  std::uint64_t transfer_stalls = 0;
  std::uint64_t transfer_errors = 0;
  std::uint64_t frames_invalid = 0;
  double wall_seconds = 0.0;
  double virtual_seconds = 0.0;
};

void metrics_from_pipeline(RunMetrics& metrics, const PipelineStats& stats);

struct ManifestInfo {
  std::string tool;
  std::string backend;  // "simulated" | "libusb"
  bool simulated = true;
  bool live = false;
  std::string output_directory;
  std::uint32_t requested_groups = 0;
  bool stopped_early = false;  // --max-groups / group target reached

  // Replay source (empty for a live/simulated capture).
  std::string source_recording;
  std::uint64_t source_transfers = 0;
  std::uint64_t source_bytes = 0;
  std::uint64_t source_digest = 0;
  bool source_verified = false;

  // Simulation description (empty/zero for replay).
  std::uint64_t simulation_seed = 0;
  std::string simulation_faults;
  std::uint32_t simulation_frames = 0;

  // Recording written alongside (empty when --record was not given).
  std::string recorded_path;
  std::uint64_t recorded_transfers = 0;
  std::uint64_t recorded_bytes = 0;
  std::uint64_t recorded_digest = 0;

  DeviceInfo device;
  RunMetrics metrics;
  double throughput_mbps = 0.0;
  double virtual_throughput_mbps = 0.0;
};

struct ManifestWriteResult {
  std::string path;
  std::uint64_t bytes = 0;
  std::uint64_t fnv1a64 = 0;
};

/// Serialise `frames_manifest.json` (schema einstar-frames-v1) atomically.
Result<ManifestWriteResult> write_frames_manifest(const std::string& path,
                                                  const ManifestInfo& info,
                                                  const GroupWriter& writer);

/// True when no grouping anomaly was observed.
bool grouping_complete(const GroupingStats& stats) noexcept;

/// 64-bit FNV-1a over a byte span (same function as the recorder digest).
std::uint64_t fnv1a64(std::span<const std::uint8_t> bytes) noexcept;

/// Minimal JSON string quoting (used by the --json stdout summaries).
std::string json_quote(std::string_view text);

// ---------------------------------------------------------------------------
// Fault presets (mirrors einstar-record-replay)
// ---------------------------------------------------------------------------

/// "clean", "lossy" or "hostile".
Result<FaultConfig> fault_preset(std::string_view name);

// ---------------------------------------------------------------------------
// Replay
// ---------------------------------------------------------------------------

struct ReplayOptions {
  std::string input;
  std::string output_directory;
  /// 0 = every group in the recording.
  std::uint32_t max_groups = 0;
  bool write_manifest = false;
  std::uint32_t queue_depth = 8;
  std::uint32_t frame_queue_capacity = 32;
  std::int64_t partial_frame_timeout_ns = 500'000'000;
};

struct ReplayReport {
  ManifestInfo manifest;
  GroupingStats grouping;
  CadenceStats cadence;
  std::vector<GroupRecord> groups;
  std::vector<IncompleteGroup> incomplete_groups;
  std::vector<DuplicateFrame> duplicates;
  std::vector<std::uint64_t> absent_group_ids;
  std::string manifest_path;
};

/// Verify the recording, then replay every transfer through SimulatedTransport
/// and the acquisition pipeline exactly like `einstar-record-replay play`.
/// Nothing is written before verification succeeds.
Result<ReplayReport> replay_recording(const ReplayOptions& options);

// ---------------------------------------------------------------------------
// Deterministic simulated capture
// ---------------------------------------------------------------------------

struct SimCaptureOptions {
  std::string output_directory;
  std::uint32_t groups = 0;
  std::uint64_t seed = 1;
  std::string faults = "clean";
  std::string recording_path;  // empty = no recording
  std::uint32_t queue_depth = 8;
  std::uint32_t trigger_period_us = 40'000;
  std::uint32_t bandwidth_bytes_per_sec = 300'000'000;
  std::uint32_t frame_queue_capacity = 32;
  std::int64_t partial_frame_timeout_ns = 500'000'000;
};

struct SimCaptureReport {
  ManifestInfo manifest;
  GroupingStats grouping;
  CadenceStats cadence;
  std::vector<GroupRecord> groups;
  std::vector<IncompleteGroup> incomplete_groups;
  std::vector<DuplicateFrame> duplicates;
  std::vector<std::uint64_t> absent_group_ids;
  std::string manifest_path;
};

Result<SimCaptureReport> capture_simulated(const SimCaptureOptions& options);

}  // namespace einstar::capture
