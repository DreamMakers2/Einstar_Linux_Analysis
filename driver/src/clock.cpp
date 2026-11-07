// SPDX-License-Identifier: MIT
#include "einstar/clock.hpp"

namespace einstar {

std::int64_t SteadyClock::now_ns() const noexcept {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
             std::chrono::steady_clock::now().time_since_epoch())
      .count();
}

}  // namespace einstar
