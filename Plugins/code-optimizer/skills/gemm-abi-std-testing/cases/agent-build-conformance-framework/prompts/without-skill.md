你在 /workspace 下工作。

任务：为待测模块 `src/`（MiniGEMM，接口见 `src/kernel.h`）搭建一套《标准符合性测试框架》。
判定依据是官方标准文本 `std/mini_standard.md`（MS-1.0，含条款 MS-1 §1.1 ~ §1.4）。
参照实现（slotA）见 `bench/`（独立 oracle，仅作正确性参照，不参与判定）。

要求：
1. 条款驱动：每条断言绑定一条条款 ID（§1.1 / §1.2 / §1.3 / §1.4）；
2. 双槽对照：参照实现作 slotA，DUT 为 `src/`，违规按槽归责；
3. 输入由确定性 seed 派生，不得使用运行期计数器，保证可复现；
4. 多通道判定：数值精度、输入不可变（A/B 调用前后 hash 不变）、语义（quick return / empty sum / 零维）；
5. 产物全部放在 `opt/conformance/`，并提供一键入口脚本与 `TEST_CASES.md`。

产物目录：`opt/conformance/`。

请完成上述工作。
