// SPDX-License-Identifier: MIT
// Measure real acquisition cadence through the working libeinstar driver.
//
// Records every completed frame (camera, device timestamp, frame id, payload
// size, host arrival time) while the scanner runs EXStar's trigger sequence,
// then stops the trigger and switches the lights off.
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <string>
#include <thread>
#include <optional>
#include <vector>

#include "einstar/device.hpp"

using namespace einstar;
using Clock = std::chrono::steady_clock;

namespace {

struct Record {
  std::int64_t host_ns = 0;
  std::uint64_t device_ts = 0;
  std::uint32_t frame_id = 0;
  std::uint8_t field = 0;
  int camera = -1;
  std::uint8_t sub = 0;
  std::size_t size = 0;
};

std::int64_t now_ns() {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
             Clock::now().time_since_epoch())
      .count();
}

}  // namespace

int main(int argc, char** argv) {
  int seconds = 15;
  std::uint32_t period_us = 90000;
  unsigned mono = 1, rgb = 1;
  bool lights = true;
  std::string out;
  auto mode_set = false;

  for (int i = 1; i < argc; ++i) {
    const char* a = argv[i];
    auto next = [&]() -> const char* { return (i + 1 < argc) ? argv[++i] : nullptr; };
    if (!std::strcmp(a, "--seconds")) { if (auto v = next()) seconds = std::atoi(v); }
    else if (!std::strcmp(a, "--period-us")) { if (auto v = next()) period_us = std::strtoul(v, nullptr, 0); }
    else if (!std::strcmp(a, "--mono")) { if (auto v = next()) mono = std::strtoul(v, nullptr, 0); mode_set = true; }
    else if (!std::strcmp(a, "--rgb")) { if (auto v = next()) rgb = std::strtoul(v, nullptr, 0); mode_set = true; }
    else if (!std::strcmp(a, "--mode")) {
      if (auto v = next()) {
        if (!std::strcmp(v, "exstar")) { mono = 1; rgb = 1; period_us = 90000; mode_set = true; }
        else if (!std::strcmp(v, "vendor")) { mono = 3; rgb = 1; period_us = 68000; mode_set = true; }
        else { std::fprintf(stderr, "unknown mode %s\n", v); return 2; }
      }
    }
    else if (!std::strcmp(a, "--no-lights")) { lights = false; }
    else if (!std::strcmp(a, "--out")) { if (auto v = next()) out = v; }
    else { std::fprintf(stderr, "usage: %s [--mode exstar|vendor] [--seconds N] [--period-us P] [--mono M --rgb R] [--no-lights] [--out PREFIX]\n", argv[0]); return 2; }
  }
  (void)mode_set;

  OpenOptions opts;
  opts.allow_writes = true;  // trigger/exposure only; flash writes are unavailable by design
  auto opened = Device::open(std::nullopt, opts);
  if (!opened) {
    std::fprintf(stderr, "open failed: %s\n", opened.error().message.c_str());
    return 1;
  }
  auto& dev = *opened.value();
  auto fw = dev.firmware_version();
  auto sensors = dev.sensor_count();
  std::printf("firmware : %s\n", fw ? fw.value().c_str() : "(query failed)");
  std::printf("sensors  : %u\n", sensors ? sensors.value() : 0);
  std::printf("mode     : mono=%u rgb=%u period=%u us, lights=%s\n", mono, rgb,
              period_us, lights ? "on" : "off");

  std::mutex mutex;
  std::vector<Record> records;
  std::atomic<bool> failed{false};
  std::atomic<std::uint64_t> wrong_size{0};

  Result<Ok> status = dev.start_stream([&](Frame&& f) {
    Record r;
    r.host_ns = now_ns();
    r.device_ts = f.info.timestamp;
    r.frame_id = f.info.frame_id;
    r.field = f.info.camera_field;
    r.camera = f.info.camera_index() ? static_cast<int>(*f.info.camera_index()) : -1;
    r.sub = f.info.sub_index;
    r.size = f.data.size();
    if (r.size != 1280u * 1024u) wrong_size.fetch_add(1);
    std::lock_guard<std::mutex> lock(mutex);
    records.push_back(r);
  });
  if (!status) {
    std::fprintf(stderr, "start_stream failed: %s\n", status.error().message.c_str());
    return 1;
  }

  if (lights) {
    if (auto st = dev.apply(ScanSettings{}); !st)
      std::fprintf(stderr, "apply settings failed: %s\n", st.error().message.c_str());
  }
  if (auto st = dev.set_trigger_counts(mono, rgb); !st) {
    std::fprintf(stderr, "set_trigger_counts failed: %s\n", st.error().message.c_str());
    dev.stop_stream();
    return 1;
  }
  if (auto st = dev.set_trigger_period(period_us); !st)
    std::fprintf(stderr, "set_trigger_period failed: %s\n", st.error().message.c_str());

  const auto t0 = Clock::now();
  std::this_thread::sleep_for(std::chrono::seconds(seconds));
  const auto t1 = Clock::now();

  if (auto st = dev.stop_scanning(); !st)
    std::fprintf(stderr, "stop_scanning failed: %s\n", st.error().message.c_str());
  std::this_thread::sleep_for(std::chrono::milliseconds(300));
  dev.stop_stream();
  if (lights) {
    if (auto st = dev.lights_off(); !st)
      std::fprintf(stderr, "lights_off failed: %s\n", st.error().message.c_str());
  }

  std::vector<Record> all;
  {
    std::lock_guard<std::mutex> lock(mutex);
    all.swap(records);
  }
  const double wall_s = std::chrono::duration<double>(t1 - t0).count();
  std::uint64_t bytes = 0;
  for (const auto& r : all) bytes += r.size;

  std::printf("frames   : %zu (wrong-size %llu)\n", all.size(),
              static_cast<unsigned long long>(wrong_size.load()));
  std::printf("bytes    : %llu\n", static_cast<unsigned long long>(bytes));
  std::printf("wall_s   : %.3f\n", wall_s);
  std::printf("MB/s     : %.3f\n", wall_s > 0 ? bytes / wall_s / 1e6 : 0.0);

  if (!out.empty()) {
    std::FILE* f = std::fopen((out + ".csv").c_str(), "w");
    if (!f) {
      std::fprintf(stderr, "cannot write %s.csv\n", out.c_str());
    } else {
      std::fprintf(f, "host_ns,device_ts,frame_id,camera_field,camera,sub,size\n");
      for (const auto& r : all)
        std::fprintf(f, "%lld,%llu,%u,%u,%d,%u,%zu\n",
                     static_cast<long long>(r.host_ns),
                     static_cast<unsigned long long>(r.device_ts), r.frame_id,
                     r.field, r.camera, r.sub, r.size);
      std::fclose(f);
      std::printf("csv      : %s.csv\n", out.c_str());
    }
  }
  (void)failed;
  return 0;
}
