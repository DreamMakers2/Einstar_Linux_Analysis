#!/usr/bin/env bash
# Build the standalone solvers in this directory.
#
#   ./build.sh            # build what the toolchain allows
#   CXX=clang++ ./build.sh
#
# calib_ref  (Eigen-free reference LM) always builds.
# calib_ceres (Ceres prototype)      builds only when Eigen 3 headers exist.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
CXX="${CXX:-g++}"
CXXFLAGS="${CXXFLAGS:--O2 -std=c++17 -Wall -Wextra}"

echo "== toolchain =="
"$CXX" --version | head -1

have_eigen=0
if printf '#include <Eigen/Core>\nint main(){return 0;}\n' | \
     "$CXX" -x c++ -fsyntax-only - >/dev/null 2>&1; then
  have_eigen=1
  echo "Eigen:  found"
else
  echo "Eigen:  MISSING (<Eigen/Core> not on the include path)"
fi

echo "== calib_ref (Eigen-free reference LM) =="
# shellcheck disable=SC2086
"$CXX" $CXXFLAGS -o "$here/calib_ref" "$here/calib_ref.cpp"
echo "built $here/calib_ref"

if [ "$have_eigen" -eq 1 ]; then
  echo "== calib_ceres (Ceres prototype) =="
  # shellcheck disable=SC2086
  "$CXX" $CXXFLAGS -o "$here/calib_ceres" "$here/calib_ceres.cpp" -lceres -lglog
  echo "built $here/calib_ceres"
else
  cat >&2 <<'EOF'

calib_ceres was NOT built: Eigen 3 headers are missing.
Ceres 2.2.0 itself is present (/usr/include/ceres, /usr/lib/libceres.so.2.2.0)
but every Ceres header needs Eigen (e.g. ceres/jet.h includes "Eigen/Core").
The Arch package "ceres-solver" on this host does not declare eigen as a
dependency, so the install looks complete while being unusable for compilation.

Fix (needs network + root, then re-run ./build.sh):
    sudo pacman -S eigen        # currently offers eigen 5.0.1-2
Ceres 2.2 was released against Eigen 3.3/3.4; validate Eigen 5.0 compatibility,
or install Eigen 3.4 headers and point CXXFLAGS at them with -I.

Exact error today:
    /usr/include/ceres/internal/fixed_array.h:33:10:
    fatal error: Eigen/Core: No such file or directory
EOF
  exit 0
fi
