# 项目数据与注解合同 v1

维护 HTML 注解时读本分册。[生成器](../scripts/build_study_view.py) 生成索引；agent 阅读源码后单独写注解，避免把自动分析与教学判断混在一起。

## 生成数据

`project-data.json` 的 `schema_version` 为整数 `1`。路径使用项目根下的相对路径、正斜杠，不能越界。

| 字段 | 内容 |
|---|---|
| `project` | `root` 绝对路径、`title`、`generated_at_utc` UTC 时间、`default_view`、`entry_node_ids` |
| `scan_options` | `include`、`exclude` 列表，`max_file_bytes`、`max_files` 正整数 |
| `coverage` | `scanned_files`、`included_files`、`parsed_files`、`indexed_only_files`，`excluded` 和 `analysis_notes` |
| `files` | 文件 `id`、`path`、`language`、完整 `source`、原字节 `sha256`、`size_bytes`、`analysis_status` |
| `nodes` | 文件和符号节点，字段见下表 |
| `edges` | 有明确类型、证据状态与源码出处的连接 |
| `lessons` | 编排的学习路线；自动扫描时为空 |

`analysis_status` 为 `parsed`、`indexed` 或 `parse_error`；只有成功解析的 Python 文件计入 `parsed_files`。UTF-8 BOM、CRLF 等在原文和哈希中保留，页面展示行号时统一识别换行。哈希匹配校验可发现源数据原文被误改。

## 节点与关系

| 节点字段 | 合同 |
|---|---|
| `id` | 文件 ID 固定 `file:src/main.py`；Python 符号通常 `symbol:src/main.py:Type.method`，重复定义可能有 `@行号` 后缀；使用实际生成 ID |
| `kind` | `file`、`class`、`function`、`method`、`external`、`unknown` |
| `name` | 显示名，不能用同名替代唯一身份 |
| `file_id` | 项目内定义的文件 ID；外部或无确定定义时为 `null` |
| `parent_id` | 同文件内父节点 ID 或 `null`，不允许循环 |
| `start_line`、`end_line` | 有源码时为真实的一基行范围，无本地定义时为 `null` |
| `role`、`explanation` | 普通文本；职责和代码讲解可为空，不能放要执行的 HTML |

每个文件也有同 ID 的 `file` 节点，`file_id` 指向自身。非 Python 手工新增符号建议使用 `manual:src/view.ts:draw` 等自己的 ID，必须提供完整字段并指向真实文件。已生成节点的注解只能包含 `id`、`role`、`explanation`。

每条边包含 `id`、`from`、`to`、`kind`、`status`、`label`、`evidence`、`note`。`kind` 为 `import`、`call`、`event`、`data`、`config`、`lifecycle`；`status` 为 `source_confirmed`、`inferred`、`unresolved`、`runtime_observed`，定义见 [navigation.md](navigation.md)。

`evidence` 是 `{file_id, start_line, end_line}` 列表。`source_confirmed` 至少有一个真实出处；事件或配置链通常需要注册和消费者的多处依据。`inferred` 在 `note` 说明推断前提；`unresolved` 说明待查信息；`runtime_observed` 的 `note` 附观察输入、版本、运行条件及实际证据位置，没有运行证据时不能使用此状态。

## 学习路线

每条路线包含唯一 `id`、`title`、`goal` 和 `steps` 列表。每站包含：

```json
{
  "node_id": "symbol:src/main.py:run",
  "title": "从入口读输入与输出",
  "explanation": "基于实际源码写职责、执行过程与关键写法，再说明下一站的关系。",
  "focus_lines": [3, 4],
  "question": "换一个明确输入时，返回值会怎样变化？请说明依据。"
}
```

上例仅展示格式，ID 和行号必须替换成当前索引中的实际值。`focus_lines` 不能超出对应文件；不需重点行时用空列表；不安排作答时 `question` 用空字符串。题目须有足够前提，不能先在题目或相邻解释中泄露待答结果。

## 注解文件

从 [空注解模板](../assets/annotations.template.json) 开始。根对象包含 `schema_version: 1` 和 `source_hashes`，可选 `project`、`nodes`、`edges`、`lessons`。`project` 只允许覆盖 `title`、`default_view`、`entry_node_ids`。默认视图只能是 `atlas`、`guided`、`reader`。

`source_hashes` 是 `{文件ID: 当前SHA256}`。所有注解涉及的文件都要登记，包括节点所在文件、关系双方、每个出处、路线各站和入口。解释另外依赖配置或契约时也登记这些依赖。构建时核对这份哈希与索引，并核对索引与实际磁盘文件；不允许旧课程未经审阅绑到新源码。

新增边不能复用自动边的 ID。注解的 `lessons` 替换数据中的路线列表，不是逐项合并；刷新时保留仍有效的路线，只修订受影响内容。

无效 ID、重复 ID、错误枚举、越界行号、父节点循环、原文哈希或大小不一致都会拒绝构建，旧 HTML 保留。语义是否成立仍由阅读与证据决定：一个结构合法的箭头也可能接错模块。
