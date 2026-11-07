// SPDX-License-Identifier: MIT
// Minimal test harness (gtest is not assumed to be available).
#pragma once

#include <cstdio>
#include <cstdlib>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

namespace etest {

struct TestCase {
  const char* name;
  void (*fn)();
};

inline std::vector<TestCase>& registry() {
  static std::vector<TestCase> cases;
  return cases;
}

struct Registrar {
  Registrar(const char* name, void (*fn)()) { registry().push_back({name, fn}); }
};

inline int& failure_count() {
  static int failures = 0;
  return failures;
}

inline int& check_count() {
  static int checks = 0;
  return checks;
}

inline void report_failure(const char* file, int line, const std::string& text) {
  std::fprintf(stderr, "  FAIL %s:%d: %s\n", file, line, text.c_str());
  ++failure_count();
}

template <typename A, typename B>
std::string format_eq(const char* left, const char* right, const A& a,
                      const B& b) {
  std::ostringstream stream;
  stream << left << " == " << right << " (" << a << " vs " << b << ")";
  return stream.str();
}

inline int run_all(int argc, char** argv) {
  const std::string filter = argc > 1 ? argv[1] : std::string();
  int ran = 0;
  for (const auto& test : registry()) {
    if (!filter.empty() && std::string(test.name).find(filter) == std::string::npos) {
      continue;
    }
    const int before = failure_count();
    std::printf("[ RUN  ] %s\n", test.name);
    test.fn();
    ++ran;
    if (failure_count() == before) {
      std::printf("[  OK  ] %s\n", test.name);
    } else {
      std::printf("[ FAIL ] %s\n", test.name);
    }
  }
  std::printf("\n%d test(s) run, %d check(s), %d failure(s)\n", ran,
              check_count(), failure_count());
  return failure_count() == 0 ? 0 : 1;
}

}  // namespace etest

#define EINSTAR_TEST(name)                                        \
  static void name();                                             \
  static ::etest::Registrar etest_registrar_##name(#name, &name); \
  static void name()

#define CHECK(condition)                                                     \
  do {                                                                       \
    ++::etest::check_count();                                                \
    if (!(condition)) {                                                      \
      ::etest::report_failure(__FILE__, __LINE__, #condition);               \
    }                                                                        \
  } while (0)

#define CHECK_EQ(a, b)                                                       \
  do {                                                                       \
    ++::etest::check_count();                                                \
    const auto& etest_a = (a);                                               \
    const auto& etest_b = (b);                                               \
    if (!(etest_a == etest_b)) {                                             \
      ::etest::report_failure(__FILE__, __LINE__,                            \
                              ::etest::format_eq(#a, #b, etest_a, etest_b)); \
    }                                                                        \
  } while (0)

#define CHECK_NEAR(a, b, tolerance)                                          \
  do {                                                                       \
    ++::etest::check_count();                                                \
    const auto etest_a = (a);                                                \
    const auto etest_b = (b);                                                \
    if (!((etest_a - etest_b) <= (tolerance) &&                              \
          (etest_b - etest_a) <= (tolerance))) {                             \
      ::etest::report_failure(__FILE__, __LINE__, #a " ~= " #b);             \
    }                                                                        \
  } while (0)

#define REQUIRE(condition)                                                   \
  do {                                                                       \
    ++::etest::check_count();                                                \
    if (!(condition)) {                                                      \
      ::etest::report_failure(__FILE__, __LINE__, #condition);               \
      return;                                                                \
    }                                                                        \
  } while (0)

#define EINSTAR_TEST_MAIN() \
  int main(int argc, char** argv) { return ::etest::run_all(argc, argv); }
