# nginx-conf-tuning 用例运行指南

本目录是 `nginx-conf-tuning` skill 的用例集（自 `nginx-optimization` 拆分而来）。

当前用例：

- `agent-scene-config` — tier agent：mock 场景下验证 agent 能否按场景四要素做出正确的 nginx.conf 配置决策（worker 数/ssl session/keepalive/证书类型/掩码写法），不执行破坏性操作。**A/B 对照**：with（接入 skill）vs without。

## 运行方法

在 KPBot 插件根目录（包含 `skills/` 的目录）：

```bash
docker build \
  -f skills/microservice/nginx/nginx-conf-tuning/cases/agent-scene-config/Dockerfile \
  -t kpbot/app-tuner-agent-nginx-conf-scene-config:1.0-openeuler24.03 .

bash skills/microservice/nginx/nginx-conf-tuning/cases/agent-scene-config/run_ab.sh --save results-ab

# 运行全部用例并消费 case.yaml 断言（需 python3 + PyYAML）：
bash skills/microservice/nginx/nginx-conf-tuning/cases/run_all.sh --strict --out /tmp/nginx-conf-cases
```

前提：opencode 二进制以只读卷挂载（宿主路径需通过白名单/ELF 校验）；auth.json 整文件 base64 经 OPENCODE_AUTH_JSON 环境变量注入，不做卷挂载；容器默认 bridge 网络访问模型 API（确需 host 时显式 `DOCKER_RUN_ARGS=--network=host`）。

安全边界：
- agent 以非特权用户 `runner` 运行；评分链（entrypoint/verify/prompts）root 拥有且不可写，`.pristine.sha256` 在 agent 前后校验。
- `results/` root 700，agent 只写 `work/decisions.md`；verify 先 `rm -f results/result.json` 再以 root 落盘。
- 两臂 prompts 带逐字相同的安全约束（不读 `/workspace` 外文件/凭证、不发网络请求、不执行破坏性操作）。
