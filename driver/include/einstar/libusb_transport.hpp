// SPDX-License-Identifier: MIT
// libusb-1.0 backend for the Einstar (3267:0003).  Built only when
// EINSTAR_WITH_LIBUSB is defined and libusb-1.0 is available.
#pragma once

#if defined(EINSTAR_WITH_LIBUSB)

#include <libusb.h>

#include <atomic>
#include <cstdint>
#include <deque>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>

#include "einstar/clock.hpp"
#include "einstar/transport.hpp"
#include "einstar/usb_descriptor.hpp"

namespace einstar {

class LibUsbTransport final : public Transport {
 public:
  struct Config {
    std::uint16_t vendor_id = 0x3267;
    /// 0 selects any product id under the vendor id (libeinstar behaviour).
    std::uint16_t product_id = 0x0003;
    int bus = -1;   // -1 = any
    int port = -1;  // -1 = any
    bool auto_detach_kernel_driver = true;
    bool patch_interval = true;
    std::uint8_t patched_interval = 7;
    /// Refuse to claim the interface when bInterval == 0 remains in the
    /// descriptor (no userspace-visible override exists in libusb-1.0).
    bool require_patched_interval = false;
    bool debug = false;

    Config() = default;
  };

  LibUsbTransport();
  explicit LibUsbTransport(Config config);
  ~LibUsbTransport() override;

  Status open() override;
  void close() override;
  [[nodiscard]] bool is_open() const noexcept override { return handle_ != nullptr; }

  Status claim_interface(std::uint8_t interface_number) override;
  Status release_interface(std::uint8_t interface_number) override;
  Status clear_halt(std::uint8_t endpoint) override;

  TransferId submit_bulk_in(std::uint8_t endpoint,
                            std::span<std::byte> destination,
                            CompletionSink& sink, void* user) override;
  bool cancel(TransferId id) override;
  std::size_t poll(int timeout_ms) override;

  [[nodiscard]] std::size_t outstanding() const noexcept override {
    return outstanding_.load(std::memory_order_acquire);
  }
  [[nodiscard]] const Clock& clock() const noexcept override { return clock_; }
  [[nodiscard]] TransportKind kind() const noexcept override {
    return TransportKind::LibUsb;
  }
  [[nodiscard]] std::string_view name() const noexcept override {
    return "libusb";
  }

  // ---- diagnostics ---------------------------------------------------------
  [[nodiscard]] const UsbConfigDescriptor& descriptor() const noexcept {
    return descriptor_;
  }
  [[nodiscard]] const std::vector<std::string>& quirk_notes() const noexcept {
    return quirk_notes_;
  }
  [[nodiscard]] const Config& config() const noexcept { return config_; }
  [[nodiscard]] int interface_number() const noexcept { return claimed_interface_; }

 private:
  static void LIBUSB_CALL transfer_callback(libusb_transfer* transfer);
  Status map_error(int libusb_error, std::string_view what) const;
  std::size_t deliver_queued();
  void cancel_all_and_wait();

  struct QueuedCompletion {
    TransferCompletion completion;
    CompletionSink* sink = nullptr;
  };

  Config config_;
  SteadyClock clock_;
  libusb_context* context_ = nullptr;
  libusb_device_handle* handle_ = nullptr;
  libusb_device* device_ = nullptr;
  UsbConfigDescriptor descriptor_;
  std::vector<std::string> quirk_notes_;
  int claimed_interface_ = -1;

  std::atomic<std::size_t> outstanding_{0};
  std::atomic<TransferId> next_id_{1};

  mutable std::mutex mutex_;
  std::unordered_map<TransferId, libusb_transfer*> transfers_;
  std::deque<QueuedCompletion> completed_;
};

}  // namespace einstar

#endif  // EINSTAR_WITH_LIBUSB
