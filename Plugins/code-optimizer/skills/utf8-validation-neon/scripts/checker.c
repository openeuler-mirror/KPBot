#include "utf8_range.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

static uint64_t st = 0x243F6A8885A308D3ULL;
static unsigned rnd(void) {
  st = st * 6364136223846793005ULL + 1442695040888963407ULL;
  return (unsigned)(st >> 33);
}

static void report(const char *id, const char *buf, size_t len) {
  printf("%s %d %zu\n", id, utf8_range_IsValid(buf, len),
         utf8_range_ValidPrefix(buf, len));
}

static void emit_cp(unsigned char *p, unsigned w) {
  unsigned r = rnd();
  if (w == 1) {
    p[0] = (unsigned char)(r & 0x7F);
  } else if (w == 2) {
    p[0] = (unsigned char)(0xC2 + (r % 0x1E));
    p[1] = (unsigned char)(0x80 + (rnd() % 0x40));
  } else if (w == 3) {
    unsigned b0 = 0xE1 + (r % 0x0C);
    unsigned char b1;
    if (b0 == 0xED) {
      b1 = 0x80 + (rnd() % 0x20);
    } else {
      b1 = 0x80 + (rnd() % 0x40);
    }
    p[0] = (unsigned char)b0;
    p[1] = b1;
    p[2] = (unsigned char)(0x80 + (rnd() % 0x40));
  } else {
    unsigned b0 = 0xF1 + (r % 0x03);
    unsigned char b1;
    if (b0 == 0xF4) {
      b1 = 0x80 + (rnd() % 0x10);
    } else {
      b1 = 0x80 + (rnd() % 0x40);
    }
    p[0] = (unsigned char)b0;
    p[1] = b1;
    p[2] = (unsigned char)(0x80 + (rnd() % 0x40));
    p[3] = (unsigned char)(0x80 + (rnd() % 0x40));
  }
}

static size_t fill_valid(unsigned char *buf, size_t len) {
  size_t off = 0;
  while (off < len) {
    unsigned w = 1 + (rnd() % 4);
    if (off + w > len) {
      w = 1;
    }
    emit_cp(buf + off, w);
    off += w;
  }
  return off;
}

static const size_t sizes[] = {16,  17,  20,  31,  32,  33,  47,  48,
                               49,  63,  64,  65,  100, 255, 256, 1000,
                               4096, 65536, 100000};

int main(void) {
  static const struct {
    const char *id;
    const char *bytes;
    size_t len;
  } cases[] = {
      {"empty", "", 0},
      {"ascii5", "hello", 5},
      {"valid_mixed", "A\xC2\xA2\xE2\x82\xAC\xF0\x9F\x98\x80", 10},
      {"u10ffff", "\xF4\x8F\xBF\xBF", 4},
      {"uD7FF", "\xED\x9F\xBF", 3},
      {"u0800", "\xE0\xA0\x80", 3},
      {"overlong2", "\xC0\x80", 2},
      {"overlong2b", "\xC1\xBF", 2},
      {"overlong3", "\xE0\x80\x80", 3},
      {"overlong3b", "\xE0\x9F\xBF", 3},
      {"surrogate", "\xED\xA0\x80", 3},
      {"above10ffff", "\xF4\x90\x80\x80", 4},
      {"badf5", "\xF5\x80\x80\x80", 4},
      {"badff", "\xFF", 1},
      {"bare_cont", "\x80", 1},
      {"bare_cont2", "\xBF", 1},
      {"trunc2_1", "\xC2", 1},
      {"trunc3_1", "\xE2", 1},
      {"trunc3_2", "\xE2\x82", 2},
      {"trunc4_1", "\xF0", 1},
      {"trunc4_2", "\xF0\x9F", 2},
      {"trunc4_3", "\xF0\x9F\x98", 3},
      {"bad2nd_f0", "\xF0\x80\x80\x80", 4},
      {"bad2nd_f4", "\xF4\xA0\x80\x80", 4},
      {"bad2nd_e0", "\xE0\x9F\xBF", 3},
      {"bad2nd_ed", "\xED\xBF\x80", 3},
      {"bad_tail", "\xE2\x82\x41", 3},
      {"mixed_bad_tail", "A\xC2\xA2\xE2\x41", 5},
  };
  for (size_t i = 0; i < sizeof(cases) / sizeof(cases[0]); ++i) {
    report(cases[i].id, cases[i].bytes, cases[i].len);
  }

  unsigned char buf[131072];
  char id[64];
  for (size_t s = 0; s < sizeof(sizes) / sizeof(sizes[0]); ++s) {
    size_t len = sizes[s];

    fill_valid(buf, len);
    snprintf(id, sizeof id, "valid_%zu", len);
    report(id, (const char *)buf, len);

    for (size_t cut = 0; cut <= len && cut <= 40; ++cut) {
      snprintf(id, sizeof id, "trunc_%zu_%zu", len, cut);
      report(id, (const char *)buf, cut);
    }

    fill_valid(buf, len);
    buf[len / 2] ^= 0x5A;
    snprintf(id, sizeof id, "corrupt_%zu", len);
    report(id, (const char *)buf, len);

    for (size_t i = 0; i < len; ++i) {
      buf[i] = (unsigned char)(rnd() & 0xFF);
    }
    snprintf(id, sizeof id, "random_%zu", len);
    report(id, (const char *)buf, len);
  }

  for (size_t i = 0; i < 200; ++i) {
    size_t len = 1 + (rnd() % 5000);
    fill_valid(buf, len);
    if (i & 1) {
      buf[rnd() % len] = (unsigned char)(rnd() & 0xFF);
    }
    snprintf(id, sizeof id, "fuzz_%zu", i);
    report(id, (const char *)buf, len);
  }
  return 0;
}
