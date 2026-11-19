# AArch64 原子与同步指令手段

| 场景 | 指令手段 | 实现要点 |
|---|---|---|
| 原子加、交换、位更新的排他循环 | LSE：LDADD、SWP、LDSET/LDCLR/LDEOR | 由语言原子内建表达操作，支持 LSE 时生成直接原子读改写；减少 LDXR/STXR 软件重试，不消除共享地址争用 |
| 比较后条件更新 | LSE CAS/CASP | CASP 用于支持相应对宽操作的实现；复杂条件仍可能重试，不能把 CAS 指令视为整个算法无需循环 |
| 同时部署在有/无 LSE 的 CPU | `-moutline-atomics` | 基线函数通过运行时辅助例程选择 LSE 或排他循环；固定 LSE 目标可内联原子指令，减少辅助调用 |
| 无 LSE 的短原子更新 | LDXR/STXR；带顺序时用 LDAXR/STLXR | 让编译器生成重试循环，排他窗口内减少额外访存和调用；比较失败与独占写失败分别处理 |
| 发布/读取普通内存数据 | release store 对应 STLR，acquire load 通常对应 LDAR | 用语言原子内存序生成所需顺序；RCpc 目标上的 LDAPR 由编译器按适用语义选择，不手工替换所有 LDAR |
| 原子 RMW 不需要最强顺序 | 选择 relaxed / acquire / release / acq_rel 的原子内建 | LSE 按所需顺序选择无后缀、A、L、AL 形式，例如 CAS/CASA/CASL/CASAL；顺序取决于同步关系 |
| 自旋循环占用执行资源 | 短等待使用 YIELD；已有事件协议可用 WFE | YIELD 是执行提示，不是线程调度调用；WFE 配合条件重查和正确的事件唤醒，不能单独替代任意等待循环 |
