// SPDX-License-Identifier: MIT
#include "einstar/ffi.h"

#include <algorithm>
#include <cstring>
#include <map>
#include <memory>
#include <mutex>
#include <new>
#include <string>

#include "einstar/pipeline.hpp"
#include "einstar/sim.hpp"

#if defined(EINSTAR_WITH_LIBUSB)
#include "einstar/libusb_transport.hpp"
#endif

namespace {

using einstar::AcquisitionPipeline;
using einstar::CopyMode;
using einstar::ErrorCode;
using einstar::Frame;
using einstar::PipelineConfig;
using einstar::Status;
using einstar::Transport;

constexpr const char* kVersion = "einstar-driver 0.1.0 (ABI 1)";

void set_error(einstar_error* error, std::int32_t code,
               const std::string& message) {
  if (error == nullptr) {
    return;
  }
  error->code = code;
  error->reserved = 0;
  std::memset(error->message, 0, sizeof(error->message));
  const std::size_t count =
      std::min<std::size_t>(message.size(), sizeof(error->message) - 1);
  std::memcpy(error->message, message.data(), count);
}

void set_error(einstar_error* error, const Status& status) {
  set_error(error, static_cast<std::int32_t>(status.code()), status.message());
}

void clear_error(einstar_error* error) {
  if (error != nullptr) {
    error->code = 0;
    error->reserved = 0;
    error->message[0] = '\0';
  }
}

std::int32_t to_c(ErrorCode code) {
  return static_cast<std::int32_t>(code);
}

int32_t fail_with(einstar_error* error, ErrorCode code,
                  const std::string& message) {
  set_error(error, static_cast<std::int32_t>(code), message);
  return static_cast<std::int32_t>(code);
}

}  // namespace

struct einstar_pipeline {
  std::unique_ptr<AcquisitionPipeline> pipeline;
  std::mutex mutex;  // protects held frames and callback fields
  std::map<std::uint64_t, Frame> held;
  std::uint64_t next_token = 1;
  std::size_t max_held = 16;
  einstar_frame_callback callback = nullptr;
  void* callback_user = nullptr;
};

namespace {

void fill_view(const Frame& frame, std::uint64_t token,
               einstar_frame_view* out) {
  std::memset(out, 0, sizeof(*out));
  out->camera_id = frame.camera_id;
  out->copy_mode = frame.mode == CopyMode::ZeroCopy ? 1 : 0;
  out->frame_index = frame.frame_index;
  out->width = frame.width;
  out->height = frame.height;
  out->payload_size = frame.payload_size;
  out->chunk_count = static_cast<std::uint32_t>(frame.chunks.size());
  out->flags = frame.is_contiguous() ? 1U : 0U;
  out->device_timestamp = frame.device_timestamp;
  out->host_timestamp_ns = frame.host_timestamp_ns;
  out->token = token;
  const auto payload = frame.payload();
  out->data = payload.empty()
                  ? nullptr
                  : reinterpret_cast<const std::uint8_t*>(payload.data());
}

}  // namespace

extern "C" {

const char* einstar_version_string(void) { return kVersion; }

const char* einstar_status_string(std::int32_t code) {
  if (code < static_cast<std::int32_t>(ErrorCode::Ok) ||
      code > static_cast<std::int32_t>(ErrorCode::Internal)) {
    return "unknown";
  }
  const auto view = einstar::to_string(static_cast<ErrorCode>(code));
  return view.data();
}

void einstar_config_init(einstar_config* config) {
  if (config == nullptr) {
    return;
  }
  std::memset(config, 0, sizeof(*config));
  config->struct_size = sizeof(einstar_config);
  config->abi_version = EINSTAR_ABI_VERSION;
  config->backend = EINSTAR_BACKEND_SIMULATED;
  config->queue_depth = 8;
  config->transfer_buffer_size = 0xA400;
  config->frame_size = 1280U * 1024U;
  config->frame_width = 1280;
  config->frame_height = 1024;
  config->copy_mode = EINSTAR_COPY_MODE_COPY;
  config->backpressure = EINSTAR_BACKPRESSURE_DROP_OLDEST;
  config->frame_queue_capacity = 16;
  config->partial_frame_timeout_ns = 100'000'000;
  config->sim_seed = 1;
  config->sim_frames = 1000;
  config->sim_camera_id = 1;
  config->sim_bandwidth_bps = 300'000'000;
  config->sim_trigger_period_ns = 40'000'000;
  config->sim_transfer_latency_ns = 500'000;
}

einstar_pipeline* einstar_pipeline_create(const einstar_config* config,
                                          einstar_error* error) {
  clear_error(error);
  if (config == nullptr) {
    (void)fail_with(error, ErrorCode::InvalidArgument, "null config");
    return nullptr;
  }
  if (config->struct_size < 8 || config->abi_version != EINSTAR_ABI_VERSION) {
    (void)fail_with(error, ErrorCode::InvalidArgument,
                    "unsupported ABI version or truncated config");
    return nullptr;
  }
  einstar_config copy{};
  std::memcpy(&copy, config,
              std::min<std::size_t>(config->struct_size, sizeof(copy)));

  PipelineConfig pipeline_config;
  pipeline_config.queue_depth = copy.queue_depth == 0 ? 8 : copy.queue_depth;
  pipeline_config.transfer_buffer_size =
      copy.transfer_buffer_size == 0 ? 0xA400 : copy.transfer_buffer_size;
  pipeline_config.frame_size =
      copy.frame_size == 0 ? 1280U * 1024U : copy.frame_size;
  pipeline_config.frame_width = copy.frame_width == 0 ? 1280 : copy.frame_width;
  pipeline_config.frame_height =
      copy.frame_height == 0 ? 1024 : copy.frame_height;
  pipeline_config.copy_mode =
      copy.copy_mode == EINSTAR_COPY_MODE_ZERO_COPY ? CopyMode::ZeroCopy
                                                    : CopyMode::Copy;
  switch (copy.backpressure) {
    case EINSTAR_BACKPRESSURE_DROP_NEWEST:
      pipeline_config.frame_backpressure =
          einstar::BackpressurePolicy::DropNewest;
      break;
    case EINSTAR_BACKPRESSURE_BLOCK:
      pipeline_config.frame_backpressure = einstar::BackpressurePolicy::Block;
      break;
    default:
      pipeline_config.frame_backpressure =
          einstar::BackpressurePolicy::DropOldest;
      break;
  }
  pipeline_config.frame_queue_capacity =
      copy.frame_queue_capacity == 0 ? 16 : copy.frame_queue_capacity;
  pipeline_config.partial_frame_timeout_ns =
      copy.partial_frame_timeout_ns > 0 ? copy.partial_frame_timeout_ns
                                        : 100'000'000;
  pipeline_config.enable_recording = copy.record_enabled != 0;
  if (copy.record_path != nullptr) {
    pipeline_config.recording_path = copy.record_path;
  }
  pipeline_config.recording_metadata = "ffi";

  std::unique_ptr<Transport> transport;
  if (copy.backend == EINSTAR_BACKEND_SIMULATED) {
    einstar::SimConfig sim;
    sim.seed = copy.sim_seed == 0 ? 1 : copy.sim_seed;
    sim.frames = copy.sim_frames == 0 ? 1000 : copy.sim_frames;
    sim.camera_id = static_cast<std::uint8_t>(
        copy.sim_camera_id == 0 ? 1 : copy.sim_camera_id);
    sim.frame_size = pipeline_config.frame_size;
    sim.bandwidth_bytes_per_sec =
        copy.sim_bandwidth_bps == 0 ? 300'000'000 : copy.sim_bandwidth_bps;
    sim.trigger_period_ns =
        copy.sim_trigger_period_ns == 0 ? 40'000'000
                                        : copy.sim_trigger_period_ns;
    sim.transfer_latency_ns =
        copy.sim_transfer_latency_ns == 0 ? 500'000
                                          : copy.sim_transfer_latency_ns;
    sim.require_interval_patch = false;
    transport = std::make_unique<einstar::SimulatedTransport>(sim);
  } else if (copy.backend == EINSTAR_BACKEND_LIBUSB) {
#if defined(EINSTAR_WITH_LIBUSB)
    transport = std::make_unique<einstar::LibUsbTransport>();
#else
    (void)fail_with(error, ErrorCode::Unsupported,
                    "this build has no libusb backend");
    return nullptr;
#endif
  } else {
    (void)fail_with(error, ErrorCode::InvalidArgument, "unknown backend");
    return nullptr;
  }

  try {
    auto* handle = new einstar_pipeline();
    handle->max_held = std::max<std::size_t>(
        1, pipeline_config.frame_queue_capacity);
    handle->pipeline = std::make_unique<AcquisitionPipeline>(
        std::move(transport), pipeline_config);
    return handle;
  } catch (const std::exception& exception) {
    (void)fail_with(error, ErrorCode::Internal,
                    std::string("pipeline creation failed: ") +
                        exception.what());
    return nullptr;
  } catch (...) {
    (void)fail_with(error, ErrorCode::Internal, "pipeline creation failed");
    return nullptr;
  }
}

int32_t einstar_pipeline_start(einstar_pipeline* pipeline,
                               einstar_error* error) {
  clear_error(error);
  if (pipeline == nullptr || pipeline->pipeline == nullptr) {
    return fail_with(error, ErrorCode::InvalidArgument, "null pipeline");
  }
  const Status status = pipeline->pipeline->start();
  if (!status.ok()) {
    set_error(error, status);
    return to_c(status.code());
  }
  return EINSTAR_STATUS_OK;
}

int32_t einstar_pipeline_poll(einstar_pipeline* pipeline, std::int32_t timeout_ms,
                              einstar_error* error) {
  clear_error(error);
  if (pipeline == nullptr || pipeline->pipeline == nullptr) {
    return fail_with(error, ErrorCode::InvalidArgument, "null pipeline");
  }
  (void)pipeline->pipeline->poll(timeout_ms);
  return EINSTAR_STATUS_OK;
}

int32_t einstar_pipeline_stop(einstar_pipeline* pipeline, einstar_error* error) {
  clear_error(error);
  if (pipeline == nullptr || pipeline->pipeline == nullptr) {
    return fail_with(error, ErrorCode::InvalidArgument, "null pipeline");
  }
  const Status status = pipeline->pipeline->stop();
  if (!status.ok()) {
    set_error(error, status);
    return to_c(status.code());
  }
  return EINSTAR_STATUS_OK;
}

int32_t einstar_pipeline_set_callback(einstar_pipeline* pipeline,
                                      einstar_frame_callback callback,
                                      void* user, einstar_error* error) {
  clear_error(error);
  if (pipeline == nullptr || pipeline->pipeline == nullptr) {
    return fail_with(error, ErrorCode::InvalidArgument, "null pipeline");
  }
  {
    std::lock_guard lock(pipeline->mutex);
    pipeline->callback = callback;
    pipeline->callback_user = user;
  }
  if (callback == nullptr) {
    pipeline->pipeline->set_frame_callback(nullptr);
    return EINSTAR_STATUS_OK;
  }
  einstar_pipeline* handle = pipeline;
  pipeline->pipeline->set_frame_callback([handle](const Frame& frame) {
    einstar_frame_callback active = nullptr;
    void* callback_user = nullptr;
    {
      std::lock_guard lock(handle->mutex);
      active = handle->callback;
      callback_user = handle->callback_user;
    }
    if (active != nullptr) {
      einstar_frame_view view;
      fill_view(frame, 0, &view);
      active(callback_user, &view);
    }
  });
  return EINSTAR_STATUS_OK;
}

int32_t einstar_pipeline_read_frame(einstar_pipeline* pipeline,
                                    einstar_frame_view* out,
                                    std::uint32_t timeout_ms,
                                    einstar_error* error) {
  clear_error(error);
  if (pipeline == nullptr || pipeline->pipeline == nullptr || out == nullptr) {
    return fail_with(error, ErrorCode::InvalidArgument, "null argument");
  }
  {
    std::lock_guard lock(pipeline->mutex);
    if (pipeline->callback != nullptr) {
      return fail_with(error, ErrorCode::InvalidArgument,
                       "pipeline is in callback mode");
    }
  }
  const std::int64_t deadline =
      pipeline->pipeline->transport().clock().now_ns() +
      static_cast<std::int64_t>(timeout_ms) * 1'000'000;
  while (true) {
    Frame frame;
    if (pipeline->pipeline->try_pop_frame(frame)) {
      std::lock_guard lock(pipeline->mutex);
      if (pipeline->held.size() >= pipeline->max_held) {
        return fail_with(error, ErrorCode::Backpressure,
                         "frame ring is full; release frames first");
      }
      const std::uint64_t token = pipeline->next_token++;
      auto [it, inserted] = pipeline->held.emplace(token, std::move(frame));
      (void)inserted;
      fill_view(it->second, token, out);
      return EINSTAR_STATUS_OK;
    }
    if (pipeline->pipeline->transport().clock().now_ns() >= deadline) {
      return fail_with(error, ErrorCode::Timeout, "no frame available");
    }
    (void)pipeline->pipeline->poll(1);
  }
}

int32_t einstar_pipeline_release_frame(einstar_pipeline* pipeline,
                                       std::uint64_t token,
                                       einstar_error* error) {
  clear_error(error);
  if (pipeline == nullptr) {
    return fail_with(error, ErrorCode::InvalidArgument, "null pipeline");
  }
  std::lock_guard lock(pipeline->mutex);
  const auto it = pipeline->held.find(token);
  if (it == pipeline->held.end()) {
    return fail_with(error, ErrorCode::InvalidArgument, "unknown frame token");
  }
  pipeline->held.erase(it);
  return EINSTAR_STATUS_OK;
}

int32_t einstar_frame_copy_payload(einstar_pipeline* pipeline,
                                   const einstar_frame_view* view,
                                   std::uint8_t* dst, std::size_t capacity,
                                   einstar_error* error) {
  clear_error(error);
  if (pipeline == nullptr || view == nullptr) {
    return fail_with(error, ErrorCode::InvalidArgument, "null argument");
  }
  if (dst == nullptr && view->payload_size != 0) {
    return fail_with(error, ErrorCode::InvalidArgument, "null destination");
  }
  if (capacity < view->payload_size) {
    return fail_with(error, ErrorCode::InvalidArgument,
                     "destination smaller than payload");
  }
  if ((view->flags & 1U) != 0U) {
    if (view->data == nullptr && view->payload_size > 0) {
      return fail_with(error, ErrorCode::Internal, "contiguous frame has no data");
    }
    if (view->payload_size > 0) {
      std::memcpy(dst, view->data, view->payload_size);
    }
    return EINSTAR_STATUS_OK;
  }
  std::lock_guard lock(pipeline->mutex);
  const auto it = pipeline->held.find(view->token);
  if (it == pipeline->held.end()) {
    return fail_with(error, ErrorCode::InvalidArgument, "unknown frame token");
  }
  const std::span<std::byte> destination(
      reinterpret_cast<std::byte*>(dst), view->payload_size);
  if (it->second.copy_payload_to(destination) != view->payload_size) {
    return fail_with(error, ErrorCode::Internal, "failed to gather frame");
  }
  return EINSTAR_STATUS_OK;
}

int32_t einstar_pipeline_get_stats(einstar_pipeline* pipeline,
                                   einstar_stats* out, einstar_error* error) {
  clear_error(error);
  if (pipeline == nullptr || pipeline->pipeline == nullptr || out == nullptr) {
    return fail_with(error, ErrorCode::InvalidArgument, "null argument");
  }
  const auto stats = pipeline->pipeline->stats();
  std::memset(out, 0, sizeof(*out));
  out->transfers_submitted = stats.transfers_submitted;
  out->transfers_completed = stats.transfers_completed;
  out->bytes_received = stats.bytes_received;
  out->packets_parsed = stats.packets_parsed;
  out->frames_completed = stats.frames_completed;
  out->frames_dropped = stats.frames_dropped;
  out->sequence_gaps = stats.sequence_gaps;
  out->partial_timeouts = stats.partial_timeouts;
  out->queue_drops = stats.queue_drops;
  out->transfer_timeouts = stats.transfer_timeouts;
  out->transfer_stalls = stats.transfer_stalls;
  out->transfer_errors = stats.transfer_errors;
  out->transfers_cancelled = stats.transfers_cancelled;
  out->latency_min_ns = static_cast<std::uint64_t>(stats.transfer_latency.min_ns);
  out->latency_p50_ns = static_cast<std::uint64_t>(stats.transfer_latency.p50_ns);
  out->latency_p95_ns = static_cast<std::uint64_t>(stats.transfer_latency.p95_ns);
  out->latency_p99_ns = static_cast<std::uint64_t>(stats.transfer_latency.p99_ns);
  out->latency_max_ns = static_cast<std::uint64_t>(stats.transfer_latency.max_ns);
  out->recorded_transfers = stats.recorded_transfers;
  const std::int64_t start = pipeline->pipeline->start_time_ns();
  std::int64_t end = pipeline->pipeline->last_frame_time_ns();
  if (end <= start) {
    end = pipeline->pipeline->transport().clock().now_ns();
  }
  const auto metrics = einstar::compute_metrics(
      stats, start, end, pipeline->pipeline->config().frame_size);
  out->frames_per_second = metrics.fps;
  out->megabytes_per_second = metrics.megabytes_per_second;
  return EINSTAR_STATUS_OK;
}

int32_t einstar_pipeline_last_error(einstar_pipeline* pipeline,
                                    einstar_error* error) {
  clear_error(error);
  if (pipeline == nullptr || pipeline->pipeline == nullptr) {
    return fail_with(error, ErrorCode::InvalidArgument, "null pipeline");
  }
  const Status status = pipeline->pipeline->last_error();
  if (!status.ok()) {
    set_error(error, status);
    return to_c(status.code());
  }
  return EINSTAR_STATUS_OK;
}

void einstar_pipeline_destroy(einstar_pipeline* pipeline) {
  if (pipeline == nullptr) {
    return;
  }
  {
    std::lock_guard lock(pipeline->mutex);
    pipeline->held.clear();
  }
  if (pipeline->pipeline != nullptr) {
    (void)pipeline->pipeline->stop();
    pipeline->pipeline.reset();
  }
  delete pipeline;
}

}  // extern "C"
