# 试卷题目—教师文稿证据绑定

本组件把试卷路线中的显式 `required_courses[].cycle_ids` 锚点，连接到
`reports/all_chapters/transcript-utilization-current.json` 的精确
`cycle/course` 文稿审计证据。它解决的是“讲解时应该调用哪门教师课程、依据哪一段
转写”的可追溯性问题，不改变试卷解锁规则，也不记录用户是否已经听课。

## 生成

在项目根目录运行：

```powershell
python -X utf8 scripts/build_exam_transcript_bindings.py
```

默认输入和输出：

| 类型 | 路径 |
|---|---|
| 试卷路线 | `data/exam_papers/manifest.json` |
| 文稿审计 | `reports/all_chapters/transcript-utilization-current.json` |
| 课程目录 | `data/all_chapters_course_catalog.json` |
| MCP 可消费证据 | `data/exam_papers/transcript_bindings.json` |
| 审计报告 | `reports/all_chapters/exam-transcript-bindings-current.json` / `.md` |

也可以用 `--project-root`、`--exam-manifest`、`--transcript-audit`、
`--course-catalog`、`--output` 和 `--report` 指定显式路径。所有路径最终都必须落在
项目根或项目的 `data/course_transcripts` 根内；旧设备绝对路径只取安全文件名，不能被
重新读取。

## 证据合同

机器文件的 `evidence_records` 是按 `evidence_key` 去重的字典。每条记录至少包含：

- 审计 `evidence_id`、节次和循环 ID、课程 key 及关系；
- 转写文件 SHA-256、全文 SHA-256、课程目录中声明的两个 SHA，以及四项匹配结果；
- 审计方法、主题词命中计数、教师信号类别；
- `sentence_indices` 和 `time_spans`（秒）；无可靠时间轴时为空并标记
  `timeline_status=not_available`；
- `eligible_for_teacher_method` 和逐项 `verification_reasons`。
- `body_character_anchors` 保存实际全文的原始字符区间，不保存原句；主题词必须在
  原文出现，不能只相信审计给出的命中计数。仅标题标记不能被非零计数覆盖。
- `semantic_source_verified` 与 `timeline_verified` 分开报告：前者表示哈希绑定的
  正文主题和教师信号有可复核依据，后者表示句索引、秒数与原转写对应行一致。
  两者都不是数学解法正确性的证明；讲解仍需读取原句并核对具体题目的适用条件。

生成器逐条核对句索引存在、非重复、原句非空，并按源审计的时间单位规则比对
起止秒数。即使时间落在课程时长内，若不是该原句的时间也会阻塞。教师方法类别
还须有原文实际出现的信号标签；无法重现的标签保留待复核。

路线记录的 `required_courses` 只保存显式锚点和 `evidence_ids`，不复制转写正文。路线
记录还保留每个所需循环的 `anchored`/`unanchored` 状态：未显式挂新课程的练习循环会被
列出，但不会凭标题或邻近课程自动补绑定。

## 三种状态

| 状态 | 允许的用途 |
|---|---|
| `verified` | 可把对应证据作为教师方法讲解入口。要求显式课程—循环锚点、目录与审计哈希一致、主题有实质命中、至少一个教师方法信号、可靠句级时间证据，以及路线本身已通过映射门禁。 |
| `review` | 证据对象存在，但语义质量不足、缺时间轴或路线仍待复核。只能作为待复核候选，不能宣称教师方法已核验。 |
| `blocked` | 来源/哈希/身份/显式锚点缺失，或原路线本身被阻塞。不得用于解锁或讲解覆盖声明。 |

`partial` 审计证据在具备实质主题命中、教师信号和时间证据时可以成为
`verified`；`full` 是更高质量等级。仅标题词命中（`substantive_match_count=0`）永远
不能通过。无时间轴的历史转写保留哈希和正文可用性，但只会得到 `review`。
此时可区分“正文已有依据但无法准确跳转”与“正文语义依据也不足”，不得把两种
缺口混写为没有文稿，也不得虚构视频时间。

当旧审计只保留前 16 个命中句，漏掉真正教学段落时，生成器可在同一哈希绑定文稿
重新定位：仅使用审计已声明的非标题主题词与教师信号标签，保留两者在原句共同
出现的真实句索引与起止时间。记录 `source_sentence_reselection=true` 及原选句哈希。
若原索引/时间已损坏、主题不存在或来源哈希失配，不得借重新选句掩盖错误。
此过程不新增课程、知识点或练习循环锚点，也不把词语共现当作题目解法已验证。

整体 `status` 是审计提示，不会把 `review` 或 `blocked` 路线改写成课程或试卷完成。
MCP 客户端必须按路线的 `binding_status` 和证据的
`eligible_for_teacher_method` 双重检查，不能只看 `required_course_keys`。

## MCP 接入顺序

后续 Worker 可以按以下方式读取静态包（或把同一字段导入 D1）：

1. 用试卷 `question_id` 在 `routes` 中定位路线；
2. 读取 `required_courses[].evidence_ids`；
3. 在 `evidence_records` 中取出对应哈希、句索引、时间片和方法信号；
4. 只有路线和证据均为 `verified` 时，才把它们作为“教师文稿方法证据”展示；否则明确
   显示“待复核/资料不足”，并继续使用普通课程读取接口；
5. 课程消费仍通过真实学习事件记录，不能由该包的哈希或读取动作推断。

绑定包不含转写原句、试卷答案或答案页内容，可安全放在答案隔离之外的路线元数据层。

## 校验与增量

生成器会重新读取每个被引用的转写并核对文件、全文和审计哈希。任何失配都会让对应
证据和路线关闭；全局审计源哈希失配会使命令返回非零。`binding_fingerprint` 是排除
生成时间后的稳定内容指纹，便于 MCP 导入前做版本比较。试卷新增/替换时先重新生成
试卷 manifest，再运行本命令；旧证据不会被静默迁移到新题目。

专项回归：

```powershell
python -X utf8 -m unittest tests.test_exam_transcript_bindings -v
```
