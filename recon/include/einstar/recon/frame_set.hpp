// Frame sets: one synchronised three-camera observation plus provenance.
//
// The USB stream order is fixed by the hardware and is NOT the calibration
// order: stream 0 carries the right IR camera rotated by 180 degrees, stream 1
// the left IR camera as calibrated, stream 2 the colour camera rotated by 180
// degrees.  calibration_image() applies that mapping exactly once; every
// consumer works in calibration orientation afterwards.
#pragma once

#include <array>
#include <cstdint>
#include <string>
#include <vector>

#include <opencv2/core.hpp>

#include "einstar/recon/profile.hpp"

namespace einstar::recon {

struct Frame {
  int stream = -1;                  // 0,1,2 (USB order)
  int width = 0, height = 0;
  cv::Mat mono;                     // CV_8UC1, raw stream orientation
  std::int64_t host_time_ns = 0;
  std::uint32_t frame_id = 0;       // device frame id, valid on the group frame
  std::string source_path;
};

struct FrameSet {
  std::array<Frame, 3> streams{};
  std::uint32_t group = 0;          // frame id of the synchronised group
  std::string source;               // directory or recording id
  bool complete() const;
  std::string summary() const;
};

inline constexpr int kStreamRightIr = 0;
inline constexpr int kStreamLeftIr = 1;
inline constexpr int kStreamColor = 2;

// Stream that carries a calibration camera, and whether it needs a 180 deg
// rotation to reach calibration orientation (hardware-observed).
int stream_for(Camera c);
bool stream_needs_rotation(int stream);
cv::Mat calibration_image(const Frame& f);   // returns rotated image if needed
cv::Mat calibration_image(const FrameSet& fs, Camera c);

// Loads frame_<id:06d>_cam{0,1,2}.{pgm,png,jpg} from `directory`.  Throws
// std::runtime_error when a camera is missing or the sizes disagree.
FrameSet load_frame_set(const std::string& directory, std::uint32_t id);
// Loads an explicit manifest {"group":N,"stream0":"path",...} (JSON).
FrameSet load_frame_set_manifest(const std::string& path);
// Lists the group ids that have all three streams in a directory.
std::vector<std::uint32_t> list_frame_groups(const std::string& directory);

}  // namespace einstar::recon
