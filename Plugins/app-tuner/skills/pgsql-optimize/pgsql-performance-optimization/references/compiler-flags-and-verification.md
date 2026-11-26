# 编译选项与产物验证

> 机制与口径见 姊妹 skill pgsql-build-deployment 的 build-from-source.md（按域路由定位） §3；
> 本页聚焦验证与脚本使用。

## 1. 选项速查

| 选项 | 生效判定 | 风险 |
|------|---------|------|
| `-flto`（LTO） | `pg_config --configure` 含该 CFLAGS；火焰图帧名 `.lto_priv` | 归因伪影（顶层小函数帧）；编译时间 |
| LSE（`-march=...+lse` 或 `-moutline-atomics`） | objdump 计 LSE 指令族（ldadd/cas/swp/ldclr/ldeor） | 老机型 illegal instruction；outline-atomics 运行时自选路径是兼容折中 |
| `-O2` vs `-O3` | `pg_config --configure` | PG 上游默认 -O2；-O3 无系统性收益证据，不做默认推荐 |

## 2. verify_build_opts.sh（产物身份核验）

```bash
bash scripts/verify_build_opts.sh --binary "${PG_BIN}/postgres" \
  [--expect-configure-token flto] [--expect-lse] [--lse-min N] \
  [--expect-symbol SYMBOL ...]
```

- `--expect-configure-token`：configure 串包含令牌（如 `--with-icu`、`flto`）；
- `--lse-min N`：LSE 指令计数下限，**须正整数**（0/负数按用法错误退出 2——
  无 LSE 的二进制会通过 0≥0 的空检查）；
- `--expect-symbol`：nm 反修饰（-C）输出按 **grep -F 字面匹配**（符号名非正则；
  `operator[]` 类符号作为正则会解析成空字符类报错）；可重复；
- 退出码：0=全过（含 WARN 不计）；1=存在 FAIL；2=用法错误（含 PG_CONFIG 指向
  非同目录程序）；
- **信任前提**：脚本会以调用方权限执行 pg_config（外部程序）——只执行命令行
  显式传入的 `--pg-config`、目标二进制同目录的 pg_config 或 PATH 中的 pg_config；
  `PG_CONFIG` 环境变量仅当恰指向二进制同目录的 pg_config 时作为定位提示生效，
  指向其他任意可执行文件（含按 CWD 解析的相对路径）一律 exit 2 拒绝执行——
  多租户构建机/CI 环境下防「只读核验」被用作任意代码执行入口；A/B 轮要指向
  对照版/实验版的 pg_config 时用 `--pg-config` 显式传入。

## 3. 判读口径

- **身份以 pg_config --configure 为准**，不凭记忆复述 configure 串；
- 符号证据只做存在性核对；产物被 strip 时记“不可用”，不用其他符号冒充；
- LSE 计数=0 只说明“未生成 LSE 路径”——若是 outline-atomics 构建，运行时才
  选路径，objdump 静态计数不可作 LSE 生效与否的最终判定（记口径注明）。

## 4. A/B 轮的编译侧纪律

- 对照/实验二进制只差目标编译选项，configure 其余串完全一致；
- 每个二进制记录 md5 + configure 串（身份入 A/B 报告）；
- 编译选项变更的收益同样走 ≥3 轮口径与 <5% 噪声阈值——编译侧不是免检通道。
