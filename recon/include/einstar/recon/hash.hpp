// Small, dependency-free SHA-256 used for provenance and determinism checks.
#pragma once
#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

namespace einstar::recon {
std::string sha256_hex(std::string_view bytes);
std::string sha256_file(const std::string& path);
}  // namespace einstar::recon
