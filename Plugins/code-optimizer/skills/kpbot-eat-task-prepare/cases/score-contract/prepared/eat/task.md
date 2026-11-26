# 减少查找目标所需的数组读取次数

## Hard Requirements (MUST be in every agent's prompt)

- 只修改 `candidate.py` 中的 `locate(size, key, read)`。通过 `read(index)` 读取数组，返回目标的位置，目标缺失时返回 `-1`。实现应支持任意有效输入。
- 保持 `bench.py`、`eat/`、用例和基线数据固定。每个必测用例均须返回正确位置，并至少读取一次数组。

在任务仓库根目录运行 `bash eat/eval.sh`。脚本打印逐用例答案和读取次数的保存路径。short、long 的原版读取次数分别是 4、16。两项正确时，分数是两个“原版读取次数 / 候选读取次数”的几何平均。任一用例答错或读取次数为零，整轮得零分。原版得 1，分数越大越好。
