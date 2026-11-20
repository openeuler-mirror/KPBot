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

先展示两份计划。确认先前部署已使用原状态安全回滚。用户同意且操作者已通过原有方式停止目标 Prefill 服务后，分别执行：

```bash
"$PY" "$CLI" apply --python "$PY" --repo vllm --state-dir "$STATE/vllm"
"$PY" "$CLI" apply --python "$PY" --repo ascend --state-dir "$STATE/ascend"
```

每个组件和每次尝试使用独立持久化状态目录。已有 `state.json` 会被拒绝。应用会重新校验匹配内容，局部冲突需要人工检查。保留命令输出、状态和备份。若两次应用只有一次成功，先用对应状态回滚已成功的一侧。操作者沿用原启动命令，然后发送一条真实请求并收集 Prefill 日志，使用 `check` 和 `report` 验证。

## 模块遮蔽

`diagnose` 默认查询模块位置，不初始化业务包。可追加 `--traceback-file` 和 `--config-file` 读取已有的启动错误或已脱敏配置。`--probe-import mooncake.engine` 会实际初始化模块，仅在需要真实导入验证时使用。

现有 `mooncake.engine` 与 `mooncake.store` 是业务依赖。启动目录或 `sys.path` 中较早位置的 `mooncake.py` 可能遮蔽安装的包。用解析结果确认后，由操作者处理已知冲突文件。保留原始文件和运行证据。旧包目录保留备份，新包安装到单独目录。

## 回滚

用记录的 `state.json`，按应用的逆序恢复：

```bash
"$PY" "$CLI" rollback --state-file "$STATE/ascend/state.json"
"$PY" "$CLI" rollback --state-file "$STATE/vllm/state.json"
```

回滚会检查打点后的文件哈希。发现后续编辑时保留文件和报错，交由操作者检查。操作者用原命令恢复服务并验证。该状态仅覆盖本次 Python 源码修改。
