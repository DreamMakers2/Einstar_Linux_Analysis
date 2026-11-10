// OpenCL/CPU micro-benchmark for the reconstruction stages that OpenCV can
// dispatch to the GPU.  The SGBM matcher itself has no OpenCL kernel in this
// OpenCV build; the benchmark measures that explicitly (UMat input is copied
// back to the CPU) so the result is not mistaken for a GPU matcher benchmark.
#include <algorithm>
#include <chrono>
#include <cstdio>
#include <string>
#include <vector>

#include <opencv2/calib3d.hpp>
#include <opencv2/core.hpp>
#include <opencv2/core/ocl.hpp>
#include <opencv2/imgcodecs.hpp>
#include <opencv2/imgproc.hpp>

namespace {
using Clock = std::chrono::steady_clock;
double ms(Clock::time_point a, Clock::time_point b) {
  return std::chrono::duration<double, std::milli>(b - a).count();
}
template <typename F>
double bench(F&& f, int reps = 10) {
  f();
  cv::ocl::finish();
  const auto t0 = Clock::now();
  for (int i = 0; i < reps; ++i) f();
  cv::ocl::finish();
  return ms(t0, Clock::now()) / reps;
}
}  // namespace

int main(int argc, char** argv) {
  const std::string dir = argc > 1 ? argv[1] : "/tmp/einstar_recon/id0";
  cv::Mat left = cv::imread(dir + "/recon_rectified_left.png", cv::IMREAD_GRAYSCALE);
  cv::Mat right = cv::imread(dir + "/recon_rectified_right.png", cv::IMREAD_GRAYSCALE);
  if (left.empty() || right.empty()) {
    std::fprintf(stderr, "cannot read rectified pair in %s\n", dir.c_str());
    return 1;
  }
  cv::Mat small_l, small_r;
  cv::resize(left, small_l, {}, 0.5, 0.5, cv::INTER_AREA);
  cv::resize(right, small_r, {}, 0.5, 0.5, cv::INTER_AREA);

  std::printf("OpenCV %s, OpenCL available=%d, enabled=%d\n", CV_VERSION,
              cv::ocl::haveOpenCL(), cv::ocl::useOpenCL());
  if (cv::ocl::haveOpenCL()) {
    cv::ocl::Device d = cv::ocl::Device::getDefault();
    std::printf("OpenCL device: %s (%s)\n", d.name().c_str(), d.vendorName().c_str());
  }

  cv::Mat mx, my;
  cv::Mat eye = cv::Mat::eye(3, 3, CV_64F);
  cv::Mat K = (cv::Mat_<double>(3, 3) << 579.18, 0, 318.2, 0, 579.18, 257.6, 0, 0, 1);
  cv::initUndistortRectifyMap(K, cv::Mat(), eye, K, left.size(), CV_32FC1, mx, my);

  cv::ocl::setUseOpenCL(false);
  const double cpu_remap = bench([&] { cv::Mat o; cv::remap(left, o, mx, my, cv::INTER_LINEAR); });
  const double cpu_resize = bench([&] { cv::Mat o; cv::resize(left, o, {}, 0.5, 0.5, cv::INTER_AREA); });
  const double cpu_sobel = bench([&] {
    cv::Mat gx, gy; cv::Sobel(small_l, gx, CV_32F, 1, 0, 3); cv::Sobel(small_l, gy, CV_32F, 0, 1, 3);
  });
  cv::Ptr<cv::StereoSGBM> sgbm = cv::StereoSGBM::create(
      0, 512, 5, 8 * 25, 96 * 25, -1, 63, 10, 100, 2, cv::StereoSGBM::MODE_SGBM_3WAY);
  cv::Mat d;
  const double cpu_sgbm = bench([&] { sgbm->compute(small_l, small_r, d); }, 5);

  cv::ocl::setUseOpenCL(true);
  cv::UMat ul, ur, umx, umy, uo;
  left.copyTo(ul); right.copyTo(ur); mx.copyTo(umx); my.copyTo(umy);
  const double ocl_remap = bench([&] { cv::remap(ul, uo, umx, umy, cv::INTER_LINEAR); });
  const double ocl_resize = bench([&] { cv::UMat o; cv::resize(ul, o, {}, 0.5, 0.5, cv::INTER_AREA); });
  const double ocl_sobel = bench([&] {
    cv::UMat gx, gy; cv::Sobel(ul, gx, CV_32F, 1, 0, 3); cv::Sobel(ul, gy, CV_32F, 0, 1, 3);
  });
  cv::UMat usl, usr, ud;
  small_l.copyTo(usl); small_r.copyTo(usr);
  const double ocl_sgbm_umat = bench([&] { sgbm->compute(usl, usr, ud); }, 3);
  ud.copyTo(d);
  const double download = bench([&] { cv::Mat o; ul.copyTo(o); }, 10);

  std::printf("%-28s %10s %10s\n", "stage", "CPU ms", "OCL ms");
  std::printf("%-28s %10.3f %10.3f\n", "remap 1280x1024", cpu_remap, ocl_remap);
  std::printf("%-28s %10.3f %10.3f\n", "resize 0.5 (INTER_AREA)", cpu_resize, ocl_resize);
  std::printf("%-28s %10.3f %10.3f\n", "Sobel 3x3 (half res)", cpu_sobel, ocl_sobel);
  std::printf("%-28s %10.3f %10.3f\n", "SGBM 640x512 (UMat input)", cpu_sgbm, ocl_sgbm_umat);
  std::printf("UMat upload (1280x1024) %.3f ms; download %.3f ms\n",
              bench([&] { cv::UMat o; left.copyTo(o); }, 10), download);
  std::printf("SGBM output: %dx%d (the UMat call includes CPU matching + copies)\n",
              d.cols, d.rows);
  return 0;
}
