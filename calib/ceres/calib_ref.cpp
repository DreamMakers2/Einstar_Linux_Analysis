// Eigen-free reference Levenberg-Marquardt for the Einstar 3-camera problem.
//
// WHY THIS FILE EXISTS: the Ceres prototype (calib_ceres.cpp) cannot be built
// on this machine because Eigen headers are missing (see README.md).  This
// program solves the *same* problem with the same analytic Jacobian, Huber
// loss and covariance estimator using only the C++ standard library, so the
// benchmark still has a compiled-language data point and a reference optimum
// against which the (later) Ceres build can be checked.
//
// It is NOT a Ceres substitute: it has no manifolds, no automatic
// differentiation, no SuiteSparse, and a dense normal-equation solve.
//
// Build:  g++ -O2 -std=c++17 -o calib_ref calib_ref.cpp
// Run:    ./calib_ref problem.txt [--max-iter N] [--json out.json]
//
// Output: one JSON object on stdout (parameters, covariance diagonal, timings).

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "problem_io.h"

using namespace einstar;

namespace {

struct Block {
  int ncol;
  int col[24];
  double r[2];
  double J[2][24];
};

class Solver {
 public:
  explicit Solver(const Problem& p) : p_(p), n_(param_count(p)) {
    x_.assign(n_, 0.0);
    for (int c = 0; c < p.n_cameras; ++c)
      for (int i = 0; i < 9; ++i) x_[intr_offset(c) + i] = p.intr[c][i];
    for (int c = 1; c < p.n_cameras; ++c)
      for (int i = 0; i < 6; ++i) x_[extr_offset(p, c) + i] = p.extr[c][i];
    for (int v = 0; v < p.n_views; ++v)
      for (int i = 0; i < 6; ++i) x_[view_offset(p, v) + i] = p.view[v][i];
  }

  const std::vector<double>& x() const { return x_; }
  int n() const { return n_; }

  // Residual (2 rows) and analytic Jacobian for one point observation.
  void block(const std::vector<double>& x, const Obs& o, Block& blk) const {
    const int c = o.cam, v = o.view;
    const double* in = &x[intr_offset(c)];
    const double fx = in[0], fy = in[1], cx = in[2], cy = in[3];
    const double k1 = in[4], k2 = in[5], p1 = in[6], p2 = in[7], k3 = in[8];

    double Rv[3][3], dRv[3][3][3], tv[3];
    rodrigues(&x[view_offset(p_, v)], Rv);
    rodrigues_dR(&x[view_offset(p_, v)], dRv);
    for (int i = 0; i < 3; ++i) tv[i] = x[view_offset(p_, v) + 3 + i];

    const auto& X = p_.pts[o.point];
    double Xl[3];
    for (int i = 0; i < 3; ++i)
      Xl[i] = Rv[i][0] * X[0] + Rv[i][1] * X[1] + Rv[i][2] * X[2] + tv[i];

    double Xc[3];
    if (c == 0) {
      Xc[0] = Xl[0]; Xc[1] = Xl[1]; Xc[2] = Xl[2];
    } else {
      double Rc[3][3];
      rodrigues(&x[extr_offset(p_, c)], Rc);
      for (int i = 0; i < 3; ++i)
        Xc[i] = Rc[i][0] * Xl[0] + Rc[i][1] * Xl[1] + Rc[i][2] * Xl[2] +
                x[extr_offset(p_, c) + 3 + i];
    }

    const double z = Xc[2];
    const double xd_ = Xc[0] / z, yd_ = Xc[1] / z;
    const double r2 = xd_ * xd_ + yd_ * yd_;
    const double rad = 1.0 + k1 * r2 + k2 * r2 * r2 + k3 * r2 * r2 * r2;
    const double dr2 = k1 + 2.0 * k2 * r2 + 3.0 * k3 * r2 * r2;
    const double xd = xd_ * rad + 2.0 * p1 * xd_ * yd_ + p2 * (r2 + 2.0 * xd_ * xd_);
    const double yd = yd_ * rad + p1 * (r2 + 2.0 * yd_ * yd_) + 2.0 * p2 * xd_ * yd_;
    const double dxd_dx = rad + xd_ * 2.0 * xd_ * dr2 + 2.0 * p1 * yd_ + 4.0 * p2 * xd_;
    const double dxd_dy = xd_ * 2.0 * yd_ * dr2 + 2.0 * p1 * xd_ + 2.0 * p2 * yd_;
    const double dyd_dx = yd_ * 2.0 * xd_ * dr2 + 2.0 * p1 * xd_ + 2.0 * p2 * yd_;
    const double dyd_dy = rad + yd_ * 2.0 * yd_ * dr2 + 4.0 * p1 * yd_ + 2.0 * p2 * xd_;

    blk.r[0] = fx * xd + cx - o.u;
    blk.r[1] = fy * yd + cy - o.v;

    // d(u,v)/dXc
    const double iz = 1.0 / z;
    double duv[2][3];
    duv[0][0] = fx * dxd_dx * iz;
    duv[0][1] = fx * dxd_dy * iz;
    duv[0][2] = -(fx * (dxd_dx * xd_ + dxd_dy * yd_)) * iz;
    duv[1][0] = fy * dyd_dx * iz;
    duv[1][1] = fy * dyd_dy * iz;
    duv[1][2] = -(fy * (dyd_dx * xd_ + dyd_dy * yd_)) * iz;

    int nc = 0;
    for (int i = 0; i < 24; ++i)
      for (int r = 0; r < 2; ++r) blk.J[r][i] = 0.0;
    for (int i = 0; i < 9; ++i) blk.col[nc++] = intr_offset(c) + i;
    for (int i = 0; i < 9; ++i) {
      blk.J[0][i] = 0.0; blk.J[1][i] = 0.0;
    }
    blk.J[0][0] = xd;   blk.J[1][0] = 0.0;   // fx
    blk.J[0][1] = 0.0;  blk.J[1][1] = yd;    // fy
    blk.J[0][2] = 1.0;  blk.J[1][2] = 0.0;   // cx
    blk.J[0][3] = 0.0;  blk.J[1][3] = 1.0;   // cy
    const double dxd_dk[5] = {xd_ * r2, xd_ * r2 * r2, 2.0 * xd_ * yd_,
                              r2 + 2.0 * xd_ * xd_, xd_ * r2 * r2 * r2};
    const double dyd_dk[5] = {yd_ * r2, yd_ * r2 * r2,
                              r2 + 2.0 * yd_ * yd_, 2.0 * xd_ * yd_,
                              yd_ * r2 * r2 * r2};
    for (int i = 0; i < 5; ++i) {
      blk.J[0][4 + i] = fx * dxd_dk[i];
      blk.J[1][4 + i] = fy * dyd_dk[i];
    }
    if (!p_.free_distortion)
      for (int i = 4; i < 9; ++i) { blk.J[0][i] = 0.0; blk.J[1][i] = 0.0; }

    int base = 9;
    if (c != 0) {
      double Rc[3][3], dRc[3][3][3];
      rodrigues(&x[extr_offset(p_, c)], Rc);
      rodrigues_dR(&x[extr_offset(p_, c)], dRc);
      for (int i = 0; i < 3; ++i) {
        double dX[3];
        matvec3(dRc[i], Xl, dX);            // dXc/dr_i = dRc_i @ Xl
        blk.J[0][base + i] = duv[0][0] * dX[0] + duv[0][1] * dX[1] + duv[0][2] * dX[2];
        blk.J[1][base + i] = duv[1][0] * dX[0] + duv[1][1] * dX[1] + duv[1][2] * dX[2];
      }
      for (int i = 0; i < 3; ++i) {
        blk.J[0][base + 3 + i] = duv[0][i];
        blk.J[1][base + 3 + i] = duv[1][i];
      }
      for (int i = 0; i < 6; ++i) blk.col[nc++] = extr_offset(p_, c) + i;
      base += 6;
    }
    // view pose
    for (int i = 0; i < 3; ++i) {
      double dXl[3], dXc[3];
      matvec3(dRv[i], &X[0], dXl);
      if (c == 0) { dXc[0] = dXl[0]; dXc[1] = dXl[1]; dXc[2] = dXl[2]; }
      else {
        double Rc[3][3];
        rodrigues(&x[extr_offset(p_, c)], Rc);
        matvec3(Rc, dXl, dXc);
      }
      blk.J[0][base + i] = duv[0][0] * dXc[0] + duv[0][1] * dXc[1] + duv[0][2] * dXc[2];
      blk.J[1][base + i] = duv[1][0] * dXc[0] + duv[1][1] * dXc[1] + duv[1][2] * dXc[2];
    }
    double tv_[3] = {0, 0, 0};
    if (c == 0) {
      for (int i = 0; i < 3; ++i) {
        blk.J[0][base + 3 + i] = duv[0][i];
        blk.J[1][base + 3 + i] = duv[1][i];
      }
    } else {
      double Rc[3][3];
      rodrigues(&x[extr_offset(p_, c)], Rc);
      for (int i = 0; i < 3; ++i) {
        double col[3] = {Rc[0][i], Rc[1][i], Rc[2][i]};
        blk.J[0][base + 3 + i] = duv[0][0] * col[0] + duv[0][1] * col[1] + duv[0][2] * col[2];
        blk.J[1][base + 3 + i] = duv[1][0] * col[0] + duv[1][1] * col[1] + duv[1][2] * col[2];
      }
    }
    for (int i = 0; i < 6; ++i) blk.col[nc++] = view_offset(p_, v) + i;
    blk.ncol = nc;
    (void)tv_;
  }

  double huber_cost(double r2) const {
    const double d2 = p_.huber_delta * p_.huber_delta;
    return (r2 <= d2) ? r2 : 2.0 * p_.huber_delta * std::sqrt(r2) - d2;
  }

  double weight(double r) const {
    const double a = std::fabs(r);
    return (a <= p_.huber_delta) ? 1.0 : p_.huber_delta / std::max(a, 1e-12);
  }

  // Cost, normal equations and (optionally) covariance pieces at x.
  double normal(const std::vector<double>& x, std::vector<double>& H,
                std::vector<double>& g, std::vector<double>* A,
                std::vector<double>* B) const {
    const int n = n_;
    std::fill(H.begin(), H.end(), 0.0);
    std::fill(g.begin(), g.end(), 0.0);
    double cost = 0.0;
    Block blk;
    for (const auto& o : p_.obs) {
      block(x, o, blk);
      const double w0 = weight(blk.r[0]), w1 = weight(blk.r[1]);
      const double sw0 = std::sqrt(w0), sw1 = std::sqrt(w1);
      double J[2][24];
      for (int i = 0; i < blk.ncol; ++i) {
        J[0][i] = blk.J[0][i] * sw0;
        J[1][i] = blk.J[1][i] * sw1;
      }
      const double r0 = blk.r[0] * sw0, r1 = blk.r[1] * sw1;
      cost += huber_cost(blk.r[0] * blk.r[0]) + huber_cost(blk.r[1] * blk.r[1]);
      for (int i = 0; i < blk.ncol; ++i) {
        const int ci = blk.col[i];
        g[ci] += J[0][i] * r0 + J[1][i] * r1;
        for (int j = i; j < blk.ncol; ++j) {
          const int cj = blk.col[j];
          H[(size_t)ci * n + cj] += J[0][i] * J[0][j] + J[1][i] * J[1][j];
        }
      }
      if (A && B) {
        for (int i = 0; i < blk.ncol; ++i) {
          const int ci = blk.col[i];
          for (int j = i; j < blk.ncol; ++j) {
            const int cj = blk.col[j];
            (*A)[(size_t)ci * n + cj] += J[0][i] * J[0][j] + J[1][i] * J[1][j];
            // meat of the Huber sandwich: psi(r) = w*r, so (w*r)^2.
            // J already carries sqrt(w), hence the extra w factor.
            (*B)[(size_t)ci * n + cj] +=
                J[0][i] * (w0 * blk.r[0] * blk.r[0]) * J[0][j] +
                J[1][i] * (w1 * blk.r[1] * blk.r[1]) * J[1][j];
          }
        }
      }
    }
    for (int i = 0; i < n; ++i)
      for (int j = 0; j < i; ++j) H[(size_t)i * n + j] = H[(size_t)j * n + i];
    if (A && B)
      for (int i = 0; i < n; ++i)
        for (int j = 0; j < i; ++j) {
          (*A)[(size_t)i * n + j] = (*A)[(size_t)j * n + i];
          (*B)[(size_t)i * n + j] = (*B)[(size_t)j * n + i];
        }
    return cost;
  }

  static bool chol(std::vector<double>& A, int n, std::vector<double>& b) {
    for (int i = 0; i < n; ++i) {
      for (int j = 0; j <= i; ++j) {
        double s = A[(size_t)i * n + j];
        for (int k = 0; k < j; ++k)
          s -= A[(size_t)i * n + k] * A[(size_t)j * n + k];
        if (i == j) {
          if (s <= 0) return false;
          A[(size_t)i * n + i] = std::sqrt(s);
        } else {
          A[(size_t)i * n + j] = s / A[(size_t)j * n + j];
        }
      }
    }
    for (int i = 0; i < n; ++i) {
      double s = b[i];
      for (int k = 0; k < i; ++k) s -= A[(size_t)i * n + k] * b[k];
      b[i] = s / A[(size_t)i * n + i];
    }
    for (int i = n - 1; i >= 0; --i) {
      double s = b[i];
      for (int k = i + 1; k < n; ++k) s -= A[(size_t)k * n + i] * b[k];
      b[i] = s / A[(size_t)i * n + i];
    }
    return true;
  }

  // C = Ainv * B * Ainv ; A, B symmetric n x n
  std::vector<double> sandwich(const std::vector<double>& A,
                               const std::vector<double>& B) const {
    const int n = n_;
    std::vector<double> Ainv((size_t)n * n, 0.0);
    std::vector<double> e(n, 0.0);
    for (int c = 0; c < n; ++c) {
      std::vector<double> M = A;
      std::fill(e.begin(), e.end(), 0.0);
      e[c] = 1.0;
      chol(M, n, e);
      for (int r = 0; r < n; ++r) Ainv[(size_t)r * n + c] = e[r];
    }
    std::vector<double> T((size_t)n * n, 0.0), C((size_t)n * n, 0.0);
    for (int i = 0; i < n; ++i)
      for (int j = 0; j < n; ++j) {
        double s = 0;
        for (int k = 0; k < n; ++k) s += Ainv[(size_t)i * n + k] * B[(size_t)k * n + j];
        T[(size_t)i * n + j] = s;
      }
    for (int i = 0; i < n; ++i)
      for (int j = 0; j < n; ++j) {
        double s = 0;
        for (int k = 0; k < n; ++k) s += T[(size_t)i * n + k] * Ainv[(size_t)k * n + j];
        C[(size_t)i * n + j] = s;
      }
    return C;
  }

  void rms(const std::vector<double>& x, double& rms_px, double& max_px,
           double& sum_cost) const {
    double s2 = 0.0, mx = 0.0;
    sum_cost = 0.0;
    Block blk;
    int npts = 0;
    for (const auto& o : p_.obs) {
      block(x, o, blk);
      const double e2 = blk.r[0] * blk.r[0] + blk.r[1] * blk.r[1];
      s2 += e2;
      mx = std::max(mx, std::sqrt(e2));
      sum_cost += huber_cost(blk.r[0] * blk.r[0]) + huber_cost(blk.r[1] * blk.r[1]);
      ++npts;
    }
    rms_px = (npts ? std::sqrt(s2 / npts) : 0.0);
    max_px = mx;
  }

  const Problem& p_;
  int n_;
  std::vector<double> x_;
};

}  // namespace

int main(int argc, char** argv) {
  if (argc < 2) {
    std::fprintf(stderr, "usage: %s problem.txt [--max-iter N]\n", argv[0]);
    return 2;
  }
  Problem p = read_problem(argv[1]);
  int max_iter = 100;
  for (int i = 2; i < argc; ++i)
    if (!std::strcmp(argv[i], "--max-iter") && i + 1 < argc) max_iter = std::atoi(argv[++i]);

  Solver s(p);
  const int n = s.n();
  std::vector<double> H((size_t)n * n), g(n), x = s.x();

  auto t_start = std::chrono::steady_clock::now();
  double cost = 0.0, rms0 = 0.0, mx0 = 0.0;
  s.normal(x, H, g, nullptr, nullptr);
  s.rms(x, rms0, mx0, cost);
  double lambda = 1e-4;
  int iter = 0;
  std::string status = "max_iterations";
  double t_jac = 0.0;
  for (; iter < max_iter; ++iter) {
    double gmax = 0.0;
    for (int i = 0; i < n; ++i) gmax = std::max(gmax, std::fabs(g[i]));
    if (gmax < 1e-10) { status = "converged_gradient"; break; }
    bool accepted = false;
    for (int attempt = 0; attempt < 12 && !accepted; ++attempt) {
      std::vector<double> Hd = H;
      for (int i = 0; i < n; ++i)
        Hd[(size_t)i * n + i] += lambda * (H[(size_t)i * n + i] + 1e-12);
      std::vector<double> delta = g;
      for (int i = 0; i < n; ++i) delta[i] = -delta[i];
      if (!Solver::chol(Hd, n, delta)) { lambda *= 10.0; continue; }
      std::vector<double> xt(n);
      double step = 0.0;
      for (int i = 0; i < n; ++i) {
        xt[i] = x[i] + delta[i];
        step = std::max(step, std::fabs(delta[i]));
      }
      double c2 = 0.0, r1 = 0.0, m1 = 0.0;
      auto tj0 = std::chrono::steady_clock::now();
      s.normal(xt, H, g, nullptr, nullptr);
      t_jac += std::chrono::duration<double>(std::chrono::steady_clock::now() - tj0).count();
      s.rms(xt, r1, m1, c2);
      if (c2 < cost) {
        x = xt; cost = c2; accepted = true; lambda = std::max(lambda * 0.3, 1e-12);
        if (step < 1e-12) { status = "converged_step"; }
      } else {
        lambda *= 10.0;
        if (lambda > 1e16) break;
      }
    }
    if (!accepted) { status = "no_descent"; break; }
    if (status == "converged_step") break;
  }
  auto t_end = std::chrono::steady_clock::now();

  double rms_px = 0.0, max_px = 0.0, c_final = 0.0;
  s.rms(x, rms_px, max_px, c_final);

  // covariance: Huber sandwich A^-1 B A^-1 at the solution
  std::vector<double> A((size_t)n * n, 0.0), B((size_t)n * n, 0.0);
  s.normal(x, H, g, &A, &B);
  std::vector<double> C = s.sandwich(A, B);

  std::printf("{\"solver\":\"cpp_ref_lm\",\"n\":%d,\"m\":%zu,", n, 2 * p.obs.size());
  std::printf("\"iterations\":%d,\"status\":\"%s\",\"lambda\":%.3g,", iter, status.c_str(), lambda);
  std::printf("\"rms_px\":%.12g,\"max_px\":%.12g,\"cost\":%.12g,", rms_px, max_px, c_final);
  std::printf("\"t_total_s\":%.6f,\"t_jac_s\":%.6f,", 
              std::chrono::duration<double>(t_end - t_start).count(), t_jac);
  std::printf("\"x\":[");
  for (int i = 0; i < n; ++i) std::printf("%s%.17g", i ? "," : "", x[i]);
  std::printf("],\"cov_diag\":[");
  for (int i = 0; i < n; ++i) std::printf("%s%.9g", i ? "," : "", C[(size_t)i * n + i]);
  std::printf("]}\n");
  return 0;
}
