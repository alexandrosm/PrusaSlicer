cmake_minimum_required(VERSION 3.12)

# This lifetime regression can also run without configuring the application or
# downloading its dependencies: cmake -S tests/imgui -B build/imgui-tests
if(CMAKE_SOURCE_DIR STREQUAL CMAKE_CURRENT_SOURCE_DIR)
    project(imgui_atlas_tests LANGUAGES CXX)
    enable_testing()
    add_subdirectory("${CMAKE_CURRENT_LIST_DIR}/../../bundled_deps/imgui" imgui)
endif()

add_executable(imgui_atlas_tests font_atlas_tests.cpp)
target_compile_features(imgui_atlas_tests PRIVATE cxx_std_17)
target_include_directories(imgui_atlas_tests PRIVATE "${CMAKE_CURRENT_LIST_DIR}/../../src")
target_link_libraries(imgui_atlas_tests PRIVATE imgui)
add_test(NAME imgui_atlas_tests COMMAND imgui_atlas_tests "${CMAKE_CURRENT_LIST_DIR}/../../resources/fonts")
