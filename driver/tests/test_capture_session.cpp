// SPDX-License-Identifier: MIT
// Offline tests for the wizard capture-session additions:
//   * control-file parsing (last line wins, unknown lines ignored);
//   * stat-based control-file caching and the missing-file "keep running" rule;
//   * ring eviction as a pure function and as GroupWriter bookkeeping;
//   * the einstar-live-status-v1 JSON fields and state/stop-reason names;
//   * the signal flag the SIGINT/SIGTERM handler sets (no real signals).
//
// No device, no libusb, no signals and no wall-clock timing are involved.
#include <algorithm>
#include <csignal>
#include <cstdint>
#include <cstdio>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <string>
#include <string_view>
#include <system_error>
#include <vector>

#include <fcntl.h>
#include <sys/stat.h>

#include "capture_core.hpp"
#include "test_util.hpp"

using namespace einstar;
using namespace einstar::capture;

namespace {

const std::string kRoot = "/tmp/einstar_capture_session_test";

void reset_root() {
  std::error_code ignored;
  std::filesystem::remove_all(kRoot, ignored);
  std::filesystem::create_directories(kRoot, ignored);
}

std::string path_in(const std::string& name) { return kRoot + "/" + name; }

void write_text(const std::string& path, std::string_view text) {
  std::ofstream file(path, std::ios::binary | std::ios::trunc);
  file.write(text.data(), static_cast<std::streamsize>(text.size()));
}

std::string read_text(const std::string& path) {
  std::ifstream file(path, std::ios::binary);
  return std::string((std::istreambuf_iterator<char>(file)),
                     std::istreambuf_iterator<char>());
}

bool contains(std::string_view text, std::string_view needle) {
  return text.find(needle) != std::string_view::npos;
}

std::size_t count_pgm(const std::string& directory) {
  std::size_t count = 0;
  std::error_code error;
  for (const auto& entry :
       std::filesystem::directory_iterator(directory, error)) {
    if (entry.is_regular_file() && entry.path().extension() == ".pgm") {
      ++count;
    }
  }
  return count;
}

std::string pgm_path(const std::string& directory, std::uint32_t group,
                     std::uint8_t camera) {
  char name[64];
  std::snprintf(name, sizeof(name), "frame_%06u_cam%u.pgm", group,
                static_cast<unsigned>(camera));
  return directory + "/" + name;
}

/// Forces a distinct, deterministic mtime on a control file so the mtime
/// branch of the stat cache is exercised even on coarse filesystem clocks.
void set_mtime(const std::string& path, std::time_t seconds,
               long nanoseconds) {
  struct timespec times[2] = {};
  times[0].tv_nsec = UTIME_OMIT;
  times[1].tv_sec = seconds;
  times[1].tv_nsec = nanoseconds;
  CHECK_EQ(::utimensat(AT_FDCWD, path.c_str(), times, 0), 0);
}

std::vector<std::uint32_t> dense_groups(std::uint32_t count) {
  std::vector<std::uint32_t> groups(count);
  for (std::uint32_t i = 0; i < count; ++i) {
    groups[i] = i;
  }
  return groups;
}

}  // namespace

EINSTAR_TEST(control_text_last_line_wins_and_unknown_is_ignored) {
  CHECK(parse_control_text("start\npause\nresume\n") ==
        ControlCommand::Resume);
  CHECK(parse_control_text("pause\nbogus\n") == ControlCommand::Pause);
  CHECK(parse_control_text("bogus\n") == ControlCommand::None);
  CHECK(parse_control_text("") == ControlCommand::None);
  CHECK(parse_control_text("   stop \r\n\n") == ControlCommand::Stop);
  CHECK(parse_control_text("start\nstop") == ControlCommand::Stop);
  CHECK(parse_control_text("start\nresume\npause") == ControlCommand::Pause);
  CHECK(control_command_name(ControlCommand::Pause) == "pause");
  CHECK(control_command_name(ControlCommand::None) == "none");
}

EINSTAR_TEST(control_file_only_rereads_when_stat_changes) {
  reset_root();
  const std::string path = path_in("control.txt");

  // Missing file: "keep running", no state change, no read.
  ControlFile missing(path);
  CHECK(missing.poll() == ControlCommand::None);
  CHECK_EQ(missing.reads(), 0U);

  write_text(path, "pause\n");
  ControlFile file(path);
  CHECK(file.poll() == ControlCommand::Pause);
  CHECK_EQ(file.reads(), 1U);

  // Unchanged file: stat only, the sticky command is returned without a read.
  CHECK(file.poll() == ControlCommand::Pause);
  CHECK_EQ(file.reads(), 1U);

  // Same size, different mtime: the stat identity changed, so it re-reads.
  write_text(path, "start\n");
  set_mtime(path, 1234567890, 123456789);
  CHECK(file.poll() == ControlCommand::Start);
  CHECK_EQ(file.reads(), 2U);
  CHECK(file.poll() == ControlCommand::Start);
  CHECK_EQ(file.reads(), 2U);

  // Different size: also re-read.
  write_text(path, "resume\n");
  CHECK(file.poll() == ControlCommand::Resume);
  CHECK_EQ(file.reads(), 3U);

  // Removal means "keep running" and does not count as a read.
  std::error_code ignored;
  std::filesystem::remove(path, ignored);
  CHECK(file.poll() == ControlCommand::None);
  CHECK_EQ(file.reads(), 3U);

  reset_root();
}

EINSTAR_TEST(ring_eviction_set_keeps_newest_and_never_escapes) {
  const std::vector<std::uint32_t> dense = dense_groups(10);
  CHECK(ring_eviction_set(dense, 5) ==
        (std::vector<std::uint32_t>{0, 1, 2, 3, 4}));
  CHECK(ring_eviction_set(dense, 10).empty());
  CHECK(ring_eviction_set(dense, 11).empty());
  CHECK(ring_eviction_set(dense, 0).empty());
  CHECK(ring_eviction_set({}, 3).empty());

  // N = 1 keeps exactly the newest group; the newest id is never returned.
  const std::vector<std::uint32_t> almost_all = ring_eviction_set(dense, 1);
  CHECK_EQ(almost_all.size(), 9U);
  CHECK(std::find(almost_all.begin(), almost_all.end(), 9U) ==
        almost_all.end());

  // Non-contiguous sets keep the newest N entries, not a numeric threshold.
  const std::vector<std::uint32_t> sparse{0, 2, 5, 9};
  CHECK(ring_eviction_set(sparse, 2) ==
        (std::vector<std::uint32_t>{0, 2}));
  CHECK(ring_eviction_set(sparse, 1) ==
        (std::vector<std::uint32_t>{0, 2, 5}));

  // Deletion is restricted to plain file names directly in the frames dir.
  CHECK(safe_frame_filename("frame_000001_cam0.pgm"));
  CHECK(safe_frame_filename("frame_000001_cam0.pgm.tmp"));
  CHECK(!safe_frame_filename(""));
  CHECK(!safe_frame_filename("."));
  CHECK(!safe_frame_filename(".."));
  CHECK(!safe_frame_filename("../frame_000000_cam0.pgm"));
  CHECK(!safe_frame_filename("sub/frame_000000_cam0.pgm"));
  CHECK(!safe_frame_filename("/tmp/frame_000000_cam0.pgm"));
  CHECK(!safe_frame_filename("a\\b.pgm"));

  reset_root();
}

EINSTAR_TEST(group_writer_ring_keeps_newest_groups_and_all_manifest_records) {
  reset_root();

  SimCaptureOptions ringed;
  ringed.output_directory = path_in("ring3_frames");
  ringed.groups = 8;
  ringed.seed = 4;
  ringed.ring_size = 3;
  auto report = capture_simulated(ringed);
  REQUIRE(report.has_value());

  CHECK_EQ(report->grouping.groups_written, 8U);
  CHECK_EQ(report->ring_size, 3U);
  CHECK_EQ(report->groups_evicted, 5U);
  CHECK_EQ(report->groups_on_disk, 3U);
  CHECK_EQ(count_pgm(ringed.output_directory), 9U);

  for (std::uint32_t group = 0; group < 5; ++group) {
    for (std::uint8_t camera = 0; camera < kStreamCount; ++camera) {
      CHECK(!std::filesystem::exists(
          pgm_path(ringed.output_directory, group, camera)));
    }
  }
  for (std::uint32_t group = 5; group < 8; ++group) {
    for (std::uint8_t camera = 0; camera < kStreamCount; ++camera) {
      CHECK(std::filesystem::exists(
          pgm_path(ringed.output_directory, group, camera)));
    }
  }

  // The manifest still records every group ever written, including the five
  // whose files the ring removed.
  const std::string manifest_text = read_text(report->manifest_path);
  CHECK(contains(manifest_text, "\"groups_written\": 8"));
  CHECK(contains(manifest_text, "\"group\": 0,"));
  CHECK(contains(manifest_text, "\"group\": 7,"));
  CHECK(contains(manifest_text, "frame_000000_cam0.pgm"));
  CHECK(contains(manifest_text, "frame_000007_cam0.pgm"));

  // N = 0 keeps everything (historical behaviour).
  SimCaptureOptions keep_all;
  keep_all.output_directory = path_in("keep_frames");
  keep_all.groups = 4;
  keep_all.seed = 5;
  auto kept = capture_simulated(keep_all);
  REQUIRE(kept.has_value());
  CHECK_EQ(kept->groups_evicted, 0U);
  CHECK_EQ(kept->groups_on_disk, 4U);
  CHECK_EQ(count_pgm(keep_all.output_directory), 12U);

  reset_root();
}

EINSTAR_TEST(live_status_json_has_documented_fields_and_transitions) {
  LiveStatus status;
  status.state = SessionState::Starting;
  status.stop_reason = StopReason::None;
  status.elapsed_s = 1.5;
  status.groups_written = 7;
  status.groups_requested = 0;
  status.latest_group = 6;
  status.last_group = 123456789;
  status.ring_size = 5;
  status.groups_on_disk = 5;
  status.groups_evicted = 2;
  status.frames_written = 21;
  status.bytes_written = 41287680;
  status.disk_free_bytes = 987654321;
  status.frames_completed = 21;
  status.frames_dropped = 1;
  status.bytes_received = 50000000;
  status.throughput_mbps = 39.4;
  status.transfer_timeouts = 2;
  status.frames_invalid = 0;
  status.output_directory = "/tmp/wiz_sim/frames";
  status.trigger_period_us = 100000;
  status.exposure_us = {1200, 1200, 5000};
  status.gain = {105, 105, 180};
  status.laser_percent = std::nullopt;
  status.simulated = true;
  status.live = false;

  const std::string starting = build_live_status_json(status);
  CHECK(contains(starting, "\"schema\":\"einstar-live-status-v1\""));
  CHECK(contains(starting, "\"state\":\"starting\""));
  CHECK(contains(starting, "\"stop_reason\":null"));
  CHECK(contains(starting, "\"elapsed_s\":1.500000"));
  CHECK(contains(starting, "\"groups_written\":7"));
  CHECK(contains(starting, "\"groups_requested\":0"));
  CHECK(contains(starting, "\"latest_group\":6"));
  CHECK(contains(starting, "\"last_group\":123456789"));
  CHECK(contains(starting, "\"ring_size\":5"));
  CHECK(contains(starting, "\"groups_on_disk\":5"));
  CHECK(contains(starting, "\"groups_evicted\":2"));
  CHECK(contains(starting, "\"frames_written\":21"));
  CHECK(contains(starting, "\"bytes_written\":41287680"));
  CHECK(contains(starting, "\"disk_free_bytes\":987654321"));
  // Historical fields consumed by tools/liveview must all remain.
  CHECK(contains(starting, "\"frames_completed\":21"));
  CHECK(contains(starting, "\"frames_dropped\":1"));
  CHECK(contains(starting, "\"bytes_received\":50000000"));
  CHECK(contains(starting, "\"throughput_mbps\":39.400"));
  CHECK(contains(starting, "\"transfer_timeouts\":2"));
  CHECK(contains(starting, "\"frames_invalid\":0"));
  CHECK(contains(starting, "\"output_directory\":\"/tmp/wiz_sim/frames\""));
  CHECK(contains(starting, "\"trigger_period_us\":100000"));
  CHECK(contains(starting, "\"exposure_us\":[1200,1200,5000]"));
  CHECK(contains(starting, "\"gain\":[105,105,180]"));
  CHECK(contains(starting, "\"laser_percent\":null"));
  CHECK(contains(starting, "\"simulated\":true"));
  CHECK(contains(starting, "\"live\":false"));

  status.state = SessionState::Running;
  CHECK(contains(build_live_status_json(status), "\"state\":\"running\""));

  status.state = SessionState::Paused;
  CHECK(contains(build_live_status_json(status), "\"state\":\"paused\""));

  status.state = SessionState::Stopping;
  status.stop_reason = StopReason::ControlStop;
  const std::string stopping = build_live_status_json(status);
  CHECK(contains(stopping, "\"state\":\"stopping\""));
  CHECK(contains(stopping, "\"stop_reason\":\"control:stop\""));

  status.state = SessionState::Stopped;
  struct ReasonCase {
    StopReason reason;
    const char* text;
  };
  const ReasonCase cases[] = {
      {StopReason::Groups, "groups"},
      {StopReason::MaxSeconds, "max_seconds"},
      {StopReason::SignalInt, "signal:SIGINT"},
      {StopReason::SignalTerm, "signal:SIGTERM"},
      {StopReason::ControlStop, "control:stop"},
      {StopReason::Error, "error"},
  };
  for (const ReasonCase& item : cases) {
    status.stop_reason = item.reason;
    const std::string json = build_live_status_json(status);
    CHECK(contains(json, std::string("\"stop_reason\":\"") + item.text + "\""));
    CHECK(contains(json, "\"state\":\"stopped\""));
  }
  CHECK(session_state_name(SessionState::Paused) == "paused");
  CHECK(stop_reason_name(StopReason::SignalInt) == "signal:SIGINT");

  // Atomic publish, including creating the parent directory.
  const std::string status_path = path_in("nested/session/status.json");
  const std::string json = build_live_status_json(status);
  CHECK(write_status_file(status_path, json).ok());
  CHECK_EQ(read_text(status_path), json);
  CHECK(!std::filesystem::exists(status_path + ".tmp"));

  reset_root();
}

EINSTAR_TEST(signal_flag_keeps_first_signal_and_is_consumed_once) {
  // Drain anything a previous test (or the runtime) left behind.
  (void)take_stop_signal();
  CHECK_EQ(take_stop_signal(), 0);

  // notify_stop_signal is exactly what the SIGINT/SIGTERM handler calls; the
  // test never raises a real signal.
  notify_stop_signal(SIGINT);
  notify_stop_signal(SIGTERM);  // ignored: one stop is already pending
  CHECK_EQ(take_stop_signal(), static_cast<int>(SIGINT));
  CHECK_EQ(take_stop_signal(), 0);

  notify_stop_signal(SIGTERM);
  CHECK_EQ(take_stop_signal(), static_cast<int>(SIGTERM));

  // Unrelated signals are ignored.
  notify_stop_signal(SIGUSR1);
  CHECK_EQ(take_stop_signal(), 0);
}

#include "test_capture_safety.cpp"

EINSTAR_TEST_MAIN()
