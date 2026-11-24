# SIMD 基准夹具（bare-metal + QEMU）
#
# 用法示例：
#   make -C <bench-dir> \
#        REF_SRC=../kernels/ref_foo.c \
#        SIMD_SRC=../kernels/simd_foo.c \
#        PROJECT_FLAGS="-O3 -march=armv9.6-a+sve2p1+sme2p2 -msve-vector-bits=256 -mtune=neoverse-v3 -flto -ffreestanding" \
#        VL_BYTES=32
#   make -C <bench-dir> run
#
# 关键约定：夹具自身（crt0 / shim / runner）不以被测项目的 -flto 参数编译。
# 原因见 README.md 的「三个坑」；简言之，-flto 会把 64 次被测调用合并成 1 次，
# 计时结果失去意义，还可能让 semihosting 输出路径错乱。

# make 内置规则把 CC 预设成 cc，?= 不会生效；只有用户没显式指定时才换成交叉编译器。
ifeq ($(origin CC),default)
CC := aarch64-elf-gcc
endif
QEMU    ?= qemu-system-aarch64
VL_BYTES ?= 32
REF_SRC  ?= kernel_ref.c
SIMD_SRC ?= kernel_simd.c

ARCH := -march=armv9.6-a+sve2p1+sme2p2
SVEBITS := $(shell expr $(VL_BYTES) \* 8)

PROJECT_FLAGS ?= -O3 $(ARCH) -msve-vector-bits=$(SVEBITS) -mtune=neoverse-v3 -flto -ffreestanding

# 夹具：固定 -O2，不带 -flto。
HARNESS_CFLAGS := -O2 $(ARCH) -msve-vector-bits=$(SVEBITS) -ffreestanding \
                  -DSIMD_BENCH_VL_BYTES=$(VL_BYTES)
# 被测函数：项目实际参数（建议保留 -flto，但要与项目构建保持一致）。
KERNEL_CFLAGS  := $(PROJECT_FLAGS) -DSIMD_BENCH_VL_BYTES=$(VL_BYTES)

LDFLAGS := -nostdlib -T link.ld

# -icount shift=0 让虚拟时间按访客指令数推进，计时完全可复现；
# 去掉 -icount 得到的是宿主墙钟时间，抖动大，只适合粗略对照。
QEMU_RUN := $(QEMU) -M virt -cpu max -icount shift=0 -nographic \
            -semihosting-config enable=on,target=native -kernel

OBJS := crt0.o bench_shim.o bench_runner.o kernel_ref.o kernel_simd.o

.PHONY: all flip run run-wall run-flip clean

all: bench.elf

# 顺序翻转版本：墙钟口径下和 bench.elf 交替跑，抵消 A/B 顺序偏差。
flip: bench-flip.elf

bench.elf: bench_runner.o $(filter-out bench_runner.o,$(OBJS)) link.ld
	$(CC) $(LDFLAGS) $(OBJS) -o $@

bench-flip.elf: bench_runner_flip.o bench_shim.o crt0.o kernel_ref.o kernel_simd.o link.ld
	$(CC) $(LDFLAGS) bench_runner_flip.o bench_shim.o crt0.o kernel_ref.o kernel_simd.o -o $@

bench_runner_flip.o: bench_runner.c bench_shim.h
	$(CC) $(HARNESS_CFLAGS) -DBENCH_SIMD_FIRST -c $< -o $@

crt0.o: crt0.S
	$(CC) $(HARNESS_CFLAGS) -c $< -o $@

bench_shim.o: bench_shim.c bench_shim.h
	$(CC) $(HARNESS_CFLAGS) -c $< -o $@

bench_runner.o: bench_runner.c bench_shim.h
	$(CC) $(HARNESS_CFLAGS) -c $< -o $@

# 被测函数用项目参数编译；不放进 LTO 之外的额外转换，保证测的就是这个函数。
kernel_ref.o: $(REF_SRC)
	$(CC) $(KERNEL_CFLAGS) -c $< -o $@

kernel_simd.o: $(SIMD_SRC)
	$(CC) $(KERNEL_CFLAGS) -c $< -o $@

run: bench.elf
	$(QEMU_RUN) bench.elf

run-flip: bench-flip.elf
	$(QEMU_RUN) bench-flip.elf

run-wall: bench.elf
	$(QEMU) -M virt -cpu max -nographic \
	        -semihosting-config enable=on,target=native -kernel bench.elf

clean:
	rm -f $(OBJS) bench_runner_flip.o bench.elf bench-flip.elf
