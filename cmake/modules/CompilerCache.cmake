# Optional compiler-cache integration shared by the application and dependency builds.
#
# SLIC3R_COMPILER_CACHE accepts:
#   auto  - use sccache or ccache when one is available (default)
#   off   - do not configure a compiler launcher
#   <path or program name> - require a specific launcher

set(SLIC3R_COMPILER_CACHE "auto" CACHE STRING
    "Compiler cache launcher: auto, off, or an executable name/path")
set_property(CACHE SLIC3R_COMPILER_CACHE PROPERTY STRINGS auto off sccache ccache)

function(slic3r_enable_compiler_cache)
    # Launchers written by an earlier invocation of this module must not turn a
    # later `off` or explicit launcher selection into a no-op. Preserve values
    # that do not match our marker because those were changed by the user.
    if (SLIC3R_COMPILER_CACHE_MANAGED)
        if ("${CMAKE_C_COMPILER_LAUNCHER}" STREQUAL "${SLIC3R_COMPILER_CACHE_MANAGED}")
            unset(CMAKE_C_COMPILER_LAUNCHER CACHE)
        endif()
        if ("${CMAKE_CXX_COMPILER_LAUNCHER}" STREQUAL "${SLIC3R_COMPILER_CACHE_MANAGED}")
            unset(CMAKE_CXX_COMPILER_LAUNCHER CACHE)
        endif()
        unset(SLIC3R_COMPILER_CACHE_MANAGED CACHE)
    endif()

    if (CMAKE_C_COMPILER_LAUNCHER OR CMAKE_CXX_COMPILER_LAUNCHER)
        message(STATUS "Compiler launcher already configured; keeping the user-provided value")
        return()
    endif()

    string(TOLOWER "${SLIC3R_COMPILER_CACHE}" _cache_mode)
    if (_cache_mode STREQUAL "" OR _cache_mode STREQUAL "off" OR _cache_mode STREQUAL "none")
        return()
    endif()

    # find_program() persists its result in CMake's cache. Clear both scopes so
    # changing from auto/sccache to ccache cannot reuse an earlier launcher.
    unset(_cache_program)
    unset(_cache_program CACHE)
    if (_cache_mode STREQUAL "auto")
        find_program(_cache_program NAMES sccache ccache)
    elseif (IS_ABSOLUTE "${SLIC3R_COMPILER_CACHE}")
        if (EXISTS "${SLIC3R_COMPILER_CACHE}")
            set(_cache_program "${SLIC3R_COMPILER_CACHE}")
        endif()
    else()
        find_program(_cache_program NAMES "${SLIC3R_COMPILER_CACHE}")
    endif()

    if (NOT _cache_program)
        if (NOT _cache_mode STREQUAL "auto")
            message(FATAL_ERROR
                "Requested compiler cache '${SLIC3R_COMPILER_CACHE}' was not found")
        endif()
        message(STATUS "Compiler cache: not found (continuing without one)")
        return()
    endif()

    # Compiler launchers are effective with command-oriented generators. Visual
    # Studio and Xcode invoke the compiler through their native build systems.
    if (CMAKE_GENERATOR MATCHES "Visual Studio|Xcode")
        message(STATUS
            "Compiler cache '${_cache_program}' found but unsupported by ${CMAKE_GENERATOR}; skipping")
        return()
    endif()

    set(CMAKE_C_COMPILER_LAUNCHER "${_cache_program}" CACHE FILEPATH
        "Launcher for C compilation" FORCE)
    set(CMAKE_CXX_COMPILER_LAUNCHER "${_cache_program}" CACHE FILEPATH
        "Launcher for C++ compilation" FORCE)
    set(SLIC3R_COMPILER_CACHE_MANAGED "${_cache_program}" CACHE INTERNAL
        "Compiler launcher managed by PrusaSlicer's cache integration" FORCE)
    message(STATUS "Compiler cache: ${_cache_program}")
endfunction()
