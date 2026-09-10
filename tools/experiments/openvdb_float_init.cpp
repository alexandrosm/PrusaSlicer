// Experimental replacement for the pinned OpenVDB 8.2 initialization unit.
// Copyright Contributors to the OpenVDB Project
// SPDX-License-Identifier: MPL-2.0
// Derived from openvdb/openvdb/openvdb.cc. Not enabled in production builds.
// Keep metadata, transforms, compression and logging unchanged. Only omit
// registration of non-FloatGrid grids and the separate point subsystem.
#include <openvdb/openvdb.h>
#include <openvdb/io/DelayedLoadMetadata.h>
#include <openvdb/util/logging.h>
#include <atomic>
#include <mutex>
#ifdef OPENVDB_USE_BLOSC
#include <blosc.h>
#endif

namespace openvdb {
OPENVDB_USE_VERSION_NAMESPACE
namespace OPENVDB_VERSION_NAME {
namespace {
std::mutex sInitMutex;
std::atomic<bool> sIsInitialized{false};
}

void initialize()
{
    if (sIsInitialized.load(std::memory_order_acquire)) return;
    std::lock_guard<std::mutex> lock(sInitMutex);
    if (sIsInitialized.load(std::memory_order_acquire)) return;
    logging::initialize();
    Metadata::clearRegistry();
    BoolMetadata::registerType();
    DoubleMetadata::registerType();
    FloatMetadata::registerType();
    Int32Metadata::registerType();
    Int64Metadata::registerType();
    StringMetadata::registerType();
    Vec2IMetadata::registerType();
    Vec2SMetadata::registerType();
    Vec2DMetadata::registerType();
    Vec3IMetadata::registerType();
    Vec3SMetadata::registerType();
    Vec3DMetadata::registerType();
    Vec4IMetadata::registerType();
    Vec4SMetadata::registerType();
    Vec4DMetadata::registerType();
    Mat4SMetadata::registerType();
    Mat4DMetadata::registerType();
    math::MapRegistry::clear();
    math::AffineMap::registerMap();
    math::UnitaryMap::registerMap();
    math::ScaleMap::registerMap();
    math::UniformScaleMap::registerMap();
    math::TranslationMap::registerMap();
    math::ScaleTranslateMap::registerMap();
    math::UniformScaleTranslateMap::registerMap();
    math::NonlinearFrustumMap::registerMap();
    GridBase::clearRegistry();
    FloatGrid::registerGrid();
    io::DelayedLoadMetadata::registerType();
#ifdef OPENVDB_USE_BLOSC
    blosc_init();
    if (blosc_set_compressor("lz4") < 0)
        OPENVDB_LOG_WARN("Blosc LZ4 compressor is unavailable");
#endif
    sIsInitialized.store(true, std::memory_order_release);
}

void uninitialize()
{
    std::lock_guard<std::mutex> lock(sInitMutex);
    sIsInitialized.store(false, std::memory_order_seq_cst);
    Metadata::clearRegistry();
    GridBase::clearRegistry();
    math::MapRegistry::clear();
}
} // namespace OPENVDB_VERSION_NAME
} // namespace openvdb
