// Human-inspectable diagnostics: colour maps, point-cloud preview, JSON report.
#pragma once

#include <string>

#include <opencv2/core.hpp>
#include <opencv2/imgproc.hpp>

#include "einstar/recon/pipeline.hpp"

namespace einstar::recon {

cv::Mat colorize_scalar(const cv::Mat& values, const cv::Mat& valid,
                        double lo, double hi, int colormap = cv::COLORMAP_TURBO);
cv::Mat colorize_confidence(const cv::Mat& confidence);
// Simple z-buffer splat of the cloud from a virtual camera; background black.
cv::Mat render_preview(const PointCloud& cloud, cv::Size size = {900, 700},
                       float yaw_deg = 0, float pitch_deg = 0);
// JSON report with calibration provenance, stage timings and statistics.
void write_report_json(const ReconResult& result, const std::string& path);

}  // namespace einstar::recon
