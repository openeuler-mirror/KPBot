# gcov 覆盖率 HTML 报告格式与判读指南

> `.gcov.html` 报告结构、行格式实测细节、计数语义、提取配方、循环迭代估算方法。
>
> 本文格式细节基于 lcov 1.14 / 1.16 / 2.2 实测验证。
>
> **边界**：本技能只读消费项目内已有的 `.gcov.html` 覆盖率报告（含配套
> `.func.html` 函数表），不执行任何覆盖率生成动作，也不依赖任何原始中间文件。

## 1. 覆盖率 HTML 报告是什么

`.gcov.html` 由 lcov/genhtml 工具链产出：`gcc --coverage` 插桩编译的程序运行后，
由 lcov 聚合数据再渲染为 HTML。每行代码附一个执行计数，计数来自生成数据时的
那次 workload。

本技能假定报告已存在于项目目录中（通常位于 `lcov-html/`、`coverage/` 等目录），
负责发现、解析与判读。

## 2. 报告结构

### 2.1 目录布局

```
<报告根目录>/                   ← 含 index.html 与 gcov.css
├── index.html                 ← 顶层汇总
├── gcov.css
└── <源码子目录镜像>/
    ├── index.html             ← 目录级汇总
    ├── foo.c.gcov.html        ← 逐行覆盖源视图（主数据源）
    ├── foo.c.func.html        ← 函数表：函数名 + hit_count（调用次数）
    └── foo.c.func-sort-c.html
```

每个源文件对应一个 `<源文件名>.gcov.html`，文件头部有该文件的汇总
（Lines/Functions 的 Hit/Total/Coverage）。

### 2.2 源视图行结构（实测 lcov 1.14 / 1.16 / 2.2）

**lcov 1.14 / 1.16**（`<a name>` 锚点 + `lineCov`/`lineNoCov`）：

```html
<pre class="source">
<a name="3"><span class="lineNum">       3 </span>            : </a>                        ← 不可执行行
<a name="8"><span class="lineNum">       8 </span><span class="lineCov">          1 : static void init_data(void)</span></a>   ← 覆盖, 计数 1
<a name="28"><span class="lineNum">      28 </span><span class="lineNoCov">          0 :     return -1;</span></a>            ← 可执行未覆盖, 计数 0
</pre>
```

**lcov 2.x**（`<span id="LN">` 锚点 + `tlaGNC`/`tlaUNC`）：

```html
<pre class="source">
<span id="L28"><span class="lineNum">      28</span> <span class="tlaUNC">           0 :     return -1;</span></span>
</pre>
```

**计数语义**（两种版本一致，判读时看"是否有覆盖 span"而非类名）：

| 形态 | 含义 |
|------|------|
| `12345`（数字） | 该行执行次数 |
| `0`（lineNoCov / tlaUNC） | 可执行但本次 workload 从未执行 |
| 无覆盖 span（仅 lineNum） | 不可执行行（注释、声明、空行等） |

### 2.3 函数表（`<源文件名>.func.html`）

```html
<tr>
  <td class="coverFn"><a href="demo.c.gcov.html#21">find_value</a></td>
  <td class="coverFnHi">1</td>
</tr>
```

`hit_count` 为**函数被调用次数**（lcov FNDA 语义），不是函数内行的执行次数。
lcov 1.14/1.16/2.x 函数表结构一致。

### 2.4 HTML 报告通常不含的信息

- 逐分支 taken/not-taken 走向数据 —— 分支优化证据只能从未覆盖行区间与源码
  形态间接推断，不得伪造分支结论
- 函数内行的归属：行号区间需结合源码判读函数边界

## 3. 提取配方（标准 shell 工具）

### 3.1 发现

```bash
find <项目根目录> -name '*.gcov.html' -not -path '*/.git/*' | head -50
```

### 3.2 源视图纯文本化（兼容 lcov 1.x / 2.x）

```bash
sed -n '/class="source"/,/<\/pre>/p' <src>.gcov.html \
  | sed -e 's/<[^>]*>//g' -e 's/&lt;/</g;s/&gt;/>/g;s/&quot;/"/g;s/&amp;/\&/g'
```

输出每行形如 `行号  计数 : 源码`（计数为空 = 不可执行，`0` = 未覆盖）。

### 3.3 过滤

```bash
# 高计数热点行（计数 ≥ 4 位数；阈值按数据量级调整）
... | grep -E '^[[:space:]]*[0-9]+[[:space:]]+[0-9]{4,}[[:space:]]*:'

# 未覆盖可执行行
... | grep -E '^[[:space:]]*[0-9]+[[:space:]]+0[[:space:]]*:'

# 按行号区间浏览（判读函数边界时）
... | sed -n '<起>,<止>p'
```

### 3.4 函数调用计数（func 页）

```bash
grep -oE 'class="coverFn"[^>]*><a[^>]*>[^<]*|class="coverFn(Hi|Med|Lo)"[^>]*>[0-9]+' \
  <src>.func.html | sed 's/.*>//' | paste - -
```

输出 `函数名<TAB>hit_count` 每行一对。

## 4. 循环迭代次数估算方法

对每个循环头（源码匹配 `for(` / `while(` / `do`）手工估算平均迭代次数：

- **进入次数** = 循环头上方最近可执行行的计数（嵌套循环时即外层循环头计数，
  为近似值）
- **体首行非循环头**：迭代 ≈ 体首行计数 ÷ 进入次数（含提前退出的场景仍近似成立）
- **体首行是循环头**（嵌套）：迭代 ≈ 头计数 ÷ 进入次数 − 1

示例（实测数据）：

```text
   201 :     for (int r = 0; r < repeat; r++)       ← 外层, 进入 1 次
205000 :         for (int i = 0; i < N; i++)        ← 内层, 进入 ~201 次
204800 :             acc += data[i];               ← 内层体
外层迭代 ≈ 201/1 − 1 = 200（真实值 200）
内层迭代 ≈ 204800/201 ≈ 1019（真实值 1024）
```

**误差来源**：进入次数近似（外层头计数含失败判定 +1）、continue/提前退出、
体首行距离循环头过远未计入。**量级判断可用，精确值不可信**：估算 1019 与真实
1024 同量级，均支持"值得向量化"的结论。

## 5. 版本兼容性实测结论

| 数据源 | 版本 | 结果 |
|--------|------|------|
| `.gcov.html` 源视图 | lcov 1.14 / 1.16 / 2.2 | 纯文本化后行号/计数语义一致，与 lcov 官方汇总数字吻合 |
| `.gcov.html` 函数表 | lcov 1.14 / 1.16 / 2.2 | `coverFn`/`coverFnHi` 结构一致，提取配方通用 |
| lcov < 1.10 HTML | 旧式表格式 | 配方不适用；尝试直接读取判读，仍不可解析时说明格式不兼容 |
