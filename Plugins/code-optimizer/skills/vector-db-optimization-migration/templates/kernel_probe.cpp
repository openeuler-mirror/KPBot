// G2 kernel bit-exact probe skeleton.
//
// Purpose: load a baseline engine library and a migrated engine library in the same process
// (avoiding symbol clashes) and compare one kernel's output bit-for-bit / by ULP.
//
// Build:
//   g++ -O2 -std=c++17 kernel_probe.cpp -ldl -o probe
// Run:
//   ./probe <baseline.so> <candidate.so> <symbol>
//
// Adapt the function-pointer typedef and the input generator to the kernel under
// test (distance, PQ LUT, scan, etc.). Keep the output deterministic
// and cover tail lengths, dimension gates and batch sizes.

#include <dlfcn.h>

#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

// Example kernel: void f(const float* a, const float* b, size_t d, float* out)
using KernelFn = void (*)(const float *, const float *, size_t, float *);

struct Lcg {
  uint64_t s;
  explicit Lcg(uint64_t seed) : s(seed) {}
  uint32_t next() {
    s = s * 6364136223846793005ULL + 1442695040888963407ULL;
    return static_cast<uint32_t>(s >> 33);
  }
  float uniform() {
    return (static_cast<float>(next()) / static_cast<float>(UINT32_MAX)) * 2.0f - 1.0f;
  }
};

static void *open_lib(const char *path) {
  void *h = dlopen(path, RTLD_NOW | RTLD_LOCAL);
  if (h == nullptr) {
    std::fprintf(stderr, "dlopen failed: %s: %s\n", path, dlerror());
  }
  return h;
}

static KernelFn load_fn(void *h, const char *symbol) {
  dlerror();
  void *p = dlsym(h, symbol);
  const char *err = dlerror();
  if (err != nullptr) {
    std::fprintf(stderr, "dlsym(%s) failed: %s\n", symbol, err);
    return nullptr;
  }
  KernelFn fn = nullptr;
  std::memcpy(&fn, &p, sizeof(fn));
  return fn;
}

static bool bit_equal(float a, float b) {
  uint32_t ua = 0, ub = 0;
  std::memcpy(&ua, &a, sizeof(ua));
  std::memcpy(&ub, &b, sizeof(ub));
  return ua == ub;
}

static int ulp_diff(float a, float b) {
  if (bit_equal(a, b)) {
    return 0;
  }
  if (std::isnan(a) || std::isnan(b)) {
    return 1 << 30;
  }
  uint32_t ua = 0, ub = 0;
  std::memcpy(&ua, &a, sizeof(ua));
  std::memcpy(&ub, &b, sizeof(ub));
  if ((ua >> 31) != (ub >> 31)) {
    return 1 << 30;
  }
  uint32_t diff = ua > ub ? ua - ub : ub - ua;
  return static_cast<int>(diff);
}

int main(int argc, char **argv) {
  if (argc != 4) {
    std::fprintf(stderr, "usage: %s <baseline.so> <candidate.so> <symbol>\n", argv[0]);
    return 2;
  }

  void *base = open_lib(argv[1]);
  void *cand = open_lib(argv[2]);
  if (base == nullptr || cand == nullptr) {
    return 2;
  }

  KernelFn fn_base = load_fn(base, argv[3]);
  KernelFn fn_cand = load_fn(cand, argv[3]);
  if (fn_base == nullptr || fn_cand == nullptr) {
    return 2;
  }

  const std::vector<size_t> dims = {1, 2, 3, 4, 7, 8, 15, 16, 31, 32, 64, 128, 256};
  const size_t queries = 32;
  Lcg rng(0x9e3779b97f4a7c15ULL);

  size_t mismatches = 0;
  int max_ulp = 0;
  size_t first_dim = 0, first_q = 0;

  for (size_t d : dims) {
    std::vector<float> a(d * queries), b(d), out_base(queries), out_cand(queries);
    for (auto &x : a) {
      x = rng.uniform();
    }
    for (auto &x : b) {
      x = rng.uniform();
    }
    for (size_t q = 0; q < queries; ++q) {
      fn_base(a.data() + q * d, b.data(), d, out_base.data() + q);
      fn_cand(a.data() + q * d, b.data(), d, out_cand.data() + q);
    }
    for (size_t q = 0; q < queries; ++q) {
      int u = ulp_diff(out_base[q], out_cand[q]);
      if (u != 0) {
        if (mismatches == 0) {
          first_dim = d;
          first_q = q;
        }
        ++mismatches;
        if (u > max_ulp) {
          max_ulp = u;
        }
      }
    }
  }

  std::printf("symbol=%s mismatches=%zu max_ulp=%d", argv[3], mismatches, max_ulp);
  if (mismatches != 0) {
    std::printf(" first_dim=%zu first_query=%zu", first_dim, first_q);
  }
  std::printf("\n");

  dlclose(cand);
  dlclose(base);
  return mismatches == 0 ? 0 : 1;
}
