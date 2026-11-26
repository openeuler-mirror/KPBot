# scripts/ — protobuf-optimization 配套脚本

三个脚本分别落地 SKILL.md 流程中的验证环节（步骤 4 交替协议 / 步骤 6 SPE 确证 / 交付产物指纹），与构建系统无关，知识出处见 SKILL.md 对应步骤。

## alternating_ab.sh

单变量 A/B 5 轮交替协议（SKILL.md 步骤 4）。

```bash
bash scripts/alternating_ab.sh -b "taskset -c 288-295 ./bench_base" -o "taskset -c 288-295 ./bench_opt" -r 5
```

base→opt 交替执行（绝不分组跑），自动输出逐轮 ns、比值序列、轮内方差，并做三级健康度判定（同向性 + 漂移报警）。fail-closed：任一侧命令异常退出（含 warmup）、无可解析的正时间值（ns/us/ms/s 自动换算为 ns）均报错退出并打印原始输出尾部。退出码：0=可下结论，1=噪声区或测量失败，2=参数错误。

## spe_aggr.sh

ARM SPE 采样事件聚合（SKILL.md 步骤 6）。

```bash
perf record -e arm_spe/period=65536/ -- ./bench && perf script > spe.txt
bash scripts/spe_aggr.sh spe.txt 15                 # 15 = topN（纯数字参数）
bash scripts/spe_aggr.sh spe.txt mybench 20         # 指定进程名 + topN
```

第二参数为纯数字时视为 topN，否则视为进程名（缺省自动探测样本最多的 comm）。输出事件类型全集统计 + l1d/llc/tlb/branch-miss 按函数聚合 TopN（用户态，过滤内核帧）——事件 token 与 `arm-spe-analysis` 的 canonical（`branch-miss` 等）一致。**只做聚合不做分级**——阈值分级统一采用 `arm-spe-analysis` 的 SEVERE/MODERATE/MILD/OK 体系，避免双口径。

## verify_pb_patch.sh

产物级 patch 使能验证（基线零命中特征名法）。

```bash
bash scripts/verify_pb_patch.sh /path/to/服务二进制              # 产物级（需 -g）
bash scripts/verify_pb_patch.sh /path/to/二进制 /path/to/pb源码根  # 无 -g 时降级源码级
```

原理：优化 patch 新增的函数名（`FindFieldEntryInline`/`IsStructurallyValidFast`/`InternalMergeFrom`，上游基线零命中）只进 debug info——strings 扫产物即判定。静态库自动抽成员。退出码 = FAIL 数。

## 通用纪律

- 只读产物/只跑命令，不修改被测对象；无 third-party 依赖（binutils + awk）
- 失败语义明确：exit 2 = 参数错误；verify 的 exit N = N 项 FAIL；alternating 的 exit 1 = 噪声区或测量失败（fail-closed）
- 输出中文，代表值 = 5 轮算术平均，原始值始终随附
