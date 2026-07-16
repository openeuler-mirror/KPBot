#!/usr/bin/env python3
"""Create a tiny C project used by the real-Claude batch pipeline self-test."""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path


VEC_ADD_H = """#ifndef VEC_ADD_H
#define VEC_ADD_H

#include <stddef.h>

void vec_add(const float *a, const float *b, float *out, size_t n);

#endif
"""


VEC_ADD_C = """#include "vec_add.h"

void vec_add(const float *a, const float *b, float *out, size_t n) {
    for (size_t i = 0; i < n; ++i) {
        out[i] = a[i] + b[i];
    }
}
"""


TEST_VEC_ADD_C = """#include "vec_add.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>

static int nearly_equal(float a, float b) {
    return fabsf(a - b) < 1.0e-6f;
}

int main(void) {
    const size_t sizes[] = {0, 1, 2, 3, 7, 16, 31, 64, 129, 1024};
    for (size_t case_id = 0; case_id < sizeof(sizes) / sizeof(sizes[0]); ++case_id) {
        size_t n = sizes[case_id];
        float *a = (float *)calloc(n + 3, sizeof(float));
        float *b = (float *)calloc(n + 3, sizeof(float));
        float *out = (float *)calloc(n + 3, sizeof(float));
        if (!a || !b || !out) {
            fprintf(stderr, "allocation failed\\n");
            return 2;
        }
        for (size_t i = 0; i < n; ++i) {
            a[i] = (float)(i % 17) * 0.25f - 2.0f;
            b[i] = (float)(i % 23) * 0.125f + 1.0f;
        }
        vec_add(a, b, out, n);
        for (size_t i = 0; i < n; ++i) {
            float expected = a[i] + b[i];
            if (!nearly_equal(out[i], expected)) {
                fprintf(stderr, "mismatch case=%zu i=%zu got=%f expected=%f\\n",
                        case_id, i, out[i], expected);
                return 1;
            }
        }
        free(a);
        free(b);
        free(out);
    }
    puts("test_vec_add: pass");
    return 0;
}
"""


BENCH_VEC_ADD_C = """#include "vec_add.h"

#include <stdio.h>
#include <stdlib.h>
#include <time.h>

static double seconds_now(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec * 1.0e-9;
}

int main(void) {
    const size_t n = 1u << 20;
    const int rounds = 80;
    float *a = (float *)aligned_alloc(64, n * sizeof(float));
    float *b = (float *)aligned_alloc(64, n * sizeof(float));
    float *out = (float *)aligned_alloc(64, n * sizeof(float));
    if (!a || !b || !out) {
        fprintf(stderr, "allocation failed\\n");
        return 2;
    }
    for (size_t i = 0; i < n; ++i) {
        a[i] = (float)(i % 97) * 0.5f;
        b[i] = (float)(i % 31) * 0.25f;
        out[i] = 0.0f;
    }
    for (int warm = 0; warm < 5; ++warm) {
        vec_add(a, b, out, n);
    }
    double start = seconds_now();
    volatile float checksum = 0.0f;
    for (int r = 0; r < rounds; ++r) {
        vec_add(a, b, out, n);
        checksum += out[(size_t)r * 997u % n];
    }
    double elapsed = seconds_now() - start;
    double elements_per_second = (double)n * (double)rounds / elapsed;
    printf("bench_vec_add: n=%zu rounds=%d seconds=%.6f Melem/s=%.3f checksum=%.3f\\n",
           n, rounds, elapsed, elements_per_second / 1.0e6, (double)checksum);
    free(a);
    free(b);
    free(out);
    return 0;
}
"""


MAKEFILE = """CC ?= cc
AUTOVEC_DISABLE_FLAGS := $(shell printf 'int x;\\n' | $(CC) -x c - -c -o /dev/null -fno-vectorize -fno-slp-vectorize >/dev/null 2>&1 && echo "-fno-vectorize -fno-slp-vectorize" || echo "-fno-tree-vectorize")
CFLAGS ?= -O3 -Wall -Wextra -std=c11 -Isrc $(AUTOVEC_DISABLE_FLAGS)
LDFLAGS ?=

.PHONY: all test bench clean

all: build/test_vec_add build/bench_vec_add

build:
\tmkdir -p build

build/vec_add.o: src/vec_add.c src/vec_add.h | build
\t$(CC) $(CFLAGS) -c src/vec_add.c -o $@

build/test_vec_add: tests/test_vec_add.c build/vec_add.o src/vec_add.h | build
\t$(CC) $(CFLAGS) tests/test_vec_add.c build/vec_add.o $(LDFLAGS) -lm -o $@

build/bench_vec_add: bench/bench_vec_add.c build/vec_add.o src/vec_add.h | build
\t$(CC) $(CFLAGS) bench/bench_vec_add.c build/vec_add.o $(LDFLAGS) -o $@

test: build/test_vec_add
\t./build/test_vec_add

bench: build/bench_vec_add
\t./build/bench_vec_add

clean:
\trm -rf build
"""

GIT_COMMAND_TIMEOUT_SECONDS = 60


def run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=str(cwd), timeout=GIT_COMMAND_TIMEOUT_SECONDS, check=True)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def prepare_output_dir(out: Path, force: bool = False) -> Path:
    out = out.expanduser().resolve()
    try:
        out.mkdir(parents=True)
        return out
    except FileExistsError:
        if not force:
            raise FileExistsError(f"output path already exists: {out}")
    try:
        shutil.rmtree(out)
    except FileNotFoundError:
        pass
    out.mkdir(parents=True)
    return out


def create_project(out: Path, force: bool = False) -> Path:
    out = prepare_output_dir(out, force=force)

    write(out / "src" / "vec_add.h", VEC_ADD_H)
    write(out / "src" / "vec_add.c", VEC_ADD_C)
    write(out / "tests" / "test_vec_add.c", TEST_VEC_ADD_C)
    write(out / "bench" / "bench_vec_add.c", BENCH_VEC_ADD_C)
    write(out / "Makefile", MAKEFILE)
    write(
        out / "README.md",
        "# Virtual vec_add optimization target\n\n"
        "This project is generated by batch-drive-optimize-pipeline self-test.\n",
    )

    run(["git", "init"], out)
    run(["git", "config", "user.email", "batch-drive@example.invalid"], out)
    run(["git", "config", "user.name", "Batch Drive Self Test"], out)
    run(["git", "add", "-A"], out)
    run(["git", "commit", "-m", "baseline virtual vec_add project"], out)
    run(["git", "tag", "batch_baseline"], out)
    return out


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="Directory to create.")
    parser.add_argument("--force", action="store_true", help="Remove --out first if it exists.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project = create_project(Path(args.out), force=args.force)
    print(project)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
