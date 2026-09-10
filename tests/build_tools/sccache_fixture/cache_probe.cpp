template<int N> struct square { static constexpr int value = N * N; };
int cache_probe(int value) { return value + square<7>::value; }
