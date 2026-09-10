// PrusaSlicer is released under the terms of the AGPLv3 or higher.
#include "slic3r/GUI/ImGuiFontAtlas.hpp"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <vector>

namespace {
std::unordered_map<void*, size_t> allocations;
size_t allocated_bytes = 0;
ImWchar missing_glyph = 0;

void check(bool condition, const char* message)
{
    if (!condition)
        throw std::runtime_error(message);
}

void* allocate(size_t size, void*)
{
    void* data = std::malloc(size);
    if (!data)
        throw std::bad_alloc();
    allocations.emplace(data, size);
    allocated_bytes += size;
    return data;
}

void release(void* data, void*)
{
    if (!data)
        return;
    const auto found = allocations.find(data);
    check(found != allocations.end(), "double free or unknown allocation");
    allocated_bytes -= found->second;
    allocations.erase(found);
    std::free(data);
}

void verify_atlas(const std::string& font_dir, float size, bool cjk, ImWchar extra)
{
    ImFontAtlas& atlas = *ImGui::GetIO().Fonts;
    atlas.Clear();
    atlas.SetTexID(nullptr);

    // Keep the ranges alive until build, just as ImGuiWrapper does.
    ImVector<ImWchar> ranges;
    ImFontGlyphRangesBuilder builder;
    builder.AddRanges(atlas.GetGlyphRangesDefault());
    builder.AddChar(0x2026);
    if (cjk)
        builder.AddChar(0x4e2d);
    if (extra)
        builder.AddChar(extra);
    builder.BuildRanges(&ranges);

    ImFont* font = atlas.AddFontFromFileTTF((font_dir + "/NotoSans-Regular.ttf").c_str(), size, nullptr, ranges.Data);
    check(font != nullptr, "could not load Noto Sans");
    if (cjk) {
        ImFontConfig config;
        config.MergeMode = true;
        check(atlas.AddFontFromFileTTF((font_dir + "/NotoSansCJK-Regular.ttc").c_str(), size, &config, ranges.Data) != nullptr,
              "could not load Noto Sans CJK");
    }

    const int icon_id = atlas.AddCustomRectFontGlyph(font, ImGui::PrintIconMarker, 32, 32, 35.f);
    unsigned char* pixels = nullptr;
    int width = 0, height = 0;
    atlas.GetTexDataAsRGBA32(&pixels, &width, &height);
    check(pixels && width > 0 && height > 0, "atlas build failed");
    check(atlas.TexPixelsAlpha8 != nullptr, "expected an alpha raster as well as RGBA pixels");
    check(font->FindGlyphNoFallback('A') != nullptr, "missing Latin glyph");
    check(!cjk || font->FindGlyphNoFallback(0x4e2d) != nullptr, "missing CJK glyph");
    check(!extra || font->FindGlyphNoFallback(extra) != nullptr, "missing newly requested glyph");

    const ImFontAtlasCustomRect icon = *atlas.GetCustomRectByIndex(icon_id);
    check(icon.IsPacked(), "icon was not packed");
    // Give the custom rectangle real colored pixels before the simulated upload.
    atlas.TexPixelsRGBA32[icon.Y * width + icon.X] = IM_COL32(20, 40, 60, 255);
    ImVec2 icon_min, icon_max;
    atlas.CalcCustomRectUV(&icon, &icon_min, &icon_max);
    ImVec2 cursor_offset, cursor_size, cursor_border[2], cursor_fill[2];
    check(atlas.GetMouseCursorTexData(ImGuiMouseCursor_Arrow, &cursor_offset, &cursor_size, cursor_border, cursor_fill),
          "mouse cursor was not packed");
    const int cursor_id = atlas.PackIdMouseCursors;
    const int lines_id = atlas.PackIdLines;
    const std::vector<ImFontGlyph> glyphs(font->Glyphs.begin(), font->Glyphs.end());

    size_t source_bytes = 0;
    for (const ImFontConfig& config : atlas.ConfigData)
        if (config.FontDataOwnedByAtlas)
            source_bytes += config.FontDataSize;
    const size_t texture_bytes = static_cast<size_t>(width) * height * 5;
    const size_t before_cleanup = allocated_bytes;
    atlas.SetTexID(reinterpret_cast<ImTextureID>(static_cast<intptr_t>(1)));
    Slic3r::GUI::clear_font_atlas_build_data(atlas);
    const size_t freed_bytes = before_cleanup - allocated_bytes;

    check(freed_bytes >= source_bytes + texture_bytes, "font source or pixel allocations were retained");
    check(atlas.ConfigData.empty() && font->ConfigData == nullptr, "font build configuration was retained");
    check(!atlas.TexPixelsAlpha8 && !atlas.TexPixelsRGBA32, "pixel buffers were retained");
    check(font->IsLoaded(), "cleanup unloaded the font");
    check(atlas.TexWidth == width && atlas.TexHeight == height, "cleanup changed texture dimensions");
    check(atlas.TexID == reinterpret_cast<ImTextureID>(static_cast<intptr_t>(1)), "cleanup discarded the GPU texture ID");
    check(atlas.PackIdMouseCursors == cursor_id && atlas.PackIdLines == lines_id, "cleanup discarded packed cursor/line IDs");
    check(font->Glyphs.Size == glyphs.size() && std::memcmp(font->Glyphs.Data, glyphs.data(), glyphs.size() * sizeof(ImFontGlyph)) == 0,
          "cleanup changed glyph geometry, advances or UVs");
    const ImFontAtlasCustomRect& retained_icon = *atlas.GetCustomRectByIndex(icon_id);
    check(retained_icon.Font == font && retained_icon.X == icon.X && retained_icon.Y == icon.Y &&
          retained_icon.Width == icon.Width && retained_icon.Height == icon.Height,
          "cleanup discarded the custom icon rectangle");
    ImVec2 retained_min, retained_max;
    atlas.CalcCustomRectUV(&retained_icon, &retained_min, &retained_max);
    check(retained_min.x == icon_min.x && retained_min.y == icon_min.y &&
          retained_max.x == icon_max.x && retained_max.y == icon_max.y, "icon UVs changed");
    ImVec2 retained_offset, retained_size, retained_border[2], retained_fill[2];
    check(atlas.GetMouseCursorTexData(ImGuiMouseCursor_Arrow, &retained_offset, &retained_size, retained_border, retained_fill),
          "cleanup broke software cursor lookup");
    check(std::memcmp(cursor_border, retained_border, sizeof(cursor_border)) == 0 &&
          std::memcmp(cursor_fill, retained_fill, sizeof(cursor_fill)) == 0, "mouse cursor UVs changed");

    // Exercise the real NewFrame/Render path after both CPU buffers are gone.
    // Software cursors and textured antialiased lines also use atlas metadata.
    ImGuiIO& io = ImGui::GetIO();
    io.MouseDrawCursor = true;
    io.MousePos = ImVec2(20.f, 20.f);
    io.DisplaySize = ImVec2(640.f, 480.f);
    ImGui::NewFrame();
    ImGui::SetNextWindowPos(ImVec2(40.f, 40.f));
    ImGui::SetNextWindowSize(ImVec2(200.f, 100.f));
    ImGui::Begin("Atlas lifetime");
    ImGui::TextUnformatted("ABC \x04");
    ImGui::GetWindowDrawList()->AddLine(ImVec2(50.f, 80.f), ImVec2(150.f, 80.f), IM_COL32_WHITE, 2.f);
    ImGui::End();
    ImGui::Render();
    check(ImGui::GetDrawData()->TotalVtxCount > 0, "rendering failed after cleanup");

    // The application queues missing glyphs for a full rebuild next frame.
    missing_glyph = 0;
    font->FindGlyph(0x65e5);
    check(extra == 0x65e5 || missing_glyph == 0x65e5, "missing-glyph callback no longer works");

    // Destruction and repeated cleanup must not free packed glyph output twice.
    Slic3r::GUI::clear_font_atlas_build_data(atlas);
    std::printf("size=%.0f cjk=%d extra=%u source=%zu pixels=%zu freed=%zu bytes\n",
                size, cjk, extra, source_bytes, texture_bytes, freed_bytes);
}
} // namespace

// The embedded ImGui calls this application hook when it needs a fallback.
void imgui_rendered_fallback_glyph(ImWchar c)
{
    missing_glyph = c;
}

int main(int argc, char** argv)
{
    if (argc != 2) {
        std::fprintf(stderr, "usage: imgui_atlas_tests <resources/fonts>\n");
        return 2;
    }
    ImGui::SetAllocatorFunctions(allocate, release);
    ImGui::CreateContext();
    ImGui::GetIO().IniFilename = nullptr;
    try {
        verify_atlas(argv[1], 18.f, false, 0);
        verify_atlas(argv[1], 27.f, false, 0);
        verify_atlas(argv[1], 18.f, true, 0);
        verify_atlas(argv[1], 18.f, true, 0x65e5);
        verify_atlas(argv[1], 18.f, false, 0);
        ImGui::DestroyContext();
        check(allocations.empty() && allocated_bytes == 0, "atlas/context destruction leaked memory");
    } catch (const std::exception& e) {
        std::fprintf(stderr, "FAIL: %s\n", e.what());
        return 1;
    }
    std::puts("font atlas lifetime checks passed");
    return 0;
}
