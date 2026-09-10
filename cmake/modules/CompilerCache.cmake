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

# Call after compiler/launcher detection and before targets. A macro deliberately
# sets normal variables in the caller: policy/format choices must initialize its
# targets, not vanish inside a function. Never remove symbolic debugging merely
# to make a compile cacheable. /Z7 embeds symbols in each object, allowing the
# existing /DEBUG link to construct a complete final PDB without a shared
# compiler PDB that would make sccache miss.
macro(slic3r_configure_msvc_debug_information)
    set(SLIC3R_MSVC_DEBUG_FORMAT_MANAGED FALSE)
    set(SLIC3R_MSVC_DEBUG_FLAG /Zi)
    if (MSVC)
        set(_slic3r_cache_embedded FALSE)
        foreach(_slic3r_launcher IN ITEMS "${CMAKE_C_COMPILER_LAUNCHER}" "${CMAKE_CXX_COMPILER_LAUNCHER}")
            get_filename_component(_slic3r_launcher_name "${_slic3r_launcher}" NAME)
            if (_slic3r_launcher_name MATCHES "^[sS][cC][cC][aA][cC][hH][eE](\\.exe)?$")
                set(_slic3r_cache_embedded TRUE)
            endif()
        endforeach()
        set(_slic3r_debug_format ProgramDatabase)
        if (_slic3r_cache_embedded)
            set(_slic3r_debug_format Embedded)
            set(SLIC3R_MSVC_DEBUG_FLAG /Z7)
            # Also repair cached flags from an existing pre-CMP0141 tree and
            # provide the CMake <=3.24 fallback. Match standalone switches only,
            # never path substrings. Preserve all unrelated flags and symbols.
            foreach(_slic3r_language C CXX)
                foreach(_slic3r_suffix "" _DEBUG _RELEASE _RELWITHDEBINFO _MINSIZEREL)
                    set(_slic3r_flag_var "CMAKE_${_slic3r_language}_FLAGS${_slic3r_suffix}")
                    while ("${${_slic3r_flag_var}}" MATCHES "(^|[ \t])[-/]Z[iI]([ \t]|$)")
                        string(REGEX REPLACE "(^|[ \t])[-/]Z[iI]([ \t]|$)" "\\1/Z7\\2"
                            ${_slic3r_flag_var} "${${_slic3r_flag_var}}")
                    endwhile()
                endforeach()
            endforeach()
        endif()
        if (POLICY CMP0141)
            cmake_policy(GET CMP0141 _slic3r_debug_policy)
            if (_slic3r_debug_policy STREQUAL "NEW")
                set(SLIC3R_MSVC_DEBUG_FORMAT_MANAGED TRUE)
                if (NOT DEFINED CMAKE_MSVC_DEBUG_INFORMATION_FORMAT)
                    if (SLIC3R_MSVC_DEBUG_SYMBOLS)
                        set(CMAKE_MSVC_DEBUG_INFORMATION_FORMAT "${_slic3r_debug_format}")
                    else()
                        set(CMAKE_MSVC_DEBUG_INFORMATION_FORMAT "$<$<CONFIG:Debug,RelWithDebInfo>:${_slic3r_debug_format}>")
                    endif()
                elseif (_slic3r_cache_embedded AND CMAKE_MSVC_DEBUG_INFORMATION_FORMAT MATCHES "ProgramDatabase|EditAndContinue")
                    message(WARNING "Explicit MSVC debug information format is incompatible with reliable sccache hits; use Embedded to retain cacheable symbols")
                endif()
            endif()
        endif()
        if (_slic3r_cache_embedded)
            message(STATUS "MSVC sccache: embedded object symbols (/Z7); linker PDB options retained")
        endif()
    endif()
endmacro()
