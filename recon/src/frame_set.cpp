#include "einstar/recon/frame_set.hpp"

#include <algorithm>
#include <cctype>
#include <filesystem>
#include <fstream>
#include <regex>
#include <sstream>
#include <stdexcept>

#include <opencv2/imgcodecs.hpp>
#include <opencv2/imgproc.hpp>

#include "einstar/recon/hash.hpp"

namespace fs = std::filesystem;

namespace einstar::recon {
namespace {

// Minimal binary PGM (P5) reader: comments and arbitrary whitespace in the
// header, 8-bit samples.  Throws with the file name on malformed input.
cv::Mat read_pgm(const std::string& path) {
  std::ifstream f(path, std::ios::binary);
  if (!f) throw std::runtime_error("cannot open " + path);
  std::string magic;
  f >> magic;
  if (magic != "P5") throw std::runtime_error(path + ": not a binary PGM (P5)");
  auto next_int = [&]() {
    for (;;) {
      int c = f.peek();
      if (c == '#') {
        std::string line;
        std::getline(f, line);
      } else if (std::isspace(c)) {
        f.get();
      } else {
        break;
      }
    }
    int v = -1;
    f >> v;
    if (!f) throw std::runtime_error(path + ": malformed PGM header");
    return v;
  };
  const int w = next_int(), h = next_int(), maxv = next_int();
  if (w <= 0 || h <= 0 || maxv <= 0 || maxv > 255)
    throw std::runtime_error(path + ": unsupported PGM dimensions/maxval");
  f.get();  // single whitespace after maxval
  cv::Mat img(h, w, CV_8UC1);
  f.read(reinterpret_cast<char*>(img.data), static_cast<std::streamsize>(w) * h);
  if (!f) throw std::runtime_error(path + ": truncated PGM payload");
  return img;
}

cv::Mat read_image_any(const std::string& path) {
  const std::string ext = fs::path(path).extension().string();
  if (ext == ".pgm" || ext == ".PGM") return read_pgm(path);
  cv::Mat img = cv::imread(path, cv::IMREAD_UNCHANGED);
  if (img.empty()) throw std::runtime_error("cannot read image: " + path);
  if (img.channels() == 3) cv::cvtColor(img, img, cv::COLOR_BGR2GRAY);
  if (img.depth() != CV_8U) {
    cv::Mat out;
    img.convertTo(out, CV_8U, 255.0 / 65535.0);
    img = out;
  }
  return img;
}

std::string find_stream_file(const std::string& dir, std::uint32_t id, int stream) {
  static const char* exts[] = {".pgm", ".png", ".jpg", ".jpeg", ".tif", ".tiff"};
  char base[64];
  std::snprintf(base, sizeof base, "frame_%06u_cam%d", id, stream);
  for (const char* e : exts) {
    fs::path p = fs::path(dir) / (std::string(base) + e);
    if (fs::exists(p)) return p.string();
  }
  return {};
}
}  // namespace

bool FrameSet::complete() const {
  return std::all_of(streams.begin(), streams.end(),
                     [](const Frame& f) { return f.stream >= 0 && !f.mono.empty(); });
}

std::string FrameSet::summary() const {
  std::ostringstream o;
  o << "group=" << group << " source=" << source;
  for (const auto& f : streams)
    o << " [s" << f.stream << " " << f.width << "x" << f.height
      << (f.mono.empty() ? " missing" : "") << "]";
  return o.str();
}

int stream_for(Camera c) {
  switch (c) {
    case Camera::IrLeft: return kStreamLeftIr;
    case Camera::IrRight: return kStreamRightIr;
    case Camera::Color: return kStreamColor;
  }
  return -1;
}
bool stream_needs_rotation(int stream) { return stream == kStreamRightIr || stream == kStreamColor; }

cv::Mat calibration_image(const Frame& f) {
  if (f.mono.empty()) return {};
  if (!stream_needs_rotation(f.stream)) return f.mono;
  cv::Mat out;
  cv::rotate(f.mono, out, cv::ROTATE_180);
  return out;
}
cv::Mat calibration_image(const FrameSet& fs, Camera c) {
  const int s = stream_for(c);
  for (const auto& f : fs.streams)
    if (f.stream == s) return calibration_image(f);
  return {};
}

FrameSet load_frame_set(const std::string& directory, std::uint32_t id) {
  FrameSet out;
  out.group = id;
  out.source = directory;
  for (int s = 0; s < 3; ++s) {
    const std::string path = find_stream_file(directory, id, s);
    if (path.empty())
      throw std::runtime_error("frame group " + std::to_string(id) + ": missing stream " +
                               std::to_string(s) + " in " + directory);
    Frame f;
    f.stream = s;
    f.frame_id = id;
    f.source_path = path;
    f.mono = read_image_any(path);
    f.height = f.mono.rows;
    f.width = f.mono.cols;
    out.streams[s] = std::move(f);
  }
  if (!(out.streams[0].width == out.streams[1].width &&
        out.streams[0].width == out.streams[2].width &&
        out.streams[0].height == out.streams[1].height &&
        out.streams[0].height == out.streams[2].height))
    throw std::runtime_error("frame group " + std::to_string(id) + ": stream sizes differ");
  return out;
}

std::vector<std::uint32_t> list_frame_groups(const std::string& directory) {
  std::regex re(R"(frame_(\d{6})_cam([0-2])\.(pgm|png|jpg|jpeg|tif|tiff))",
                std::regex::icase);
  std::map<std::uint32_t, int> have;
  for (const auto& e : fs::directory_iterator(directory)) {
    if (!e.is_regular_file()) continue;
    std::smatch m;
    const std::string name = e.path().filename().string();
    if (std::regex_match(name, m, re)) {
      const auto id = static_cast<std::uint32_t>(std::stoul(m[1].str()));
      have[id] |= 1 << std::stoi(m[2].str());
    }
  }
  std::vector<std::uint32_t> out;
  for (const auto& [id, mask] : have)
    if (mask == 0b111) out.push_back(id);
  return out;
}

FrameSet load_frame_set_manifest(const std::string& path) {
  cv::FileStorage fs(path, cv::FileStorage::READ);
  if (!fs.isOpened()) throw std::runtime_error("cannot open manifest: " + path);
  FrameSet out;
  out.group = static_cast<std::uint32_t>(static_cast<int>(fs["group"]));
  out.source = path;
  for (int s = 0; s < 3; ++s) {
    const std::string key = "stream" + std::to_string(s);
    std::string p;
    fs[key] >> p;
    if (p.empty()) throw std::runtime_error("manifest: missing " + key);
    Frame f;
    f.stream = s;
    f.frame_id = out.group;
    f.source_path = p;
    f.mono = read_image_any(p);
    f.height = f.mono.rows;
    f.width = f.mono.cols;
    out.streams[s] = std::move(f);
  }
  return out;
}
}  // namespace einstar::recon
