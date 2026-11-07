// SPDX-License-Identifier: MIT
#include "einstar/libusb_transport.hpp"

#if defined(EINSTAR_WITH_LIBUSB)

#include <algorithm>
#include <array>
#include <chrono>
#include <cstdio>
#include <chrono>
#include <cstring>
#include <thread>
#include <string>
#include <utility>

namespace einstar {
namespace {

constexpr std::uint8_t kDescriptorConfig = 0x02;
constexpr std::uint8_t kDescriptorInterface = 0x04;
constexpr std::uint8_t kDescriptorEndpoint = 0x05;

void append_u16(std::vector<std::byte>& out, std::uint16_t value) {
  out.push_back(static_cast<std::byte>(value & 0xFFU));
  out.push_back(static_cast<std::byte>((value >> 8U) & 0xFFU));
}

std::vector<std::byte> build_raw_descriptor(
    const libusb_config_descriptor& config) {
  std::vector<std::byte> raw;
  raw.reserve(config.wTotalLength == 0 ? 256 : config.wTotalLength);
  raw.push_back(static_cast<std::byte>(9));
  raw.push_back(static_cast<std::byte>(kDescriptorConfig));
  append_u16(raw, config.wTotalLength);
  raw.push_back(static_cast<std::byte>(config.bNumInterfaces));
  raw.push_back(static_cast<std::byte>(config.bConfigurationValue));
  raw.push_back(static_cast<std::byte>(config.iConfiguration));
  raw.push_back(static_cast<std::byte>(config.bmAttributes));
  raw.push_back(static_cast<std::byte>(config.MaxPower));

  for (int interface_index = 0; interface_index < config.bNumInterfaces;
       ++interface_index) {
    const libusb_interface& interface = config.interface[interface_index];
    if (interface.num_altsetting == 0) {
      continue;
    }
    const libusb_interface_descriptor& descriptor = interface.altsetting[0];
    raw.push_back(static_cast<std::byte>(9));
    raw.push_back(static_cast<std::byte>(kDescriptorInterface));
    raw.push_back(static_cast<std::byte>(descriptor.bInterfaceNumber));
    raw.push_back(static_cast<std::byte>(descriptor.bAlternateSetting));
    raw.push_back(static_cast<std::byte>(descriptor.bNumEndpoints));
    raw.push_back(static_cast<std::byte>(descriptor.bInterfaceClass));
    raw.push_back(static_cast<std::byte>(descriptor.bInterfaceSubClass));
    raw.push_back(static_cast<std::byte>(descriptor.bInterfaceProtocol));
    raw.push_back(static_cast<std::byte>(descriptor.iInterface));
    for (int endpoint_index = 0; endpoint_index < descriptor.bNumEndpoints;
         ++endpoint_index) {
      const libusb_endpoint_descriptor& endpoint =
          descriptor.endpoint[endpoint_index];
      raw.push_back(static_cast<std::byte>(7));
      raw.push_back(static_cast<std::byte>(kDescriptorEndpoint));
      raw.push_back(static_cast<std::byte>(endpoint.bEndpointAddress));
      raw.push_back(static_cast<std::byte>(endpoint.bmAttributes));
      append_u16(raw, endpoint.wMaxPacketSize);
      raw.push_back(static_cast<std::byte>(endpoint.bInterval));
    }
  }
  // wTotalLength must match the bytes we built.
  const std::uint16_t length = static_cast<std::uint16_t>(raw.size());
  raw[2] = static_cast<std::byte>(length & 0xFFU);
  raw[3] = static_cast<std::byte>((length >> 8U) & 0xFFU);
  return raw;
}

TransferStatus map_transfer_status(libusb_transfer_status status) {
  switch (status) {
    case LIBUSB_TRANSFER_COMPLETED:
      return TransferStatus::Ok;
    case LIBUSB_TRANSFER_TIMED_OUT:
      return TransferStatus::Timeout;
    case LIBUSB_TRANSFER_CANCELLED:
      return TransferStatus::Cancelled;
    case LIBUSB_TRANSFER_STALL:
      return TransferStatus::Stall;
    case LIBUSB_TRANSFER_OVERFLOW:
      return TransferStatus::Overflow;
    case LIBUSB_TRANSFER_ERROR:
    case LIBUSB_TRANSFER_NO_DEVICE:
      return TransferStatus::Error;
  }
  return TransferStatus::Error;
}

constexpr int kTransferTimeoutMs = 1000;

struct TransferContext {
  LibUsbTransport* transport = nullptr;
  CompletionSink* sink = nullptr;
  void* user = nullptr;
  TransferId id = 0;
};

}  // namespace

LibUsbTransport::LibUsbTransport() : LibUsbTransport(Config{}) {}

LibUsbTransport::LibUsbTransport(Config config) : config_(std::move(config)) {}

LibUsbTransport::~LibUsbTransport() { close(); }

Status LibUsbTransport::map_error(int libusb_error,
                                  std::string_view what) const {
  ErrorCode code = ErrorCode::TransportError;
  switch (libusb_error) {
    case LIBUSB_ERROR_TIMEOUT:
      code = ErrorCode::Timeout;
      break;
    case LIBUSB_ERROR_NO_DEVICE:
      code = ErrorCode::TransportError;
      break;
    case LIBUSB_ERROR_ACCESS:
      code = ErrorCode::TransportError;
      break;
    case LIBUSB_ERROR_NOT_SUPPORTED:
      code = quirk_notes_.empty() ? ErrorCode::Unsupported
                                  : ErrorCode::DescriptorQuirk;
      break;
    case LIBUSB_ERROR_INVALID_PARAM:
      code = quirk_notes_.empty() ? ErrorCode::InvalidArgument
                                  : ErrorCode::DescriptorQuirk;
      break;
    case LIBUSB_ERROR_NOT_FOUND:
      code = ErrorCode::NotOpen;
      break;
    case LIBUSB_ERROR_OVERFLOW:
      code = ErrorCode::Overflow;
      break;
    case LIBUSB_ERROR_PIPE:
      code = ErrorCode::Stall;
      break;
    case LIBUSB_ERROR_INTERRUPTED:
      code = ErrorCode::Cancelled;
      break;
    default:
      code = ErrorCode::TransportError;
      break;
  }
  std::string message(what);
  message += ": ";
  message += libusb_error_name(libusb_error);
  message += " (";
  message += libusb_strerror(static_cast<enum libusb_error>(libusb_error));
  message += ")";
  return Status::error(code, std::move(message));
}

Status LibUsbTransport::open() {
  if (handle_ != nullptr) {
    return Status::error(ErrorCode::AlreadyOpen, "libusb handle already open");
  }
  int rc = libusb_init(&context_);
  if (rc != 0) {
    context_ = nullptr;
    return Status::error(ErrorCode::Unsupported,
                         std::string("libusb_init failed: ") +
                             libusb_strerror(
                                 static_cast<enum libusb_error>(rc)));
  }
  if (config_.debug) {
    libusb_set_option(context_, LIBUSB_OPTION_LOG_LEVEL, LIBUSB_LOG_LEVEL_DEBUG);
  }

  libusb_device** devices = nullptr;
  const ssize_t count = libusb_get_device_list(context_, &devices);
  if (count < 0) {
    const Status status =
        map_error(static_cast<int>(count), "libusb_get_device_list");
    libusb_exit(context_);
    context_ = nullptr;
    return status;
  }

  Status last_error = Status::error(
      ErrorCode::NotFound,
      "no Einstar device found (VID 0x3267, PID " +
          (config_.product_id == 0
               ? std::string("any")
               : "0x" + [&] {
                   char buffer[8];
                   std::snprintf(buffer, sizeof(buffer), "%04X",
                                 static_cast<unsigned>(config_.product_id));
                   return std::string(buffer);
                 }()) +
          "); check the cable, udev permissions and that the scanner is "
          "powered");
  libusb_device_handle* open_handle = nullptr;

  for (ssize_t index = 0; index < count; ++index) {
    libusb_device* candidate = devices[index];
    libusb_device_descriptor descriptor{};
    if (libusb_get_device_descriptor(candidate, &descriptor) != 0) {
      continue;
    }
    if (descriptor.idVendor != config_.vendor_id) {
      continue;
    }
    if (config_.product_id != 0 && descriptor.idProduct != config_.product_id) {
      continue;
    }
    if (config_.bus >= 0 &&
        libusb_get_bus_number(candidate) != config_.bus) {
      continue;
    }
    if (config_.port >= 0 &&
        libusb_get_port_number(candidate) != config_.port) {
      continue;
    }
    rc = libusb_open(candidate, &open_handle);
    if (rc != 0) {
      last_error = map_error(rc, "libusb_open");
      open_handle = nullptr;
      continue;
    }
    device_ = candidate;
    break;
  }
  libusb_free_device_list(devices, 1);

  if (open_handle == nullptr) {
    libusb_exit(context_);
    context_ = nullptr;
    return last_error;
  }
  handle_ = open_handle;
  if (config_.auto_detach_kernel_driver) {
    (void)libusb_set_auto_detach_kernel_driver(handle_, 1);
  }

  libusb_config_descriptor* config_descriptor = nullptr;
  rc = libusb_get_active_config_descriptor(device_, &config_descriptor);
  if (rc != 0) {
    rc = libusb_get_config_descriptor(device_, 0, &config_descriptor);
  }
  if (rc != 0 || config_descriptor == nullptr) {
    const Status status = map_error(rc, "read configuration descriptor");
    libusb_close(handle_);
    handle_ = nullptr;
    device_ = nullptr;
    libusb_exit(context_);
    context_ = nullptr;
    return status;
  }
  const std::vector<std::byte> raw = build_raw_descriptor(*config_descriptor);
  libusb_free_config_descriptor(config_descriptor);

  auto parsed = UsbConfigDescriptor::parse(raw);
  if (!parsed.has_value()) {
    libusb_close(handle_);
    handle_ = nullptr;
    device_ = nullptr;
    libusb_exit(context_);
    context_ = nullptr;
    return parsed.error();
  }
  descriptor_ = std::move(*parsed);
  quirk_notes_ = descriptor_.quirk_notes();
  // Linux USB core repairs bInterval=0 automatically for high-speed interrupt
  // endpoints (drivers/usb/core/config.c::usb_parse_endpoint: fls(0*8) == 0 ->
  // n = 7, "8 ms = 2^(7-1) uframes").  libusb still reports the raw descriptor
  // value, but the kernel schedules with the repaired interval, so no
  // user-space patch, quirk override or kernel change is required.  Keep the
  // detection for diagnostics only and never refuse to open because of it.
  return Status::success();
}

Status LibUsbTransport::write_interrupt(std::uint8_t endpoint,
                                         std::span<const std::byte> data,
                                         std::uint32_t timeout_ms) {
  if (handle_ == nullptr || claimed_interface_ < 0) {
    return Status::error(ErrorCode::NotOpen, "device is not open");
  }
  if (data.empty() || timeout_ms == 0) {
    return Status::error(ErrorCode::InvalidArgument,
                         "empty interrupt write or zero timeout");
  }
  int transferred = 0;
  const int rc = libusb_interrupt_transfer(
      handle_, endpoint,
      reinterpret_cast<unsigned char*>(const_cast<std::byte*>(data.data())),  // NOLINT
      static_cast<int>(data.size()), &transferred,
      static_cast<unsigned int>(timeout_ms));
  if (rc != 0) {
    return map_error(rc, "libusb_interrupt_transfer(out)");
  }
  if (transferred != static_cast<int>(data.size())) {
    return Status::error(ErrorCode::IoError, "short interrupt write");
  }
  return Status::success();
}

Status LibUsbTransport::write_bulk(std::uint8_t endpoint,
                                   std::span<const std::byte> data,
                                   std::uint32_t timeout_ms) {
  if (handle_ == nullptr || claimed_interface_ < 0) {
    return Status::error(ErrorCode::NotOpen, "device is not open");
  }
  if (data.empty() || timeout_ms == 0) {
    return Status::error(ErrorCode::InvalidArgument,
                         "empty bulk write or zero timeout");
  }
  // The FX3 page-read request is issued as 0x400-byte transfers with a 1 ms
  // gap, matching libeinstar and the vendor stack; larger single writes are
  // accepted by the endpoint but the device firmware expects the paced form.
  constexpr std::size_t kChunk = 0x400;
  std::size_t offset = 0;
  while (offset < data.size()) {
    const std::size_t size = std::min(kChunk, data.size() - offset);
    int transferred = 0;
    const int rc = libusb_bulk_transfer(
        handle_, endpoint,
        reinterpret_cast<unsigned char*>(const_cast<std::byte*>(  // NOLINT
            data.data() + offset)),
        static_cast<int>(size), &transferred,
        static_cast<unsigned int>(timeout_ms));
    if (rc != 0) {
      return map_error(rc, "libusb_bulk_transfer(out)");
    }
    if (transferred != static_cast<int>(size)) {
      return Status::error(ErrorCode::IoError, "short bulk write");
    }
    offset += size;
    if (offset < data.size()) {
      std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
  }
  return Status::success();
}

Status LibUsbTransport::read_interrupt(std::uint8_t endpoint,
                                       std::span<std::byte> destination,
                                       std::uint32_t timeout_ms,
                                       std::size_t& transferred) {
  transferred = 0;
  if (handle_ == nullptr || claimed_interface_ < 0) {
    return Status::error(ErrorCode::NotOpen, "device is not open");
  }
  if (destination.empty() || timeout_ms == 0) {
    return Status::error(ErrorCode::InvalidArgument,
                         "empty interrupt read or zero timeout");
  }
  int received = 0;
  const int rc = libusb_interrupt_transfer(
      handle_, endpoint, reinterpret_cast<unsigned char*>(destination.data()),
      static_cast<int>(destination.size()), &received,
      static_cast<unsigned int>(timeout_ms));
  if (rc != 0) {
    return map_error(rc, "libusb_interrupt_transfer(in)");
  }
  transferred = static_cast<std::size_t>(received);
  return Status::success();
}

Status LibUsbTransport::read_bulk(std::uint8_t endpoint,
                                  std::span<std::byte> destination,
                                  std::uint32_t timeout_ms,
                                  std::size_t& transferred) {
  transferred = 0;
  if (handle_ == nullptr || claimed_interface_ < 0) {
    return Status::error(ErrorCode::NotOpen, "device is not open");
  }
  if (destination.empty() || timeout_ms == 0) {
    return Status::error(ErrorCode::InvalidArgument,
                         "empty bulk read or zero timeout");
  }
  while (transferred < destination.size()) {
    int received = 0;
    const int rc = libusb_bulk_transfer(
        handle_, endpoint,
        reinterpret_cast<unsigned char*>(destination.data() + transferred),
        static_cast<int>(destination.size() - transferred), &received,
        static_cast<unsigned int>(timeout_ms));
    if (rc != 0) {
      return map_error(rc, "libusb_bulk_transfer(in)");
    }
    if (received <= 0) {
      break;  // short/zero-length packet: the device has nothing more
    }
    transferred += static_cast<std::size_t>(received);
  }
  return Status::success();
}

Status LibUsbTransport::claim_interface(std::uint8_t interface_number) {
  if (handle_ == nullptr) {
    return Status::error(ErrorCode::NotOpen, "device is not open");
  }
  if (claimed_interface_ >= 0) {
    return Status::error(ErrorCode::AlreadyOpen, "interface already claimed");
  }
  const int rc = libusb_claim_interface(handle_, interface_number);
  if (rc != 0) {
    std::string message = "libusb_claim_interface(" +
                          std::to_string(interface_number) + ")";
    if (!quirk_notes_.empty()) {
      message +=
          " [bInterval=0 quirk detected; Linux USB core repairs it to 7 at "
          "high speed, so this is not the claim failure reason]";
    }
    return map_error(rc, message);
  }
  claimed_interface_ = static_cast<int>(interface_number);
  return Status::success();
}

Status LibUsbTransport::release_interface(std::uint8_t interface_number) {
  if (handle_ == nullptr) {
    return Status::success();
  }
  if (claimed_interface_ < 0) {
    return Status::success();
  }
  const int rc = libusb_release_interface(handle_, interface_number);
  claimed_interface_ = -1;
  return rc == 0 ? Status::success() : map_error(rc, "libusb_release_interface");
}

Status LibUsbTransport::clear_halt(std::uint8_t endpoint) {
  if (handle_ == nullptr) {
    return Status::error(ErrorCode::NotOpen, "device is not open");
  }
  const int rc = libusb_clear_halt(handle_, endpoint);
  if (rc == 0 || rc == LIBUSB_ERROR_NOT_FOUND) {
    return Status::success();
  }
  return map_error(rc, "libusb_clear_halt");
}

TransferId LibUsbTransport::submit_bulk_in(std::uint8_t endpoint,
                                           std::span<std::byte> destination,
                                           CompletionSink& sink, void* user) {
  if (handle_ == nullptr || claimed_interface_ < 0) {
    return 0;
  }
  libusb_transfer* transfer = libusb_alloc_transfer(0);
  if (transfer == nullptr) {
    return 0;
  }
  auto* context = new (std::nothrow) TransferContext();
  if (context == nullptr) {
    libusb_free_transfer(transfer);
    return 0;
  }
  const TransferId id = next_id_.fetch_add(1, std::memory_order_relaxed);
  context->transport = this;
  context->sink = &sink;
  context->user = user;
  context->id = id;
  libusb_fill_bulk_transfer(
      transfer, handle_, endpoint,
      reinterpret_cast<unsigned char*>(destination.data()),  // NOLINT
      static_cast<int>(destination.size()), &LibUsbTransport::transfer_callback,
      context, kTransferTimeoutMs);
  transfer->user_data = context;

  {
    std::lock_guard lock(mutex_);
    transfers_.emplace(id, transfer);
  }
  outstanding_.fetch_add(1, std::memory_order_acq_rel);
  const int rc = libusb_submit_transfer(transfer);
  if (rc != 0) {
    {
      std::lock_guard lock(mutex_);
      transfers_.erase(id);
    }
    outstanding_.fetch_sub(1, std::memory_order_acq_rel);
    libusb_free_transfer(transfer);
    delete context;
    return 0;
  }
  return id;
}

void LIBUSB_CALL LibUsbTransport::transfer_callback(libusb_transfer* transfer) {
  auto* context = static_cast<TransferContext*>(transfer->user_data);
  if (context == nullptr) {
    libusb_free_transfer(transfer);
    return;
  }
  LibUsbTransport* self = context->transport;
  TransferCompletion completion;
  completion.id = context->id;
  completion.status = map_transfer_status(transfer->status);
  completion.bytes_transferred =
      transfer->actual_length < 0
          ? 0
          : static_cast<std::size_t>(transfer->actual_length);
  completion.host_time_ns = self->clock_.now_ns();
  completion.user = context->user;

  {
    std::lock_guard lock(self->mutex_);
    self->transfers_.erase(context->id);
    self->completed_.push_back(QueuedCompletion{completion, context->sink});
  }
  self->outstanding_.fetch_sub(1, std::memory_order_acq_rel);
  libusb_free_transfer(transfer);
  delete context;
}

bool LibUsbTransport::cancel(TransferId id) {
  std::lock_guard lock(mutex_);
  const auto it = transfers_.find(id);
  if (it == transfers_.end()) {
    return false;
  }
  return libusb_cancel_transfer(it->second) == 0;
}

std::size_t LibUsbTransport::deliver_queued() {
  std::deque<QueuedCompletion> local;
  {
    std::lock_guard lock(mutex_);
    local.swap(completed_);
  }
  for (auto& entry : local) {
    if (entry.sink != nullptr) {
      entry.sink->on_transfer_complete(entry.completion);
    }
  }
  return local.size();
}

std::size_t LibUsbTransport::poll(int timeout_ms) {
  if (context_ == nullptr) {
    return 0;
  }
  timeval timeout{};
  if (timeout_ms < 0) {
    timeout.tv_sec = 0;
    timeout.tv_usec = 200'000;  // 200 ms safety wake-up, never blocks forever
  } else {
    timeout.tv_sec = timeout_ms / 1000;
    timeout.tv_usec = static_cast<suseconds_t>((timeout_ms % 1000) * 1000);
  }
  (void)libusb_handle_events_timeout_completed(context_, &timeout, nullptr);
  return deliver_queued();
}

bool LibUsbTransport::cancel_all_and_wait() {
  if (context_ == nullptr) {
    return true;
  }
  std::vector<TransferId> ids;
  {
    std::lock_guard lock(mutex_);
    ids.reserve(transfers_.size());
    for (const auto& [id, transfer] : transfers_) {
      (void)transfer;
      ids.push_back(id);
    }
  }
  for (const TransferId id : ids) {
    std::lock_guard lock(mutex_);
    const auto it = transfers_.find(id);
    if (it != transfers_.end()) {
      (void)libusb_cancel_transfer(it->second);
    }
  }
  // libusb owns a submitted transfer until its completion callback runs.  We
  // must therefore drain every cancel through the event loop before closing
  // the handle or freeing anything; closing with a transfer in flight would be
  // a use-after-free inside libusb.
  constexpr int kMaxSpins = 500;  // 500 * 10 ms = 5 s worst case
  int spins = 0;
  while (outstanding_.load(std::memory_order_acquire) > 0 && spins < kMaxSpins) {
    timeval timeout{};
    timeout.tv_sec = 0;
    timeout.tv_usec = 10'000;
    (void)libusb_handle_events_timeout_completed(context_, &timeout, nullptr);
    spins++;
  }
  deliver_queued();
  return outstanding_.load(std::memory_order_acquire) == 0;
}

void LibUsbTransport::close() {
  if (context_ == nullptr) {
    handle_ = nullptr;
    device_ = nullptr;
    return;
  }
  if (handle_ != nullptr) {
    const bool drained = cancel_all_and_wait();
    if (!drained) {
      // Refuse to close a context that libusb still owns transfers for.
      // Leaking the handle/context is the only memory-safe option; the process
      // is expected to exit, and shutdown_incomplete() reports it.
      shutdown_incomplete_ = true;
      handle_ = nullptr;
      device_ = nullptr;
      context_ = nullptr;
      claimed_interface_ = -1;
      return;
    }
    if (claimed_interface_ >= 0) {
      (void)libusb_release_interface(handle_,
                                     static_cast<int>(claimed_interface_));
      claimed_interface_ = -1;
    }
    libusb_close(handle_);
    handle_ = nullptr;
  }
  device_ = nullptr;
  libusb_exit(context_);
  context_ = nullptr;
  {
    std::lock_guard lock(mutex_);
    transfers_.clear();
    completed_.clear();
  }
}

}  // namespace einstar

#endif  // EINSTAR_WITH_LIBUSB
