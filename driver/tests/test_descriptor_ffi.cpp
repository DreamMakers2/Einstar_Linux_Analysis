// SPDX-License-Identifier: MIT
#include <array>
#include <cstdint>
#include <cstring>
#include <string>
#include <vector>

#include "einstar/ffi.h"
#include "einstar/packet.hpp"
#include "einstar/sim.hpp"
#include "einstar/usb_descriptor.hpp"
#include "test_util.hpp"

using namespace einstar;

namespace {

UsbConfigDescriptor make_einstar_descriptor(bool zero_intervals) {
  InterfaceInfo interface_info;
  interface_info.number = 0;
  interface_info.alternate_setting = 0;
  interface_info.class_code = 0xEF;
  interface_info.subclass = 0x02;
  interface_info.protocol = 0x01;
  interface_info.endpoints = {
      EndpointInfo{0x01, 0x03, 64, 0, 0},
      EndpointInfo{0x81, 0x03, 64, 0, 0},
      EndpointInfo{0x02, 0x02, 512, 0, 0},
      EndpointInfo{0x82, 0x02, 512, 0, 0},
      EndpointInfo{0x83, 0x02, 512, 0, 0},
  };
  return UsbConfigDescriptor::make_synthetic(1, {std::move(interface_info)},
                                             zero_intervals);
}

std::uint64_t fnv1a(const std::uint8_t* data, std::size_t size) {
  std::uint64_t hash = 14695981039346656037ULL;
  for (std::size_t i = 0; i < size; ++i) {
    hash ^= static_cast<std::uint64_t>(data[i]);
    hash *= 1099511628211ULL;
  }
  return hash;
}

struct CallbackState {
  std::size_t count = 0;
  std::uint64_t first_digest = 0;
  std::size_t first_size = 0;
};

}  // namespace

EINSTAR_TEST(descriptor_parse_and_quirk) {
  const UsbConfigDescriptor descriptor = make_einstar_descriptor(true);
  REQUIRE(descriptor.valid());
  CHECK_EQ(descriptor.info().total_length, 9U + 9U + 5U * 7U);
  CHECK_EQ(descriptor.info().configuration_value, 1U);
  REQUIRE(descriptor.info().interfaces.size() == 1U);
  const InterfaceInfo& interface_info = descriptor.info().interfaces[0];
  CHECK_EQ(interface_info.number, 0U);
  CHECK_EQ(interface_info.class_code, 0xEFU);
  CHECK_EQ(interface_info.subclass, 0x02U);
  CHECK_EQ(interface_info.protocol, 0x01U);
  CHECK_EQ(interface_info.endpoints.size(), 5U);
  const auto zero = descriptor.zero_interval_interrupt_endpoints();
  CHECK_EQ(zero.size(), 2U);
  CHECK_EQ(descriptor.quirk_notes().size(), 2U);

  UsbConfigDescriptor patched = descriptor;
  auto patched_count = patched.patch_zero_intervals(7);
  REQUIRE(patched_count.has_value());
  CHECK_EQ(*patched_count, 2U);
  CHECK(patched.was_patched());
  CHECK(patched.zero_interval_interrupt_endpoints().empty());
  CHECK(patched.find_endpoint(0x81) != nullptr);
  CHECK_EQ(patched.find_endpoint(0x81)->interval, 7U);
  CHECK(patched.find_endpoint(0x02) != nullptr);
  CHECK(patched.find_endpoint(0x99) == nullptr);
  CHECK(patched.find_interface(0, 0) != nullptr);
  CHECK(patched.find_interface(0, 1) == nullptr);

  auto reparsed = UsbConfigDescriptor::parse(patched.serialize());
  REQUIRE(reparsed.has_value());
  CHECK_EQ(reparsed->find_endpoint(0x01)->interval, 7U);

  CHECK(!patched.patch_interrupt_interval(0x02, 7).has_value());
  CHECK(!patched.patch_interrupt_interval(0x81, 0).has_value());
  CHECK(!patched.patch_interrupt_interval(0x81, 17).has_value());
  CHECK(!patched.patch_interrupt_interval(0x77, 7).has_value());
}

EINSTAR_TEST(descriptor_rejects_malformed) {
  std::vector<std::byte> tiny(5, std::byte{0});
  CHECK(!UsbConfigDescriptor::parse(tiny).has_value());
  const UsbConfigDescriptor descriptor = make_einstar_descriptor(false);
  std::vector<std::byte> raw = descriptor.raw();
  raw.resize(raw.size() - 1);
  CHECK(!UsbConfigDescriptor::parse(raw).has_value());
  raw = descriptor.raw();
  raw[0] = std::byte{4};
  CHECK(!UsbConfigDescriptor::parse(raw).has_value());
}

EINSTAR_TEST(ffi_config_defaults) {
  einstar_config config{};
  einstar_config_init(&config);
  CHECK_EQ(config.struct_size, sizeof(einstar_config));
  CHECK_EQ(config.abi_version, EINSTAR_ABI_VERSION);
  CHECK_EQ(config.backend, EINSTAR_BACKEND_SIMULATED);
  CHECK_EQ(config.queue_depth, 8U);
  CHECK_EQ(config.transfer_buffer_size, 0xA400U);
  CHECK_EQ(config.frame_size, 1280U * 1024U);
  CHECK(std::strstr(einstar_status_string(EINSTAR_STATUS_TIMEOUT), "timeout") !=
        nullptr);
  CHECK(std::strstr(einstar_version_string(), "einstar-driver") != nullptr);
  CHECK(std::strlen(einstar_status_string(9999)) > 0U);
}

EINSTAR_TEST(ffi_simulated_read_frame) {
  einstar_config config{};
  einstar_config_init(&config);
  config.sim_frames = 8;
  config.queue_depth = 4;
  config.frame_queue_capacity = 8;
  config.sim_seed = 42;
  einstar_error error{};
  einstar_pipeline* pipeline = einstar_pipeline_create(&config, &error);
  REQUIRE(pipeline != nullptr);
  REQUIRE(einstar_pipeline_start(pipeline, &error) == EINSTAR_STATUS_OK);

  std::size_t received = 0;
  std::size_t iterations = 0;
  std::vector<std::uint8_t> payload;
  while (received < 8 && iterations++ < 20000) {
    einstar_frame_view view{};
    const std::int32_t rc =
        einstar_pipeline_read_frame(pipeline, &view, 5, &error);
    if (rc == EINSTAR_STATUS_TIMEOUT) {
      continue;
    }
    REQUIRE(rc == EINSTAR_STATUS_OK);
    CHECK_EQ(view.camera_id, 0U);
    CHECK_EQ(view.payload_size, 1280U * 1024U);
    CHECK_EQ(view.chunk_count, 1U);
    CHECK((view.flags & 1U) != 0U);
    payload.resize(view.payload_size);
    CHECK_EQ(einstar_frame_copy_payload(pipeline, &view, payload.data(),
                                        payload.size(), &error),
             EINSTAR_STATUS_OK);
    CHECK_EQ(fnv1a(payload.data(), payload.size()),
             simulated_frame_digest(view.frame_index, kDefaultFrameSize,
                                    kStreamChunkPayload));
    CHECK_EQ(einstar_pipeline_release_frame(pipeline, view.token, &error),
             EINSTAR_STATUS_OK);
    received++;
  }
  CHECK_EQ(received, 8U);

  einstar_stats stats{};
  CHECK_EQ(einstar_pipeline_get_stats(pipeline, &stats, &error),
           EINSTAR_STATUS_OK);
  CHECK_EQ(stats.frames_completed, 8U);
  CHECK(stats.frames_per_second > 0.0);
  CHECK_EQ(einstar_pipeline_stop(pipeline, &error), EINSTAR_STATUS_OK);
  einstar_pipeline_destroy(pipeline);

  // null / ABI error handling
  CHECK(einstar_pipeline_create(nullptr, &error) == nullptr);
  einstar_config bad{};
  bad.struct_size = sizeof(bad);
  bad.abi_version = 99;
  CHECK(einstar_pipeline_create(&bad, &error) == nullptr);
  CHECK(error.code == EINSTAR_STATUS_INVALID_ARGUMENT);
}

void ffi_frame_callback(void* user, const einstar_frame_view* view) {
  auto* state = static_cast<CallbackState*>(user);
  if (state->count == 0) {
    state->first_digest = fnv1a(view->data, view->payload_size);
    state->first_size = view->payload_size;
  }
  state->count++;
}

EINSTAR_TEST(ffi_callback_mode) {
  einstar_config config{};
  einstar_config_init(&config);
  config.sim_frames = 3;
  config.queue_depth = 4;
  config.sim_seed = 5;
  einstar_error error{};
  einstar_pipeline* pipeline = einstar_pipeline_create(&config, &error);
  REQUIRE(pipeline != nullptr);
  CallbackState state;
  REQUIRE(einstar_pipeline_set_callback(pipeline, ffi_frame_callback, &state,
                                        &error) == EINSTAR_STATUS_OK);
  REQUIRE(einstar_pipeline_start(pipeline, &error) == EINSTAR_STATUS_OK);
  std::size_t iterations = 0;
  while (state.count < 3 && iterations++ < 20000) {
    (void)einstar_pipeline_poll(pipeline, 5, &error);
  }
  CHECK_EQ(state.count, 3U);
  CHECK_EQ(state.first_size, 1280U * 1024U);
  CHECK_EQ(state.first_digest,
           simulated_frame_digest(0, kDefaultFrameSize, kStreamChunkPayload));
  einstar_frame_view view{};
  CHECK(einstar_pipeline_read_frame(pipeline, &view, 0, &error) ==
        EINSTAR_STATUS_INVALID_ARGUMENT);
  REQUIRE(einstar_pipeline_stop(pipeline, &error) == EINSTAR_STATUS_OK);
  einstar_pipeline_destroy(pipeline);
}

EINSTAR_TEST_MAIN()
