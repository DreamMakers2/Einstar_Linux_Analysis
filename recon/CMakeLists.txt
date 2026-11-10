cmake_minimum_required(VERSION 3.20)
project(einstar_recon VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

find_package(OpenCV 5 REQUIRED)

add_library(einstar_recon STATIC
  src/hash.cpp
  src/profile.cpp
  src/frame_set.cpp
  src/rectify.cpp
  src/stereo.cpp
  src/depth.cpp
  src/pointcloud.cpp
  src/diagnostics.cpp
  src/pipeline.cpp
  src/synthetic.cpp)
target_include_directories(einstar_recon PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>)
target_link_libraries(einstar_recon PUBLIC ${OpenCV_LIBS})
target_compile_options(einstar_recon PRIVATE -Wall -Wextra -Wpedantic)

add_executable(recon_cli tools/recon_cli.cpp)
target_link_libraries(recon_cli PRIVATE einstar_recon)

enable_testing()
add_subdirectory(tests)
