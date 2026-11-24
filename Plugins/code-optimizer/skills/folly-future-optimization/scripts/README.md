# scripts/ — folly-future-optimization 配套脚本

三个脚本均为**产物级/协议级**工具，与构建系统无关（cmake/make/blade 等均适用），知识出处与使用位置见 SKILL.md 对应步骤。

## verify_folly_patch.sh

产物级验证 folly 优化是否编入二进制（SKILL.md 步骤 6）。

```bash
bash scripts/verify_folly_patch.sh /path/to/服务二进制
```

| 检查 | 机制 | 依赖 |
|---|---|---|
| F3 flag 字符串 | gflag 常量直接 strings 命中 | 无（最可靠） |
| F1 瘦身标记 | debug info 中的类型名 | -g 构建 |
| F2/F2b 内联 | out-of-line 符号**消失**判定 | 非 stripped |

边界：未链接 CPUThreadPoolExecutor 编译单元的二进制，F3 为假阴性（脚本内已注明）；无 debug info 时 F1 跳过、F3/F2 仍有效。退出码 = FAIL 数。

## inspect_build_flags.sh（已拆分）

产物取证三链（comment 版本 / DW_AT_producer 指纹 / SIMD 兜底）已迁至 binary-compile-forensics skill）；本目录不再维护该脚本。

## allocator_ab.sh

分配器三方交替对照协议（SKILL.md 步骤 5）。

```bash
bash scripts/allocator_ab.sh -c "taskset -c 288-295 ./futures_benchmark --benchmark_filter=twoThens" -r 5
```

自动探测系统可用的 jemalloc/tcmalloc 库，glibc 作为基线，每轮三方交替（防共享机器慢漂移伪装差异）。判定纪律与 SKILL.md 三级健康度一致。**任何分配器结论不可跨负载形态搬运。**

## 通用纪律

- 三个脚本都只读产物/只跑 benchmark，不修改被测对象
- `strings`/`nm`/`readelf` 来自 binutils，`ar`/`objdump` 同源；无 third-party 依赖
- 脚本输出中文，失败语义明确（exit 2 = 参数错误，verify 的 exit N = N 项 FAIL，allocator 的 exit 1 = 环境不可对照或测量失败 fail-closed）

> **inspect_build_flags.sh 已拆分**：产物取证三链（comment 版本 / DW_AT_producer 指纹 / SIMD 兜底）迁至 binary-compile-forensics skill（ skill（安装后位于 skills/binary-compile-forensics/）），本目录不再维护。
