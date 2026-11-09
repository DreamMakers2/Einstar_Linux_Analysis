// Minimal, dependency-free reader for the benchmark observation format.
//
// Used by both the Ceres prototype (calib_ceres.cpp) and the Eigen-free
// reference LM (calib_ref.cpp), so the two consume byte-identical inputs.
//
// Format (whitespace separated, '#' starts a comment line):
//
//   n_cameras 3
//   n_views   12
//   n_points  40
//   pitch_mm  27.72
//   huber_delta 1.0
//   free_distortion 1
//   intrinsics <cam> fx fy cx cy k1 k2 p1 p2 k3
//   extrinsics <cam> rx ry rz tx ty tz        # cam 1..n_cameras-1, cam 0 fixed identity
//   point <i> x y z                            # board frame, mm
//   obs <cam> <view> <point> u v               # pixels
//
// Parameter vector order matches calib.calibrate._Packer exactly:
//   [intr 0..2 (9 each)][extr 1..2 (6 each)][view poses (6 each)]
#pragma once

#include <array>
#include <cmath>
#include <cstdio>
#include <fstream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace einstar {

struct Obs {
  int cam, view, point;
  double u, v;
};

struct Problem {
  int n_cameras = 3;
  int n_views = 0;
  int n_points = 0;
  double pitch_mm = 27.72;
  double huber_delta = 1.0;
  bool free_distortion = true;
  std::vector<std::array<double, 9>> intr;   // per camera
  std::vector<std::array<double, 6>> extr;   // per camera (0 unused)
  std::vector<std::array<double, 6>> view;   // per view
  std::vector<std::array<double, 3>> pts;
  std::vector<Obs> obs;
};

inline int param_count(const Problem& p) {
  return 9 * p.n_cameras + 6 * (p.n_cameras - 1) + 6 * p.n_views;
}

inline int intr_offset(int cam) { return 9 * cam; }
inline int extr_offset(const Problem& p, int cam) {
  return 9 * p.n_cameras + 6 * (cam - 1);
}
inline int view_offset(const Problem& p, int v) {
  return 9 * p.n_cameras + 6 * (p.n_cameras - 1) + 6 * v;
}

inline Problem read_problem(const std::string& path) {
  std::ifstream in(path);
  if (!in) throw std::runtime_error("cannot open " + path);
  Problem p;
  p.intr.assign(3, std::array<double, 9>{});
  p.extr.assign(3, std::array<double, 6>{});
  std::string line, key;
  while (std::getline(in, line)) {
    if (line.empty() || line[0] == '#') continue;
    std::istringstream ss(line);
    ss >> key;
    if (key == "n_cameras") ss >> p.n_cameras;
    else if (key == "n_views") ss >> p.n_views;
    else if (key == "n_points") ss >> p.n_points;
    else if (key == "pitch_mm") ss >> p.pitch_mm;
    else if (key == "huber_delta") ss >> p.huber_delta;
    else if (key == "free_distortion") { int f = 1; ss >> f; p.free_distortion = f != 0; }
    else if (key == "intrinsics") {
      int c; ss >> c;
      for (int i = 0; i < 9; ++i) ss >> p.intr[c][i];
    } else if (key == "extrinsics") {
      int c; ss >> c;
      for (int i = 0; i < 6; ++i) ss >> p.extr[c][i];
    } else if (key == "point") {
      int i; ss >> i;
      std::array<double, 3> x{};
      ss >> x[0] >> x[1] >> x[2];
      if ((int)p.pts.size() <= i) p.pts.resize(i + 1);
      p.pts[i] = x;
    } else if (key == "obs") {
      Obs o{};
      ss >> o.cam >> o.view >> o.point >> o.u >> o.v;
      p.obs.push_back(o);
    } else if (key == "view") {
      int v; ss >> v;
      std::array<double, 6> x{};
      for (int i = 0; i < 6; ++i) ss >> x[i];
      if ((int)p.view.size() <= v) p.view.resize(v + 1);
      p.view[v] = x;
    }
  }
  if (p.view.size() != (size_t)p.n_views)
    throw std::runtime_error("missing view poses");
  if ((int)p.pts.size() != p.n_points)
    throw std::runtime_error("missing board points");
  return p;
}

// --- SO(3) in the cv2.Rodrigues convention -------------------------------
inline void rodrigues(const double r[3], double R[3][3]) {
  const double th = std::sqrt(r[0] * r[0] + r[1] * r[1] + r[2] * r[2]);
  double K[3][3] = {{0, -r[2], r[1]}, {r[2], 0, -r[0]}, {-r[1], r[0], 0}};
  double K2[3][3];
  for (int i = 0; i < 3; ++i)
    for (int j = 0; j < 3; ++j) {
      K2[i][j] = 0;
      for (int k = 0; k < 3; ++k) K2[i][j] += K[i][k] * K[k][j];
    }
  double A, B;
  if (th < 1e-8) { A = 1.0 - th * th / 6.0; B = 0.5 - th * th / 24.0; }
  else { A = std::sin(th) / th; B = (1.0 - std::cos(th)) / (th * th); }
  for (int i = 0; i < 3; ++i)
    for (int j = 0; j < 3; ++j)
      R[i][j] = (i == j ? 1.0 : 0.0) + A * K[i][j] + B * K2[i][j];
}

// dR/dr_i, matching rodrigues(); dR[i] is a 3x3 matrix.
inline void rodrigues_dR(const double r[3], double dR[3][3][3]) {
  const double th = std::sqrt(r[0] * r[0] + r[1] * r[1] + r[2] * r[2]);
  double K[3][3] = {{0, -r[2], r[1]}, {r[2], 0, -r[0]}, {-r[1], r[0], 0}};
  double K2[3][3];
  for (int i = 0; i < 3; ++i)
    for (int j = 0; j < 3; ++j) {
      K2[i][j] = 0;
      for (int k = 0; k < 3; ++k) K2[i][j] += K[i][k] * K[k][j];
    }
  double A, B, dA, dB;
  if (th < 1e-8) {
    A = 1.0 - th * th / 6.0; B = 0.5 - th * th / 24.0;
    dA = -th / 3.0; dB = -th / 12.0;
  } else {
    A = std::sin(th) / th; B = (1.0 - std::cos(th)) / (th * th);
    dA = (th * std::cos(th) - std::sin(th)) / (th * th);
    dB = (th * std::sin(th) - 2.0 * (1.0 - std::cos(th))) / (th * th * th);
  }
  for (int i = 0; i < 3; ++i) {
    double e[3] = {0, 0, 0}; e[i] = 1.0;
    double Ei[3][3] = {{0, -e[2], e[1]}, {e[2], 0, -e[0]}, {-e[1], e[0], 0}};
    double EK[3][3], KE[3][3];
    for (int a = 0; a < 3; ++a)
      for (int b = 0; b < 3; ++b) {
        EK[a][b] = KE[a][b] = 0;
        for (int k = 0; k < 3; ++k) {
          EK[a][b] += Ei[a][k] * K[k][b];
          KE[a][b] += K[a][k] * Ei[k][b];
        }
      }
    const double s = (th > 1e-12) ? r[i] / th : 0.0;
    for (int a = 0; a < 3; ++a)
      for (int b = 0; b < 3; ++b)
        dR[i][a][b] = dA * s * K[a][b] + A * Ei[a][b] + dB * s * K2[a][b] +
                      B * (EK[a][b] + KE[a][b]);
  }
}

inline void matmul3(const double A[3][3], const double B[3][3], double C[3][3]) {
  for (int i = 0; i < 3; ++i)
    for (int j = 0; j < 3; ++j) {
      C[i][j] = 0;
      for (int k = 0; k < 3; ++k) C[i][j] += A[i][k] * B[k][j];
    }
}

inline void matvec3(const double A[3][3], const double x[3], double y[3]) {
  for (int i = 0; i < 3; ++i)
    y[i] = A[i][0] * x[0] + A[i][1] * x[1] + A[i][2] * x[2];
}

}  // namespace einstar
