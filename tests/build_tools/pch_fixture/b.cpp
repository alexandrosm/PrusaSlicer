#include "stable.hpp"
#include "project.hpp"
int probe_b() {
    std::unordered_map<std::string, std::tuple<int, int>> values;
    values.emplace("probe", std::make_tuple(edited_value, 2));
    return std::get<0>(values.at("probe"));
}
