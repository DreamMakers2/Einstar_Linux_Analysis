// SPDX-License-Identifier: MIT
#include <atomic>
#include <chrono>
#include <thread>
#include <vector>

#include "einstar/queue.hpp"
#include "test_util.hpp"

using namespace einstar;

EINSTAR_TEST(queue_drop_oldest) {
  BoundedQueue<int> queue(2, BackpressurePolicy::DropOldest);
  CHECK(queue.push(1) == PushOutcome::Accepted);
  CHECK(queue.push(2) == PushOutcome::Accepted);
  CHECK(queue.push(3) == PushOutcome::DroppedOldest);
  CHECK_EQ(queue.size(), 2U);
  CHECK_EQ(queue.dropped(), 1U);
  int value = 0;
  CHECK(queue.try_pop(value));
  CHECK_EQ(value, 2);
  CHECK(queue.try_pop(value));
  CHECK_EQ(value, 3);
  CHECK(!queue.try_pop(value));
}

EINSTAR_TEST(queue_drop_newest) {
  BoundedQueue<int> queue(2, BackpressurePolicy::DropNewest);
  CHECK(queue.push(1) == PushOutcome::Accepted);
  CHECK(queue.push(2) == PushOutcome::Accepted);
  CHECK(queue.push(3) == PushOutcome::DroppedNewest);
  int value = 0;
  CHECK(queue.try_pop(value));
  CHECK_EQ(value, 1);
  CHECK(queue.try_pop(value));
  CHECK_EQ(value, 2);
  CHECK_EQ(queue.dropped(), 1U);
}

EINSTAR_TEST(queue_block_policy) {
  BoundedQueue<int> queue(2, BackpressurePolicy::Block);
  std::vector<int> consumed;
  std::thread consumer([&] {
    std::this_thread::sleep_for(std::chrono::milliseconds(10));
    int value = 0;
    while (queue.pop(value, std::chrono::seconds(2))) {
      consumed.push_back(value);
      if (consumed.size() == 3) {
        break;
      }
    }
  });
  CHECK(queue.push(1) == PushOutcome::Accepted);
  CHECK(queue.push(2) == PushOutcome::Accepted);
  CHECK(queue.push(3) == PushOutcome::Accepted);  // blocks until consumer pops
  consumer.join();
  REQUIRE(consumed.size() == 3U);
  CHECK_EQ(consumed[0], 1);
  CHECK_EQ(consumed[1], 2);
  CHECK_EQ(consumed[2], 3);
}

EINSTAR_TEST(queue_close_semantics) {
  BoundedQueue<int> queue(2, BackpressurePolicy::Block);
  queue.close();
  CHECK(queue.push(1) == PushOutcome::Closed);
  CHECK(queue.closed());
  int value = 0;
  CHECK(!queue.try_pop(value));
  CHECK(!queue.pop(value, std::chrono::milliseconds(1)));
}

EINSTAR_TEST(spsc_ring_basic_and_wrap) {
  SpscRing<int> ring(4);
  CHECK_EQ(ring.capacity(), 4U);
  CHECK(ring.try_push(1));
  CHECK(ring.try_push(2));
  CHECK(ring.try_push(3));
  CHECK(ring.try_push(4));
  CHECK(!ring.try_push(5));
  CHECK_EQ(ring.size(), 4U);
  int value = 0;
  for (int expected = 1; expected <= 4; ++expected) {
    REQUIRE(ring.try_pop(value));
    CHECK_EQ(value, expected);
  }
  CHECK(!ring.try_pop(value));
  // Wrap around several times.
  for (int i = 0; i < 1000; ++i) {
    REQUIRE(ring.try_push(i));
    REQUIRE(ring.try_pop(value));
    CHECK_EQ(value, i);
  }
}

EINSTAR_TEST(spsc_ring_threads) {
  SpscRing<std::uint64_t> ring(1024);
  constexpr int kItems = 20000;
  std::atomic<bool> producer_done{false};
  std::thread producer([&] {
    for (int i = 0; i < kItems; ++i) {
      while (!ring.try_push(static_cast<std::uint64_t>(i))) {
        std::this_thread::yield();
      }
    }
    producer_done.store(true);
  });
  std::uint64_t sum = 0;
  int received = 0;
  std::uint64_t expected_sum = 0;
  for (int i = 0; i < kItems; ++i) {
    expected_sum += static_cast<std::uint64_t>(i);
  }
  while (received < kItems) {
    std::uint64_t value = 0;
    if (ring.try_pop(value)) {
      sum += value;
      received++;
    } else if (producer_done.load() && ring.size() == 0) {
      break;
    } else {
      std::this_thread::yield();
    }
  }
  producer.join();
  CHECK_EQ(received, kItems);
  CHECK_EQ(sum, expected_sum);
}

EINSTAR_TEST_MAIN()
