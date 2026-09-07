# 数学试卷渐进练习合同

## 目标

期中、期末、月考和入学检测卷是独立练习来源。它们不能替代《一本通》，也不能因为未完成而阻塞课程、循环、节次或章节主线。

## 题目接入

1. 保存原卷页、题号、图形和来源哈希；答案页不能充当题面。
2. OCR 只用于检索。公式、图形、选项和条件以原卷页图为准。
3. 一道综合题可依赖多个循环、多个节次甚至整章；所有前置必须显式列出。
4. 只在全部前置完成后显示“可选可做”。前置不足时列出缺少的课程、知识点和循环。
5. 题内顺序保持原卷顺序；不能为了课程顺序拆散同一道大题的小问。
6. 一份 PDF 可能混排原卷与答案页；每页必须有 `page_role=question|answer|unknown`。只有 `question` 页且有页图 SHA-256 才能成为题面证据，`answer`/`unknown` 页只能进入待复核队列。

`scripts/index_exam_papers.py` 负责文件级去重和页级证据索引，不替代语义映射。配置 `--allowlist` 后只扫描匹配的数学原卷路径/文件名（也支持 SHA-256 或 `exam-*` 身份），可用 `--page-sidecar` 合并 OCR、页图哈希、页角色和题号。重复文件按源 PDF SHA-256 合并为一个稳定来源，并把别名保留在 `aliases`。

## 路由字段

- `source_id`：试卷来源哈希身份。
- `question_ref`：原卷页和题号，例如“第 3 页第 12 题”。
- `required_course_keys`：需要先听完的课程。
- `required_cycle_ids`：需要完成的循环。
- `required_section_ids`：跨节综合题的前置节次。
- `topic_tags`（兼容称 `knowledge_tags`）/ `type_tags`：知识点与题型候选。
- `mapping_status`：通常为 `candidate`、`visually_verified` 或 `semantically_verified`；无法核对时可暂记 `needs_review`/`blocked`，但不得解锁。
- `uncertainties`：题面或映射中仍待确认的位置和证据。

索引器还会为可识别题号生成稳定的 `question_id`（`exam-{hash16}:p{pdf_page}:q{number}:r{occurrence}`），并保留 `source_sha256`、`source_page_sha256`、`extraction_method` 和 `question_authority`。自动识别的题号仍是候选，必须经过原页视觉核对后才可提升为 `visually_verified`/`semantically_verified`。

`scripts/validate_exam_routes.py` 除了检查循环/节次身份，还会检查课程存在性、来源与页图 SHA、页角色权威、题目 ID 唯一性、`unlock_granularity` 与前置数量，以及 `ready`/`needs_review`/`blocked` 的理由闭合。`ready` 必须是语义核验且无未决不确定项；答案页或未标页不能放行。

## 当前生成与查询

`scripts/build_exam_routes.py --source-root <目录>` 读取映射规则和五章 manifest，输出：

- `data/exam_papers/manifest.json`：来源、题目、原卷页证据、必需节次/循环/课程和状态；
- `data/exam_papers/question_index.json`：按稳定题目 ID 的查询索引；
- `data/exam_papers/page_assets/`：只保存题面页图，不复制答案页；
- `data/exam_papers/learning_route_guide.md`：面向学习者的“题目 -> 前置路径”清单；
- `reports/exam-papers-current.{json,md}`：本次来源和路由统计。

`scripts/query_exam_routes.py --source-id <id> --question-number <n>` 会读取显式进度记录（`--progress` 或命令行完成项），返回 `ready`、缺少的循环/课程和下一动作。文件存在、标题匹配或聊天中说“做完了”都不会被当作完成记录。原卷文件被替换/移除时，旧来源保留为历史但自动标成阻塞。

Cloudflare MCP 对应 `math_get_exam_routes`、`math_get_exam_question` 和 `math_record_exam_attempt`：前两个只读并实时计算云端解锁，最后一个只写独立试卷作答，不改变《一本通》进度。答案页不上传到 R2；若题面仍待复核，工具会明确返回待复核而不是放行。

ChatGPT 讲题时继续先读取网课老师文稿，再核对原卷页，最后根据真实学习进度决定提示深度。试卷答案与模型解法必须分栏，并明确推荐方案。

## 云端 MCP 接口

`cloud/mcp/scripts/import_exam_papers.mjs` 将本索引导入 Cloudflare D1/R2。D1 中的 `exam_sources`、`exam_pages`、`exam_questions`、`exam_question_evidence` 和 `exam_route_links` 保存可追溯路线；`exam_attempts` 只保存用户明确提交的可选试卷作答。导入计划可用 `npm run exams:dry` 预览，确认后才使用 `npm run exams:remote`。

ChatGPT 使用 `math_get_exam_routes` 查询筛选后的双向关系，使用 `math_get_exam_question`（或兼容别名 `math_get_exam_route`）读取单题原卷页图和前置状态。只有 `mapping_status=semantically_verified`、无待复核且所有 required 循环/课程前置完成时才返回 `ready=true`。`math_record_exam_attempt` 不写学习事件、不改变一本通或章节进度；答案页只作为来源元数据，永远不随题面页包上传。

## 页面视觉复核门禁

“有页图”与“页图已经核对”是两个状态。生成器在每个 `question` 页写入：

- `visual_review_status=verified`：映射规则中有明确的页面复核声明，且页图 SHA-256 有效；
- `visual_review_status=pending`：页图可取但尚未留下复核声明（默认状态）；
- `visual_review_status=blocked`：页图缺失、哈希无效或复核证据明确失败。

一道题的所有原卷页（含续页）都必须为 `verified`，路线才可为 `ready_for_optional_unlock`。任何 `pending`/`blocked` 页都会把路线留在待复核/阻塞状态；课程和循环进度不会因此被回写。旧版 manifest 没有 `visual_review_gate` 时，普通校验保持兼容但会标记 legacy notice；发布前应运行：

```powershell
python scripts/validate_exam_routes.py --strict-visual-review
```

映射规则支持 `visual_verification: true`（全部题面页）或 `{"status":"verified","pages":[1,2],"reviewer":"...","reviewed_at":"..."}`（指定页）。声明只在对应页图存在且 SHA-256 有效时生效，不能用 OCR 置信度或文本层代替。

不改 manifest 的只读盘点可运行：

```powershell
python scripts/report_exam_visual_review.py `
  --manifest data/exam_papers/manifest.json `
  --json-output reports/all_chapters/exam-visual-review-current.json `
  --markdown-output reports/all_chapters/exam-visual-review-current.md
```

该报告会列出所有题面页、续页和“manifest 宣称可解锁但视觉未通过”的路线；`--strict` 适合发布门禁。当前旧版 manifest 的盘点结果保留在 `reports/all_chapters/exam-visual-review-current.md`，不自动替换历史路线。

## 增量扫描与差异报告

保留上一版索引并显式传入 `--previous`。命令会按完整 PDF SHA-256 去重，并把同路径换版、页证据变更、重命名、移除和 allowlist 排除分别列出：

```powershell
python scripts/index_exam_papers.py `
  --source-root <下载目录> `
  --allowlist data/exam_papers/source_allowlist.json `
  --previous data/exam_papers/source_inventory.json `
  --output data/exam_papers/source_inventory.next.json `
  --diff-output reports/exam-source-diff.json
```

报告中的 `changed_source_ids` 包括 PDF 换版和页级证据变更；`replaced_sources` 保存旧/新 SHA 配对；`deallowed_source_ids` 表示文件仍在扫描目录但因 allowlist 被排除，不应误报为删除。`--fail-on-change` 可用于门禁：发现新增、换版、证据变更、移除或排除时返回退出码 `2`，但仍会写出完整 diff，便于人工复核后再建新版本。
