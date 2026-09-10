if(BUILD_SHARED_LIBS)
    set(_build_shared ON)
    set(_build_static OFF)
else()
    set(_build_shared OFF)
    set(_build_static ON)
endif()

add_cmake_project(OpenVDB
    # 8.2 patched
    URL https://github.com/prusa3d/openvdb/archive/339ee88230da33e3fefb133d8c1a9e16bef09144.zip
    URL_HASH SHA256=098c67620a3884b7c09775e5819e88ff09e6c69b09c07695a4301f77f9382664
    
    CMAKE_ARGS
        -DCMAKE_POSITION_INDEPENDENT_CODE=ON 
        -DOPENVDB_BUILD_PYTHON_MODULE=OFF
        -DUSE_BLOSC=ON
        # OpenVDB 8.2 provides its own Half type; external OpenEXR is unused.
        -DUSE_IMATH_HALF:BOOL=OFF
        -DOPENVDB_CORE_SHARED=${_build_shared} 
        -DOPENVDB_CORE_STATIC=${_build_static}
        -DOPENVDB_ENABLE_RPATH:BOOL=OFF
        -DTBB_STATIC=${_build_static}
        -DOPENVDB_BUILD_VDB_PRINT=OFF
        -DDISABLE_DEPENDENCY_VERSION_CHECKS=ON # Centos6 has old zlib
)
