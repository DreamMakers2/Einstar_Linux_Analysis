cmake_minimum_required(VERSION 3.22)
project(einstar_driver VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

option(EINSTAR_WITH_LIBUSB "Build the libusb transport when libusb-1.0 is available" ON)
option(EINSTAR_BUILD_TOOLS "Build command line tools" ON)
option(EINSTAR_BUILD_TESTS "Build offline tests" ON)
option(EINSTAR_WERROR "Treat compiler warnings as errors" ON)
option(EINSTAR_SANITIZE "Enable AddressSanitizer + UndefinedBehaviorSanitizer" OFF)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE RelWithDebInfo CACHE STRING "Build type" FORCE)
endif()

find_package(Threads REQUIRED)

# ---------------------------------------------------------------------------
# Optional libusb-1.0.  Never faked: if it is absent the simulated transport is
# built alone and EINSTAR_WITH_LIBUSB is reported as OFF.
# ---------------------------------------------------------------------------
set(EINSTAR_LIBUSB_AVAILABLE OFF)
if(EINSTAR_WITH_LIBUSB)
  find_package(PkgConfig QUIET)
  if(PkgConfig_FOUND)
    pkg_check_modules(LIBUSB QUIET IMPORTED_TARGET libusb-1.0)
  endif()
  if(LIBUSB_FOUND)
    set(EINSTAR_LIBUSB_AVAILABLE ON)
  else()
    find_path(LIBUSB_INCLUDE_DIR NAMES libusb.h PATH_SUFFIXES libusb-1.0)
    find_library(LIBUSB_LIBRARY NAMES usb-1.0 libusb-1.0)
    if(LIBUSB_INCLUDE_DIR AND LIBUSB_LIBRARY)
      set(EINSTAR_LIBUSB_AVAILABLE ON)
      add_library(einstar_libusb_external UNKNOWN IMPORTED)
      set_target_properties(einstar_libusb_external PROPERTIES
        IMPORTED_LOCATION "${LIBUSB_LIBRARY}"
        INTERFACE_INCLUDE_DIRECTORIES "${LIBUSB_INCLUDE_DIR}")
    endif()
  endif()
endif()

if(NOT EINSTAR_LIBUSB_AVAILABLE)
  message(STATUS "libusb-1.0 not found: building the simulated transport only")
else()
  message(STATUS "libusb-1.0 found: building LibUsbTransport")
endif()

set(EINSTAR_WARNING_FLAGS -Wall -Wextra -Wpedantic -Wshadow -Wnon-virtual-dtor -Wold-style-cast)
if(EINSTAR_WERROR)
  list(APPEND EINSTAR_WARNING_FLAGS -Werror)
endif()

set(EINSTAR_SOURCES
  src/status.cpp
  src/clock.cpp
  src/usb_descriptor.cpp
  src/frame.cpp
  src/queue.cpp
  src/stats.cpp
  src/reassembler.cpp
  src/recorder.cpp
  src/sim.cpp
  src/control_channel.cpp
  src/pipeline.cpp
)

if(EINSTAR_LIBUSB_AVAILABLE)
  list(APPEND EINSTAR_SOURCES src/libusb_transport.cpp)
endif()

add_library(einstar_core STATIC ${EINSTAR_SOURCES})
target_include_directories(einstar_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_link_libraries(einstar_core PUBLIC Threads::Threads)
target_compile_options(einstar_core PRIVATE ${EINSTAR_WARNING_FLAGS})
if(EINSTAR_LIBUSB_AVAILABLE)
  target_compile_definitions(einstar_core PUBLIC EINSTAR_WITH_LIBUSB=1)
  if(TARGET PkgConfig::LIBUSB)
    target_link_libraries(einstar_core PUBLIC PkgConfig::LIBUSB)
  else()
    target_link_libraries(einstar_core PUBLIC einstar_libusb_external)
  endif()
endif()

if(EINSTAR_SANITIZE)
  foreach(t einstar_core)
    target_compile_options(${t} PUBLIC -fsanitize=address,undefined -fno-omit-frame-pointer)
    target_link_options(${t} PUBLIC -fsanitize=address,undefined)
  endforeach()
endif()

# C ABI shared library for the Rust control plane / Qt GUI / engines.
add_library(einstar_ffi SHARED src/ffi.cpp)
target_link_libraries(einstar_ffi PRIVATE einstar_core)
target_include_directories(einstar_ffi PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_compile_options(einstar_ffi PRIVATE ${EINSTAR_WARNING_FLAGS})
set_target_properties(einstar_ffi PROPERTIES OUTPUT_NAME einstar)
if(EINSTAR_SANITIZE)
  target_compile_options(einstar_ffi PRIVATE -fsanitize=address,undefined -fno-omit-frame-pointer)
  target_link_options(einstar_ffi PRIVATE -fsanitize=address,undefined)
endif()

if(EINSTAR_BUILD_TOOLS OR EINSTAR_BUILD_TESTS)
  # Shared capture/replay tooling: PGM triplet + manifest output, recording
  # verification, simulated capture and replay.  Used by both new tools and by
  # test_replay_frames, so the tested code is exactly the code the tools run.
  add_library(einstar_capture_core STATIC tools/capture_core.cpp)
  target_link_libraries(einstar_capture_core PUBLIC einstar_core)
  target_include_directories(einstar_capture_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/tools)
  target_compile_options(einstar_capture_core PRIVATE ${EINSTAR_WARNING_FLAGS})
endif()

if(EINSTAR_BUILD_TOOLS)
  add_executable(einstar-record-replay tools/record_replay.cpp)
  target_link_libraries(einstar-record-replay PRIVATE einstar_core)
  target_compile_options(einstar-record-replay PRIVATE ${EINSTAR_WARNING_FLAGS})

  add_executable(einstar-bench tools/bench_acq.cpp)
  target_link_libraries(einstar-bench PRIVATE einstar_core)
  target_compile_options(einstar-bench PRIVATE ${EINSTAR_WARNING_FLAGS})

  add_executable(einstar-replay-frames tools/replay_frames.cpp)
  target_link_libraries(einstar-replay-frames PRIVATE einstar_capture_core)
  target_compile_options(einstar-replay-frames PRIVATE ${EINSTAR_WARNING_FLAGS})

  # Memory-bounded streaming replay (verification, range extraction, --pipe).
  add_executable(einstar-stream-frames tools/stream_frames.cpp)
  target_link_libraries(einstar-stream-frames PRIVATE einstar_capture_core)
  target_compile_options(einstar-stream-frames PRIVATE ${EINSTAR_WARNING_FLAGS})

  add_executable(einstar-capture-frames tools/capture_frames.cpp)
  target_link_libraries(einstar-capture-frames PRIVATE einstar_capture_core)
  target_compile_options(einstar-capture-frames PRIVATE ${EINSTAR_WARNING_FLAGS})
endif()

if(EINSTAR_BUILD_TESTS)
  enable_testing()

  set(EINSTAR_TESTS
    test_conformance
    test_fuzz
    test_packet
    test_reassembler
    test_queue
    test_transport_sim
    test_pipeline
    test_record_replay
    test_descriptor_ffi
    test_control_channel
    test_replay_frames
    test_stream_recording
    test_stress
    test_capture_session
  )

  foreach(test_name ${EINSTAR_TESTS})
    add_executable(${test_name} tests/${test_name}.cpp)
    target_link_libraries(${test_name} PRIVATE einstar_core)
    target_include_directories(${test_name} PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/tests)
    target_compile_options(${test_name} PRIVATE ${EINSTAR_WARNING_FLAGS})
    if(EINSTAR_SANITIZE)
      target_compile_options(${test_name} PRIVATE -fsanitize=address,undefined -fno-omit-frame-pointer)
      target_link_options(${test_name} PRIVATE -fsanitize=address,undefined)
    endif()
    add_test(NAME ${test_name} COMMAND ${test_name})
    set_tests_properties(${test_name} PROPERTIES TIMEOUT 600)
  endforeach()

  target_link_libraries(test_descriptor_ffi PRIVATE einstar_ffi)
  target_link_libraries(test_replay_frames PRIVATE einstar_capture_core)
  target_link_libraries(test_stream_recording PRIVATE einstar_capture_core)
  target_link_libraries(test_capture_session PRIVATE einstar_capture_core)
endif()

include(GNUInstallDirs)
install(TARGETS einstar_core einstar_ffi
        ARCHIVE DESTINATION ${CMAKE_INSTALL_LIBDIR}
        LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR}
        RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})
install(DIRECTORY include/ DESTINATION ${CMAKE_INSTALL_INCLUDEDIR})
