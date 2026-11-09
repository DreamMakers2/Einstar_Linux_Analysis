// Ceres prototype: joint 3-camera bundle adjustment for the Einstar calibration.
//
// STATUS ON THE BENCHMARK HOST: **NOT BUILT**.  Eigen 3 headers are absent and
// Ceres 2.2 cannot be compiled against without them.  See README.md for the
// exact error and the fix.  This file is the drop-in prototype for the day the
// toolchain is complete; it has never been compiled or run here, so treat it
// as unvalidated source, not as a measured backend.
//
// Model (identical to calib/calibrate.py):
//   X_l   = Rv @ Xb + tv            board -> left IR camera
//   X_c   = Rc @ X_l + tc           left -> camera c (c = 0 identity)
//   x,y   = X_c.xy / X_c.z
//   radial= 1 + k1 r2 + k2 r2^2 + k3 r2^3
//   xd    = x radial + 2 p1 x y + p2 (r2 + 2 x^2)
//   yd    = y radial + p1 (r2 + 2 y^2) + 2 p2 x y
//   u     = fx xd + cx ;  v = fy yd + cy
//
// Parameter blocks: 3 x 9 intrinsics, 2 x 6 extrinsics, n_views x 6 view poses.
// The left camera pose is the gauge and is not a parameter; the board pitch
// fixes the metric scale.
//
// Build: see build.sh (g++ -lceres -lglog).
// Run:   ./calib_ceres problem.txt [--solver sparse|schur] [--covariance]

#include <ceres/ceres.h>
#include <Eigen/Core>

#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <memory>
#include <string>
#include <vector>

#include "problem_io.h"

using namespace einstar;
using ceres::AutoDiffCostFunction;
using ceres::CauchyLoss;
using ceres::CostFunction;
using ceres::HuberLoss;
using ceres::Problem;
using ceres::Solve;
using ceres::Solver;

namespace {

// Observation with a camera extrinsic block (right / tex).
struct ReprojCost {
  ReprojCost(const Problem* p, int view, int point, double u, double v)
      : p_(p), view_idx_(view), pt_idx_(point), u_(u), v_(v) {}

  template <typename T>
  bool operator()(const T* const intr, const T* const extr, const T* const view,
                  T* residual) const {
    const T* Xb = &p_->pts[pt_idx_][0];
    T Rv[9], Rc[9];
    ceres::AngleAxisToRotationMatrix(view, Rv);   // row-major 3x3
    ceres::AngleAxisToRotationMatrix(extr, Rc);
    T Xl[3], Xc[3];
    for (int i = 0; i < 3; ++i) {
      Xl[i] = Rv[3 * i + 0] * Xb[0] + Rv[3 * i + 1] * Xb[1] +
              Rv[3 * i + 2] * Xb[2] + view[3 + i];
      Xc[i] = Rc[3 * i + 0] * Xl[0] + Rc[3 * i + 1] * Xl[1] +
              Rc[3 * i + 2] * Xl[2] + extr[3 + i];
    }
    const T x = Xc[0] / Xc[2], y = Xc[1] / Xc[2];
    const T r2 = x * x + y * y;
    const T radial = T(1.0) + intr[4] * r2 + intr[5] * r2 * r2 +
                     intr[8] * r2 * r2 * r2;
    const T xd = x * radial + T(2.0) * intr[6] * x * y +
                 intr[7] * (r2 + T(2.0) * x * x);
    const T yd = y * radial + intr[6] * (r2 + T(2.0) * y * y) +
                 T(2.0) * intr[7] * x * y;
    residual[0] = intr[0] * xd + intr[2] - T(u_);
    residual[1] = intr[1] * yd + intr[3] - T(v_);
    return true;
  }

  const Problem* p_;
  int view_idx_, pt_idx_;
  double u_, v_;
};

// Same, for the gauge camera (no extrinsic block).
struct ReprojCostLeft {
  ReprojCostLeft(const Problem* p, int view, int point, double u, double v)
      : p_(p), view_idx_(view), pt_idx_(point), u_(u), v_(v) {}

  template <typename T>
  bool operator()(const T* const intr, const T* const view, T* residual) const {
    const T* Xb = &p_->pts[pt_idx_][0];
    T Rv[9];
    ceres::AngleAxisToRotationMatrix(view, Rv);
    T Xl[3];
    for (int i = 0; i < 3; ++i)
      Xl[i] = Rv[3 * i + 0] * Xb[0] + Rv[3 * i + 1] * Xb[1] +
              Rv[3 * i + 2] * Xb[2] + view[3 + i];
    const T x = Xl[0] / Xl[2], y = Xl[1] / Xl[2];
    const T r2 = x * x + y * y;
    const T radial = T(1.0) + intr[4] * r2 + intr[5] * r2 * r2 +
                     intr[8] * r2 * r2 * r2;
    const T xd = x * radial + T(2.0) * intr[6] * x * y +
                 intr[7] * (r2 + T(2.0) * x * x);
    const T yd = y * radial + intr[6] * (r2 + T(2.0) * y * y) +
                 T(2.0) * intr[7] * x * y;
    residual[0] = intr[0] * xd + intr[2] - T(u_);
    residual[1] = intr[1] * yd + intr[3] - T(v_);
    return true;
  }

  const Problem* p_;
  int view_idx_, pt_idx_;
  double u_, v_;
};

void print_json(const Problem& p, const std::vector<std::array<double, 9>>& intr,
                const std::vector<std::array<double, 6>>& extr,
                const std::vector<std::array<double, 6>>& view,
                const double* cov_diag, int n_cov, double rms_px, double max_px,
                int iterations, const char* status, double t_total,
                double t_setup) {
  std::printf("{\"solver\":\"ceres\",\"n\":%d,\"m\":%zu,",
              param_count(p), 2 * p.obs.size());
  std::printf("\"iterations\":%d,\"status\":\"%s\",", iterations, status);
  std::printf("\"rms_px\":%.12g,\"max_px\":%.12g,", rms_px, max_px);
  std::printf("\"t_total_s\":%.6f,\"t_setup_s\":%.6f,", t_total, t_setup);
  std::printf("\"x\":[");
  bool first = true;
  for (int c = 0; c < p.n_cameras; ++c)
    for (int i = 0; i < 9; ++i) {
      std::printf("%s%.17g", first ? "" : ",", intr[c][i]);
      first = false;
    }
  for (int c = 1; c < p.n_cameras; ++c)
    for (int i = 0; i < 6; ++i) {
      std::printf("%s%.17g", first ? "" : ",", extr[c][i]);
      first = false;
    }
  for (int v = 0; v < p.n_views; ++v)
    for (int i = 0; i < 6; ++i) {
      std::printf("%s%.17g", first ? "" : ",", view[v][i]);
      first = false;
    }
  std::printf("],\"cov_diag\":[");
  for (int i = 0; i < n_cov; ++i) std::printf("%s%.9g", i ? "," : "", cov_diag[i]);
  std::printf("]}\n");
}

}  // namespace

int main(int argc, char** argv) {
  if (argc < 2) {
    std::fprintf(stderr, "usage: %s problem.txt [--solver sparse|schur] [--covariance]\n",
                 argv[0]);
    return 2;
  }
  Problem p = read_problem(argv[1]);
  std::string solver = "sparse";
  bool want_cov = false;
  for (int i = 2; i < argc; ++i) {
    if (!std::strcmp(argv[i], "--solver") && i + 1 < argc) solver = argv[++i];
    else if (!std::strcmp(argv[i], "--covariance")) want_cov = true;
  }

  std::vector<std::array<double, 9>> intr(p.n_cameras);
  std::vector<std::array<double, 6>> extr(p.n_cameras), view(p.n_views);
  for (int c = 0; c < p.n_cameras; ++c) intr[c] = p.intr[c];
  for (int c = 0; c < p.n_cameras; ++c) extr[c] = p.extr[c];
  for (int v = 0; v < p.n_views; ++v) view[v] = p.view[v];

  auto t0 = std::chrono::steady_clock::now();
  Problem problem;
  auto* loss = new HuberLoss(p.huber_delta);
  std::vector<double*> intr_ptr(p.n_cameras), extr_ptr(p.n_cameras);
  for (int c = 0; c < p.n_cameras; ++c) intr_ptr[c] = intr[c].data();
  for (int c = 1; c < p.n_cameras; ++c) extr_ptr[c] = extr[c].data();
  intr_ptr[0] = intr[0].data();
  extr_ptr[0] = nullptr;

  std::vector<std::unique_ptr<CostFunction>> costs;
  for (const auto& o : p.obs) {
    double* vp = view[o.view].data();
    if (o.cam == 0) {
      auto* cf = new AutoDiffCostFunction<ReprojCostLeft, 2, 9, 6>(
          new ReprojCostLeft(&p, o.view, o.point, o.u, o.v));
      costs.emplace_back(cf);
      problem.AddResidualBlock(cf, loss, intr_ptr[0], vp);
    } else {
      auto* cf = new AutoDiffCostFunction<ReprojCost, 2, 9, 6, 6>(
          new ReprojCost(&p, o.view, o.point, o.u, o.v));
      costs.emplace_back(cf);
      problem.AddResidualBlock(cf, loss, intr_ptr[o.cam], extr_ptr[o.cam], vp);
    }
  }

  // Hold the distortion coefficients when fixed_distortion is requested.
  ceres::SubsetManifold* fixed_dist = nullptr;
  if (!p.free_distortion) {
    fixed_dist = new ceres::SubsetManifold(9, {4, 5, 6, 7, 8});
    for (int c = 0; c < p.n_cameras; ++c)
      problem.SetManifold(intr_ptr[c], fixed_dist);
  }

  Solver::Options options;
  options.linear_solver_type = (solver == "schur")
                                   ? ceres::DENSE_SCHUR
                                   : ceres::SPARSE_NORMAL_CHOLESKY;
  options.sparse_linear_algebra_library_type = ceres::SUITE_SPARSE;
  options.trust_region_strategy_type = ceres::LEVENBERG_MARQUARDT;
  options.max_num_iterations = 200;
  options.function_tolerance = 1e-12;
  options.gradient_tolerance = 1e-12;
  options.parameter_tolerance = 1e-14;
  options.minimizer_progress_to_stdout = false;
  Solver::Summary summary;
  auto t1 = std::chrono::steady_clock::now();
  Solve(options, &problem, &summary);
  auto t2 = std::chrono::steady_clock::now();

  // Residual RMS on the same convention as the SciPy bench.
  {
    std::vector<double> r;
    problem.Evaluate(Problem::EvaluateOptions(), nullptr, &r, nullptr, nullptr);
    double s2 = 0.0, mx = 0.0;
    for (size_t i = 0; i + 1 < r.size(); i += 2) {
      const double e2 = r[i] * r[i] + r[i + 1] * r[i + 1];
      s2 += e2;
      mx = std::max(mx, std::sqrt(e2));
    }
    const double rms = std::sqrt(s2 / (double)(r.size() / 2));

    std::vector<double> cov_diag;
    if (want_cov && p.free_distortion) {
      // Marginal variances of the 39 rig parameters.  The per-view poses are
      // separate parameter blocks and are marginalized by Ceres automatically.
      // (With a SubsetManifold the tangent space differs, which is why this is
      // skipped when the distortion is fixed.)
      std::vector<std::pair<const double*, const double*>> blocks;
      for (int c = 0; c < p.n_cameras; ++c) blocks.push_back({intr_ptr[c], intr_ptr[c]});
      for (int c = 1; c < p.n_cameras; ++c) blocks.push_back({extr_ptr[c], extr_ptr[c]});
      ceres::Covariance::Options copts;
      copts.algorithm_type = ceres::SPARSE_QR;
      ceres::Covariance covariance(copts);
      if (covariance.Compute(blocks, &problem)) {
        for (int c = 0; c < p.n_cameras; ++c) {
          double buf[81];
          if (covariance.GetCovarianceBlockInTangentSpace(intr_ptr[c], intr_ptr[c], buf))
            for (int i = 0; i < 9; ++i) cov_diag.push_back(buf[i * 9 + i]);
          else
            for (int i = 0; i < 9; ++i) cov_diag.push_back(-1.0);
        }
        for (int c = 1; c < p.n_cameras; ++c) {
          double buf[36];
          if (covariance.GetCovarianceBlockInTangentSpace(extr_ptr[c], extr_ptr[c], buf))
            for (int i = 0; i < 6; ++i) cov_diag.push_back(buf[i * 6 + i]);
          else
            for (int i = 0; i < 6; ++i) cov_diag.push_back(-1.0);
        }
      }
    }
    print_json(p, intr, extr, view, cov_diag.data(), (int)cov_diag.size(), rms, mx,
               summary.iterations.size(), summary.IsSolutionUsable() ? "ok" : "failed",
               std::chrono::duration<double>(t2 - t1).count(),
               std::chrono::duration<double>(t1 - t0).count());
  }
  delete fixed_dist;
  return 0;
}
