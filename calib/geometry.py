"""Projection, pose, triangulation and epipolar helpers (OpenCV convention).

All camera models follow ``docs/calibration/model.md``:

    X_cam = R @ X_world + T
    x = X/Z, y = Y/Z
    (xd, yd) = distort(x, y, (k1,k2,p1,p2,k3))
    u = fx*xd + skew*yd + cx,  v = fy*yd + cy
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


def rodrigues(rvec: np.ndarray) -> np.ndarray:
    R, _ = cv2.Rodrigues(np.asarray(rvec, float).reshape(3))
    return R


def rvec_from_R(R: np.ndarray) -> np.ndarray:
    return cv2.Rodrigues(np.asarray(R, float))[0].ravel()


def distort(x: np.ndarray, y: np.ndarray, kc: np.ndarray):
    k1, k2, p1, p2, k3 = np.asarray(kc, float).ravel()[:5]
    r2 = x * x + y * y
    radial = 1.0 + k1 * r2 + k2 * r2 * r2 + k3 * r2 * r2 * r2
    xd = x * radial + 2.0 * p1 * x * y + p2 * (r2 + 2.0 * x * x)
    yd = y * radial + p1 * (r2 + 2.0 * y * y) + 2.0 * p2 * x * y
    return xd, yd


def project(K: np.ndarray, kc: np.ndarray, X_cam: np.ndarray) -> np.ndarray:
    X = np.asarray(X_cam, float).reshape(-1, 3)
    z = X[:, 2]
    x = X[:, 0] / z
    y = X[:, 1] / z
    xd, yd = distort(x, y, kc)
    u = K[0, 0] * xd + K[0, 1] * yd + K[0, 2]
    v = K[1, 1] * yd + K[1, 2]
    return np.stack([u, v], axis=1)


def project_world(K, kc, R, T, Xw) -> np.ndarray:
    X = np.asarray(Xw, float).reshape(-1, 3) @ np.asarray(R, float).T + np.asarray(T, float)
    return project(K, kc, X)


@dataclass
class Pose:
    R: np.ndarray
    t: np.ndarray

    @property
    def rvec(self) -> np.ndarray:
        return rvec_from_R(self.R)

    def transform(self, X: np.ndarray) -> np.ndarray:
        return np.asarray(X, float) @ self.R.T + self.t


def solve_pnp(obj: np.ndarray, img: np.ndarray, K: np.ndarray, kc: np.ndarray,
              rvec0=None, tvec0=None) -> Pose | None:
    obj = np.asarray(obj, np.float32).reshape(-1, 3)
    img = np.asarray(img, np.float32).reshape(-1, 2)
    flags = cv2.SOLVEPNP_ITERATIVE
    ok, rvec, tvec = cv2.solvePnP(obj, img, np.asarray(K, np.float64),
                                  np.asarray(kc, np.float64).ravel(),
                                  rvec0, tvec0, False, flags)
    if not ok:
        ok, rvec, tvec = cv2.solvePnP(obj, img, np.asarray(K, np.float64),
                                      np.asarray(kc, np.float64).ravel(), flags=cv2.SOLVEPNP_EPNP)
        if not ok:
            return None
    ok, rvec, tvec = cv2.solvePnP(obj, img, np.asarray(K, np.float64),
                                  np.asarray(kc, np.float64).ravel(),
                                  rvec, tvec, True, cv2.SOLVEPNP_ITERATIVE)
    if not ok:
        return None
    return Pose(rodrigues(rvec), tvec.ravel())


def triangulate(P1: np.ndarray, P2: np.ndarray, x1: np.ndarray, x2: np.ndarray) -> np.ndarray:
    Xh = cv2.triangulatePoints(np.asarray(P1, float), np.asarray(P2, float),
                               np.asarray(x1, float).T, np.asarray(x2, float).T)
    return (Xh[:3] / Xh[3]).T


def projection_matrix(K: np.ndarray, R: np.ndarray, T: np.ndarray) -> np.ndarray:
    return np.asarray(K, float) @ np.hstack([np.asarray(R, float), np.asarray(T, float).reshape(3, 1)])


def relative_pose(R1, T1, R2, T2) -> tuple[np.ndarray, np.ndarray]:
    """Pose of camera 2 expressed in camera 1: X2 = R X1 + t."""
    R = np.asarray(R2, float) @ np.asarray(R1, float).T
    t = np.asarray(T2, float) - R @ np.asarray(T1, float)
    return R, t


def reprojection_errors(K, kc, R, T, Xw, uv) -> np.ndarray:
    pred = project_world(K, kc, R, T, Xw)
    return np.linalg.norm(pred - np.asarray(uv, float), axis=1)


def epipolar_residual(F: np.ndarray, x1: np.ndarray, x2: np.ndarray) -> np.ndarray:
    """Sampson distance (pixels) for correspondences."""
    x1h = np.column_stack([x1, np.ones(len(x1))])
    x2h = np.column_stack([x2, np.ones(len(x2))])
    Fx1 = x1h @ F.T
    Ftx2 = x2h @ F
    num = np.sum(x2h * (x1h @ F.T), axis=1)
    den = Fx1[:, 0] ** 2 + Fx1[:, 1] ** 2 + Ftx2[:, 0] ** 2 + Ftx2[:, 1] ** 2
    return np.abs(num) / np.sqrt(np.maximum(den, 1e-12))


def fundamental(K1, K2, R, t) -> np.ndarray:
    tx = np.array([[0, -t[2], t[1]], [t[2], 0, -t[0]], [-t[1], t[0], 0]], float)
    E = tx @ np.asarray(R, float)
    return np.linalg.inv(K2).T @ E @ np.linalg.inv(K1)
