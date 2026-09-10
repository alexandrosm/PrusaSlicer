
set(_context_abi_line "")
set(_context_arch_line "")
if (APPLE AND CMAKE_OSX_ARCHITECTURES)
    if (CMAKE_OSX_ARCHITECTURES MATCHES "x86")
        set(_context_abi_line "-DBOOST_CONTEXT_ABI:STRING=sysv")
    elseif (CMAKE_OSX_ARCHITECTURES MATCHES "arm")
        set (_context_abi_line "-DBOOST_CONTEXT_ABI:STRING=aapcs")
    endif ()
    set(_context_arch_line "-DBOOST_CONTEXT_ARCHITECTURE:STRING=${CMAKE_OSX_ARCHITECTURES}")
endif ()

# Keep the default dependency bundle unchanged. The fast profiles can omit
# compiled modules that are outside PrusaSlicer's link/component closure and
# whose headers are not consumed by the application or selected dependencies.
set(_boost_excluded_libraries contract fiber numpy stacktrace wave test)
if (PrusaSlicer_deps_LEAN_BOOST)
    list(APPEND _boost_excluded_libraries
        json
        poly_collection
        program_options
        timer
        type_erasure
        url
    )
endif ()
list(JOIN _boost_excluded_libraries "|" _boost_excluded_libraries_arg)

add_cmake_project(Boost
    URL "https://github.com/boostorg/boost/releases/download/boost-1.83.0/boost-1.83.0.zip"
    URL_HASH SHA256=9effa3d7f9d92b8e33e2b41d82f4358f97ff7c588d5918720339f2b254d914c6
    LIST_SEPARATOR |
    CMAKE_ARGS
        -DBOOST_EXCLUDE_LIBRARIES:STRING=${_boost_excluded_libraries_arg}
        -DBOOST_LOCALE_ENABLE_ICU:BOOL=OFF # do not link to libicu, breaks compatibility between distros
        -DBUILD_TESTING:BOOL=OFF
        -DBOOST_IOSTREAMS_ENABLE_ZSTD:BOOL=OFF
        "${_context_abi_line}"
        "${_context_arch_line}"
)
