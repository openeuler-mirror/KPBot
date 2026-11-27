# 分配器三方对照 — 实测数据与部署检查

> 配套 SKILL.md 步骤 5。协议脚本见 `allocator-ab-trial` skill 的 `scripts/allocator_ab.sh`（安装平铺布局下位于 skills/allocator-ab-trial/scripts/）。核心结论：**分配器选择没有普适答案，必须按负载形态三方实测**。

## 实测数据（folly future 链路，小对象高频构造负载）

| 分配器 | twoThens 等用例 | 结论 |
|---|---|---|
| glibc（基线） | 基准 | 持平参考 |
| tcmalloc | 提速 +8% | **本形态推荐**（malloc 面收益，见归属判决案例一） |
| jemalloc | **耗时 +69.5%**（耗时口径：性能约 -41%） | 本形态**禁用** |

⚠️ **口径说明**：负结果数字为耗时变化口径（jemalloc 下用例耗时增加 69.5%，折算性能下降约 41%）。跨口径混读（把 +69.5% 耗时当性能 -69.5% 即耗时 ×3.3）会放大 2 倍——报数时必须声明口径。

## 为什么 jemalloc 在此负载翻车

folly future 链路的分配特征：**高频小对象构造/析构**（每个 then 节点的 Core、CoreCallbackState、Function 等，生命周期短、尺寸小）。jemalloc 的 arena/页管理策略在"大量小对象快速生灭"的负载下产生更高的元数据与回收开销；tcmalloc 的 thread-cache 模型正对此形态。**换一个负载形态（大对象、长生命周期、多线程争用主导）结论可能完全反转**——这正是"不可跨形态搬运"的原因。

## 部署前检查清单

1. **确认现状**（最重要）：
   ```bash
   cat /proc/<pid>/maps | grep -E 'jemalloc|tcmalloc'
   ```
   - 无命中 → glibc，可用 LD_PRELOAD 切 tcmalloc 试验
   - 已在用 jemalloc → 本负结果数字**不适用**（那说明业务曾因别的形态收益切过 jemalloc），需按当前形态重新三方对照
   - 已在用 tcmalloc → malloc 面收益已在账上，勿重复计算
2. **LD_PRELOAD 而非重链**：不改构建链即可验证，先 `allocator_ab.sh -r 5` 拿交替数据，方向 5/5 稳定再考虑固化
3. **切换后回归**：分配器影响的是全局分配行为，切换后跑全量回归（不只 future 用例）
4. **容器/沙箱环境**：LD_PRELOAD 在容器内需确认目标 so 可见（挂载路径一致）

## 与其他 skill 的边界

- 独立的分配器选型请求（不涉及 folly 优化流程）→ app-tuner 的 `performance-library-selection`
- 本档案只覆盖"folly future 链路优化流程内"的分配器环节
