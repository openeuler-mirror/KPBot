# UPSTREAM.md · 来源与版本溯源

本 skill 内容的权威性来源。**引用结论时优先使用目标工作区 dpdk 源码仓的
`doc/guides/`（版本精确）；本文件记录内容编写时的上游事实核对来源。**

## 核对来源

| 上游本地文档（dpdk/doc/guides/） | 上游 URL | 核对状态 |
|------------------------------|----------|----------|
| linux_gsg/sys_reqs.rst | https://doc.dpdk.org/guides/linux_gsg/sys_reqs.html | ✅ 实测抓取 |
| linux_gsg/linux_eal_parameters.rst | https://doc.dpdk.org/guides/linux_gsg/linux_eal_parameters.html | ✅ 实测抓取 |
| linux_gsg/enable_func.rst | https://doc.dpdk.org/guides/linux_gsg/enable_func.html | ✅ 实测抓取 |
| linux_gsg/nic_perf_intel_platform.rst | https://doc.dpdk.org/guides/linux_gsg/nic_perf_intel_platform.html | ✅ 实测抓取 |
| linux_gsg/amd_platform.rst | https://doc.dpdk.org/guides/linux_gsg/amd_platform.html | ✅ 实测抓取 |
| prog_guide/power_man.rst | https://doc.dpdk.org/guides/prog_guide/power_man.html | ✅ 实测抓取 |
| testpmd_app_ug/run_app.rst | https://doc.dpdk.org/guides/testpmd_app_ug/run_app.html | ✅ 实测抓取 |
| linux_gsg/index.rst（目录核对） | https://doc.dpdk.org/guides/linux_gsg/index.html | ✅ 实测抓取 |
| platform/index.rst（目录核对） | https://doc.dpdk.org/guides/platform/index.html | ✅ 实测抓取 |
| nics/mlx5.rst | https://doc.dpdk.org/guides/nics/mlx5.html | ✅ 26.11.0-rc2 本地工作区文档逐字核对（devargs 名称/默认值随版本变化快，引用前查目标环境版本文档） |

- 抓取时上游版本：**Data Plane Development Kit 26.11.0-rc2**（页面标题实测）。
- 抓取日期：2026-11-06。
- 目标环境 DPDK 版本可能不同：使用本 skill 前先确认目标环境版本
  （`dpdk-testpmd --version` / EAL 启动日志 / meson VERSION），
  引用参数与默认值前与对应版本文档核对。
- 引用但未逐字核对的文档（上游存在、结论按通用机制表述）：
  `prog_guide/mempool_lib.rst`、`prog_guide/mbuf_lib.rst`、`howto/rte_flow.rst`、
  `prog_guide/vhost_lib.rst`、`nics/*.rst`、`tools/proc_info.rst`、
  `howto/telemetry.rst`、`linux_gsg/cross_build_dpdk_for_arm64.rst`。
  —— 使用时以目标环境对应版本文档为准。

## 已知版本差异（写内容时注意）

1. **`--socket-mem`/`--socket-limit` → `--numa-mem`/`--numa-limit`**：26.11 文档仅列新名；
   旧版本仓可能仍用旧名。引用 EAL 参数时先查目标环境对应版本文档。
2. `--pagesz-mem <size>:<limit>`、`-R/--remap-lcore-ids`、`--huge-unlink[=mode]`、
   `--huge-worker-stack` 为较新参数，旧版本可能没有。
3. **mlx5 devargs 演进快**：`txq_inline` 已弃用并入 `txq_inline_max`；
   `txqs_max_vec`/`tx_vec_en`/`txq_max_inline_len`/`txq_mpw_hdr_dseg_en` 已弃用且被
   忽略（26.11 核对）；HWS 硬件 steering 用 `dv_flow_en=2`（旧版为独立 devargs）。
   引用 mlx5 参数前必须查目标环境版本文档。
4. 官方 platform 指南（26.11）目录：BlueField/cnxk/DPAA/DPAA2/MLX5/OCTEON TX——
   **无 Kunpeng 专页**；Kunpeng 920 相关以 `nics/hns3.rst` + 供应商 SoC 指南为准。
5. AMD：rte_power 自 23.11 LTS（amd_pstate passive）、uncore 自 25.03（HSMP）。
6. testpmd `--forward-mode` 合法值清单为 26.11 实测核对（含 recycle_mbufs、shared-rxq）。

## 维护约定

- 目标工作区 dpdk 仓升级时：diff 对应版本 `linux_gsg/linux_eal_parameters.rst` 与
  `testpmd_app_ug/run_app.rst`，更新 references/eal-args.md、observability-benchmark.md
  的默认值与参数表。
- 新增结论必须有出处（目标仓本地 doc 或本文档列表中的 URL），否则标注 UNVERIFIED。
