// SPDX-License-Identifier: MIT
// Offline benchmark harness.
//
// Every number printed here is produced by the deterministic simulated device
// Every row is produced by the deterministic simulated device (virtual clock).
// This is NOT a hardware measurement.  Host wall time is measured separately
// and labelled.  The "scenario:" rows isolate the link explicitly: USB 2.0 HS
// (~40 MB/s), a hypothetical USB 3.0 SS (~350 MB/s), the vendor 68 ms trigger
// cadence, and a host-only ceiling.  A generic 300 MB/s model is not a scanner
// result and must not be presented as one.
#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iomanip>
#include <memory>
#include <string>
#include <utility>
#include <vector>

#include "einstar/pipeline.hpp"
#include "einstar/sim.hpp"

using namespace einstar;

namespace {

struct Options {
  std::uint32_t frames = 120;
  std::string output = "bench_results.json";
  std::uint64_t seed = 1;
  double bandwidth_mbps = 300.0;
  std::int64_t trigger_us = 40'000;
  bool quick = false;
  bool scenarios_only = false;
};

struct Row {
  std::string experiment;
  std::string mode;
  std::uint32_t transfer_size = 0;
  std::uint32_t depth = 0;
  std::string copy;
  std::uint32_t frames = 0;
  std::uint32_t cameras = 1;
  double virtual_seconds = 0.0;
  double fps = 0.0;
  double mbps = 0.0;
  double p50_us = 0.0;
  double p95_us = 0.0;
  double p99_us = 0.0;
  double max_us = 0.0;
  std::uint64_t drops = 0;
  std::size_t queue_peak = 0;
  double host_ms = 0.0;
  double host_fps = 0.0;
};

double to_us(std::int64_t ns) { return static_cast<double>(ns) / 1000.0; }

Row run_case(std::string experiment, const SimConfig& base_sim,
             const PipelineConfig& base_config, std::uint32_t frames,
             std::uint32_t depth, CopyMode mode, bool saturated) {
  SimConfig sim = base_sim;
  sim.frames = frames;  // exactly the target: no partial frame at stop
  sim.trigger_period_ns = saturated ? 0 : base_sim.trigger_period_ns;
  PipelineConfig config = base_config;
  config.queue_depth = depth;
  config.copy_mode = mode;
  config.frame_queue_capacity = 32;
  if (mode == CopyMode::ZeroCopy) {
    config.transfer_pool_buffers = depth + 96;
  }

  Row row;
  row.experiment = std::move(experiment);
  row.mode = depth == 1 ? "sync" : "async";
  row.transfer_size = config.transfer_buffer_size;
  row.depth = depth;
  row.copy = mode == CopyMode::ZeroCopy ? "zero-copy" : "copy";
  row.frames = frames;
  row.cameras = sim.camera_count == 0 ? 1 : sim.camera_count;

  auto transport = std::make_unique<einstar::SimulatedTransport>(sim);
  auto pipeline = std::make_unique<einstar::AcquisitionPipeline>(
      std::move(transport), config);
  const auto wall_start = std::chrono::steady_clock::now();
  if (!pipeline->start().ok()) {
    std::fprintf(stderr, "benchmark case failed to start: %s\n",
                 row.experiment.c_str());
    return row;
  }
  std::size_t received = 0;
  std::size_t iterations = 0;
  einstar::Frame frame;
  while (received < frames && iterations++ < 5'000'000) {
    (void)pipeline->poll(4);
    while (pipeline->try_pop_frame(frame)) {
      ++received;
      frame = einstar::Frame{};
    }
  }
  const auto wall_end = std::chrono::steady_clock::now();
  const std::int64_t start_ns = pipeline->start_time_ns();
  const std::int64_t end_ns = pipeline->last_frame_time_ns();
  (void)pipeline->stop();
  const einstar::PipelineStats stats = pipeline->stats();
  const auto metrics = einstar::compute_metrics(
      stats, start_ns, end_ns, config.frame_size);

  row.virtual_seconds = metrics.elapsed_seconds;
  row.fps = metrics.fps;
  row.mbps = metrics.megabytes_per_second;
  row.p50_us = to_us(stats.transfer_latency.p50_ns);
  row.p95_us = to_us(stats.transfer_latency.p95_ns);
  row.p99_us = to_us(stats.transfer_latency.p99_ns);
  row.max_us = to_us(stats.transfer_latency.max_ns);
  row.drops = stats.queue_drops + stats.frames_dropped;
  row.queue_peak = stats.frame_queue_peak;
  const double host_seconds =
      std::chrono::duration<double>(wall_end - wall_start).count();
  row.host_ms = host_seconds * 1000.0;
  row.host_fps = host_seconds > 0.0
                     ? static_cast<double>(received) / host_seconds
                     : 0.0;
  return row;
}

void print_table(const std::vector<Row>& rows) {
  std::printf(
      "%-10s %-6s %8s %5s %-9s %6s %9s %8s %8s %8s %8s %8s %6s %6s %9s "
      "%10s\n",
      "experiment", "mode", "xfer", "depth", "copy", "frames", "MB/s(sim)",
      "fps(sim)", "p50us", "p95us", "p99us", "maxus", "drops", "qpeak",
      "host_ms", "host_fps");
  std::printf("%s\n", std::string(147, '-').c_str());
  for (const Row& row : rows) {
    std::printf("%-10s %-6s 0x%06X %5u %-9s %6u %9.1f %8.2f %8.1f %8.1f "
                "%8.1f %8.1f %6llu %6zu %9.1f %10.1f\n",
                row.experiment.c_str(), row.mode.c_str(), row.transfer_size,
                row.depth, row.copy.c_str(), row.frames, row.mbps, row.fps,
                row.p50_us, row.p95_us, row.p99_us, row.max_us,
                static_cast<unsigned long long>(row.drops), row.queue_peak,
                row.host_ms, row.host_fps);
  }
}

void write_json(const Options& options, const std::vector<Row>& rows) {
  std::ofstream out(options.output, std::ios::trunc);
  if (!out) {
    std::fprintf(stderr, "cannot write %s\n", options.output.c_str());
    return;
  }
  out << std::fixed << std::setprecision(3);
  out << "{\n";
  out << "  \"schema\": \"einstar-bench-v1\",\n";
  out << "  \"simulated\": true,\n";
  out << "  \"hardware\": false,\n";
  out << "  \"note\": \"SIMULATED device model (300 MB/s link, 40 ms trigger "
         "period). Not a hardware measurement. host_ms is real CPU time on "
         "this machine.\",\n";
  out << "  \"device\": {\"bandwidth_bytes_per_sec\": "
      << static_cast<std::uint64_t>(options.bandwidth_mbps * 1'000'000.0)
      << ", \"trigger_period_us\": " << options.trigger_us
      << ", \"frame_size\": 1310720, \"chunk_payload\": 40960, \"seed\": "
      << options.seed << "},\n";
  out << "  \"rows\": [\n";
  for (std::size_t i = 0; i < rows.size(); ++i) {
    const Row& row = rows[i];
    out << "    {\"experiment\": \"" << row.experiment << "\", \"mode\": \""
        << row.mode << "\", \"transfer_size\": " << row.transfer_size
        << ", \"depth\": " << row.depth << ", \"copy\": \"" << row.copy
        << "\", \"frames\": " << row.frames
        << ", \"virtual_seconds\": " << row.virtual_seconds
        << ", \"fps\": " << row.fps << ", \"mbps\": " << row.mbps
        << ", \"p50_us\": " << row.p50_us << ", \"p95_us\": " << row.p95_us
        << ", \"p99_us\": " << row.p99_us << ", \"max_us\": " << row.max_us
        << ", \"drops\": " << row.drops << ", \"queue_peak\": "
        << row.queue_peak << ", \"host_ms\": " << row.host_ms
        << ", \"host_fps\": " << row.host_fps << "}";
    out << (i + 1 == rows.size() ? "\n" : ",\n");
  }
  out << "  ]\n}\n";
}

// ---------------------------------------------------------------------------
// Separated link scenarios.  The earlier tables all used one generic 300 MB/s
// model; that number is neither USB 2.0 nor USB 3.0 and must not be read as an
// achievable scanner result.  These scenarios are explicit about the link:
//   usb2-hs      : practical USB 2.0 High Speed bulk throughput (~40 MB/s)
//   usb3-ss      : hypothetical USB 3.0 SuperSpeed (~350 MB/s), unverified
//   trigger-68ms : vendor cadence (ScanTriggerPeriod 68000 us, 3 cameras)
//   host-only    : link not limiting; measures the host pipeline ceiling
// ---------------------------------------------------------------------------
struct Scenario {
  const char* name;
  double bandwidth_mbps;
  std::int64_t trigger_us;
  std::uint8_t cameras;
  const char* note;
};

constexpr Scenario kScenarios[] = {
    {"usb2-hs", 40.0, 0, 3, "USB 2.0 HS bulk, back-to-back (link-limited)"},
    {"usb3-ss", 350.0, 0, 3, "USB 3.0 SS, hypothetical (unverified)"},
    {"trigger-68ms", 350.0, 68'000, 3, "vendor cadence, 3 cameras per group"},
    {"host-only", 10'000.0, 0, 3, "no link limit (host CPU ceiling)"},
};

double frame_wire_bytes() {
  const std::uint32_t chunks =
      (einstar::kDefaultFrameSize + einstar::kStreamChunkPayload - 1) /
      einstar::kStreamChunkPayload;
  return static_cast<double>(einstar::kDefaultFrameSize) +
         static_cast<double>(chunks) *
             (einstar::kStreamHeaderSize + einstar::kStreamLengthSize);
}

void print_limit_analysis() {
  const double per_frame = frame_wire_bytes();
  const double group = per_frame * 3.0;
  const double period = 0.068;  // 68 ms
  std::printf(
      "\n-- bandwidth vs cadence (1280x1024, 8-bit, 32 packets/frame) --\n");
  std::printf("frame on the wire   : %.0f B (payload 1310720 + framing)\n",
              per_frame);
  std::printf("3-camera group      : %.0f B\n", group);
  std::printf("68 ms cadence needs : %.1f MB/s aggregate\n", group / period / 1e6);
  std::printf("USB2 HS practical   : 40 MB/s -> %.1f fps aggregate, %.1f fps/camera\n",
              40e6 / per_frame, 40e6 / per_frame / 3.0);
  std::printf("USB3 SS (if usable) : 350 MB/s -> cadence-limited %.1f fps aggregate, "
              "%.1f fps/camera\n",
              3.0 / period, 1.0 / period);
  std::printf(
      "=> at 68 ms and three full frames per trigger the device needs ~58 MB/s; "
      "a USB 2.0 HS link cannot carry it.  Either the scanner enumerates at "
      "SuperSpeed in normal use, or it reduces the transmitted frame set.  This "
      "must be measured on hardware.\n");
}

void run_scenarios(const Options& options, const PipelineConfig& base_config,
                   std::vector<Row>& rows) {
  for (const Scenario& scenario : kScenarios) {
    for (std::uint32_t depth : {1U, 8U}) {
      SimConfig sim;
      sim.seed = options.seed;
      sim.frame_size = einstar::kDefaultFrameSize;
      sim.chunk_payload = einstar::kStreamChunkPayload;
      sim.trigger_period_ns = scenario.trigger_us * 1000;
      sim.bandwidth_bytes_per_sec =
          static_cast<std::uint64_t>(scenario.bandwidth_mbps * 1'000'000.0);
      sim.transfer_latency_ns = 500'000;
      sim.expect_interval_quirk = false;
      sim.camera_count = scenario.cameras;
      sim.first_camera_index = 0;
      const bool saturated = scenario.trigger_us == 0;
      rows.push_back(run_case(std::string("scenario:") + scenario.name, sim,
                              base_config, options.frames, depth,
                              CopyMode::Copy, saturated));
    }
  }
}

void print_scenarios(const std::vector<Row>& rows) {
  std::printf(
      "\n=== link scenarios (SIMULATED; host columns are real) ===\n");
  std::printf("%-18s %5s %7s %10s %9s %11s %8s %8s %8s %9s\n", "scenario",
              "depth", "cameras", "agg MB/s", "frame/s", "fps/camera", "p50us",
              "p99us", "host_ms", "host_fps");
  for (const Row& row : rows) {
    if (row.experiment.rfind("scenario:", 0) != 0) {
      continue;
    }
    std::printf("%-18s %5u %7u %10.1f %9.1f %11.2f %8.0f %8.0f %8.1f %9.0f\n",
                row.experiment.c_str() + 9, row.depth, row.cameras, row.mbps,
                row.fps, row.fps / std::max(1U, row.cameras), row.p50_us,
                row.p99_us, row.host_ms, row.host_fps);
  }
}

}  // namespace

int main(int argc, char** argv) {
  Options options;
  for (int i = 1; i < argc; ++i) {
    const std::string argument = argv[i];
    if (argument == "--quick") {
      options.quick = true;
    } else if (argument == "--frames" && i + 1 < argc) {
      options.frames = static_cast<std::uint32_t>(std::strtoul(argv[++i], nullptr, 0));
    } else if (argument == "--out" && i + 1 < argc) {
      options.output = argv[++i];
    } else if (argument == "--seed" && i + 1 < argc) {
      options.seed = std::strtoull(argv[++i], nullptr, 0);
    } else if (argument == "--bandwidth-mbps" && i + 1 < argc) {
      options.bandwidth_mbps = std::strtod(argv[++i], nullptr);
    } else if (argument == "--trigger-us" && i + 1 < argc) {
      options.trigger_us = std::strtoll(argv[++i], nullptr, 0);
    } else if (argument == "--scenarios-only") {
      options.scenarios_only = true;
    } else if (argument == "--help" || argument == "-h") {
      std::printf(
          "usage: einstar-bench [--frames N] [--out FILE] [--seed S]\n"
          "                     [--bandwidth-mbps B] [--trigger-us T] [--quick]\n"
          "                     [--scenarios-only]\n");
      return 0;
    } else {
      std::fprintf(stderr, "unknown argument: %s\n", argument.c_str());
      return 2;
    }
  }
  if (options.quick) {
    options.frames = std::max<std::uint32_t>(20, options.frames / 2);
  }

  SimConfig base_sim;
  base_sim.seed = options.seed;
  base_sim.frame_size = einstar::kDefaultFrameSize;
  base_sim.chunk_payload = einstar::kStreamChunkPayload;
  base_sim.trigger_period_ns = options.trigger_us * 1000;
  base_sim.bandwidth_bytes_per_sec =
      static_cast<std::uint64_t>(options.bandwidth_mbps * 1'000'000.0);
  base_sim.transfer_latency_ns = 500'000;  // 500 us per URB
  base_sim.expect_interval_quirk = false;

  PipelineConfig base_config;
  base_config.transfer_buffer_size = 0xA400;
  base_config.max_packet_bytes = 0xA400;
  base_config.frame_queue_capacity = 32;
  base_config.partial_frame_timeout_ns = 500'000'000;

  std::vector<Row> rows;

  if (options.scenarios_only) {
    run_scenarios(options, base_config, rows);
    print_scenarios(rows);
    print_limit_analysis();
    write_json(options, rows);
    std::printf("\nJSON written to %s\n", options.output.c_str());
    return 0;
  }

  // A. Sync vs async queue depth (triggered, copy path).
  for (std::uint32_t depth : {1U, 2U, 4U, 8U, 16U}) {
    rows.push_back(run_case("depth", base_sim, base_config, options.frames,
                            depth, CopyMode::Copy, /*saturated=*/false));
  }

  // B. memcpy-into-frame vs zero-copy.
  for (std::uint32_t depth : {2U, 8U, 16U}) {
    rows.push_back(run_case("copy-vs-zero", base_sim, base_config,
                            options.frames, depth, CopyMode::ZeroCopy, false));
  }

  // C. Transfer size sweep at depth 8.
  for (std::uint32_t transfer_size : {0x4000U, 0xA000U, 0xA400U, 0x40000U}) {
    PipelineConfig config = base_config;
    config.transfer_buffer_size = transfer_size;
    rows.push_back(run_case("xfer-size", base_sim, config, options.frames, 8,
                            CopyMode::Copy, false));
  }

  // D. Saturated pipeline (trigger gap removed) to show the link ceiling.
  const std::uint32_t saturated_frames =
      options.quick ? 80U : options.frames * 2U;
  for (std::uint32_t depth : {1U, 2U, 4U, 8U, 16U}) {
    rows.push_back(run_case("saturated", base_sim, base_config,
                            saturated_frames, depth, CopyMode::Copy, true));
  }

  std::printf(
      "SIMULATED RESULTS - no scanner, no CUDA, no network. Device model: "
      "%.0f MB/s link, %.1f ms trigger period, 1280x1024 frames.\n",
      options.bandwidth_mbps, static_cast<double>(options.trigger_us) / 1000.0);
  std::printf(
      "'sync' = one outstanding transfer. host_ms/host_fps are real wall-clock "
      "measurements of this machine; everything else is virtual-clock.\n\n");
  print_table(rows);

  // E. Separated link scenarios (see kScenarios).
  run_scenarios(options, base_config, rows);
  print_scenarios(rows);
  print_limit_analysis();

  write_json(options, rows);
  std::printf("\nJSON written to %s\n", options.output.c_str());
  return 0;
}
