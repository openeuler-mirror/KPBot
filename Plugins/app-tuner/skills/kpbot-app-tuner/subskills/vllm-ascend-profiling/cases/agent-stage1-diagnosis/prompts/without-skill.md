请对 docker 容器 {{VLLM_CONTAINER}} 完成 vllm-ascend profiling 前的环境诊断（Stage-1），确认它是否具备采集 profiling 数据的条件。

要求：
1. 目标容器内运行着 vLLM 服务，诊断侧重：容器/进程可达、vllm 进程内存分配器环境、Python 包加载方式（PYTHONPATH 指向的 vllm checkout 是否带 TTFT 打桩）、vllm-ascend 版本与内在补丁特征、以及启动配置中 torch_profiler_with_stack 是否为 true（检查命令行参数与命令行引用的配置文件）。
2. 只诊断：不要重启 vLLM 服务，不要改变正在运行的 vllm 进程参数；版本不对的 vllm_ascend 问题只诊断不修复。
3. 诊断完成后，把结论写入 opt/diagnosis.json，格式：
   {"container": "<目标容器名>", "failed_checks": ["<失败检查项>", ...], "fix_plan": "<一段话修复方案>"}
   failed_checks 必须使用以下规范检查名，不得自造别名或中文：
   docker_daemon / container_running / vllm_process / PYTHONMALLOC / vllm_ttft_fork / vllm_ascend / vllm_config
   语义对照：docker 守护进程可达→docker_daemon；目标容器处于运行态→container_running；容器内存在 vllm 进程→vllm_process；
   内存分配器环境→PYTHONMALLOC；PYTHONPATH 指向的 vllm checkout 是否带 TTFT 打桩→vllm_ttft_fork；
   vllm-ascend 版本与内在补丁特征→vllm_ascend；torch_profiler_with_stack 等启动配置→vllm_config。
   全部通过时 failed_checks 为 []。
4. 不要修改 .opencode/、opt/、results/ 下其它文件；最终产物仅 opt/diagnosis.json。
5. 完成后用一段话说明诊断结论与修复计划。
