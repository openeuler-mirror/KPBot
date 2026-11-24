---
name: prepare-fastokens-candidate
description: >
  在独立目录准备指定 fastokens 官方版本，并用 vLLM 的分词器入口验证版本和后端是否生效。
---

# 候选版本准备与加载验证

## 操作流程

1. 从现有 `probe.json` 或用户提供的 PyPI 官方 wheel 确定目标版本（不低于 0.3.5）。核对 wheel 的平台及架构标签；Linux 上其 manylinux 标签要求的 glibc 版本不能高于 `probe.json` 记录的版本。探测确认 Rust 前端或缺少 Python 切换能力时，不进行候选安装。
2. 使用新的候选目录 `$OVERLAY`。已有候选目录可直接验证；探测确认环境已安装所需官方版本时，跳过安装。其他情况按环境选择一种方式：

   | 条件 | 命令 |
   |---|---|
   | pip 可访问 PyPI 或镜像 | `"$PY" -m pip install --no-deps --target "$OVERLAY" "fastokens==$VERSION"` |
   | 使用 uv | `uv pip install --python "$PY" --no-deps --target "$OVERLAY" "fastokens==$VERSION"` |
   | 使用用户提供的官方 wheel | `"$PY" -m zipfile -e "$WHEEL" "$OVERLAY"` |

   不在服务的 site-packages 里安装。解包后应有 `fastokens` 和 `fastokens-<版本>.dist-info`；解包不会检查平台兼容性，需运行下一步。
3. 使用服务的 Python 和实际分词器；有自定义分词模式或 revision 时传入相同值。

   ```bash
   "$PY" "$S/scripts/fastokens_segment_cache.py" probe --model "$MODEL" \
       --overlay "$OVERLAY" --target-version "$VERSION" --offline --out "$OUT"
   ```

   验证服务环境中已安装的目标版本时，从上例中去掉 `--overlay "$OVERLAY"`。

4. 检查 `probe.json` 的 `load_candidate`：`ok` 为真，`backend_module` 为 fastokens 实现，`fastokens_version` 等于目标版本，且有段缓存统计接口（`has_stats`）。以实际字段判定，不以 `probe` 退出码或 `verdict` 中的版本提示代替。加载通过只说明候选可用，是否命中由回放验证。

## 输出

记录候选版本、来源、候选目录和加载验证结果。结果另存，不覆盖当前分词后端分析的 `probe.json`。加载不符时说明原因，不进入候选回放。
