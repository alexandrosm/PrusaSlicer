// Isolated registration experiment: link this same probe and the actual
// OpenVDBUtils implementation against each initializer in separate executables.
// The runner compares stdout byte-for-byte; no production code is modified.
#include "libslic3r/OpenVDBUtils.hpp"
#include "libslic3r/TriangleMesh.hpp"

#include <oneapi/tbb/global_control.h>

#include <array>
#include <cmath>
#include <cstdint>
#include <iomanip>
#include <iostream>
#include <locale>
#include <stdexcept>
#include <string>

namespace {

using namespace Slic3r;

void require(bool condition, const std::string &message)
{
    if (!condition)
        throw std::runtime_error(message);
}

// Hash individual scalar objects, never Eigen vectors/structures whose padding
// may be uninitialized. Exact output comparison is for the same platform/build.
struct Hash {
    std::uint64_t value = UINT64_C(14695981039346656037);

    template<class T> void add(const T &scalar)
    {
        const auto *bytes = reinterpret_cast<const unsigned char *>(&scalar);
        for (std::size_t i = 0; i < sizeof(T); ++i) {
            value ^= bytes[i];
            value *= UINT64_C(1099511628211);
        }
    }
};

struct Summary {
    std::uint64_t mesh_hash;
    std::uint64_t sample_hash;
};

Summary report(const char *label, const VoxelGrid &grid,
               double iso = 0.0, double adaptivity = 0.0, bool relax = true)
{
    require(!is_grid_empty(grid), std::string(label) + ": empty grid");
    const auto mesh = grid_to_mesh(grid, iso, adaptivity, relax);
    require(!mesh.vertices.empty() && !mesh.indices.empty(),
            std::string(label) + ": empty extracted surface");

    Hash mesh_hash;
    mesh_hash.add(static_cast<std::uint64_t>(mesh.vertices.size()));
    mesh_hash.add(static_cast<std::uint64_t>(mesh.indices.size()));
    double position_magnitude = 0.0;
    for (const auto &vertex : mesh.vertices) {
        for (int axis = 0; axis < 3; ++axis) {
            const float coordinate = vertex[axis];
            require(std::isfinite(coordinate), std::string(label) + ": non-finite vertex");
            mesh_hash.add(coordinate);
            position_magnitude += std::abs(static_cast<double>(coordinate));
        }
    }
    for (const auto &triangle : mesh.indices) {
        for (int corner = 0; corner < 3; ++corner) {
            const auto index = static_cast<std::int32_t>(triangle[corner]);
            require(index >= 0 && static_cast<std::size_t>(index) < mesh.vertices.size(),
                    std::string(label) + ": invalid triangle index");
            mesh_hash.add(index);
        }
    }
    const double volume = std::abs(static_cast<double>(its_volume(mesh)));
    require(std::isfinite(volume) && volume > 1.0,
            std::string(label) + ": invalid or implausibly small enclosed volume");

    const std::array<Vec3f, 8> probes = {{
        Vec3f(0, 0, 0), Vec3f(-2, 0, 0), Vec3f(2, 0, 0), Vec3f(6, 0, 0),
        Vec3f(10, 0, 0), Vec3f(0, 2, 1), Vec3f(0, 0, -6), Vec3f(20, 20, 20)
    }};
    std::array<double, 8> distances;
    Hash sample_hash;
    const float scale = get_voxel_scale(grid);
    require(std::isfinite(scale) && scale > 0.0f,
            std::string(label) + ": missing or invalid scale metadata");
    sample_hash.add(scale);
    reset_accessor(grid);
    for (std::size_t i = 0; i < probes.size(); ++i) {
        distances[i] = get_distance_raw(probes[i], grid);
        require(std::isfinite(distances[i]), std::string(label) + ": non-finite distance");
        sample_hash.add(distances[i]);
    }
    reset_accessor(grid);
    for (std::size_t i = 0; i < probes.size(); ++i)
        require(get_distance_raw(probes[i], grid) == distances[i],
                std::string(label) + ": accessor reset changed a distance");

    std::cout << label << " vertices=" << mesh.vertices.size()
              << " triangles=" << mesh.indices.size()
              << " mesh=" << std::hex << mesh_hash.value
              << " samples=" << sample_hash.value << std::dec
              << " volume=" << volume << " magnitude=" << position_magnitude
              << " scale=" << scale << '\n';
    return {mesh_hash.value, sample_hash.value};
}

indexed_triangle_set shifted(indexed_triangle_set mesh, const Vec3f &offset)
{
    for (auto &vertex : mesh.vertices)
        vertex += offset;
    return mesh;
}

VoxelGridPtr voxelize(const indexed_triangle_set &mesh,
                      const MeshToGridParams &params = MeshToGridParams{})
{
    auto grid = mesh_to_grid(mesh, params);
    require(static_cast<bool>(grid), "mesh_to_grid returned no grid");
    require(!is_grid_empty(*grid), "mesh_to_grid returned an empty grid");
    require(get_voxel_scale(*grid) == params.voxel_scale(), "voxel_scale metadata changed");
    return grid;
}

void sign_at(const VoxelGrid &grid, const Vec3f &point, bool inside, const char *label)
{
    reset_accessor(grid);
    const double distance = get_distance_raw(point, grid);
    require(std::isfinite(distance) && (inside ? distance < 0.0 : distance > 0.0),
            std::string(label) + ": incorrect signed distance");
}

void check_derived(const VoxelGridPtr &derived, const VoxelGrid &source, const char *label)
{
    require(static_cast<bool>(derived), std::string(label) + ": no derived grid");
    require(get_voxel_scale(*derived) == get_voxel_scale(source),
            std::string(label) + ": derived grid lost scale metadata");
    report(label, *derived);
}

void run_probe()
{
    const auto cube = shifted(its_make_cube(8, 8, 8), Vec3f(-4, -4, -4));
    const auto overlap_cube = shifted(cube, Vec3f(4, 0, 0));
    const auto disjoint_cube = shifted(cube, Vec3f(12, 0, 0));
    const auto params = MeshToGridParams{}.voxel_scale(1.5f);

    auto empty = make_voxelgrid<>();
    require(static_cast<bool>(empty) && is_grid_empty(*empty), "default grid is not empty");
    require(get_voxel_scale(*empty) == 1.0f, "missing scale metadata fallback changed");
    std::cout << "empty-grid=ok metadata-fallback=1\n";

    auto base = voxelize(cube, params);
    sign_at(*base, Vec3f(0, 0, 0), true, "cube interior");
    sign_at(*base, Vec3f(10, 0, 0), false, "cube exterior");
    const auto base_summary = report("cube", *base);
    report("cube-adaptive-offset", *base, 0.5, 0.2, false);

    auto sphere = voxelize(its_make_sphere(5.0, 3.14159265358979323846 / 8.0), params);
    sign_at(*sphere, Vec3f(0, 0, 0), true, "sphere interior");
    report("sphere", *sphere);

    Transform3f transform = Transform3f::Identity();
    transform.linear() << 0.0f, -1.5f, 0.0f,
                          0.75f, 0.0f, 0.0f,
                          0.0f, 0.0f, 1.25f;
    transform.translation() = Vec3f(2.0f, -1.0f, 0.5f);
    auto transformed = voxelize(cube, MeshToGridParams{params}.trafo(transform));
    sign_at(*transformed, transform.translation(), true, "transformed cube interior");
    report("transformed", *transformed);

    auto merged = cube;
    its_merge(merged, overlap_cube);
    auto overlap = voxelize(merged, params);
    sign_at(*overlap, Vec3f(6, 0, 0), true, "overlapping mesh union");
    report("multipart-overlap", *overlap);
    merged = cube;
    its_merge(merged, disjoint_cube);
    auto disjoint = voxelize(merged, params);
    sign_at(*disjoint, Vec3f(12, 0, 0), true, "disjoint second solid");
    sign_at(*disjoint, Vec3f(6, 0, 0), false, "disjoint gap");
    report("multipart-disjoint", *disjoint);

    auto unchanged = dilate_grid(*base, 0.0f, 0.0f);
    require(static_cast<bool>(unchanged), "zero dilation returned no grid");
    const auto unchanged_summary = report("dilate-zero", *unchanged);
    require(base_summary.mesh_hash == unchanged_summary.mesh_hash &&
            base_summary.sample_hash == unchanged_summary.sample_hash,
            "zero dilation changed the grid");
    check_derived(dilate_grid(*base, 4.0f, 4.0f), *base, "dilate-both");
    check_derived(dilate_grid(*base, 0.0f, 2.0f), *base, "dilate-interior");
    check_derived(dilate_grid(*base, 2.0f, 0.0f), *base, "dilate-exterior");
    check_derived(redistance_grid(*base, 0.0f), *base, "redistance-default");
    check_derived(redistance_grid(*base, -0.75f, 2.0f, 3.0f), *base, "redistance-offset");

    // CSG consumes/modifies its arguments: each operation gets independent
    // voxelizations rather than depending on clone() or a previous CSG result.
    for (int operation = 0; operation < 3; ++operation) {
        auto first = voxelize(cube, params);
        auto second = voxelize(overlap_cube, params);
        const char *label = nullptr;
        if (operation == 0) {
            grid_union(*first, *second);
            label = "csg-union";
        } else if (operation == 1) {
            grid_difference(*first, *second);
            label = "csg-difference";
        } else {
            grid_intersection(*first, *second);
            label = "csg-intersection";
        }
        sign_at(*first, Vec3f(-2, 0, 0), operation != 2, label);
        sign_at(*first, Vec3f(2, 0, 0), operation != 1, label);
        sign_at(*first, Vec3f(6, 0, 0), operation == 0, label);
        require(get_voxel_scale(*first) == params.voxel_scale(), "CSG lost scale metadata");
        report(label, *first);
    }

    auto rescaled = voxelize(cube, params);
    rescale_grid(*rescaled, 2.0f);
    sign_at(*rescaled, Vec3f(6, 0, 0), true, "rescaled interior");
    report("rescaled", *rescaled);

    int cancellation_calls = 0;
    auto cancelled = mesh_to_grid(cube, MeshToGridParams{params}.statusfn(
        [&cancellation_calls](int) { ++cancellation_calls; return true; }));
    require(!cancelled && cancellation_calls > 0, "cancellation was not honored");
    std::cout << "cancellation=ok\n";
}

} // namespace

int main()
{
    try {
        oneapi::tbb::global_control limit(oneapi::tbb::global_control::max_allowed_parallelism, 1);
        std::cout.imbue(std::locale::classic());
        std::cout << std::setprecision(17);
        run_probe();
        std::cout << "PASS\n";
        return 0;
    } catch (const std::exception &error) {
        std::cerr << "FAIL: " << error.what() << '\n';
        return 1;
    } catch (...) {
        std::cerr << "FAIL: unknown exception\n";
        return 1;
    }
}
