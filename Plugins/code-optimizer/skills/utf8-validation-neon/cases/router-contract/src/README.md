# Fixture 来源声明

| 文件 | 来源 | 许可 |
|---|---|---|
| `utf8_range.c`、`utf8_range.h`、`range-neon.c`、`naive.c` | protobuf-27.3 `third_party/utf8_range/`（上游捆绑副本，未改动快照） | 上游 utf8_range 项目（MIT-style，见文件头 Google/作者声明） |
| `utf8_range_opt.c` | `utf8_range.c` 应用本 SKILL 分发补丁模板生成的形态（+13 行） | 同上 |

fixture 与真实源码树解耦：用例仅验证本 SKILL 规定的改写语义，不依赖外部仓库存在。
