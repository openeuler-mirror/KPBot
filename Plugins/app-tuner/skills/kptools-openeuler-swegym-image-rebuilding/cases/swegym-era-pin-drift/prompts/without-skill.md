修复 /workspace 里这个 SWE-Gym 实例（swegym-mini-001）的环境漂移问题，让官方验收翻案为 gold=1。

要求：
1. 当前环境验收 gold=0。先复现：bash run_gold.sh 查看失败原因。
2. 漂移源定位必须讲证据；修复必须落在 env/requirements.txt（环境构建配置）上，然后 bash build_env.sh 重建环境。
3. 完成后重跑 bash run_gold.sh，直到 gold=1。
4. 把结果写入 opt/build_report.json（相对 /workspace），JSON 格式：
   {"drift_source": "<漂移源是什么，证据是什么>",
    "fix_description": "<怎么修的>",
    "base_commit": "<你用作时代锚点的 commit>",
    "era_pin": "<最终 pin 的包与精确版本>",
    "gold_final": 1}
5. 不要修改 /workspace 下的 tests_official/、run_gold.sh、build_env.sh、instance.json、prompts/、verify.sh、entrypoint.sh、opencode.json；opt/ 目录下只写 build_report.json。
6. 完成后用一句话说明：为什么修复必须锚定到特定版本而不是装最新版。
