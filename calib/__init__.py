"""Linux-native calibration engine for the SHINING 3D Einstar (EinScan10_01).

The package implements the physical marker-plate calibration that EXStar
performs in ``Sn3DCalibrateAlgorithmWrapper`` / ``Sn3DCalibrationJR``, but
with a robust, uncertainty-aware and adaptive pipeline.

Public modules
--------------
plate       recovered plate specification (8x5 marker grid, 28.0 mm pitch, 4 large markers)
detect      marker detection, sub-pixel refinement and grid assignment
geometry    homographies, poses, triangulation, epipolar checks
calibrate   intrinsic/extrinsic estimation, bundle adjustment, covariance
adaptive    Fisher-information viewpoint selection and stopping rules
profiles    versioned host-side calibration profiles (factory preserved)
workflow    EXStar's five-stage / 25-capture workflow model
"""
__version__ = "0.1.0"
