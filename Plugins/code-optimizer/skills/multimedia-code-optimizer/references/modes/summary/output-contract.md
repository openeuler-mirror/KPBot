# 输出契约

本文件规定三件事：Agent 交给合并脚本的 bundle JSON 长什么样、合并后的文件长什么样、失败时怎么收场。照抄字段名，不要自造同义字段。

## 1. bundle JSON

```json
{
  "mode": "C_NEON",
  "methods": [
    {
      "slug": "multi-accumulator",
      "title": "用多累加器拆分隐藏归约依赖",
      "scenario": [
        "长循环中单个向量累加器形成依赖链，限制流水并行"
      ],
      "points": [
        "按 4 份独立累加器拆分归约变量",
        "循环结束后两两合并，再统一横向归约"
      ],
      "examples": [
        {
          "note": "int16 残差平方和",
          "lang": "c",
          "before": "int32_t sum = 0;\nfor (int i = 0; i < n; i++)\n    sum += (int32_t)src[i] * src[i];",
          "after": "int32x4_t a0 = vdupq_n_s32(0), a1 = vdupq_n_s32(0);\nfor (int i = 0; i < n; i += 16) {\n    int16x8_t v = vld1q_s16(src + i);\n    a0 = vmlal_s16(a0, vget_low_s16(v), vget_low_s16(v));\n    a1 = vmlal_high_s16(a1, v, v);\n}\nint32_t sum = vaddvq_s32(vaddq_s32(a0, a1));"
        }
      ]
    }
  ]
}
```

字段：

| 字段 | 必需 | 类型 | 规则 |
| --- | --- | --- | --- |
| `mode` | 是 | string | `C_NEON`、`C_SVE`、`NEON_SVE` 三选一，按代码实际 ISA 归类 |
| `methods` | 是 | array | 非空；同一批可以有多个方法 |
| `methods[].slug` | 是 | string | kebab-case，`^[a-z0-9]+(-[a-z0-9]+)*$`；跨批次稳定 |
| `methods[].title` | 是 | string | 一句话说清手段；不带项目名/函数名 |
| `methods[].scenario` | 是 | string 或 array | 通用场景；数组更清晰，合并时用 `；` 连接 |
| `methods[].points` | 是 | array | 至少 1 条；写动作和约束，不写代码复述 |
| `methods[].examples` | 是 | array | 至少 1 个；每个示例必须有 `before` 和 `after` |
| `methods[].examples[].note` | 否 | string | 这段代码在做什么；不带函数名/文件/项目 |
| `methods[].examples[].lang` | 否 | string | `c`（默认）或 `asm`；修改前和修改后相同时使用 |
| `methods[].examples[].before_lang` | 否 | string | 覆盖修改前的围栏语言，例如 C→SVE 的修改前是 `c` |
| `methods[].examples[].after_lang` | 否 | string | 覆盖修改后的围栏语言，例如 C→SVE 的修改后是 `asm` |
| `methods[].examples[].before` | 是 | string | 修改前代码切片，保留必要上下文 |
| `methods[].examples[].after` | 是 | string | 修改后代码切片，保留必要上下文 |

约束：

- `before` / `after` 不要写文件路径、行号、`// filename:line` 注释；代码里本来有版权头或作者注释时删掉。
- 同一方法有多个函数时，`examples` 放多个对象，不要只留一个“代表”。
- 同一方法在不同批次重复提交是安全的：合并脚本按 `slug` 合并，按代码内容去重。
- 不要把扫描器输出直接改写成 bundle；候选里的 `file`、`line`、`name` 不能出现在 bundle 的文本字段里。

## 2. 目录与文件格式

```
summary/
├── README.md                     # 由脚本重建的导航
├── manual/                       # 用户维护；只在缺失时创建
└── auto/
    ├── C_NEON/
    │   ├── summary.md
    │   └── patterns/
    │       └── <slug>.md
    ├── C_SVE/
    │   └── ...
    └── NEON_SVE/
        └── ...
```

### 2.1 `summary/auto/<MODE>/summary.md`

由合并脚本维护。结构固定为：TOC 占位块 + 每个方法一个一级标题。

```markdown
<!-- simd-methods:toc:start -->
## 方法目录

1. [用多累加器拆分隐藏归约依赖](#用多累加器拆分隐藏归约依赖)
2. [用谓词化尾部合并标量收尾](#用谓词化尾部合并标量收尾)

<!-- simd-methods:toc:end -->

# 用多累加器拆分隐藏归约依赖
- 场景：长循环中单个向量累加器形成依赖链，限制流水并行
- 手法：[patterns/multi-accumulator.md](patterns/multi-accumulator.md)
```

- 方法标题必须是一级标题；TOC 用 `1.` 有序列表。
- `- 场景：` 一行；多个场景用 `；` 分隔。
- `- 手法：` 指向本模式 `patterns/` 下的文件。
- 不要手工插入二级标题充当方法；合并脚本按一级标题解析。

### 2.2 `summary/auto/<MODE>/patterns/<slug>.md`

每个方法一个文件，至少两个一级标题：

````markdown
# 修改要点

- 按 4 份独立累加器拆分归约变量
- 循环结束后两两合并，再统一横向归约

# 修改代码示例

## 示例 1：int16 残差平方和

### 修改前

```c
int32_t sum = 0;
for (int i = 0; i < n; i++)
    sum += (int32_t)src[i] * src[i];
```

### 修改后

```c
int32x4_t a0 = vdupq_n_s32(0), a1 = vdupq_n_s32(0);
for (int i = 0; i < n; i += 16) {
    int16x8_t v = vld1q_s16(src + i);
    a0 = vmlal_s16(a0, vget_low_s16(v), vget_low_s16(v));
    a1 = vmlal_high_s16(a1, v, v);
}
int32_t sum = vaddvq_s32(vaddq_s32(a0, a1));
```
````

- `# 修改要点` 和 `# 修改代码示例` 必须原样出现；校验脚本按这两个标题检查。
- 每对示例用 `## 示例 N：...` 分节；`### 修改前` 和 `### 修改后` 各一个代码块。
- 同一方法多个函数时追加 `## 示例 2`、`## 示例 3`，不要合并成一个大代码块。
- 代码围栏用 `c` 或 `asm`；不要把整段文件贴进来。

### 2.3 `summary/README.md`

由脚本按三种模式的方法数和 `manual/` 下的文件重建。不要手工往里塞项目名、路径或性能数据；它只是导航。

## 3. 合并语义

`merge_summary.py` 对每个 `methods[]`：

1. `slug` 已存在（pattern 文件存在或 `summary.md` 有链接）→ 合并：
   - 新场景不在旧场景里 → 追加到 `- 场景：` 行，用 `；` 连接；
   - 新要点不在旧要点里 → 追加到 `# 修改要点`；
   - 新示例的 `before`/`after` 与已有示例都不同 → 追加 `## 示例 N`；
   - 标题保留旧的，避免历史链接失效。
2. `slug` 不存在 → 新建 pattern 和 `summary.md` 条目。
3. 重建 TOC 和 README。
4. 不删除、不重排、不覆盖已有要点/示例；`summary/manual/` 永远不写。

去重按空白归一化后的文本比较，所以同一段代码换个缩进不会被重复追加，但改写过的代码会被当成新示例。**不要为了“去重”删掉有差异的示例**：差异本身就是方法的一部分。

## 4. 失败路径

| 情况 | 处理 |
| --- | --- |
| 路径不存在/不是目录 | 不写 bundle，`success=false`，`error_message` 说明哪个路径 |
| 新旧路径相同 | 不启动扫描，要求用户重新指定 |
| 扫描无候选 | 不生成空方法；`success=true` 但 `scanned.candidates=0`，`warnings` 说明范围和可能原因 |
| bundle 校验失败 | 修 bundle 后重跑；不要在脚本外手工改 `summary.md` 绕过 |
| 合并脚本报 `slug must be kebab-case` | 改 slug，不要改成文件名式 |
| 合并脚本报 `missing examples` | 补至少一对 before/after |
| `validate_summary.py` 报来源/性能内容 | 删掉对应文字或代码注释，重跑；不要关闭校验 |
| 只完成部分候选 | `coverage=partial`，`warnings` 写清剩余目录/文件族 |

## 5. 最终输出 JSON

```json
{
  "simd_summary_result": {
    "success": true,
    "mode": "project",
    "paths": {"project": "<project-root>", "old": "", "new": ""},
    "scanned": {"files": 0, "functions": 0, "candidates": 0, "verified_pairs": 0},
    "by_pair": {"C_NEON": 0, "C_SVE": 0, "NEON_SVE": 0},
    "methods": {"added": 0, "merged": 0, "examples_added": 0},
    "summary_paths": ["summary/auto/C_NEON/summary.md"],
    "coverage": "complete",
    "warnings": [],
    "error_message": ""
  }
}
```

`coverage` 的允许值固定为 `complete` 和 `partial`；不要新增别的取值。
`success=false` 时：`methods.added` / `methods.merged` / `examples_added` 为 0，`summary_paths` 为空数组，`coverage` 为 `partial`，`error_message` 必填。
