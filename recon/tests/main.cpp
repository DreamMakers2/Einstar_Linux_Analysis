#include "test_util.hpp"

int main() {
  test_profile();
  test_geometry();
  test_stereo();
  test_pipeline();
  if (recon_test::failures == 0)
    std::printf("PASS (%d checks)\n", recon_test::checks);
  else
    std::printf("FAILED (%d/%d checks)\n", recon_test::failures, recon_test::checks);
  return recon_test::failures == 0 ? 0 : 1;
}
