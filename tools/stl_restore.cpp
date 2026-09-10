// Experimental offline decoder for stl_codec.py. No Python or geometry library.
// All STL bytes are recovered exactly and SHA-256 checked before publication.
// Build: cl /nologo /O2 /MT /EHsc /std:c++17 stl_restore.cpp bcrypt.lib
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <bcrypt.h>
#include <algorithm>
#include <array>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

namespace fs = std::filesystem;
using Bytes = std::vector<unsigned char>;
constexpr size_t max_source = 256u * 1024u * 1024u;

static void require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}
static uint32_t u32(const unsigned char* p) {
    return uint32_t(p[0]) | (uint32_t(p[1]) << 8) | (uint32_t(p[2]) << 16) | (uint32_t(p[3]) << 24);
}
static void put32(unsigned char* p, uint32_t value) {
    for (unsigned i = 0; i < 4; ++i) p[i] = static_cast<unsigned char>(value >> (8 * i));
}
static std::array<unsigned char, 32> sha256(const Bytes& data) {
    BCRYPT_ALG_HANDLE algorithm = nullptr;
    BCRYPT_HASH_HANDLE hash = nullptr;
    require(BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0) >= 0,
            "Cannot initialize SHA-256");
    std::array<unsigned char, 32> digest{};
    NTSTATUS status = BCryptCreateHash(algorithm, &hash, nullptr, 0, nullptr, 0, 0);
    if (status >= 0) status = BCryptHashData(hash, const_cast<PUCHAR>(data.data()), static_cast<ULONG>(data.size()), 0);
    if (status >= 0) status = BCryptFinishHash(hash, digest.data(), static_cast<ULONG>(digest.size()), 0);
    if (hash) BCryptDestroyHash(hash);
    BCryptCloseAlgorithmProvider(algorithm, 0);
    require(status >= 0, "SHA-256 operation failed");
    return digest;
}
static void safe_path(const fs::path& path) {
    for (auto current = fs::absolute(path); !current.empty();) {
        const auto attributes = GetFileAttributesW(current.c_str());
        if (attributes != INVALID_FILE_ATTRIBUTES)
            require(!(attributes & FILE_ATTRIBUTE_REPARSE_POINT), "Link/reparse path rejected");
        else require(GetLastError() == ERROR_FILE_NOT_FOUND || GetLastError() == ERROR_PATH_NOT_FOUND,
                     "Cannot inspect path");
        const auto parent = current.parent_path();
        if (parent == current) break;
        current = parent;
    }
}
static Bytes read_bytes(const fs::path& path) {
    safe_path(path);
    HANDLE file = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING,
                              FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT, nullptr);
    require(file != INVALID_HANDLE_VALUE, "Cannot open input");
    LARGE_INTEGER size{};
    if (!GetFileSizeEx(file, &size) || size.QuadPart < 0 || size.QuadPart > 2 * max_source + 1024) {
        CloseHandle(file); throw std::runtime_error("Input length exceeds decoder limit");
    }
    Bytes data(static_cast<size_t>(size.QuadPart));
    DWORD actual = 0;
    const bool ok = data.empty() || (ReadFile(file, data.data(), static_cast<DWORD>(data.size()), &actual, nullptr) && actual == data.size());
    CloseHandle(file);
    require(ok, "Cannot read complete input");
    return data;
}
static Bytes unshuffle(const Bytes& input, size_t& offset, size_t length, size_t width) {
    require(length % width == 0 && offset <= input.size() && length <= input.size() - offset,
            "Truncated shuffled data");
    const size_t count = length / width;
    Bytes result(length);
    for (size_t byte = 0; byte < width; ++byte)
        for (size_t item = 0; item < count; ++item)
            result[item * width + byte] = input[offset + byte * count + item];
    offset += length;
    return result;
}
static void undo_xor(Bytes& data, size_t stride) {
    std::array<uint32_t, 3> previous{};
    require(data.size() % 4 == 0 && stride > 0 && stride <= previous.size(), "Invalid XOR stream");
    for (size_t i = 0; i < data.size() / 4; ++i) {
        const auto word = u32(data.data() + i * 4) ^ previous[i % stride];
        previous[i % stride] = word;
        put32(data.data() + i * 4, word);
    }
}
static void undo_delta(Bytes& data) {
    int64_t previous = 0;
    for (size_t i = 0; i < data.size() / 4; ++i) {
        const uint32_t word = u32(data.data() + i * 4);
        const int64_t delta = int64_t(word >> 1) ^ -int64_t(word & 1);
        const int64_t decoded = previous + delta;
        require(decoded >= 0 && decoded <= 0xffffffffLL, "Invalid delta index");
        previous = decoded;
        put32(data.data() + i * 4, static_cast<uint32_t>(decoded));
    }
}
static Bytes decode(const Bytes& encoded) {
    require(encoded.size() >= 136, "Truncated codec header");
    require(std::memcmp(encoded.data(), "PSSTL1\0\0", 8) == 0, "Unknown codec magic/version");
    const auto mode = encoded[8];
    require(mode >= 1 && mode <= 4 && encoded[9] == 0 && encoded[10] == 0 && encoded[11] == 0,
            "Unknown mode or reserved flags");
    const uint64_t source_size = u32(encoded.data() + 12) | (uint64_t(u32(encoded.data() + 16)) << 32);
    require(source_size >= 84 && source_size <= max_source && (source_size - 84) % 50 == 0,
            "Invalid reconstructed length");
    const size_t count = u32(encoded.data() + 132);
    require(uint64_t(count) * 50 + 84 == source_size, "Contradictory triangle count");
    Bytes result(encoded.begin() + 52, encoded.begin() + 136);
    result.reserve(static_cast<size_t>(source_size));
    size_t offset = 136;
    if (mode == 1) {
        require(encoded.size() == source_size + 52, "Truncated or trailing plane data");
        auto faces = unshuffle(encoded, offset, count * 50, 50);
        result.insert(result.end(), faces.begin(), faces.end());
    } else {
        require(encoded.size() >= offset + 8, "Truncated dictionary sizes");
        const size_t vertex_count = u32(encoded.data() + offset);
        const size_t normal_count = u32(encoded.data() + offset + 4);
        offset += 8;
        require(vertex_count <= count * 3 && normal_count <= count, "Oversized dictionary");
        require(encoded.size() == offset + vertex_count * 12 + normal_count * 12 + count * 18,
                "Truncated or trailing dictionary data");
        auto vertices = unshuffle(encoded, offset, vertex_count * 12, 12);
        auto normals = unshuffle(encoded, offset, normal_count * 12, 12);
        auto vertex_refs = unshuffle(encoded, offset, count * 12, 4);
        auto normal_refs = unshuffle(encoded, offset, count * 4, 4);
        auto attributes = unshuffle(encoded, offset, count * 2, 2);
        if (mode == 3) {
            undo_xor(vertices, 3); undo_xor(normals, 3);
            undo_xor(vertex_refs, 1); undo_xor(normal_refs, 1);
        }
        if (mode == 4) {
            undo_xor(vertices, 3); undo_xor(normals, 3);
            undo_delta(vertex_refs); undo_delta(normal_refs);
        }
        for (size_t face = 0; face < count; ++face) {
            const size_t normal = u32(normal_refs.data() + face * 4);
            require(normal < normal_count, "Normal index out of bounds");
            result.insert(result.end(), normals.begin() + normal * 12, normals.begin() + normal * 12 + 12);
            for (size_t corner = 0; corner < 3; ++corner) {
                const size_t vertex = u32(vertex_refs.data() + (face * 3 + corner) * 4);
                require(vertex < vertex_count, "Vertex index out of bounds");
                result.insert(result.end(), vertices.begin() + vertex * 12, vertices.begin() + vertex * 12 + 12);
            }
            result.push_back(attributes[face * 2]); result.push_back(attributes[face * 2 + 1]);
        }
    }
    const auto digest = sha256(result);
    require(result.size() == source_size && std::equal(digest.begin(), digest.end(), encoded.begin() + 20),
            "Reconstructed SHA-256 mismatch");
    return result;
}
static void publish(const fs::path& destination, const Bytes& decoded) {
    safe_path(destination);
    require(!fs::exists(destination), "Refusing to overwrite destination");
    // Only this exclusively-created sibling is ever removed on failure.
    auto temporary = destination;
    temporary += L".restore-" + std::to_wstring(GetCurrentProcessId()) + L"-" + std::to_wstring(GetTickCount64());
    HANDLE file = CreateFileW(temporary.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, nullptr);
    require(file != INVALID_HANDLE_VALUE, "Cannot reserve output temporary file");
    DWORD actual = 0;
    const bool ok = WriteFile(file, decoded.data(), static_cast<DWORD>(decoded.size()), &actual, nullptr) &&
                    actual == decoded.size() && FlushFileBuffers(file);
    CloseHandle(file);
    if (!ok || !MoveFileExW(temporary.c_str(), destination.c_str(), MOVEFILE_WRITE_THROUGH)) {
        DeleteFileW(temporary.c_str());
        throw std::runtime_error("Cannot publish reconstructed STL (existing files are never overwritten)");
    }
}
static void restore_one(const fs::path& input, const fs::path& output, bool remove_encoded) {
    const auto encoded = read_bytes(input);
    const auto decoded = decode(encoded);
    safe_path(output);
    if (fs::exists(output))
        require(read_bytes(output) == decoded, "Existing STL differs; refusing overwrite");
    else publish(output, decoded);
    // Verify actual disk bytes, not just the in-memory reconstruction.
    require(read_bytes(output) == decoded, "Written STL verification failed");
    if (remove_encoded) {
        safe_path(input);
        require(read_bytes(input) == encoded, "Encoded input changed during restoration");
        require(DeleteFileW(input.c_str()) != 0, "Restored STL but could not remove encoded copy");
    }
}
int wmain(int argc, wchar_t** argv) {
    try {
        if (argc == 4 && std::wstring(argv[1]) == L"--decode") {
            restore_one(fs::absolute(argv[2]), fs::absolute(argv[3]), false);
            return 0;
        }
        wchar_t executable[32768]{};
        DWORD chars = GetModuleFileNameW(nullptr, executable, 32768);
        require(chars > 0 && chars < 32768, "Cannot locate decoder");
        fs::path root = fs::path(executable).parent_path();
        bool remove_encoded = false;
        for (int i = 1; i < argc; ++i) {
            const std::wstring argument(argv[i]);
            if (argument == L"--root" && i + 1 < argc) root = fs::absolute(argv[++i]);
            else if (argument == L"--remove-encoded") remove_encoded = true;
            else throw std::runtime_error("Usage: RestoreSTL [--root directory] [--remove-encoded], or --decode input output");
        }
        safe_path(root);
        require(fs::is_directory(root), "Release directory not found");
        std::vector<fs::path> inputs;
        for (const auto& entry : fs::recursive_directory_iterator(root)) {
            safe_path(entry.path());
            if (entry.is_regular_file() && entry.path().extension() == L".pstlc") inputs.push_back(entry.path());
        }
        std::sort(inputs.begin(), inputs.end());
        for (const auto& input : inputs) {
            auto output = input; output.replace_extension();
            require(_wcsicmp(output.extension().c_str(), L".stl") == 0, "Unexpected transformed file extension");
            restore_one(input, output, remove_encoded);
        }
        std::cout << "Restored and SHA-256 verified " << inputs.size() << " STL files. No network access required.\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "STL preparation failed: " << error.what() << '\n';
        return 1;
    }
}
