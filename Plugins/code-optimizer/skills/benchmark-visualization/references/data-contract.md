# 数据契约

## 文件与指标

配置包含 `input` 或 `inputs`、`mapping.metrics`、`charts`、`output`。

多个同构文件使用 `inputs: [{"path":"a.csv","format":"csv","dimensions":{"version":"A"}},{"path":"b.txt","format":"csv","dimensions":{"version":"B"}}]`。dimensions 为文件级分组字段，与记录冲突时报错。分隔符可用 delimiter 指定；异构日志先按 data-preparation.md 整理。每条图表数据保留来源文件与适配后记录索引。

`input.path` 相对于配置目录；format 为 csv、tsv、json 或 vector。JSON 默认是对象数组；嵌套数组用 `records_path: ["results", "rows"]` 定位。vector 适配 P0 原始输入，每条记录增加 dataset、side、source_index；不伪造独立 run ID。

每个指标声明 name、column、unit、sample_level，可选 label、scale。scale 是用户明确的单位换算因子，unit 必须填写换算后单位。sample_level 建议 independent_run、request、time_sample、summary。零值和负值允许；NaN、Infinity、布尔值、缺失指标报错，不静默排除。

宽表直接为不同列声明多个指标。长表将 value 列作为指标列，并在每幅图中用 filter 选择 metric_name 等分类字段。不同单位应分别声明并分图。

## 每幅图参数

| 参数 | 含义 |
| --- | --- |
| id | 唯一字母/数字/下划线/短横线标识，同时用于文件名 |
| type | scatter、line、point、bar、box、hist、ecdf、heatmap |
| metric | mapping.metrics 中的 name |
| x | 横轴列；省略时为单组 all |
| x_type | numeric 时转数值并排序；默认按类别显示 |
| series | 列名或列名数组，组合为系列 |
| facet | 列名或列名数组，组合为子图 |
| filter | 列名到允许值或允许值列表的映射；按原始值精确匹配，CSV 值为字符串 |
| aggregate | mean/median/min/max/count/std/mad/p90/p99 等；null 表示不聚合 |
| aggregate_summary | 现成统计值再次聚合需要显式 true |
| unit_id | 独立运行编号列；同 facet/series/x 内同编号重复报错 |
| spread | minmax、iqr、sd、mad；需要显式 aggregate |
| show_samples | line/point/bar/box 叠加原始点；scatter 聚合时叠加中心值 |
| order | 类别横轴完整顺序；每个分面必须匹配 |
| xlim / ylim | 两元素范围，如 [0, 500]；null 表示该端自动，例如 [0, null] |
| xscale / yscale | linear、log、symlog 等 Matplotlib 支持的尺度 |
| xlabel / ylabel / title | 用户指定标签和标题 |
| figsize / dpi | 整张图宽高（英寸）和导出分辨率 |
| columns | 分面列数，行数按数据自动计算 |
| colors | 自定义系列颜色列表，数量不足时循环 |
| marker / linewidth | 折线标记及线宽 |
| point_size / alpha | 散点大小和透明度 |
| rotation / grid | 类别标签旋转角和网格开关 |
| legend_loc / legend_fontsize | 图例位置及字号 |
| bins / density | 直方图分箱和密度开关；系列共用分箱 |
| cmap / vmin / vmax | 热力图色图和颜色范围 |

热力图中 x 为列，series（可多维组合）为行，facet 可继续拆图。每单元需要唯一值或显式 aggregate。折线遇到其他系列存在而本系列缺少的 x 时断线；所有系列都缺失的时间点无法自动推知，需由上游提供明确的网格。

output.formats 为 svg/png/pdf 数组，默认 svg 与 png。output.directory 可被 --output-dir 覆盖。允许同目录重绘。output.run_name 可设 v1、v2，前缀应用于图表、CSV、索引和全部 JSON；省略时，已有目录自动分配新前缀。若显式名字冲突则报错，旧文件不覆盖。

## 后续修改

使用输出的 chart-spec.resolved.json，修改 charts 中对应参数即可。输入已解析为绝对路径，可在其他目录复用。--input 可覆盖数据源，适用于相同列结构的新实验。

示例：把第一幅图纵轴放大到 500，设置 `charts[0].ylim=[0,500]`；切换折线为散点，设置 `type="scatter"`；增加分组列，设置 `series=["version","compiler"]`。标签和单位必须与实际数据一致。

## 长标签与重复实验

label_max_length 默认 24；类别横轴名称超过阈值时自动改为 A/B/.../AA 代号。label_map 可指定全称到短名称映射，完整映射写入摘要与索引；图右侧显示前 12 项，更长映射查附表。

多条观测的 line/point/bar 若未指定 spread，自动补充 minmax。heatmap 自动增加 -samples 散点伴随图；散点、箱线、直方图、ECDF 本身展示观测或分布。已有汇总没有离散数据时不能推测波动。
