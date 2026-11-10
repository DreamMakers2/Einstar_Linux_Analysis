// CUDA SGM vs CPU SGBM on the same rectified Einstar IR pair.
//
// This tool is not part of the normal build: it needs an OpenCV built with
// CUDA and the contrib cudastereo module (OpenCV >= 5 moved CUDA there).  Build
// instructions are in docs/reconstruction/gpu_benchmark.md.
#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include <opencv2/stereo.hpp>
#include <opencv2/core.hpp>
#include <opencv2/cudastereo.hpp>
#include <opencv2/imgcodecs.hpp>
#include <opencv2/imgproc.hpp>

namespace {
using Clock = std::chrono::steady_clock;
double ms(Clock::time_point a, Clock::time_point b) {
  return std::chrono::duration<double, std::milli>(b - a).count();
}
struct Stats {
  double valid = 0, p05 = 0, p50 = 0, p95 = 0;
};
// Both the CPU and CUDA implementations use (minDisparity - 1) as the invalid
// marker and return absolute disparities; only [minDisp, minDisp+numDisp) is
// a real match.
Stats stats(const cv::Mat& d, int min_disp, int num_disp) {
  std::vector<float> v;
  v.reserve(d.total());
  for (int y = 0; y < d.rows; ++y)
    for (int x = 0; x < d.cols; ++x) {
      const float f = d.at<float>(y, x);
      if (f >= min_disp && f < min_disp + num_disp && std::isfinite(f)) v.push_back(f);
    }
  Stats s;
  s.valid = double(v.size()) / d.total();
  if (v.empty()) return s;
  std::sort(v.begin(), v.end());
  auto q = [&](double p) {
    const double pos = p * (v.size() - 1);
    const auto i = std::size_t(pos);
    const double t = pos - i;
    return double(v[i]) * (1 - t) + (i + 1 < v.size() ? double(v[i + 1]) * t : v[i]);
  };
  s.p05 = q(0.05); s.p50 = q(0.5); s.p95 = q(0.95);
  return s;
}
std::string arg(int argc, char** argv, const char* n, const std::string& def = {}) {
  for (int i = 1; i + 1 < argc; ++i) if (!std::strcmp(argv[i], n)) return argv[i + 1];
  return def;
}
}  // namespace

int main(int argc, char** argv) {
  const std::string left_path = arg(argc, argv, "--left");
  const std::string right_path = arg(argc, argv, "--right");
  const std::string out = arg(argc, argv, "--out", "/tmp/cuda_sgm");
  const double scale = std::stod(arg(argc, argv, "--scale", "0.5"));
  const int min_disp = std::stoi(arg(argc, argv, "--min-disp", "200"));
  const int num_disp = std::stoi(arg(argc, argv, "--num-disp", "128"));
  if (left_path.empty() || right_path.empty()) {
    std::fprintf(stderr, "usage: cuda_sgm_bench --left L.png --right R.png "
                         "[--out PREFIX] [--scale .5] [--min-disp 200] [--num-disp 128]\n");
    return 2;
  }
  cv::Mat l = cv::imread(left_path, cv::IMREAD_GRAYSCALE);
  cv::Mat r = cv::imread(right_path, cv::IMREAD_GRAYSCALE);
  if (l.empty() || r.empty()) { std::fprintf(stderr, "cannot read pair\n"); return 1; }
  if (scale != 1.0) {
    cv::resize(l, l, {}, scale, scale, cv::INTER_AREA);
    cv::resize(r, r, {}, scale, scale, cv::INTER_AREA);
  }
  std::printf("pair %dx%d, minDisp=%d numDisp=%d, %d CUDA devices\n",
              l.cols, l.rows, min_disp, num_disp, cv::cuda::getCudaEnabledDeviceCount());

  // CPU SGBM with the same window (direct minDisparity, no padding).
  auto cpu = cv::StereoSGBM::create(min_disp, num_disp, 5, 8 * 25, 96 * 25,
                                    -1, 63, 10, 100, 2,
                                    cv::StereoSGBM::MODE_SGBM_3WAY);
  cv::Mat cpu16, cpu_d;
  cpu->compute(l, r, cpu16);
  cpu16.convertTo(cpu_d, CV_32F, 1.0 / 16.0);
  auto time_cpu = [&] {
    const auto t0 = Clock::now();
    cpu->compute(l, r, cpu16);
    return ms(t0, Clock::now());
  };
  time_cpu();
  double cpu_ms = 0;
  for (int i = 0; i < 5; ++i) cpu_ms += time_cpu();
  cpu_ms /= 5;

  // GPU SGM (CUDA, contrib cudastereo).  MODE_HH = full 8-direction SGM.
  double gpu_ms = 0, upload_ms = 0, download_ms = 0, total_ms = 0;
  cv::Mat gpu_d;
  for (int mode : {cv::cuda::StereoSGM::MODE_HH, cv::cuda::StereoSGM::MODE_HH4}) {
    auto gpu = cv::cuda::createStereoSGM(min_disp, num_disp, 10, 120, 5, mode);
    cv::cuda::GpuMat gl, gr, gd;
    const auto t0 = Clock::now();
    gl.upload(l); gr.upload(r);
    const auto t1 = Clock::now();
    gpu->compute(gl, gr, gd);
    const auto t2 = Clock::now();
    cv::Mat g16;
    gd.download(g16);
    const auto t3 = Clock::now();
    upload_ms = ms(t0, t1); gpu_ms = ms(t1, t2); download_ms = ms(t2, t3);
    total_ms = ms(t0, t3);
    g16.convertTo(gpu_d, CV_32F, 1.0 / 16.0);
    // The CUDA SGM output may be relative to minDisparity; detect and add it.
    Stats gs = stats(gpu_d, min_disp, num_disp);
    // Warm repetitions for the steady-state number.
    double steady = 0;
    for (int i = 0; i < 10; ++i) {
      const auto a = Clock::now();
      gpu->compute(gl, gr, gd);
      gd.download(g16);
      const auto b = Clock::now();
      steady += ms(a, b);
    }
    steady /= 10;
    const Stats cs = stats(cpu_d, min_disp, num_disp);
    std::printf("mode=%s CPU %.3f ms (valid %.3f p50 %.2f) | GPU upload %.3f compute %.3f "
                "download %.3f first %.3f steady(compute+download) %.3f ms (valid %.3f "
                "p50 %.2f p05 %.2f p95 %.2f)\n",
                mode == cv::cuda::StereoSGM::MODE_HH ? "HH" : "HH4",
                cpu_ms, cs.valid, cs.p50, upload_ms, gpu_ms, download_ms, total_ms,
                steady, gs.valid, gs.p50, gs.p05, gs.p95);
    if (mode == cv::cuda::StereoSGM::MODE_HH) {
      cv::imwrite(out + "_cuda_disparity.tiff", gpu_d);
      cv::imwrite(out + "_cpu_disparity.tiff", cpu_d);
    }
  }
  std::printf("saved %s_{cuda,cpu}_disparity.tiff (float, includes minDisparity)\n",
              out.c_str());
  return 0;
}
