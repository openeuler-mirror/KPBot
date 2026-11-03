请使用 openeuler-agentenv skill，在这台 openEuler 24.03 机器上完成 AgentENV（kvcache-ai）的构建环境准备与 release 编译。

要求：
1. 机器上没有 AgentENV 源码，也没有 Rust 工具链；源码自行从 GitHub 获取（kvcache-ai/AgentENV），建议放在 /workspace 下。
2. 完成编译所需的环境准备（下载通道与构建依赖），然后编译出三个 release 二进制：server、uvm-ublk、uvm-ublk-daemon。
3. 本机容器没有 /dev/kvm：只做环境准备与编译，不启动 server、不加载内核模块、不运行任何 Firecracker 相关组件。
4. 把结果写入 opt/build_report.json（相对 /workspace），JSON 格式：
   {"repo_dir": "<源码绝对路径>",
    "binaries": {"server": "<server 绝对路径>", "uvm_ublk": "<uvm-ublk 绝对路径>", "uvm_ublk_daemon": "<uvm-ublk-daemon 绝对路径>"},
    "cargo_version": "<cargo --version 输出>",
    "rustc_version": "<rustc --version 输出>",
    "build_command": "<实际使用的 cargo build 命令>"}
5. 不要修改 /workspace 下的 prompts/、verify.sh、entrypoint.sh、opencode.json；opt/ 目录下只写 build_report.json。
6. 完成后用一句话说明你选择的镜像源，以及 openEuler 上与常见 Linux 发行版不同的依赖包名。
