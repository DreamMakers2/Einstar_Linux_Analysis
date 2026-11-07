// SPDX-License-Identifier: MIT
#include "einstar/queue.hpp"

#include <string_view>

namespace einstar {

std::string_view to_string(BackpressurePolicy policy) noexcept {
  switch (policy) {
    case BackpressurePolicy::DropOldest:
      return "drop-oldest";
    case BackpressurePolicy::DropNewest:
      return "drop-newest";
    case BackpressurePolicy::Block:
      return "block";
  }
  return "unknown";
}

}  // namespace einstar
