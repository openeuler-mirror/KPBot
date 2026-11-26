# TTFT 打点部署与回滚

使用真实服务的 Python 环境。脚本使用 Python 标准库。在原服务启动目录执行 `diagnose`，保留原目录对 Python 模块解析的影响。

```bash
PY=/path/to/serving/bin/python
CLI="$SKILL_DIR/scripts/ttft.py"
STATE=/path/to/persistent/ttft-state/new-attempt

"$PY" "$CLI" diagnose --python "$PY"
"$PY" "$CLI" inspect --python "$PY"
"$PY" "$CLI" plan --python "$PY" --repo vllm
"$PY" "$CLI" plan --python "$PY" --repo ascend
```

如环境必须用已安装的 uv 进入，使用 `uv run --no-project --offline --python "$PY" python "$CLI" ...`。记录包的来源、目标源码路径、版本和本地修改。源码兼容性以 payload 内容匹配为准。

`inspect.version` 是 Python 版本，不是 vLLM 版本。在同一解释器和启动目录，将 `ORIGIN` 设为 `inspect` 中对应模块的 `origin`，`DIST` 设为 `vllm` 或 `vllm-ascend`，用安装记录把版本与实际导入路径绑定：

```bash
"$PY" - "$DIST" "$ORIGIN" <<'PY'
from importlib.metadata import distribution
from pathlib import Path
import sys

dist = distribution(sys.argv[1])
origin = Path(sys.argv[2]).resolve()
if not any(item.locate().resolve() == origin for item in dist.files or ()):
    raise SystemExit(f"安装记录不包含实际加载路径：{origin}")
print(f"{dist.metadata['Name']} {dist.version}: {origin}")
PY
```

可编辑安装或源码构建若无法从安装记录关联，使用部署构建记录核实版本及其对应的实际加载路径。无法建立对应关系或不是目标 v0.27.1rc 基线时停止；目录名、Git HEAD 或 `plan` 通过都不足以证明版本。

先展示两份计划。确认先前部署已使用原状态安全回滚。用户同意且操作者已通过原有方式停止目标 Prefill 服务后，分别执行：

```bash
"$PY" "$CLI" apply --python "$PY" --repo vllm --state-dir "$STATE/vllm"
"$PY" "$CLI" apply --python "$PY" --repo ascend --state-dir "$STATE/ascend"
```

每个组件和每次尝试使用独立持久化状态目录。已有 `state.json` 会被拒绝。应用会重新校验匹配内容，并在写入任何源码前保存 `state.json`；局部冲突需要人工检查。若应用中断或只完成一侧，保持停服，保留状态和命令输出；取得授权后按对应状态回滚已改动的文件。应用完整成功后，操作者沿用原启动命令，再发送一条真实请求并收集 Prefill 日志，使用 `check` 和 `report` 验证。

## 模块遮蔽

`diagnose` 默认查询模块位置，不初始化业务包。可追加 `--traceback-file` 和 `--config-file` 读取已有的启动错误或已脱敏配置。`--probe-import mooncake.engine` 会实际初始化模块，仅在需要真实导入验证时使用。

现有 `mooncake.engine` 与 `mooncake.store` 是业务依赖。启动目录或 `sys.path` 中较早位置的 `mooncake.py` 可能遮蔽安装的包。用解析结果确认后，由操作者处理已知冲突文件。保留原始文件和运行证据。旧包目录保留备份，新包安装到单独目录。

## 回滚

用记录的 `state.json`，按应用的逆序恢复：

```bash
"$PY" "$CLI" rollback --state-file "$STATE/ascend/state.json"
"$PY" "$CLI" rollback --state-file "$STATE/vllm/state.json"
```

回滚先核对所有文件哈希，跳过仍是原始内容的文件，只恢复已打点的文件或移除本次创建的文件。发现不匹配的后续改动时，不覆盖任何文件，交由操作者检查。该状态仅覆盖本次 Python 源码修改；完成后由操作者用原命令恢复服务并验证。
