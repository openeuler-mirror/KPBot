#ifndef IMPL
#define IMPL "org_utf8_range.c"
#endif
#include IMPL

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define REPEATS 5
#define TARGET_SECONDS 0.05

typedef size_t (*validate_fn)(const char *data, size_t len);

typedef struct {
  const char *name;
  validate_fn fn;
} api_case;

typedef struct {
  const char *name;
  int invalid_at_end;
} input_case;

static volatile size_t result_sink;

static size_t call_validate_rp0(const char *data, size_t len) {
  return utf8_range_Validate(data, len, 0);
}

static size_t call_validate_rp1(const char *data, size_t len) {
  return utf8_range_Validate(data, len, 1);
}

static double now_seconds(void) {
  struct timespec ts;
  if (clock_gettime(CLOCK_MONOTONIC_RAW, &ts) != 0) {
    perror("clock_gettime");
    exit(EXIT_FAILURE);
  }
  return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}

static size_t run_loop(validate_fn fn, const char *data, size_t len,
                       size_t iterations) {
  size_t result = 0;
  for (size_t i = 0; i < iterations; ++i) {
    result += fn(data, len);
  }
  return result;
}

static double run_timed(validate_fn fn, const char *data, size_t len,
                        size_t iterations) {
  double start = now_seconds();
  result_sink = run_loop(fn, data, len, iterations);
  return now_seconds() - start;
}

static size_t calibrate(validate_fn fn, const char *data, size_t len) {
  size_t iterations = 1;
  for (;;) {
    double elapsed = run_timed(fn, data, len, iterations);
    if (elapsed >= TARGET_SECONDS) {
      return iterations;
    }
    if (elapsed <= 0.0) {
      iterations *= 16;
      continue;
    }
    double scale = TARGET_SECONDS / elapsed;
    if (scale > 16.0) {
      iterations *= 16;
    } else {
      size_t next = (size_t)((double)iterations * scale * 1.15) + 1;
      iterations = next > iterations ? next : iterations + 1;
    }
  }
}

static int compare_double(const void *lhs, const void *rhs) {
  double a = *(const double *)lhs;
  double b = *(const double *)rhs;
  return (a > b) - (a < b);
}

static void fill_mixed(char *data, size_t len) {
  static const char *const codepoints[] = {
      "A", "\xC2\xA2", "\xE2\x82\xAC", "\xF0\x9F\x98\x80"};
  static const size_t widths[] = {1, 2, 3, 4};
  size_t offset = 0;
  size_t index = 0;
  while (offset < len) {
    size_t width = widths[index];
    if (width > len - offset) {
      memset(data + offset, 'x', len - offset);
      break;
    }
    memcpy(data + offset, codepoints[index], width);
    offset += width;
    index = (index + 1) % 4;
  }
}

static void fill_input(char *data, size_t len, const input_case *input) {
  if (input->invalid_at_end) {
    fill_mixed(data, len - 1);
    data[len - 1] = (char)0xFF;
  } else if (strcmp(input->name, "ascii") == 0) {
    memset(data, 'a', len);
  } else {
    fill_mixed(data, len);
  }
}

static int expect_result(const char *label, size_t actual, size_t expected) {
  if (actual == expected) {
    return 0;
  }
  fprintf(stderr, "%s: got %zu, expected %zu\n", label, actual, expected);
  return 1;
}

static int run_correctness_checks(void) {
  static const char mixed[] = "A\xC2\xA2\xE2\x82\xAC\xF0\x9F\x98\x80";
  static const char invalid[] = {'A', (char)0xC2, 'B'};
  int failed = 0;
  failed |= expect_result("empty rp=0", call_validate_rp0("", 0), 1);
  failed |= expect_result("empty rp=1", call_validate_rp1("", 0), 0);
  failed |= expect_result("ascii rp=0", call_validate_rp0("hello", 5), 1);
  failed |= expect_result("ascii rp=1", call_validate_rp1("hello", 5), 5);
  failed |= expect_result("mixed rp=0",
                          call_validate_rp0(mixed, sizeof(mixed) - 1), 1);
  failed |= expect_result("mixed rp=1",
                          call_validate_rp1(mixed, sizeof(mixed) - 1),
                          sizeof(mixed) - 1);
  failed |= expect_result("invalid rp=0", call_validate_rp0(invalid, 3), 0);
  failed |= expect_result("invalid rp=1", call_validate_rp1(invalid, 3), 1);
  return failed;
}

int main(void) {
  static const size_t sizes[] = {16, 64, 256, 1024, 4096, 65536, 1048576};
  static const api_case apis[] = {
      {"utf8_range_Validate(rp=0)", call_validate_rp0},
      {"utf8_range_Validate(rp=1)", call_validate_rp1},
  };
  static const input_case inputs[] = {
      {"ascii", 0}, {"mixed_utf8", 0}, {"invalid_at_end", 1}};

  if (run_correctness_checks() != 0) {
    return EXIT_FAILURE;
  }
  puts("correctness: PASS");
  puts("api,input,bytes,iterations,ns_per_call,gib_per_second");

  for (size_t s = 0; s < sizeof(sizes) / sizeof(sizes[0]); ++s) {
    size_t len = sizes[s];
    char *data = (char *)malloc(len);
    if (data == NULL) {
      perror("malloc");
      return EXIT_FAILURE;
    }
    for (size_t p = 0; p < sizeof(inputs) / sizeof(inputs[0]); ++p) {
      fill_input(data, len, &inputs[p]);
      size_t expected_valid = inputs[p].invalid_at_end ? 0 : 1;
      size_t expected_prefix = inputs[p].invalid_at_end ? len - 1 : len;
      if (expect_result("bench rp=0", call_validate_rp0(data, len),
                        expected_valid) ||
          expect_result("bench rp=1", call_validate_rp1(data, len),
                        expected_prefix)) {
        free(data);
        return EXIT_FAILURE;
      }
      for (size_t a = 0; a < sizeof(apis) / sizeof(apis[0]); ++a) {
        size_t iterations = calibrate(apis[a].fn, data, len);
        double samples[REPEATS];
        for (size_t r = 0; r < REPEATS; ++r) {
          samples[r] = run_timed(apis[a].fn, data, len, iterations);
        }
        qsort(samples, REPEATS, sizeof(samples[0]), compare_double);
        double median_seconds = samples[REPEATS / 2];
        double ns_per_call = median_seconds * 1e9 / (double)iterations;
        double gib_per_second =
            (double)len * 1e9 / (ns_per_call * 1073741824.0);
        printf("%s,%s,%zu,%zu,%.3f,%.3f\n", apis[a].name, inputs[p].name, len,
               iterations, ns_per_call, gib_per_second);
      }
    }
    free(data);
  }

  return EXIT_SUCCESS;
}
