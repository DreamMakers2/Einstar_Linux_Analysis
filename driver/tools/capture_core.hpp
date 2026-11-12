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
#include <atomic>
#include <cstdint>
#include <map>
#include <optional>
#include <set>
#include <span>
#include <string>
#include <string_view>
#include <thread>
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
  /// The three cameras of a group did not report the same 8-bit exposure slot
  /// id.  On the reference unit they always match; a non-zero count means the
  /// grouping key is not trustworthy and the capture should be rejected.
  std::uint64_t slot_id_mismatches = 0;
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

  /// Keep only the newest `ring_size` complete groups on disk (0 = keep all).
  /// Set before the first group is written.  `groups()` and the manifest still
  /// record every group ever written; only the triplet files of the oldest
  /// groups are removed from this writer's output directory.
  void set_ring_size(std::uint32_t ring_size) noexcept {
    ring_size_ = ring_size;
  }
  [[nodiscard]] std::uint32_t ring_size() const noexcept { return ring_size_; }
  /// Complete triplets removed by the ring so far.
  [[nodiscard]] std::uint64_t groups_evicted() const noexcept {
    return groups_evicted_;
  }
  /// Complete triplets currently present in the frames directory.
  [[nodiscard]] std::uint64_t groups_on_disk() const noexcept {
    return static_cast<std::uint64_t>(groups_.size()) - groups_evicted_;
  }

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
  void evict_old_groups();
  [[nodiscard]] std::string file_path(std::string_view name) const;

  std::string directory_;
  std::uint32_t width_ = kFrameWidth;
  std::uint32_t height_ = kFrameHeight;
  std::uint32_t ring_size_ = 0;
  std::uint64_t groups_evicted_ = 0;
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
  bool cleanup_attempted = false;
  bool cleanup_verified = false;
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
  std::array<std::uint16_t, kStreamCount> max_width{};
  std::array<std::uint16_t, kStreamCount> max_height{};
  /// Illuminator state, read before capture and after any volatile change.
  std::optional<std::uint32_t> laser_percent;
  std::array<std::optional<std::uint32_t>, 2> strobe{};
  bool laser_set = false;
  std::array<bool, 2> strobe_set{};
  std::uint16_t temperature_raw = 0;
  bool temperature_read = false;
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
  std::uint64_t dropped_incomplete = 0;
  std::uint64_t dropped_overflow = 0;
  std::uint64_t dropped_stale = 0;
  std::uint64_t dropped_capacity = 0;
  std::uint64_t dropped_pool = 0;
  std::uint64_t dropped_timeout = 0;
  std::uint64_t dropped_flush = 0;
  std::uint64_t packet_index_gaps = 0;
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
// Control file, ring eviction and session state
// ---------------------------------------------------------------------------

/// A command in the GUI control file.  `None` covers an absent file, an empty
/// file and an unrecognised last line; all three mean "keep doing what you are
/// doing".
enum class ControlCommand { None, Start, Pause, Resume, Stop };

std::string_view control_command_name(ControlCommand command) noexcept;

/// Parse the whole control file: commands are newline terminated, the last
/// non-empty line wins and unknown commands are ignored (`None`).
ControlCommand parse_control_text(std::string_view text);

/// Cheap control-file poller.  It stats the path on every poll and only reads
/// the file when its inode/size/mtime identity changed.  The last recognised
/// command is sticky, so an unchanged file keeps reporting it: `pause` written
/// once keeps meaning paused, and re-reading is not needed for idempotence.
/// A missing file reports `None` and changes nothing ("keep running").
class ControlFile {
 public:
  explicit ControlFile(std::string path);
  ControlFile(const ControlFile&) = delete;
  ControlFile& operator=(const ControlFile&) = delete;

  ControlCommand poll();
  [[nodiscard]] ControlCommand last_command() const noexcept {
    return command_;
  }
  /// Number of file reads; the stat-only polls do not increment it.
  [[nodiscard]] std::uint64_t reads() const noexcept { return reads_; }
  [[nodiscard]] const std::string& path() const noexcept { return path_; }

 private:
  std::string path_;
  ControlCommand command_ = ControlCommand::None;
  bool have_stat_ = false;
  std::uint64_t device_ = 0;
  std::uint64_t inode_ = 0;
  std::uint64_t size_ = 0;
  std::int64_t mtime_sec_ = 0;
  std::int64_t mtime_nsec_ = 0;
  std::uint64_t reads_ = 0;
};

/// Expected deletion set for a bounded ring: the oldest `written.size() - N`
/// written group ids when more than N groups exist, otherwise empty.  N = 0
/// keeps everything.  The newest written group is never returned.
std::vector<std::uint32_t> ring_eviction_set(
    std::span<const std::uint32_t> written_groups, std::uint32_t ring_size);

/// True for a plain file name directly inside the frames directory: non-empty,
/// no path separator and not "."/"..".  The ring only ever deletes names that
/// pass this check, so it cannot escape its frames directory.
bool safe_frame_filename(std::string_view name) noexcept;

enum class SessionState { Starting, Running, Paused, Stopping, Stopped };

std::string_view session_state_name(SessionState state) noexcept;

enum class StopReason {
  None,
  Groups,
  MaxSeconds,
  SignalInt,
  SignalTerm,
  ControlStop,
  Error,
};

/// "groups" | "max_seconds" | "signal:SIGINT" | "signal:SIGTERM" |
/// "control:stop" | "error"; empty for `None` (JSON null).
std::string_view stop_reason_name(StopReason reason) noexcept;

/// Everything the GUI-visible `einstar-live-status-v1` file carries.  The
/// fields that existed before this structure must stay valid for
/// tools/liveview; the session fields are additive.
struct LiveStatus {
  bool cleanup_attempted = false;
  bool cleanup_verified = false;
  std::optional<std::array<std::uint8_t, 2>> trigger_counts;
  SessionState state = SessionState::Starting;
  StopReason stop_reason = StopReason::None;
  double elapsed_s = 0.0;
  std::uint64_t groups_written = 0;
  /// Requested upper bound; 0 means unbounded (--control).
  std::uint64_t groups_requested = 0;
  /// Dense output id of the newest written group (0 when none); matches the
  /// historical `latest_group` field.
  std::uint64_t latest_group = 0;
  /// Device trigger-group counter of the newest written group (0 when none).
  std::uint64_t last_group = 0;
  std::uint32_t ring_size = 0;
  std::uint64_t groups_on_disk = 0;
  std::uint64_t groups_evicted = 0;
  std::uint64_t frames_written = 0;
  std::uint64_t bytes_written = 0;
  std::uint64_t disk_free_bytes = 0;
  std::uint64_t frames_completed = 0;
  std::uint64_t frames_dropped = 0;
  std::uint64_t bytes_received = 0;
  double throughput_mbps = 0.0;
  std::uint64_t transfer_timeouts = 0;
  std::uint64_t frames_invalid = 0;
  std::string output_directory;
  std::uint32_t trigger_period_us = 0;
  std::array<std::uint32_t, kStreamCount> exposure_us{};
  std::array<std::uint16_t, kStreamCount> gain{};
  std::optional<std::uint32_t> laser_percent;
  bool simulated = false;
  bool live = false;
};

/// Serialise the status object (single line plus trailing newline).
std::string build_live_status_json(const LiveStatus& status);

/// Write `json` to `path` through `path.tmp` + rename.  Creates the parent
/// directory when needed.  The viewer never sees a partially written file.
Status write_status_file(const std::string& path, std::string_view json);

/// Free bytes on the filesystem holding `path` (statvfs); 0 when unavailable.
std::uint64_t disk_free_bytes(const std::string& path) noexcept;

/// Install SIGINT/SIGTERM handlers that only set the signal flag below.
Status install_signal_handlers();

/// CLI lifetime guard, armed before any acquisition or USB setup. Linux uses
/// PR_SET_PDEATHSIG plus an immediate expected-parent check. The optional
/// inherited heartbeat pipe is provided by the wizard, not the CLI protocol.
/// A stop gets five seconds to unwind before forced exit (cleanup unverified).
class ProcessSafetyGuard {
 public:
  ProcessSafetyGuard() = default;
  ~ProcessSafetyGuard();
  ProcessSafetyGuard(const ProcessSafetyGuard&) = delete;
  ProcessSafetyGuard& operator=(const ProcessSafetyGuard&) = delete;
  Status arm_from_environment();
 private:
  std::atomic<bool> finished_{false};
  std::thread watchdog_;
  int heartbeat_fd_ = -1;
};

/// The complete body of the signal handler (also called by tests): records the
/// first SIGINT/SIGTERM received and ignores everything else.
void notify_stop_signal(int signum) noexcept;

/// Reads and clears the pending signal flag; returns 0 when none is pending.
int take_stop_signal() noexcept;

// ---------------------------------------------------------------------------
// Capture session (shared by the live and simulated backends)
// ---------------------------------------------------------------------------

/// A volatile settings change requested by the operator/auto-exposure loop.
/// Every field is optional; the driver applies only the fields present and
/// updates its reported DeviceInfo after a successful device write.
struct VolatileSettings {
  std::array<std::uint32_t, 3> exposure_us{};
  std::array<std::uint16_t, 3> gain{};
  std::optional<std::uint8_t> laser_percent;
  bool has_exposure = false;
  bool has_gain = false;
};

/// Device-side operations the session loop drives.  The live implementation
/// wraps ControlChannel + AcquisitionPipeline; the simulated one freezes its
/// deterministic generator while paused.
class CaptureSessionDriver {
 public:
  virtual ~CaptureSessionDriver() = default;

  /// Start triggering (live) / start the generator (simulated).
  virtual Status start() = 0;
  /// Pump the transport for at most `timeout_ms`.
  virtual Status poll(int timeout_ms) = 0;
  /// Non-blocking completed-frame dequeue.
  virtual bool try_pop_frame(Frame& frame) = 0;
  [[nodiscard]] virtual PipelineStats stats() const = 0;
  [[nodiscard]] virtual const DeviceInfo& device() const = 0;

  /// Pause: stop triggering and turn the illuminator off, keeping the
  /// transport and the recording open.
  virtual Status pause() = 0;
  /// Resume: restore the illuminator and start triggering again.
  virtual Status resume() = 0;
  /// Keep the pipeline alive while paused.  The live driver keeps draining
  /// in-flight USB transfers; the simulator deliberately does not poll, which
  /// freezes its generator (pumping it would generate new frames).
  virtual Status pump_paused(int timeout_ms) = 0;

  /// Apply a volatile exposure/gain/illumination change while the session is
  /// running.  The default is Unsupported so a driver cannot silently claim
  /// it changed the device.  The live implementation writes only volatile
  /// registers, validates each value against the device range, and updates the
  /// reported DeviceInfo only after success.
  virtual Status apply_volatile(const VolatileSettings&) {
    return Status::error(ErrorCode::Unsupported,
                         "volatile settings changes are not supported");
  }

  /// Stop triggering, leave the illuminator dark and release the transport.
  /// Must be idempotent and is called by the session loop on every exit path.
  virtual Status stop() = 0;

  [[nodiscard]] virtual double virtual_duration_seconds() const = 0;
  [[nodiscard]] virtual std::uint64_t frames_generated() const { return 0; }
  /// True once a finite deterministic source has produced everything and the
  /// pipeline stopped making progress.  Always false for a live device.
  virtual bool source_exhausted() { return false; }
};

struct CaptureSessionOptions {
  std::string output_directory;  // frames directory (not the session dir)
  /// Empty: fixed-count capture with no external control.
  std::string control_path;
  std::string status_file;       // empty: no status file
  /// Optional path polled for volatile exposure/gain/laser changes.  Written
  /// atomically by the wizard; missing/unchanged files change nothing.
  std::string volatility_path;
  /// Requested groups; 0 is only valid with `control_path` or `max_seconds`.
  std::uint32_t groups = 0;
  std::uint32_t ring_size = 0;
  double max_seconds = 0.0;      // 0 = disabled
  std::uint32_t timeout_seconds = 30;
  /// The tool only sets this when the user passed --timeout-seconds; with
  /// --control an unset safety limit must not end a multi-minute session.
  bool timeout_seconds_set = false;
  double progress_seconds = 0.0;
  std::string record_path;       // empty: no raw recording
  ManifestInfo manifest;         // template: tool/backend/device/simulation
};

struct CaptureSessionResult {
  ManifestInfo manifest;
  GroupingStats grouping;
  CadenceStats cadence;
  std::vector<GroupRecord> groups;
  std::vector<IncompleteGroup> incomplete_groups;
  std::vector<DuplicateFrame> duplicates;
  std::vector<std::uint64_t> absent_group_ids;
  std::string manifest_path;
  SessionState state = SessionState::Stopped;
  StopReason stop_reason = StopReason::None;
  int signal_number = 0;
  bool timed_out = false;     // --timeout-seconds safety limit fired
  bool target_reached = false;
  bool stopped_early = false; // control/signal/max_seconds/max groups
  std::uint32_t ring_size = 0;
  std::uint64_t groups_evicted = 0;
  std::uint64_t groups_on_disk = 0;
};

/// Run the control/status/ring/signal-aware capture loop.  On every exit path
/// (target, `stop`, SIGINT/SIGTERM, max seconds, safety timeout, write error)
/// it calls driver.stop() so the trigger is off and the illuminator is dark,
/// then finalises the recording (verifies it when one was requested), flushes
/// the writer, writes the manifest and publishes the final `stopped` status.
Result<CaptureSessionResult> run_capture_session(
    const CaptureSessionOptions& options, CaptureSessionDriver& driver);

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
  /// Wire framing.  std::nullopt = take it from the recording metadata, and if
  /// the metadata does not say, detect it from the first transfer.
  std::optional<FramingMode> framing;
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

  // Wizard session controls (all optional; the defaults keep the historical
  // fixed-count behaviour used by test_replay_frames).
  std::string control_path;
  std::string status_file;
  std::string volatility_path;
  std::uint32_t ring_size = 0;
  double max_seconds = 0.0;
  std::uint32_t timeout_seconds = 30;
  bool timeout_seconds_set = false;
  double progress_seconds = 0.0;
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
  std::uint32_t ring_size = 0;
  std::uint64_t groups_evicted = 0;
  std::uint64_t groups_on_disk = 0;
  SessionState state = SessionState::Stopped;
  StopReason stop_reason = StopReason::None;
};

Result<SimCaptureReport> capture_simulated(const SimCaptureOptions& options);

}  // namespace einstar::capture
