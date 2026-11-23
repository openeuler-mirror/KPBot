# Go pprof 火焰图管线（实测校验版）

蓝本：CubeSandbox CubeMaster（Go 服务，/debug/pprof 端点）create-path 优化报告的
火焰图制作全程，含一次返工事故的根因。

## 管线总览

```
目标服务 /debug/pprof (端口从服务配置读)
  → curl profile?seconds=N  (或 load 期间采 CPU profile)   [.pb.gz]
  → go tool pprof -top  <pb.gz>          # 函数表（对账用）
  → go tool pprof -traces <pb.gz>        # 符号化调用栈文本（折叠源）
  → python3 fold_traces.py traces.txt out.collapsed   # 本 skill scripts/ 目录
  → flamegraph.pl out.collapsed > full.svg
  → (可选) grep 过滤 collapsed 得聚焦视图 / 用两臂 collapsed 做差分图
```

## 铁律 1：折叠源必须是 -traces，禁止解析 -raw

`pprof -raw` 输出的位置列是**数字 location ID**，不是符号名——按"函数名"解析它
产出的 SVG 全是垃圾帧（实证：第一批三张图全废，返工重做）。

`-traces` 输出格式（叶子帧在前，块间虚线分隔）：

```
-----------+-------------------------------------------------------
     460ms   runtime.stealWork
              runtime.findRunnable
              runtime.schedule
-----------+-------------------------------------------------------
```

fold_traces.py 做三件事：块内逆序（collapsed 要求根帧在前）、权重归一化到 ms
（兼容 "460ms" 和 "1.23s"）、剥 `(inline)` 尾注。自带校验输出（总权重 + top-10
帧份额）。CLI 契约见 [../scripts/README.md](../scripts/README.md)。

## 铁律 2：折叠后三道校验门，全过才能用

| 门 | 校验什么 | 怎么验 |
|---|---|---|
| 权重门 | collapsed 总权重 ≈ pprof 头部 `Total samples` | fold 输出的 total vs `pprof -top` 首行（实证：125.94s/105.82s 两臂都对上） |
| 帧门 | top 帧与 `-top` 表一致 | 逐个比对前 10 |
| 语义门 | 预期消失的函数在 patched 视图确实消失、预期保留的在两视图都在 | grep collapsed 文件逐项确认（实证：QueryContext/Resolve/ListReplicas base 有 patched 无；handler 入口两臂都有） |

差分图额外门：diff 视图中蓝色区域 = 消除的路径，必须能在 base 全图里找到对应帧
（不是凭空出现）。

## 铁律 3：窗口与口径

1. **pprof 窗口 ≈ 负载时长**：负载 7.5s 就采 ~10s，别采 30s（后 22s 空闲 GC 噪声
   稀释信号；实证：30s 窗口被接受但如实披露了稀释问题）；
2. **切割范围 = handler 路径全帧**：按入口 handler（如 createSandbox）以下的栈
   过滤，**不过度滤 runtime.***——全滤掉后图只剩应用帧，丢失 GC/调度上下文，
   评审会判"看不出这 290ms 在等什么"（实证：被否决重做）；
3. **预期缺席要解释**：GC worker 跑在独立 goroutine，不在 handler 调用栈上——
   图里没有 gcDrain 不是丢帧，报告里写明，别让读者误以为采集失败。

## 差分图制作

```sh
# 两臂 collapsed 就绪后：
flamegraph.pl base.collapsed > base_full.svg
flamegraph.pl patched.collapsed > patched_full.svg
# 差分（蓝=patched 比 base 窄=被消除；红=patched 比 base 宽=新增路径）：
flamegraph.pl --negate base.collapsed patched.collapsed > diff_full.svg
```

注意 `--negate` 的方向语义随 flamegraph.pl 版本可能反转——**交付前打开 SVG 亲验**：
base 图里的 DB 读色带，在 diff 图里必须是蓝色。

## 报告引用规范

| 图 | 报告里的表述 |
|---|---|
| base_full.svg | "base 组 create 路径全帧视图（500ms），DB 读色带清晰可见" |
| patched_full.svg | "patched 组同口径视图（220ms），DB 读色带消失" |
| diff_full.svg | "差分视图：蓝色为被消除的读取路径" |

图的口径（哪些帧、什么负载窗口、总权重多少）必须写进报告附表，和 SVG 自身
title 一致。

## 环境坑（实测沉淀）

- 死代理环境变量挂在 shell 配置里：curl 拉 /debug/pprof 前先确认代理环境变量
  不是陈旧的（`unset http_proxy`），否则超时假象；
- 折叠/绘图在 Linux 侧做（flamegraph.pl、python3 现成），SVG 小文件 base64
  拉回本地给用户亲验后再入仓——**用户没亲验过的图不提交**；
- 本 skill 的 fold_traces.py 容忍 CRLF 输入（io.open universal newlines +
  rstrip 双保险），无需转换——但脚本文件本身如需 shebang 直执行必须 LF
  （`#!/usr/bin/env python3\r` 会被 env 视为解释器名的一部分）。
