# HPE Paper Evaluator（v3.1 参考实现）

> 目录名和 skill ID 仍保留 `hpe-paper-evaluator-v2`，以兼容已有安装；内部协议
> 版本与数据契约版本为 v3.1 / `hpe/v2`。

面向本地 PDF 的**证据锚定、多审稿人**论文评审 skill。它把评审拆成
“确定性协议层 + 宿主认知层”，既能由当前 OpenCode / Codex CLI / DeepSeek
Harness 的 agent 模型直接执行（host 模式），也能通过独立配置的外部
OpenAI-compatible API 执行（API/hybrid 模式）。

- 设计基线：[`../hpe-paper-evaluator-v2-design/`](../hpe-paper-evaluator-v2-design)
- 设计审查：[`../hpe-paper-evaluator-v2-design/REVIEW-REPORT.md`](../hpe-paper-evaluator-v2-design/REVIEW-REPORT.md)
- 施工/测试记录：[`CONSTRUCTION-TEST-LOG.md`](CONSTRUCTION-TEST-LOG.md)
- 运行产物：默认写到 PDF 同目录的 `<PDF文件名>.hpe/`，不写入 skill 仓库
- 宿主适配：[`docs/HOSTS.md`](docs/HOSTS.md)
- 宿主入口：[`SKILL.md`](SKILL.md)

---

## 0. 一句话总结

> 脚本负责**看得见、可复现**的部分（PDF、分节、任务、状态、证据校验、台账、
> 裁决、渲染）；模型只负责**认知**部分（摘要、审稿、答辩、交叉复审、元审稿）。
> 默认评审模型就是当前 agent 模型；中文/英文输出可切换；PDF 文本抽取是证据
> 底座，多模态页面图只作为表格/公式/扫描件的补充。

---

## 1. 能力边界

### 1.1 能做什么

| 维度 | 取值 | 说明 |
|---|---|---|
| `purpose` | `review` / `summary` / `problem_finding` | 结构化评审 / 详细总结 / 以漏洞查找为主 |
| `material` | `paper` / `proposal` / `design` | 论文 / 研究提案 / 项目或实验设计 |
| `domain` | `ai-ict` / `materials` / `mechanical` / `auto` / 自定义 | AI/CS/ICT、金属与高熵合金、机械工程 |
| `profile` | `paper` / `conference` / `journal` / `proposal` / `experiment` | 六维权重与默认决策政策 |
| 模式 | `host` / `api` / `hybrid` | 当前 agent / 外部 API / 混合 |
| 语言 | `auto` / `zh` / `en` | 评审正文与报告语言 |
| 输出 | Markdown / JSON / HTML | `output/report.*` 或 `output/summary.*` |

### 1.2 不做什么

- 不做查重、引文数据库检索、学术不端检测；
- 不做纯翻译或润色（`summary` 是分析性总结，不是翻译）；
- 没有本地 PDF 时不臆造内容；
- 不把模型输出当作正式同行评审结论；最终判断由人类作者/编辑负责。

---

## 2. 设计思路

### 2.1 三个平面

```text
┌────────────────────────────────────────────────────────────────────┐
│ 确定性平面（Python，无 LLM）                                         │
│ PDF 摄取 → 分节 → 质量门 → 任务 DAG → 状态/锁 → Schema/证据校验      │
│ → 问题台账 → 决策政策 → 渲染 → 页面图（可选）                         │
└────────────────────────────────────────────────────────────────────┘
                 │ 任务包（自包含 prompt + schema + output path）
                 ▼
┌────────────────────────────────────────────────────────────────────┐
│ 认知平面（宿主 agent / 子代理）                                       │
│ digest → 独立审稿 → 作者答辩 → 交叉复审 → 元审稿                     │
│ 每个审稿任务使用 fresh context；round 0 互相不可见                    │
└────────────────────────────────────────────────────────────────────┘
                 │ 可选
                 ▼
┌────────────────────────────────────────────────────────────────────┐
│ API 平面（OpenAI-compatible provider）                              │
│ 同一任务图可由脚本直接调用外部 Qwen/GLM/Kimi/DeepSeek 执行            │
│ API key 只在独立文件中；默认沿用 agent 基座                           │
└────────────────────────────────────────────────────────────────────┘
```

**为什么这样分层？**

1. **证据必须可机器校验。** 页码、连续引文、absence 说明、覆盖率、状态变化
   都不应该由模型“自证”。脚本用 `QuoteIndex` 在页级文本上做归一化匹配。
2. **盲审必须由结构保证。** round 0 的每个审稿人只看到论文、摘要和 rubric；
   其他审稿人的意见在交叉复审阶段才匿名出现。即使使用同一个基座，也不能让
   三个 persona 在同一个上下文里互相污染。
3. **决策必须可复现。** 分数由脚本按 profile 权重计算；最终 decision 由
   issue severity/status 的确定性政策计算；元审稿只能解释或在书面理由下覆盖。
4. **状态必须能恢复。** 任务有 claim/lease、stale 回收、`fail` 记录；
   宿主崩溃后可以 `status` + `next` 继续。

### 2.2 评审管线

```text
init
  └─ ingest PDF → paper/manifest.json + sections + pages + quote index
       └─ digest（可选）────────────────────────────┐
            └─ N × independent review（fresh）        │
                 └─ ledger round 0                    │
                      ├─ rounds=0 ────────────────────┤
                      └─ author rebuttal (round 1)     │
                           └─ N × cross-review update  │
                                └─ ledger round 1      │
                                     ├─ early stop ───┤
                                     └─ next round ... │
                                                       ▼
                                                    meta review
                                                       ▼
                                                    render
```

`summary` 是短管线：`digest（可选）→ summary task → render`，不产生
ledger/decision。`problem_finding` 复用 review 管线，但 reviewer prompt 以
逻辑/技术/创新漏洞为主交付，亮点不能抵消漏洞。

### 2.3 状态机

```text
digesting → reviewing → rebutting → updating → meta → rendered → done
     └──────────── summary ────────────┘
     └──────────── failed（覆盖率 < 50%）┘
```

- `next` 返回 `execute` / `advance` / `wait` / `done` / `failed` /
  `other_executor`（hybrid 模式下另一执行器还有任务）。
- `next` 即 claim：任务进入 `running`，带 `claimed_at`；2 小时无心跳或宿主
  崩溃后自动回收。
- 子代理只写 JSON 文件；**只有宿主**调用 `submit` / `fail` / `advance`。

---

## 3. 实现机制

### 3.1 PDF 摄取（`scripts/hpe/ingest.py`）

| 能力 | 实现 |
|---|---|
| 引擎回退 | PyMuPDF → pdfplumber → pypdf → `pdftotext`；按质量等级和文本量选最佳结果 |
| 页级文本 | `paper/pages.json`；全文 `paper/text.txt` 含 `[[PAGE n]]` 物理页标记 |
| 分节 | 字号/粗体 + 中英文标题正则；无标题时按 3 页窗口回退 |
| 质量门 | 字符/页、空页比、替换字符比、字母比 → `good/partial/poor`；`poor` 默认拒绝 |
| 去页眉页脚 | 只对每页前后两行、跨页重复 ≥35% 的短行做删除 |
| 引文索引 | NFKC、弯引号、全角、断行连字符、空白折叠；返回 exact/normalized/segmented/fuzzy |
| 图表检测 | 提取 Figure/Table 标题与 `content_type`（`text_maybe` / `caption_only`） |
| OCR | `--ocr` 可选；依赖 pytesseract + tesseract；默认最多 30 页 |
| 页面图 | `--vision-pages auto|all|off`；PyMuPDF 渲染 PNG，供多模态模型读取 |
| 安全 | 文件扩展名、空文件、200 MB、500 页、加密 PDF、`pdftotext` 180s 超时 |

### 3.2 任务包与校验

每个 `next` 返回的任务包含：

- `prompt_path` / `prompt_json_path` / `prompt_sha256`
- `schema_path` / `output_path`
- `depends_on` / `optional_depends_on`
- `context_isolation`（`fresh` / `shared`）
- `constraints`（token、temperature、language、backend、executor）
- `context`（提供的 section ID、digest、prior reviews、rebuttal、page images）

校验分三层：

1. **JSON Schema**：字段、枚举、条件证据（`paper` 必须有 quote，`absence`
   必须有 note，`external` 必须有 note）。
2. **语义校验**：页码范围、连续引文、issue 更新完整性、分数变化、
   recommendation 一致性、meta override 规则、覆盖率。
3. **证据验证**：每条 quote 写入 `artifacts/evidence/T-xxxx.json`，元审稿
   收到聚合后的 exact/normalized/fuzzy/not_found 报告。

### 3.3 台账、政策与元审稿

- `ledger.py`：显式 alias + 保守 fingerprint 合并；保留历史 canonical ID。
- `policy.py`：按 profile 权重计算均分/中位数/维度分；按 issue 状态计算
  `accept / minor_revision / major_revision / reject`。
- 元审稿只能：
  - 逐条给出 `issue_resolution`；
  - 解释或按规则覆盖 decision；
  - 保留 dissent；
  - 不能篡改脚本计算的分数（脚本会覆盖并 warning）。
- 覆盖率 <50% → run failed；50–99% → provisional；报告明确标注。

### 3.4 上下文预算

- 论文 section 按 persona 优先级选择；
- digest、prior reviews、rebuttal、ledger、evidence 各自有 token 上限；
- 超预算时 `bounded_json` 递归裁剪；
- 关键语义上下文（作者答辩）不会静默退化成 ID 列表；
- 论文放不下时默认**失败关闭**，除非 `--allow-truncation`。

### 3.5 评审基座一致性（v3.1）

这是本版最重要的行为变化之一。

**问题：** 旧实现/上游工具在 API 模式下可能默认 `deepseek-chat`，而宿主
agent 实际运行在 `deepseek-v4.1-flash` 或 `deepseek-v4-pro`。同一个
provider/key，却换了基座，导致评审风格、能力和成本与 agent 不一致。

**新规则：**

1. **host 模式**：所有角色都用当前 agent 模型（默认，最安全）。
2. **api 模式**：默认角色 backend = 当前 agent 模型，而不是厂商默认值。
3. **自动检测顺序**：
   - `--agent-backend provider:model`
   - `HPE_AGENT_BACKEND`
   - `HPE_AGENT_PROVIDER` + `HPE_AGENT_MODEL`
   - `DEEPSEEK_HARNESS_MODEL`
   - OpenCode 的 `OPENCODE_SESSION_ID` → `opencode api get /api/session/<id>`
   - `~/.hpe-paper-evaluator/agent.json`
4. **同厂商不同模型会被拒绝**：例如 agent 是 `deepseek-v4.1-flash`，某个
   role 解析成 `deepseek-chat`，`init` 直接报错；除非显式
   `--allow-backend-mismatch`。
5. **不同厂商允许但记录 warning**（例如 Qwen reviewer + DeepSeek meta），
   因为这是有意的多模型多样性，不是“同 key 偷偷换基座”。
6. **无法解析 agent 模型且 API 模式没有显式 backend**：报错并提示用 host
   模式或配置 agent provider，不再静默回退。
7. `run.json` 记录：
   - `agent`：label/provider/model/kind/source/detected
   - `backend_consistency`：consistent/mismatches/cross_provider
   - `backends`：每个角色的 label、key-present 布尔（不含 key）
8. 报告中显示 agent backend；任务包 `model_hint` 和 `constraints.backend`
   也记录同一身份。

示例：

```bash
# 正确：审查沿用当前 agent 模型
python scripts/hpe.py init --pdf paper.pdf --mode api \
  --agent-backend opencode-go:deepseek-v4.1-flash

# 错误（默认拒绝）：agent 是 flash，reviewer 却用 chat
python scripts/hpe.py init ... --mode api \
  --agent-backend deepseek:deepseek-v4.1-flash \
  --reviewer-backends deepseek:deepseek-chat
# -> Refusing a same-provider/different-model review base ...
```

> 与上游 HPE-tool 的差异：上游在 `config.py` / provider 层硬编码
> DeepSeek/Qwen/GLM 的模型名，默认 debate/summary 可能用与宿主 agent 不同
> 的基座；本实现不继承这一行为。`config/models.example.json` 仅作为
> legacy/显式 API 配置的参考，不再是默认评审基座。

### 3.6 中英文输出（v3.1）

- `--language auto`：跟随论文语言。中文论文 → 中文评审 + 中文报告；
  英文论文 → 英文。
- `--language zh`：强制中文正文、报告标题、状态标签、summary 标题，
  即使论文是英文。
- `--language en`：强制英文。
- JSON 字段名和枚举值保持英文（契约稳定）；引文保持原文语言。
- 解析结果写入 `run.json:output_language`、`report.json:output_language`。
- 旧报告没有该字段时，按论文语言回退。

### 3.7 PDF 文本抽取 vs 多模态：应该保留哪个？

**结论：保留 Python 确定性抽取作为主证据层；多模态页面图作为补充；不要把
整条链路换成“只看图”。**

| 维度 | Python 文本抽取 | 多模态 agent 直接读 PDF/图 |
|---|---|---|
| 页码/引文可验证 | ✅ 页级文本 + QuoteIndex | ❌ 无法稳定给出可校验的连续引文 |
| 可复现 | ✅ 同一 PDF 同一结果 | ❌ 采样、模型版本、视觉解析都可能变 |
| 表格/公式/扫描件 | ⚠️ 单元格/公式常缺失 | ✅ 能读图、表格、坐标轴、公式 |
| 成本/延迟 | ✅ 低 | ❌ 每页图片 tokens 高 |
| 无视觉模型时 | ✅ 可运行 | ❌ 不可用 |
| 双栏/复杂版式 | ⚠️ 可能串栏 | ✅ 视觉理解更好 |

因此本版采用**混合方案**：

```text
确定性文本抽取（canonical）
   ├─ 页标记、分节、quote index、证据校验
   └─ 可选：包含 table/figure 的页面全部渲染为 PNG
          └─ 宿主多模态模型读取表格、图、公式
               └─ 观察写入 evidence.note；paper quote 仍必须来自文本
               └─ coverage.images_read / summary.images_read 记录实际看图页
```

- 默认 `--vision-pages auto`：渲染**所有包含 table/figure 的页面**，优先
  `caption_only/unknown`，其次 `text_maybe` 表格，再其次图；最多
  `--vision-max-pages`（默认 20）页，150 DPI。
- `--vision-pages all`：渲染所有页（受 max-pages 限制）。
- `--vision-pages off`：纯文本；适合无视觉模型或严格成本控制。
- 如果没有 PyMuPDF，页面图跳过并 warning，文本链路继续。
- **回退兜底**：如果宿主/模型不能读图，prompt 要求：
  - `coverage.images_read`（review）或 `images_read`（summary）写 `[]`；
  - 表格/图单元格标为不可提取，写入 `missing_evidence` / `missing_information`；
  - 降低 confidence，不得编造单元格数值；
  - 仍可使用 `--ocr` 处理扫描件（需 tesseract）。
- 测试结果：对 CVAE 论文，修复后 `auto` 选中 pages 4–8，summary 实际读取
  Table 1–4；Table 2/4 的数值与页面图核对一致，并发现 Table 4 的 CUB
  指标口径与 Table 2 不一致。说明“多模态读表 + 文本证据底座”组合有效。

**什么时候可以只用多模态？** 只在“快速阅读/总结、不需要页码引文校验”的
场景。**什么时候必须保留 Python 抽取？** 只要需要证据锚定、quote 校验、
可复现报告或 API/CI 批量运行，就必须保留。

### 3.8 安全与隐私

- API key 只放在独立 `llm_apis.json`（0600）或环境变量；
  不进入命令行、run 目录、报告、prompt、日志。
- `doctor` 只显示 key 是否存在，不显示值。
- 论文全文可能包含未公开内容；run 目录应按私有数据处理。
- 论文/答辩/其他审稿文本都按不可信数据注入声明；另有 prompt-injection
  扫描警告。
- 未实现 `--redact` 分享导出；分享前手动清理 run 目录。

---

## 4. 安装与兼容

### 4.1 依赖

- Python ≥ 3.10（仅标准库即可运行）；
- 推荐：`pymupdf`（最佳抽取）、`jsonschema`（严格 Schema 校验）；
- 可选：`pdfplumber`、`pypdf`、系统 `pdftotext`、`pytesseract` + tesseract；
- 测试：`pytest`；
- API 模式不依赖 `openai` SDK，使用标准库 HTTP。

```bash
python3 -m pip install pymupdf jsonschema pytest
python scripts/hpe.py doctor --json
```

### 4.2 OpenCode V2 安装

OpenCode 发现目录型 skill 的位置：

- 全局：`~/.config/opencode/skills/<skill-id>/SKILL.md`
- 项目：`.opencode/skills/<skill-id>/SKILL.md`

目录名就是可调用的 skill ID。推荐用符号链接保持单一副本：

```bash
# 全局
ln -s "/mnt/d/workspace/插件/hpe-paper-evaluator-v2" \
      ~/.config/opencode/skills/hpe-paper-evaluator-v2

# 项目
mkdir -p .opencode/skills
ln -s "/mnt/d/workspace/插件/hpe-paper-evaluator-v2" \
      .opencode/skills/hpe-paper-evaluator-v2

# 验证（路径 + CLI 自检）
readlink -f ~/.config/opencode/skills/hpe-paper-evaluator-v2
python scripts/hpe.py doctor --json
```

OpenCode 会在下一次模型步骤把该 skill 加入可用列表；也可以直接对 agent 说
“使用 hpe-paper-evaluator-v2” 或用 `@hpe-paper-evaluator-v2` 显式加载。

Windows 没有符号链接权限时，用目录联接或直接复制：

```bat
mklink /J "%USERPROFILE%\.config\opencode\skills\hpe-paper-evaluator-v2" "D:\workspace\插件\hpe-paper-evaluator-v2"
```

> 注意：OpenCode 的 skill 发现以**当前 OpenCode 服务的工作目录/配置目录**为准。
> 如果服务运行在 WSL，使用 WSL 路径；如果运行在 Windows，使用 Windows 路径。
> 安装后让 agent 明确 `@hpe-paper-evaluator-v2` 或说“使用 HPE skill”。

### 4.3 Codex CLI 安装

Codex 0.163+ 使用 Agent Skills 目录：

```bash
ln -sfn "/mnt/d/workspace/插件/hpe-paper-evaluator-v2" \
  ~/.codex/skills/hpe-paper-evaluator-v2

# 静态验证：不启动 agent session
codex debug prompt-input | grep hpe-paper-evaluator-v2
```

`agents/openai.yaml` 已提供 Codex UI 元数据。Codex 没有 OpenCode 式
`subagent` 工具：需要盲审时，用独立的 `codex exec` 进程/会话执行每个任务；
只能单会话时，传 `--isolation session` 并在报告中说明独立性受限。PDF 与
run 目录在 Codex workspace 之外时，需使用：

```bash
# 默认输出仍与 PDF 同目录；不要额外传 --out
codex exec --sandbox workspace-write --add-dir "<PDF目录>" \
  -C "/path/to/hpe-paper-evaluator-v2" \
  "Use \$hpe-paper-evaluator-v2 to review /abs/path/paper.pdf"
```

Codex 的 `view_image` / `-i` 可读取页面图；不能读图时按 text-only fallback
处理。宿主模型身份从 `CODEX_MODEL` 或 `~/.codex/config.toml` 的
`model = "..."` 读取。

### 4.4 DeepSeek Harness 安装

- 以 `skills/hpe-paper-evaluator-v2/SKILL.md` 形式放入 Harness 的 skill 目录；
- 以 `scripts/hpe.py` 为入口，保持 skill 目录为 shell cwd；
- 参考 `agents/deepseek.yaml.example` 的 workflow/host_contract；
- 默认输出与 PDF 同目录，**不要传 `--out`**；使用绝对 PDF 路径；
- 如果 Harness 暴露 `DEEPSEEK_HARNESS_MODEL`，会自动记录为 agent 基座；
  否则设置 `HPE_AGENT_BACKEND=provider:model`，或把
  `config/agent.example.json` 复制为 `~/.hpe-paper-evaluator/agent.json`。

### 4.5 载入 skill 时的使用提示

`SKILL.md` 顶部有强制的 "Load-time usage hint" 段：agent 在 `/skills`、
`@hpe-paper-evaluator-v2` 或 `skill` 工具载入后，应先输出使用提示，而不是
直接开始跑任务。提示内容包括：

- 必需输入：本地 PDF 路径；
- 默认输出：`<PDF 目录>/<PDF 文件名>.hpe/`；
- 可选参数：purpose、language、domain、material、reviewers、rounds；
- 只有用户想覆盖默认位置时才询问 `--out`。

也可以直接运行确定性命令生成提示：

```bash
python scripts/hpe.py usage --language zh          # 中文文本
python scripts/hpe.py usage --language en --json  # 英文结构化提示
```

### 4.6 兼容性检查清单

```bash
python scripts/hpe.py doctor --json          # Python/引擎/Schema/agent/LLM 配置
python scripts/hpe.py usage --language zh    # 载入提示
python scripts/hpe.py init --help            # 所有参数可见
python scripts/hpe.py init ... --json        # 生成 run
python scripts/hpe.py next --run ... --json  # 任务包

# Codex 静态发现（不启动 agent）
codex debug prompt-input | grep hpe-paper-evaluator-v2
codex sandbox -- python3 scripts/hpe.py doctor --json
```

---

## 5. 用法

### 5.0 默认输出位置

`--out` 是**可选参数**。省略时，所有中间文件和最终报告默认写到被审阅
PDF 的同一目录下。这个默认对 **OpenCode / Codex CLI / DeepSeek Harness
完全一致**；宿主适配器不得自行注入 `--out`。始终传入 PDF 的绝对路径，确保
run 目录落在论文旁边：

```text
<PDF 所在目录>/
└── <PDF 文件名>.hpe/
    ├── paper/                 页级文本、分节、页面图
    ├── prompts/               每个任务的完整提示词
    ├── digest/
    ├── artifacts/             审稿 JSON、作者回复、ledger、meta、证据校验
    ├── logs/
    └── output/
        ├── report.md/json          主报告（运行语言）
        ├── report-zh.md / report-en.md  最终报告中英文对照
        ├── review-rounds-zh.md     所有审稿人的多轮意见 + 作者每轮回复（中文）
        ├── review-rounds-en.md     同上（English）
        ├── final-opinion-zh.md     最终审查意见（中文）
        └── final-opinion-en.md     最终审查意见（English）
```

review 模式默认 `rounds >= 1`，即至少包含一轮作者扮演者回复；只有显式
`--rounds 0` 才会跳过作者回复轮次。`render` 会生成主语言文件，并输出
`output/bilingual-tasks.json` + 翻译提示；执行翻译子代理后，用
`python scripts/hpe.py bilingual --run "<RUN>" --check --json` 校验中英文
物料是否齐全。

- 如果 `<PDF 文件名>.hpe/` 已存在，`init` 会自动使用带时间戳的兄弟目录，
  不会覆盖已有 run。
- 只有用户明确指定输出目录时，才需要传 `--out "<目录>"`。
- 默认目录名经过跨平台安全处理（移除 Windows 非法字符、保留中文/空格）。

### 5.1 快速开始：host 模式（推荐）

```bash
cd "/path/to/hpe-paper-evaluator-v2"

# --out 可省略：默认输出到 /path/to/paper.hpe/
RUN="/path/to/paper.hpe"

python scripts/hpe.py init \
  --pdf "/path/to/paper.pdf" \
  --purpose review \
  --material paper \
  --domain ai-ict \
  --profile conference \
  --reviewers methodology,experiments,novelty \
  --rounds 2 \
  --language zh \
  --isolation subagent

python scripts/hpe.py next --run "$RUN" --executor host --json --limit 4
```

若想覆盖默认位置，显式传 `--out "/path/to/somewhere"`。

对返回的每个任务：

1. 读取 `prompt_path` 的**完整内容**（不要改写、摘要、压缩）；
2. 交给一个**全新上下文**的子代理；
3. 子代理只写一个 JSON 到 `output_path`，并匹配 `schema_path`；
4. 宿主执行：

```bash
python scripts/hpe.py submit --run "$RUN" --task T-0007 --file artifacts/reviews/R2.round0.review.json
```

如果子代理失败：

```bash
python scripts/hpe.py fail --run "$RUN" --task T-0007 --reason "timeout"
```

循环：

```bash
python scripts/hpe.py advance --run "$RUN"
python scripts/hpe.py render --run "$RUN" --format md,json
python scripts/hpe.py status --run "$RUN" --json
```

关键规则：

- 每个 round-0 reviewer 必须是 fresh context；
- 子代理不能调用 `submit` / `advance`；
- 不要手改 `state.json` / `ledger.json` / 报告；
- 不要给 round-0 reviewer 看其他 reviewer 的意见。

### 5.2 中文输出

```bash
python scripts/hpe.py init ... --language zh
```

效果：

- `run.json:output_language = "zh"`；
- 审稿 prompt 的 `{{OUTPUT_LANGUAGE}} = Chinese`；
- `output/report.md` 的标题、字段、状态、summary 标题为中文；
- JSON 字段名/枚举保持英文；引文保持原文。

### 5.2.1 中英文对照物料（review 默认）

review/problem_finding 模式在任何语言下都会准备中英文两套审稿物料：

```bash
python scripts/hpe.py render --run "$RUN" --format md,json
# render 返回 bilingual.tasks；对每个 prompt 启动 fresh 子代理翻译
python scripts/hpe.py bilingual --run "$RUN" --check --json
```

最终应存在：

```text
output/report-zh.md
output/report-en.md
output/review-rounds-zh.md
output/review-rounds-en.md
output/final-opinion-zh.md
output/final-opinion-en.md
```

`report-zh/en.md` 是最终报告的中英文对照；`review-rounds-zh/en.md` 包含
每轮所有 reviewer 的完整评价和作者扮演者的逐 issue 回复；
`final-opinion-zh/en.md` 是最终审查意见。默认 review 至少包含一轮交叉复审
（`rounds >= 1`），只有显式 `--rounds 0` 才跳过作者回复。如果
`bilingual --check` 返回 `ok=false`，不要宣称报告已完成。

### 5.3 详细总结

```bash
python scripts/hpe.py init \
  --pdf proposal.pdf \
  --purpose summary \
  --material proposal \
  --domain ai-ict \
  --language zh \
  --no-digest

# 默认输出：proposal.hpe/
RUN="proposal.hpe"
python scripts/hpe.py next --run "$RUN" --executor host --json
# 子代理写 summary/summary.json
python scripts/hpe.py submit --run "$RUN" --task T-0001 --file summary/summary.json
python scripts/hpe.py advance --run "$RUN"
python scripts/hpe.py render --run "$RUN" --format md,json
```

输出：`output/summary.md`、`output/summary.json`、`output/report.*`。
summary 不产生 ledger/decision。

### 5.4 问题查找

```bash
python scripts/hpe.py init \
  --pdf paper.pdf \
  --purpose problem_finding \
  --material paper \
  --domain materials \
  --reviewers methodology,experiments \
  --rounds 0
```

报告标题为“问题查找报告”，decision 只作为建议动作，不是录用/拒稿结论。

### 5.5 API 模式（可选）

先创建独立 key 文件（0600）：

```bash
python scripts/hpe.py init-llm-config
# 编辑 ~/.hpe-paper-evaluator/llm_apis.json，填入 provider/base_url/model/key
```

显式指定 agent 基座（推荐）：

```bash
python scripts/hpe.py run \
  --pdf paper.pdf \
  --mode api \
  --agent-backend deepseek:deepseek-v4.1-flash \
  --purpose review --material paper --domain ai-ict \
  --reviewers methodology,experiments,novelty \
  --rounds 2 --budget-usd 3.0
```

- 未显式指定 reviewer/author/digest/meta backend 时，全部继承 agent 基座。
- 同厂商不同模型默认拒绝；不同厂商允许但记录 warning。
- 没有 key 时退出码 3，错误只显示 provider/env 变量名。
- 如果 `opencode-go` 等宿主网关没有配置在 `llm_apis.json`，API 模式会明确
  报错并建议改用 host 模式；它不会自动换成 `deepseek-chat` 之类的模型。
- 真实网络 API 未在本仓库测试中调用；只提供本地 stub 回归。

### 5.6 领域与 reviewer 选择

| 场景 | reviewers |
|---|---|
| 通用论文 | `methodology,experiments,novelty` |
| 理论/算法 | `methodology,novelty,devils_advocate` |
| 实验/系统 | `experiments,reproducibility,practitioner` |
| 研究提案 | `methodology,novelty,practitioner` |
| 实验设计 | `experiments,reproducibility,devils_advocate` |
| 高风险/争议 | 加 `devils_advocate` |

默认 3 个审稿人；更多人不自动提高质量，只会增加成本。`--rounds 0` 是
最快的有用模式。

### 5.7 恢复与失败

```bash
python scripts/hpe.py status --run "$RUN" --json
python scripts/hpe.py next --run "$RUN" --executor host --json
```

- 宿主崩溃：stale task 自动回收；
- reviewer 失败：coverage N-1/N，decision provisional；
- coverage < 50%：run failed，不生成“完整”报告；
- 预算超限：安全点停止，增大预算后继续；
- meta 输出非法：`advance` 写确定性 policy-only decision 并标 provisional。

---

## 6. CLI 参考

| 命令 | 作用 |
|---|---|
| `init` | 摄取 PDF、创建 run、生成第一批任务 |
| `next` | 取得可执行任务包（并 claim） |
| `submit` | 校验并提交一个任务 JSON |
| `fail` | 标记任务失败（timeout/空输出等） |
| `advance` | 当前阶段全部终态后推进 |
| `status` | 查看阶段、任务、覆盖率、policy |
| `render` | 生成 `output/report.md/json/html` 或 summary，并准备中英文对照任务 |
| `bilingual` | 生成/校验 `report-zh/en.md`、`review-rounds-zh/en.md`、`final-opinion-zh/en.md` |
| `run` | API/hybrid 模式下执行整个循环 |
| `usage` | 输出载入时的使用提示（`--language zh|en`、`--json`） |
| `doctor` | 显示 Python/PDF 引擎/Schema/agent/LLM 配置状态 |
| `init-llm-config` | 创建 0600 的独立 LLM key 模板 |
| `legacy` | 转发到 v1 `hpe_runner.py`（兼容旧 20 节输出） |

常用参数：

```text
--pdf, --out, --profile, --purpose, --material, --domain
--reviewers, --rounds/--debate-rounds, --language
--mode host|api|hybrid, --agent-backend, --allow-backend-mismatch
--reviewer-backends, --author-backend, --digest-backend, --meta-backend
--llm-config, --isolation, --blind-paper
--max-paper-tokens, --max-output-tokens, --allow-truncation
--budget-usd, --max-input-tokens, --max-output-tokens-total
--allow-poor-extraction, --ocr
--vision-pages off|auto|all, --vision-max-pages, --vision-dpi
--no-digest, --no-early-stop, --lenient-evidence
--force-unlock, --run-id, --json
```

---

## 7. 输出文件

```text
<PDF目录>/<PDF文件名>.hpe/        ← 默认位置；--out 可覆盖
├── run.json                    运行配置、agent、backend 一致性、vision
├── state.json                  阶段、任务、覆盖率、warnings、policy
├── paper/
│   ├── manifest.json           paper_id、标题、分节、图表、vision
│   ├── text.txt                全文（含 [[PAGE n]]）
│   ├── pages.json              页级文本
│   ├── sections/Sxx.md         分节文本
│   └── pages/page-0006.png     可选页面图（多模态）
├── prompts/T-xxxx.prompt.md    实际发送给子代理的完整提示词
├── digest/digest.json
├── artifacts/
│   ├── reviews/R1.round0.review.json
│   ├── rebuttals/A1.round1.rebuttal.json
│   ├── evidence/T-xxxx.json    每条证据的校验结果
│   ├── ledger.json / ledger.final.json
│   ├── meta_review.json
│   └── _invalid/               校验失败时的原始输出
├── logs/events.jsonl / usage.json
└── output/
    ├── report.md / report.json / report.html
    ├── report-zh.md / report-en.md                最终报告中英文对照
    ├── review-rounds-zh.md / review-rounds-en.md   所有审稿人的多轮意见 + 作者每轮回复
    ├── final-opinion-zh.md / final-opinion-en.md   最终审查意见
    ├── bilingual-tasks.json                       待执行的翻译任务
    └── summary.md / summary.json（summary 模式）
```

`review-rounds-zh/en.md` 在 review/problem_finding 模式下包含：

- 每一轮的每个 reviewer：recommendation、六维分数、confidence、summary、
  strengths、highlights、issues、questions、missing_evidence、coverage；
- round ≥ 1 的 issue_updates、position_changes、new_issues、remaining_concerns；
- 每一轮作者（含 simulated）的逐 issue response、paper evidence、revision plan、
  global clarifications、unresolved。

`final-opinion-zh/en.md` 是最终审查意见：decision/policy、共识、必须修订、
可选建议、争议/少数意见、限制、问题统计。主语言版本由脚本直接生成，另一
语言版本由 `bilingual` 翻译任务生成；`hpe.py bilingual --check` 用于确认
四个文件齐全。

报告内容包括：

- 裁决摘要（recommendation、policy、override、provisional、score、coverage）；
- 论文信息、agent backend、提取质量；
- 评分矩阵（六维 + overall + recommendation）；
- 问题台账（severity/status/evidence/author response/editor rationale）；
- 优点、创新/技术亮点；
- 分歧与少数意见、必须修订；
- 警告与局限（提取、注入、预算、隔离、backend mismatch）；
- 运行清单与证据索引。

---

## 8. 测试

```bash
cd hpe-paper-evaluator-v2
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q
python3 -m pyflakes scripts tests
python3 -m compileall -q scripts tests
```

当前测试覆盖：

- 摄取、分节、质量门、页范围、引文归一化、CJK、扫描件；
- Schema + 语义校验（错页、伪造引文、覆盖率、状态变化、meta override）；
- ledger 合并、policy 阈值、meta 决议、分数/dissent；
- 完整 host 循环（rounds=0/2）、early stop、coverage 失败、stale 恢复；
- prompt 渲染、匿名化、上下文预算、注入扫描；
- CLI 退出码、API stub、Kimi/OpenAI-compatible provider；
- 新增：agent 基座一致性、同厂商不同模型拒绝、中文报告、自动语言、
  中文 summary、页面图生成与 prompt 注入、默认输出到 PDF 同目录、
  多轮审稿/作者回复文件、最终审查意见文件、中英文对照任务与校验、
  内联图表标题检测、全 table/figure 页面图选择、`images_read` 回退提示、
  `usage` 命令。

历史真实论文评测记录保留在 [`CONSTRUCTION-TEST-LOG.md`](CONSTRUCTION-TEST-LOG.md)；
当前版本不再把测试 run 写入 skill 仓库——运行产物默认在 PDF 同目录的
`<PDF文件名>.hpe/`。

---

## 9. 已知限制

- 表格/公式/图的**单元格**默认不结构化抽取；`caption_only` 只保证标题，
  页面图是补充，不保证所有宿主都能读图。
- OCR 可选且默认关闭；扫描件默认拒绝。
- 分节器对复杂双栏/书籍仍会过度切分；已知问题见评测报告。
- `paper_id` 是 PDF SHA-256 前缀；字节相同的 PDF 共享 ID。
- 同厂商不同模型默认拒绝，但 `--allow-backend-mismatch` 可以显式放开；
  放开后报告会记录 mismatch。
- host 模式 usage 默认不可知；需要宿主 `submit --usage-json` 才有成本数据。
- `author=provided` 只导入一轮答辩；后续轮退化为 `none`。
- 未实现 `--redact` 分享导出；run 目录包含论文全文，分享前手动清理。
- 报告是模型辅助产物，不是同行评审结论。

---

## 10. 常见问题

**Q: 我应该用 host 还是 api？**
A: 默认用 host。它就是当前 agent 模型，不需要第二个 key，也不会出现
“同 provider 不同基座”。需要批量/CI/多模型时才用 api。

**Q: 为什么 API 模式报 “Agent backend ... not configured”？**
A: 脚本检测到当前 agent 模型（例如 OpenCode 的 `opencode-go:deepseek-v4.1-flash`），
但 `llm_apis.json` 没有该 provider 的 base_url/key。用 host 模式，或把该
provider/model 加入独立 key 文件，不要让它偷偷换成别的模型。

**Q: 中文论文但报告是英文？**
A: 旧版本按论文语言判断；v3.1 用 `run.json:output_language`。重新 `init`
并传 `--language zh`，或检查 run.json 是否来自旧版本。

**Q: 表格数字读不出来？**
A: 用 `--vision-pages auto`（默认）或 `all`，让宿主多模态模型读页面图；
如果宿主不支持读图，报告会要求降低 confidence 并列入 missing_evidence。

**Q: 为什么 reviewer 不能看其他 reviewer 的 round-0 意见？**
A: 这是盲审独立性的核心。交叉复审阶段才匿名提供上一轮其他意见。

**Q: 可以只跑一个 reviewer 吗？**
A: 可以，但 coverage/decision 的解释力下降；默认 3 个。

---

## 11. 第三方与许可

见 [`THIRD_PARTY.md`](THIRD_PARTY.md)。重点：

- PyMuPDF 是 AGPL-3.0 或商业许可；分发服务/二进制前需评估；
- `pdftotext` 属于 Poppler（GPL）；
- 上游 HPE-tool 的 20/12/14 节结构与 4 类质询 checklist 在
  `prompts/upstream/` 中保留来源与 commit；上游仓库未声明许可证，再分发前
  需与上游作者确认。

---

## 12. 相关文件

```text
SKILL.md                     宿主 agent 的操作手册
README.md                    本文件：设计、实现、用法
PLAN-v3.md                   v3 规划
CONSTRUCTION-TEST-LOG.md     施工/测试记录
docs/HOSTS.md                OpenCode / Codex CLI / DeepSeek Harness 适配
prompts/                     角色、领域、上游结构
schemas/                     JSON Schema 契约
config/                      profile/domain/LLM/agent 配置模板
scripts/hpe/                 确定性实现
tests/                       117 项 pytest
```

运行产物默认不写入仓库，而是写到 PDF 同目录的
`<PDF文件名>.hpe/`（中间文件 + 最终报告中英文物料）。
