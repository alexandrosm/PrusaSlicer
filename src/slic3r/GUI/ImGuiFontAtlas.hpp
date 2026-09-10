// PrusaSlicer is released under the terms of the AGPLv3 or higher.
#ifndef slic3r_ImGuiFontAtlas_hpp_
#define slic3r_ImGuiFontAtlas_hpp_

#include <imgui/imgui.h>

namespace Slic3r::GUI {

// Call only after the texture has been uploaded. Rebuilding afterwards requires
// Clear() and reloading the fonts, as done by ImGuiWrapper::init_font().
inline void clear_font_atlas_build_data(ImFontAtlas& atlas)
{
    // In ImGui 1.83 ClearInputData() also discards custom rectangles. Their
    // packed positions are still used to draw our icons and software cursors.
    ImVector<ImFontAtlasCustomRect> custom_rects;
    custom_rects.swap(atlas.CustomRects);
    const int mouse_cursors = atlas.PackIdMouseCursors;
    const int lines = atlas.PackIdLines;
    atlas.ClearInputData();
    atlas.CustomRects.swap(custom_rects);
    atlas.PackIdMouseCursors = mouse_cursors;
    atlas.PackIdLines = lines;
    atlas.ClearTexData();
}

} // namespace Slic3r::GUI

#endif
