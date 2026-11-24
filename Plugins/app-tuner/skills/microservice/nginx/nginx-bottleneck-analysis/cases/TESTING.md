# nginx-bottleneck-analysis 用例运行指南

本目录是 `nginx-bottleneck-analysis` skill 的用例集（自 `nginx-optimization` 拆分而来，新增判定用例）。

当前用例：

- `agent-hotspot-classify` — tier agent：两个 mock 证据场景（A：单核 %soft=45% + 单队列中断 80% → 中断不均/环境级；B：TLS 符号聚合 35% → 握手层/配置级先行），验证 agent 按判定树输出正确类别、量化依据与路由。**A/B 对照**：with（接入 skill）vs without。

## 运行方法

在 KPBot 插件根目录：

```bash
docker build --network host \
  -f skills/microservice/nginx/nginx-bottleneck-analysis/cases/agent-hotspot-classify/Dockerfile \
  -t kpbot/app-tuner-agent-nginx-hotspot-classify:1.0-openeuler24.03 .

bash skills/microservice/nginx/nginx-bottleneck-analysis/cases/agent-hotspot-classify/run_ab.sh --save results-ab
```

前提：opencode 二进制与 auth.json 运行时挂载、容器可访问模型 API。
