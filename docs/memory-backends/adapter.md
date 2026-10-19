# 记忆后端适配器开发说明

适用于 TencentDB Agent Memory、OpenViking、Mem0 OSS。各实现采用共同的输入输出约定，内部存储、原生接口组合和代码组织由开发者选择。

## 1. 功能与分工

```text
KPBot：初始化 → 检查状态 → 查找记忆 → 执行任务
                         → 提交任务过程及审核后的经验 → 关闭

KPBot → 北向接口（公共层）→ 南向接口（后端适配器）→ 记忆系统
```

- **KPBot**决定保存的内容、查询文本和问题特征。
- **公共层**负责配置选择、身份与查询范围检查、参数封装和返回格式转换，正文与查询信息保持不变。
- **后端适配器**负责实现本文第4节的方法，将请求映射到记忆系统，整理原生返回结果。
- **记忆系统**提供自身的存储、提炼、索引和检索能力。适配器直接复用这些能力，不另行开发内容提炼模型或搜索引擎。

适配器以本地Python组件运行，兼容Python 3.10及以上。后端可使用本地库、本机进程或本机服务；若依赖外部模型或服务，应在部署配置中说明。

## 2. 北向接口：KPBot如何使用

北向接口由公共层提供。下表用于说明适配器服务的场景，后端开发范围为第4节。

| 操作 | 输入 | 输出与用途 |
|---|---|---|
| initialize(config) | 后端选择、当前用户、后端配置 | 准备资源，返回初始化状态。 |
| status() | 无 | 判断是否已初始化、后端是否可用及故障原因。 |
| remember(record) | 类型、项目、任务和完整内容 | 保存审核后的经验或完整任务过程，返回来源标识及处理状态。 |
| recall(request) | 查询文本或问题特征、项目范围、是否包含共享记忆、数量 | 返回可供当前任务使用的相关记忆及来源。 |
| close() | 无 | 释放资源。 |

公开操作与底层调用可以是一对多关系。例如，提交任务过程可能涉及多次消息写入和会话结束操作；查找记忆可能涉及搜索和读取命中内容。按id读取、列举、删除暂不作为初版必备北向接口。适配器内部可按需使用这些原生操作。

初始化负责使组件可用。对象创建方式由实现说明，公共层取得对象后调用其初始化方法。

## 3. 数据和查询范围

### 两类输入

| kind | 内容 | 保存要求 |
|---|---|---|
| experience | KPBot已审核的经验，包含其提供的条件、结论和证据 | 完整保存，保持已有结论和证据。 |
| task_process | 完整任务过程，包括输入、执行记录、工具结果和最终结果 | 保留原始过程及顺序，可调用后端原生能力生成派生记忆。 |

content接受文本、JSON对象或数组。允许可还原的编码、分段和格式封装；中间层不摘要、不补写、不改变原有信息。一次提交可以对应多条底层记录。

**原始输入的保存与后端提炼分别处理。** 只得到几条提炼结果，不能视为完整任务过程已保存。原始输入及其定位信息应随记忆系统保存，适配器重启后仍可使用。

### 来源与可见范围

每次保存都分别保留project_id、owner_id、task_id和visibility，用于描述项目来源、记录归属、任务关联和可见范围。

- 初版visibility固定为private，支持查找当前用户在一个或多个指定项目中的记忆。
- 北向查询保留include_shared，默认false。初版尚未实现共享时，请求true明确返回UNSUPPORTED。
- 公共层根据当前用户和查询要求，生成允许访问的来源列表，再传给适配器；适配器严格按列表检索。
- 后续共享能力使用独立的可见范围与授权规则。来源字段描述记录归属，不能单凭指定他人的owner_id取得其记忆。

## 4. 南向接口：适配器需要实现什么

提供一个具有以下六个异步方法的对象。实现不负责KPBot的登录、业务判断或北向接口。

初始化成功后才调用保存和搜索方法；status与close可随时调用。除close外，各方法接收timeout，默认30秒且必须大于0，表示整个调用的等待时限，包含内部多次操作。数据方法支持并发调用。

统一成功返回：

```json
{"ok": true, "data": {"state": "ready", "message": "", "supports_shared": false}}
```

统一失败返回：

```json
{"ok": false, "error": {"code": "UNAVAILABLE", "message": "记忆系统暂时不可用", "source_id": null}}
```

成功只含ok、data；失败只含ok、error。error的三个字段均必填；source_id通常为null，已知某次提交的来源标识时填写。以下示例展示各方法成功时的data，返回值均使用普通JSON兼容类型。

### initialize(options: dict, *, timeout=30.0)

options是当前后端所需的配置字典，例如数据目录和模型设置；字段由实现定义并说明用途与默认值。初始化对象、加载资源并检查后端可用性，不产生业务记录。

成功返回状态对象，格式同status。相同配置重复初始化不得清空数据；更换配置时使用新实例。初始化失败可以再次调用。对象实例不固定绑定某个项目，数据来源由保存和查询参数指定。

### status(*, timeout=30.0)

返回：`{"state":"ready","message":"","supports_shared":false}`。

state取值：uninitialized（尚未初始化）、ready（检查通过，可接受操作）、unavailable（后端或必要依赖不可用）、closed（已关闭）。message说明故障或尚未检查的依赖。supports_shared表示是否已实现本系统的共享查询规则，初版为false。

status在初始化前和关闭后也可调用。状态检查只说明当前可用性，不保证后续每次请求成功，也不代替某次记忆处理的完成状态；未实际验证的模型或索引能力应在message中说明。

### store_experience(record: dict, *, timeout=30.0)

输入示例：

```json
{
  "kind": "experience",
  "project_id": "project-a",
  "owner_id": "user-01",
  "task_id": "task-101",
  "visibility": "private",
  "content": {
    "summary": "示例经验：减少循环中的重复搬移",
    "conditions": ["输入规模较小"],
    "evidence": ["示例证据文本，由KPBot提供"]
  }
}
```

六个字段均必填。project_id、owner_id、task_id为非空字符串；content的业务结构由KPBot决定，示例不限定其字段。公共层补充并核对owner_id，适配器保留这些来源字段。kind必须为experience。

返回：`{"source_id":"来源标识","processing":"unknown"}`。

- source_id为非空字符串，标识本次保存的完整输入，格式由实现决定，可以对应多条底层记录。
- 成功表示原始输入已完整保存。processing描述配置中启用的后续索引或记忆处理，取ready、pending、failed或unknown；没有可查询的进度时返回unknown，不能虚构后台任务状态。
- 后续处理失败但原始输入已保存时，保留source_id并返回processing=failed；无法确认原始输入是否完整保存时返回错误。
- 后续提交不得覆盖已确认保存的审核内容。超出支持的长度时返回TOO_LARGE并说明限制，不能静默截断。

### store_process(record: dict, *, timeout=30.0)

字段和返回格式与store_experience相同，kind必须为task_process。content示例：

```json
[
  {"role":"user","content":"示例任务：优化计算循环"},
  {"role":"assistant","content":"示例执行记录：检查重复搬移"},
  {"role":"tool","content":"示例工具结果：基准测试完成"},
  {"role":"assistant","content":"示例最终结果及说明"}
]
```

初版接收任务结束后的一次完整提交，不要求流式追加。可通过后端原生会话、消息或资源能力保存，并按配置进行记忆提炼和索引。分段后仍须保留原始内容、顺序及同一任务的关联。会话结束等原生操作由适配器完成。

公共层仅根据kind选择这两个保存方法，内容不作业务加工。

### search(request: dict, *, timeout=30.0)

输入示例：

```json
{
  "query": "循环中存在重复搬移，怎样降低开销？",
  "features": ["寄存器压力", "循环优化"],
  "sources": [
    {"project_id":"project-a","owner_id":"user-01"},
    {"project_id":"project-b","owner_id":"user-01"}
  ],
  "kinds": ["experience", "task_process"],
  "limit": 5
}
```

- 五个字段均必填。query为字符串，features为字符串数组，至少一项提供非空检索信息；kinds为两种输入类型的非空子集；limit为1..20的整数。
- sources为公共层核定的非空来源列表，初版只包含当前用户的来源。结果须同时匹配其中某一项的project_id和owner_id，且kind属于kinds。初版需实现同用户多项目查询和kind筛选；共享等尚未开放的请求返回UNSUPPORTED。所有来源均需查询，任一来源查询失败按实际错误返回，不能当作无命中。
- query和features是检索线索，复用后端的关键词、语义或混合检索。仅做原生接口需要的格式转换，不另行生成查询意图；例如可将关键词按固定格式拼接到原生query参数。
- features用于相关性检索，不表示每个词必须字面出现。project_id、owner_id、kinds用于限定结果范围。需要强制字段筛选的功能另行明确，不能混用这两种含义。

返回示例：

```json
{
  "items": [{
    "content": "示例：可将循环不变量对应的数据搬移到循环外。",
    "content_type": "derived",
    "kind": "task_process",
    "project_id": "project-b",
    "owner_id": "user-01",
    "task_id": "task-088",
    "source_id": "来源标识",
    "score": 0.72
  }],
  "partial": false
}
```

- items最多limit条，按后端相关性排序。每项包含示例中的全部字段；score保留原生数值，无分数时为null，不作为经验正确率。
- content_type取original（完整原始输入）、excerpt（原始过程片段）、derived（后端生成的记忆）。审核经验的命中应返回完整原文；任务过程可返回相关片段或派生记忆。需要补读内容时由适配器内部完成。
- project_id、owner_id、kind必须能确认并符合查询条件；无法确认范围的内容不得返回。task_id、source_id无法可靠追溯时填null，不编造来源。
- 无命中返回空items且partial=false；部分命中因读取失败被跳过时partial=true；全部命中因读取故障无法返回时报告READ_FAILED。整次操作失败按实际原因返回错误。

跨用户共享上线时，sources表示可查询来源，还必须同时应用记录可见性和授权规则，不能把某个用户或项目整体放开。

### close()

释放资源，成功返回null。重复关闭仍成功，已保存的数据保持不变。关闭后status可读取closed状态，数据方法返回NOT_READY；重新使用时创建新实例。

## 5. 必要约定与交付

| 错误码 | 含义 |
|---|---|
| INVALID_ARGUMENT | 参数缺失、类型错误或取值无效。 |
| NOT_READY | 尚未成功初始化，或已关闭。 |
| ACCESS_DENIED | 缺少所需权限。 |
| UNSUPPORTED | 当前实现不支持请求的功能或查询范围。 |
| TOO_LARGE | 输入超过支持的大小，message说明限制及单位。 |
| UNAVAILABLE | 所需后端、模型或存储不可用。 |
| TIMEOUT | 只读操作超时，或保存操作尚未开始执行就已超时。 |
| WRITE_UNKNOWN | 保存可能已经部分或全部执行，但无法确认完整结果；保留已知source_id。 |
| READ_FAILED | 检索命中后无法取得可返回的内容。 |
| BACKEND_ERROR | 其余后端错误，message说明原因。 |

保存操作不自动重试。开始执行后超时、取消或响应丢失，不能据此断言未保存。异步取消正常传播asyncio.CancelledError，由公共层按结果不确定处理。读取重试计入timeout。凭证和完整正文不写入日志。

交付适配器代码、依赖与配置说明，注明如何创建具有上述六个方法的对象，并附最小调用示例：初始化、状态检查、保存两类输入、跨两个项目查询、关闭。说明输入长度限制、索引可见性及任务过程的原文保留方式。

框架限制优先在适配器内部处理；确实影响共同功能或接口时，说明限制后统一调整，保持各实现的调用约定一致。
