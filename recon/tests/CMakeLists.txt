add_executable(recon_tests
  main.cpp
  test_profile.cpp
  test_geometry.cpp
  test_pipeline.cpp
  test_stereo.cpp)
target_link_libraries(recon_tests PRIVATE einstar_recon)
add_test(NAME recon_tests COMMAND recon_tests)
