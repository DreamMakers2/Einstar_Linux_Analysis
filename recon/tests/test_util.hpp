#pragma once
#include <cmath>
#include <cstdio>
#include <string>
#include <vector>

namespace recon_test {
inline int failures = 0;
inline int checks = 0;
inline void check(bool ok, const char* expr, const char* file, int line) {
  ++checks;
  if (!ok) {
    ++failures;
    std::fprintf(stderr, "FAIL %s:%d: %s\n", file, line, expr);
  }
}
inline void check_near(double a, double b, double tol, const char* expr, const char* file,
                       int line) {
  check(std::abs(a - b) <= tol, expr, file, line);
  if (!(std::abs(a - b) <= tol))
    std::fprintf(stderr, "     a=%.9g b=%.9g tol=%.3g\n", a, b, tol);
}
}  // namespace recon_test

#define CHECK(cond) ::recon_test::check((cond), #cond, __FILE__, __LINE__)
#define CHECK_NEAR(a, b, tol) ::recon_test::check_near((a), (b), (tol), #a " ~= " #b, __FILE__, __LINE__)

void test_profile();
void test_geometry();
void test_pipeline();
void test_stereo();
