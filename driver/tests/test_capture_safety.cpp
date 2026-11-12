// SPDX-License-Identifier: MIT
// Included by test_capture_session.cpp so no build-system ownership is needed.
// Exercises cleanup on exits before and inside the shared session loop.
#include <stdexcept>
#include <unistd.h>

namespace {
class SafetyTestDriver final : public CaptureSessionDriver {
 public:
  DeviceInfo info;
  bool started = false;
  bool stopped = false;
  bool throw_poll = false;
  bool fail_start = false;
  bool fail_stop = false;
  bool block_stop = false;
  std::string stop_marker;
  Status start() override {
    started = true;
    return fail_start ? Status::error(ErrorCode::IoError, "injected start failure")
                      : Status::success();
  }
  Status poll(int) override {
    if (throw_poll) { throw std::runtime_error("injected poll exception"); }
    notify_stop_signal(SIGTERM);
    return Status::success();
  }
  bool try_pop_frame(Frame&) override { return false; }
  PipelineStats stats() const override { return {}; }
  const DeviceInfo& device() const override { return info; }
  Status pause() override { return Status::success(); }
  Status resume() override { return Status::success(); }
  Status pump_paused(int timeout_ms) override { return poll(timeout_ms); }
  Status stop() override {
    stopped = true;
    info.cleanup_attempted = true;
    if (block_stop) {
      write_text(stop_marker, "cleanup attempted; readback unavailable\n");
      std::this_thread::sleep_for(std::chrono::seconds(30));
    }
    info.cleanup_verified = !fail_stop;
    if (!fail_stop) { info.laser_percent = 0; }
    return fail_stop ? Status::error(ErrorCode::IoError, "injected cleanup failure")
                     : Status::success();
  }
  double virtual_duration_seconds() const override { return 0; }
};

CaptureSessionOptions safety_options(const std::string& suffix) {
  CaptureSessionOptions options;
  options.output_directory = "/tmp/einstar_safety_" +
      std::to_string(::getpid()) + "_" + suffix;
  std::filesystem::remove_all(options.output_directory);
  options.groups = 1;
  options.timeout_seconds = 1;
  options.manifest.simulated = true;
  options.status_file = options.output_directory + "/status.json";
  return options;
}
}  // namespace

EINSTAR_TEST(safety_cleanup_on_output_preparation_failure) {
  auto options = safety_options("prepare");
  write_text(options.output_directory, "not a directory");
  SafetyTestDriver driver;
  CHECK(!run_capture_session(options, driver));
  CHECK(!driver.started);
  CHECK(driver.stopped);
  std::filesystem::remove(options.output_directory);
}

EINSTAR_TEST(safety_cleanup_on_start_failure) {
  auto options = safety_options("start");
  SafetyTestDriver driver;
  driver.fail_start = true;
  CHECK(!run_capture_session(options, driver));
  CHECK(driver.started);
  CHECK(driver.stopped);
  std::filesystem::remove_all(options.output_directory);
}

EINSTAR_TEST(safety_cleanup_on_exception_unwinding) {
  auto options = safety_options("exception");
  SafetyTestDriver driver;
  driver.throw_poll = true;
  bool caught = false;
  try { (void)run_capture_session(options, driver); }
  catch (const std::runtime_error&) { caught = true; }
  CHECK(caught);
  CHECK(driver.stopped);
  std::filesystem::remove_all(options.output_directory);
}

EINSTAR_TEST(safety_pending_signal_prevents_start) {
  auto options = safety_options("signal");
  SafetyTestDriver driver;
  notify_stop_signal(SIGTERM);
  auto result = run_capture_session(options, driver);
  CHECK(result);
  CHECK(!driver.started);
  CHECK(driver.stopped);
  CHECK(result->stop_reason == StopReason::SignalTerm);
  CHECK(contains(read_text(options.status_file), "\"cleanup_verified\":true"));
  std::filesystem::remove_all(options.output_directory);
}

EINSTAR_TEST(safety_cleanup_failure_is_not_reported_verified) {
  auto options = safety_options("failed_stop");
  SafetyTestDriver driver;
  driver.fail_stop = true;
  CHECK(!run_capture_session(options, driver));
  CHECK(driver.stopped);
  const auto status = read_text(options.status_file);
  CHECK(contains(status, "\"cleanup_attempted\":true"));
  CHECK(contains(status, "\"cleanup_verified\":false"));
  CHECK(contains(status, "\"laser_percent\":null"));
  std::filesystem::remove_all(options.output_directory);
}

#ifdef __linux__
#include <poll.h>
#include <sys/prctl.h>
#include <sys/wait.h>

namespace {
int safety_wait_child(pid_t child, int timeout_ms) {
  int status = 0;
  for (int elapsed = 0; elapsed < timeout_ms; elapsed += 10) {
    if (::waitpid(child, &status, WNOHANG) == child) { return status; }
    std::this_thread::sleep_for(std::chrono::milliseconds(10));
  }
  (void)::kill(child, SIGKILL);
  (void)::waitpid(child, &status, 0);
  return -1;
}
}  // namespace

EINSTAR_TEST(safety_linux_parent_death_signal_is_installed) {
  const pid_t child = ::fork();
  CHECK(child >= 0);
  if (child == 0) {
    (void)::unsetenv("EINSTAR_CAPTURE_PARENT_PID");
    (void)::unsetenv("EINSTAR_CAPTURE_HEARTBEAT_FD");
    ProcessSafetyGuard guard;
    if (!guard.arm_from_environment().ok()) { ::_exit(10); }
    int death_signal = 0;
    if (::prctl(PR_GET_PDEATHSIG, &death_signal) != 0 || death_signal != SIGTERM) {
      ::_exit(11);
    }
    ::_exit(0);
  }
  const int status = safety_wait_child(child, 2000);
  CHECK(status != -1);
  CHECK(WIFEXITED(status));
  CHECK_EQ(WEXITSTATUS(status), 0);
}

EINSTAR_TEST(safety_watchdog_bounds_blocked_teardown_and_reports_unverified) {
  const auto options = safety_options("blocked");
  const std::string diagnostic = options.output_directory + ".stderr";
  const std::string marker = options.output_directory + ".attempt";
  const pid_t child = ::fork();
  CHECK(child >= 0);
  if (child == 0) {
    (void)::unsetenv("EINSTAR_CAPTURE_PARENT_PID");
    (void)::unsetenv("EINSTAR_CAPTURE_HEARTBEAT_FD");
    if (::freopen(diagnostic.c_str(), "w", stderr) == nullptr) { ::_exit(12); }
    ProcessSafetyGuard guard;
    if (!guard.arm_from_environment().ok()) { ::_exit(13); }
    SafetyTestDriver driver;
    driver.block_stop = true;
    driver.stop_marker = marker;
    notify_stop_signal(SIGTERM);
    (void)run_capture_session(options, driver);
    ::_exit(14);
  }
  const auto start = std::chrono::steady_clock::now();
  const int status = safety_wait_child(child, 7000);
  const double elapsed = std::chrono::duration<double>(
      std::chrono::steady_clock::now() - start).count();
  CHECK(status != -1);
  CHECK(WIFSIGNALED(status));
  CHECK_EQ(WTERMSIG(status), static_cast<int>(SIGKILL));
  CHECK(elapsed >= 4.9 && elapsed < 7.0);
  CHECK(contains(read_text(marker), "cleanup attempted"));
  CHECK(contains(read_text(diagnostic), "illuminator state UNVERIFIED"));
  std::printf("MEASURE blocked teardown: %.3f s\n", elapsed);
  std::filesystem::remove_all(options.output_directory);
  std::filesystem::remove(marker);
  std::filesystem::remove(diagnostic);
}
#endif

#ifdef __linux__
namespace {
int safety_signal_pipe = -1;
}

EINSTAR_TEST(safety_linux_pdeathsig_is_delivered_on_parent_sigkill) {
  // Isolate kernel delivery from the watchdog: only the SIGTERM handler writes
  // the evidence byte; watchdog expiry merely sets the cooperative stop flag.
  int previous_subreaper = 0;
  CHECK_EQ(::prctl(PR_GET_CHILD_SUBREAPER, &previous_subreaper), 0);
  CHECK_EQ(::prctl(PR_SET_CHILD_SUBREAPER, 1), 0);
  int identity[2];
  int events[2];
  CHECK_EQ(::pipe(identity), 0);
  CHECK_EQ(::pipe(events), 0);
  const pid_t parent = ::fork();
  CHECK(parent >= 0);
  if (parent == 0) {
    (void)::close(identity[0]);
    (void)::close(events[0]);
    const pid_t child = ::fork();
    if (child < 0) { ::_exit(20); }
    if (child > 0) {
      (void)::write(identity[1], &child, sizeof(child));
      for (;;) { ::pause(); }
    }
    (void)::unsetenv("EINSTAR_CAPTURE_PARENT_PID");
    (void)::unsetenv("EINSTAR_CAPTURE_HEARTBEAT_FD");
    ProcessSafetyGuard guard;
    if (!guard.arm_from_environment().ok()) { ::_exit(21); }
    safety_signal_pipe = events[1];
    struct sigaction action {};
    action.sa_handler = [](int) {
      const char delivered = 'S';
      (void)::write(safety_signal_pipe, &delivered, 1);
      ::_exit(0);
    };
    (void)::sigemptyset(&action.sa_mask);
    if (::sigaction(SIGTERM, &action, nullptr) != 0) { ::_exit(22); }
    const char ready = 'R';
    (void)::write(events[1], &ready, 1);
    for (;;) { ::pause(); }
  }
  (void)::close(identity[1]);
  (void)::close(events[1]);
  struct pollfd id_ready {identity[0], POLLIN, 0};
  const int identity_poll = ::poll(&id_ready, 1, 2000);
  pid_t child = -1;
  const auto id_count = identity_poll > 0 ? ::read(identity[0], &child, sizeof(child)) : 0;
  struct pollfd event_ready {events[0], POLLIN, 0};
  char ready = 0;
  const int ready_poll = ::poll(&event_ready, 1, 2000);
  if (ready_poll > 0) { (void)::read(events[0], &ready, 1); }
  (void)::kill(parent, SIGKILL);
  char delivered = 0;
  const int delivered_poll = ::poll(&event_ready, 1, 2000);
  if (delivered_poll > 0) { (void)::read(events[0], &delivered, 1); }
  const int parent_status = safety_wait_child(parent, 2000);
  const int child_status = child > 0 ? safety_wait_child(child, 2000) : -1;
  (void)::close(identity[0]);
  (void)::close(events[0]);
  (void)::prctl(PR_SET_CHILD_SUBREAPER, previous_subreaper);
  CHECK_EQ(id_count, static_cast<ssize_t>(sizeof(child)));
  CHECK_EQ(ready, 'R');
  CHECK_EQ(delivered, 'S');
  CHECK(parent_status != -1 && child_status != -1);
  CHECK(WIFEXITED(child_status));
  CHECK_EQ(WEXITSTATUS(child_status), 0);
}
#endif
