# scripts/ — folly-future-optimization 配套脚本

一个脚本为**产物级**工具，与构建系统无关（cmake/make/blade 等均适用），知识出处与使用位置见 SKILL.md 对应步骤。

> 产物取证三链（原 inspect_build_flags.sh）已拆分至 `binary-compile-forensics` skill；分配器三方对照（原 allocator_ab.sh，步骤 5）已拆分至 `allocator-ab-trial` skill（microservice/），用法与退出码契约见各自 skill 的 scripts/README.md。

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

## 通用纪律

- 脚本只读产物/只跑 benchmark，不修改被测对象
- `strings`/`nm`/`readelf` 来自 binutils，`ar`/`objdump` 同源；无 third-party 依赖
- 脚本输出中文，失败语义明确（exit 2 = 参数错误，verify 的 exit N = N 项 FAIL）
