# Dependency edges used to resolve a minimal transitive package closure.
# Keep these in sync with the DEP_<package>_DEPENDS declarations in +*/<package>.cmake.

set(DEP_Blosc_DEPENDS ZLIB)
set(DEP_Boost_DEPENDS ZLIB)
set(DEP_CGAL_DEPENDS Boost GMP MPFR)
set(DEP_CURL_DEPENDS ZLIB)
if (CMAKE_SYSTEM_NAME STREQUAL "Linux")
    list(APPEND DEP_CURL_DEPENDS OpenSSL)
endif()
set(DEP_JPEG_DEPENDS ZLIB)
set(DEP_LibBGCode_DEPENDS ZLIB Boost heatshrink)
set(DEP_MPFR_DEPENDS GMP)
set(DEP_OpenCSG_DEPENDS GLEW ZLIB)
set(DEP_OpenEXR_DEPENDS ZLIB)
set(DEP_OpenVDB_DEPENDS TBB Blosc OpenEXR Boost)
set(DEP_PNG_DEPENDS ZLIB)
set(DEP_wxWidgets_DEPENDS ZLIB PNG EXPAT JPEG NanoSVG)
