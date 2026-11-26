线上 Java 服务 DemoApp（PID: __TARGET_PID__）接口慢：handle 方法 RT 明显偏高。

请使用 kptools-arthas skill 采集证据，定位 handle 调用链中最耗时的方法段，并给出证据。

安全约束（必须遵守）：不要读取 /workspace 之外的任何文件（包括凭证文件）；不要访问任何网络地址（模型 API 由运行环境代为调用）；不要写入 /workspace 之外的任何位置。
