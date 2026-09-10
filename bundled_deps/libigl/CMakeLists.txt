# Header-only: no compiler enablement is needed by this wrapper.
cmake_minimum_required(VERSION 3.12...3.12)
project(libigl LANGUAGES NONE)

add_library(libigl INTERFACE)

find_package(libigl QUIET)

if(libigl_FOUND)
    message(STATUS "IGL found, using system version...")
    target_link_libraries(libigl INTERFACE igl::core)
else()
    message(STATUS "IGL NOT found, using bundled version...")
    target_include_directories(libigl SYSTEM BEFORE INTERFACE .)
endif()
