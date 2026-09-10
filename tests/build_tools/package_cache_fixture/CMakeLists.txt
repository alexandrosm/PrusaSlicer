cmake_minimum_required(VERSION 3.19)
project(PackageCacheFixture NONE)
include(ExternalProject)
set(MSVC TRUE) # Exercise cache contract only; this fixture has no compiler.
set(CMAKE_BUILD_TYPE Release)
set(DEP_DEBUG OFF)
set(FOUND_PACKAGES A B C)
set(DEP_B_DEPENDS A)
include("${CACHE_MODULE}")
prusaslicer_load_package_cache()
foreach(_package A B C)
  if(_package IN_LIST PRUSASLICER_CACHED_PACKAGES)
    prusaslicer_add_cached_package(${_package})
  else()
    ExternalProject_Add(dep_${_package}
      SOURCE_DIR "${CMAKE_CURRENT_LIST_DIR}/package"
      BINARY_DIR "${CMAKE_CURRENT_BINARY_DIR}/builds/${_package}"
      CMAKE_ARGS "-DPACKAGE_NAME=${_package}" "-DBUILD_MARKER=${BUILD_MARKER}"
        "-DCMAKE_INSTALL_PREFIX=${PrusaSlicer_deps_DEP_INSTALL_PREFIX}"
        "-DCMAKE_MAKE_PROGRAM=${CMAKE_MAKE_PROGRAM}"
      BUILD_COMMAND "${CMAKE_COMMAND}" --build . --parallel 1
      INSTALL_COMMAND "${CMAKE_COMMAND}" --install .)
  endif()
endforeach()
add_dependencies(dep_B dep_A)
add_custom_target(deps DEPENDS dep_A dep_B dep_C)
