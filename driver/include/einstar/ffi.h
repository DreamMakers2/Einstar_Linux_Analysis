/* SPDX-License-Identifier: MIT
 *
 * C ABI for the Einstar acquisition backend.
 *
 * Designed as the integration boundary for:
 *   - a Rust control plane (dlopen / bindgen, opaque handle),
 *   - a Qt 6 GUI, which must go through the control plane and never touch the
 *     transport directly,
 *   - C++23/CUDA and TensorRT reconstruction engines that consume frames.
 *
 * Threading: create/start/stop/poll/read/release may be called from any one
 * thread at a time.  Callbacks are invoked on the thread that calls
 * einstar_pipeline_poll().  A callback must not re-enter the pipeline.
 *
 * Frame export is a ring of held frames.  einstar_pipeline_read_frame() returns
 * a view with an opaque token; the underlying memory stays valid until
 * einstar_pipeline_release_frame().  `data` is a linear pointer only for
 * contiguous frames (copy_mode = 0).  For zero-copy frames `data` is NULL and
 * the consumer gathers chunks with einstar_frame_copy_payload().
 */
#ifndef EINSTAR_FFI_H
#define EINSTAR_FFI_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define EINSTAR_ABI_VERSION 1u

typedef enum einstar_status_code {
  EINSTAR_STATUS_OK = 0,
  EINSTAR_STATUS_INVALID_ARGUMENT = 1,
  EINSTAR_STATUS_NOT_OPEN = 2,
  EINSTAR_STATUS_ALREADY_OPEN = 3,
  EINSTAR_STATUS_TIMEOUT = 4,
  EINSTAR_STATUS_CANCELLED = 5,
  EINSTAR_STATUS_STALL = 6,
  EINSTAR_STATUS_OVERFLOW = 7,
  EINSTAR_STATUS_TRANSPORT_ERROR = 8,
  EINSTAR_STATUS_DESCRIPTOR_QUIRK = 9,
  EINSTAR_STATUS_BACKPRESSURE = 10,
  EINSTAR_STATUS_PROTOCOL = 11,
  EINSTAR_STATUS_IO_ERROR = 12,
  EINSTAR_STATUS_CLOSED = 13,
  EINSTAR_STATUS_EXHAUSTED = 14,
  EINSTAR_STATUS_UNSUPPORTED = 15,
  EINSTAR_STATUS_INTERNAL = 16,
  EINSTAR_STATUS_NOT_FOUND = 17
} einstar_status_code;

typedef enum einstar_backend {
  EINSTAR_BACKEND_SIMULATED = 0,
  EINSTAR_BACKEND_LIBUSB = 1
} einstar_backend;

typedef enum einstar_copy_mode {
  EINSTAR_COPY_MODE_COPY = 0,
  EINSTAR_COPY_MODE_ZERO_COPY = 1
} einstar_copy_mode;

typedef enum einstar_backpressure {
  EINSTAR_BACKPRESSURE_DROP_OLDEST = 0,
  EINSTAR_BACKPRESSURE_DROP_NEWEST = 1,
  EINSTAR_BACKPRESSURE_BLOCK = 2
} einstar_backpressure;

typedef struct einstar_error {
  int32_t code;       /* einstar_status_code */
  uint32_t reserved;
  char message[256];  /* NUL-terminated diagnostic */
} einstar_error;

typedef struct einstar_config {
  uint32_t struct_size;            /* sizeof(einstar_config) */
  uint32_t abi_version;            /* EINSTAR_ABI_VERSION */
  int32_t backend;                 /* einstar_backend */
  uint32_t queue_depth;
  uint32_t transfer_buffer_size;   /* 0 = 0xA400 */
  uint32_t frame_size;             /* 0 = 1280*1024 */
  uint32_t frame_width;            /* 0 = 1280 */
  uint32_t frame_height;           /* 0 = 1024 */
  int32_t copy_mode;               /* einstar_copy_mode */
  int32_t backpressure;            /* einstar_backpressure */
  uint32_t frame_queue_capacity;   /* 0 = 16 */
  uint32_t reserved0;
  int64_t partial_frame_timeout_ns;/* 0 = 100 ms */
  uint64_t sim_seed;
  uint32_t sim_frames;             /* frames the simulated device produces */
  uint32_t sim_camera_id;          /* 0 = 1 */
  uint64_t sim_bandwidth_bps;      /* 0 = 300 MB/s */
  int64_t sim_trigger_period_ns;   /* 0 = 40 ms */
  int64_t sim_transfer_latency_ns; /* 0 = 500 us */
  uint32_t record_enabled;
  uint32_t reserved1;
  const char* record_path;         /* NULL = disabled */
  int64_t reserved2[4];
} einstar_config;

typedef struct einstar_frame_view {
  uint8_t camera_id;
  uint8_t copy_mode;               /* einstar_copy_mode */
  uint16_t reserved;
  uint32_t frame_index;
  uint32_t width;
  uint32_t height;
  uint32_t payload_size;
  uint32_t chunk_count;
  uint32_t flags;                  /* bit 0 = contiguous */
  uint64_t device_timestamp;
  int64_t host_timestamp_ns;
  uint64_t token;                  /* release with einstar_pipeline_release_frame */
  const uint8_t* data;             /* NULL when chunked */
} einstar_frame_view;

typedef struct einstar_stats {
  uint64_t transfers_submitted;
  uint64_t transfers_completed;
  uint64_t bytes_received;
  uint64_t packets_parsed;
  uint64_t frames_completed;
  uint64_t frames_dropped;
  uint64_t sequence_gaps;
  uint64_t partial_timeouts;
  uint64_t queue_drops;
  uint64_t transfer_timeouts;
  uint64_t transfer_stalls;
  uint64_t transfer_errors;
  uint64_t transfers_cancelled;
  uint64_t latency_min_ns;
  uint64_t latency_p50_ns;
  uint64_t latency_p95_ns;
  uint64_t latency_p99_ns;
  uint64_t latency_max_ns;
  double frames_per_second;
  double megabytes_per_second;
  uint64_t recorded_transfers;
} einstar_stats;

typedef struct einstar_pipeline einstar_pipeline;

typedef void (*einstar_frame_callback)(void* user, const einstar_frame_view* view);

const char* einstar_version_string(void);
const char* einstar_status_string(int32_t code);

/* Fill a config with defaults (including struct_size/abi_version). */
void einstar_config_init(einstar_config* config);

einstar_pipeline* einstar_pipeline_create(const einstar_config* config,
                                          einstar_error* error);
int32_t einstar_pipeline_start(einstar_pipeline* pipeline, einstar_error* error);
int32_t einstar_pipeline_poll(einstar_pipeline* pipeline, int32_t timeout_ms,
                              einstar_error* error);
int32_t einstar_pipeline_stop(einstar_pipeline* pipeline, einstar_error* error);
int32_t einstar_pipeline_set_callback(einstar_pipeline* pipeline,
                                      einstar_frame_callback callback,
                                      void* user, einstar_error* error);
int32_t einstar_pipeline_read_frame(einstar_pipeline* pipeline,
                                    einstar_frame_view* out,
                                    uint32_t timeout_ms, einstar_error* error);
int32_t einstar_pipeline_release_frame(einstar_pipeline* pipeline, uint64_t token,
                                       einstar_error* error);
int32_t einstar_frame_copy_payload(einstar_pipeline* pipeline,
                                   const einstar_frame_view* view, uint8_t* dst,
                                   size_t capacity, einstar_error* error);
int32_t einstar_pipeline_get_stats(einstar_pipeline* pipeline, einstar_stats* out,
                                   einstar_error* error);
void einstar_pipeline_destroy(einstar_pipeline* pipeline);

/* Last error recorded internally by the pipeline (thread-safe copy). */
int32_t einstar_pipeline_last_error(einstar_pipeline* pipeline,
                                    einstar_error* error);

#ifdef __cplusplus
}  /* extern "C" */
#endif

#endif /* EINSTAR_FFI_H */
