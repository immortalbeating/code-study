# code-study · 代码与项目导学

从真实源码学习一门语言和一个项目：让代理先定位入口、梳理模块关系，再沿功能路线讲解写法、执行机制与设计取舍，减少在不同文件之间反复查找的时间。

这个仓库包含一个独立的 Agent Skill，以及生成离线 HTML 导读的脚本和固定模板。讲解、学习路线和理解题由代理阅读源码后编排；脚本负责索引、版本检查和页面生成。

## 能得到什么

| 视图 | 学习方式 |
|---|---|
| 关系地图 `atlas` | 找到入口和模块职责，选择节点查看局部关系，选择连接查看具体类型、证据和源码出处 |
| 路线导学 `guided` | 沿一个实际功能逐站阅读，理解关键写法，完成预测题，保存作答和笔记 |
| 源码精读 `reader` | 阅读完整文件、真实行号和基础代码着色，在符号、调用者与关联文件之间定位 |

生成的 HTML 内嵌源码、注解、CSS 和 JavaScript，浏览器直接打开即可阅读，无需启动服务器。学习记录保存在浏览器本地，也可以导出后交给代理继续反馈。

文件图会聚合同方向、相同起终点的关系。例如“事件等 · 3条”表示该连接包含三条原始关系记录；选择连接后可以逐条查看导入、调用或事件等具体类型及出处。数量表示记录条数，不是运行次数。主干视图折叠的辅助关系可在完整关系和局部邻域中查看，跨层与回返关系有独立入口。

## 安装到 Codex

将仓库克隆为个人技能目录中的 `code-study` 文件夹。Windows PowerShell 示例：

```powershell
New-Item -ItemType Directory -Path "$env:USERPROFILE/.agents/skills" -Force | Out-Null
git clone https://github.com/immortalbeating/code-study.git "$env:USERPROFILE/.agents/skills/code-study"
```

也可以将整个文件夹放入目标项目的 `.agents/skills/code-study/`。Codex 会发现技能；若列表未更新，重启 Codex。目录规则见 [OpenAI 官方技能文档](https://learn.chatgpt.com/docs/build-skills)。其他支持 Agent Skills 的工具按各自的技能加载方式使用，并保留仓库内文件的相对位置。

已克隆的安装可在保留本地修改的前提下更新：

```powershell
git -C "$env:USERPROFILE/.agents/skills/code-study" pull --ff-only
```

## 开始学习

在目标项目中向 Codex 提出学习目标，例如：

```text
使用 $code-study。我是 Python 初学者，请生成这个项目的离线 HTML 学习地图，
先带我理解“点击按钮后如何更新状态”，解释涉及的语言写法，并给我一条阅读路线。
```

也可以缩小范围：

```text
使用 $code-study，带我读这个函数，追踪它的输入、调用者和输出消费者。
解释这个类为什么这样拆分，并用一个边界条件检查我的理解。
```

整体学习通常按以下顺序进行：

1. 核实项目入口与学习范围，索引实际源码。
2. 阅读关键实现，补充关系的类型、证据和出处，编排一条功能路线。
3. 生成项目下的 `.code-study/index.html`，在地图、导学和源码之间联动阅读。
4. 保存自己的复述、预测或笔记，使用页面的“复制提问上下文”或导出记录继续向代理提问。
5. 项目代码更新后核对源码版本，刷新受影响的讲解，保留历史作答。

页面保存的是原始学习记录，不自动评分；代理根据实际作答反馈具体误解并调整后续讲解。

## 生成器使用

需要 Python 3.10 或更新版本；两个脚本只使用标准库。阅读生成后的 HTML 不需要 Python。以下命令从本仓库根目录执行，将示例项目路径替换为自己的实际路径：

```powershell
$studyProjectRoot = 'D:/Projects/your-project'
$studyOutputDir = Join-Path $studyProjectRoot '.code-study'
python -X utf8 scripts/build_study_view.py scan --root "$studyProjectRoot" --output "$studyOutputDir/project-data.json" --title '项目学习地图'
```

代理需要读取索引和源码，参照 [注解模板](assets/annotations.template.json) 完成 `$studyOutputDir/annotations.json`，填写真实节点、源码 SHA256、关系依据及学习路线，再构建页面：

```powershell
python -X utf8 scripts/build_study_view.py build --data "$studyOutputDir/project-data.json" --annotations "$studyOutputDir/annotations.json" --output "$studyOutputDir/index.html"
```

`--default-view guided` 或 `--default-view reader` 可指定首页。复用旧资料前检查源码：

```powershell
python -X utf8 scripts/build_study_view.py check --data "$studyOutputDir/project-data.json"
```

`current` 表示同一收录范围仍一致；`changed` 表示需要重读和刷新；`unavailable` 表示无法核实。确认刷新已有派生产物时，在对应 `scan` 和 `build` 命令中加 `--force`。完整流程见 [HTML 导读说明](references/project-view.md)，数据格式见 [项目数据与注解合同](references/project-data.md)。

## 覆盖范围

- 自动关系分析目前支持 Python 定义、词法导入和部分直接调用；类型提示推导的方法标为推断，动态或歧义目标保留未确认状态。
- 其他语言保留完整 UTF-8 原文，符号与关系由代理、项目配置或语义工具补充。图中的“完整关系”指当前已收录且已记录的关系，不保证运行时穷尽。
- 默认最多收录 500 个候选文件，单文件上限 1 MiB；可通过 include、exclude 和预算参数调整。覆盖统计会列出排除项。
- 索引不会导入或执行目标项目代码。生成的 HTML 是源码快照，包含收录的文件原文，打开页面不会自动读取磁盘中的新代码。

## 仓库结构

| 路径 | 用途 |
|---|---|
| [SKILL.md](SKILL.md) | 触发范围、教学流程与完成标准 |
| [agents/openai.yaml](agents/openai.yaml) | Codex 的展示名称、描述和调用提示 |
| [assets/study-viewer.html](assets/study-viewer.html) | 三种视图共用的离线页面模板 |
| [scripts/build_study_view.py](scripts/build_study_view.py) | 项目索引、注解校验、版本检查和 HTML 生成 |
| [scripts/source_snapshot.py](scripts/source_snapshot.py) | 显式选定文件的源码摘要与续接检查 |
| [references/navigation.md](references/navigation.md) | 入口定位、功能追踪与模块关联 |
| [references/teaching.md](references/teaching.md) | 语言写法讲解与理解练习 |
| [references/learning-record.md](references/learning-record.md) | 学习证据、记录与续接 |
| [references/viewer-design.md](references/viewer-design.md) | HTML 布局、交互与维护依据 |
