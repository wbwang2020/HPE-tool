# 运行目录示例

```text
runs/20261105-120000-a1b2c3/
├── run.json
├── state.json
├── run.lock
├── paper/
│   ├── manifest.json
│   ├── text.txt
│   └── sections/
│       ├── S01.md          # Abstract
│       ├── S02.md          # Introduction
│       ├── S03.md          # Related Work
│       ├── S04.md          # Method
│       ├── S05.md          # Experiments
│       ├── S06.md          # Results
│       ├── S07.md          # Discussion
│       ├── S08.md          # Conclusion
│       └── S09.md          # References
├── digest/
│   └── digest.json
├── prompts/
│   ├── T-0001.prompt.md    # digest
│   ├── T-0002.prompt.md    # R1 round0
│   ├── T-0003.prompt.md    # R2 round0
│   ├── T-0004.prompt.md    # R3 round0
│   ├── T-0005.prompt.md    # R4 round0
│   ├── T-0006.prompt.md    # author round1
│   ├── T-0007.prompt.md    # R1 round1
│   ├── T-0008.prompt.md    # R2 round1
│   ├── T-0009.prompt.md    # R3 round1
│   ├── T-0010.prompt.md    # R4 round1
│   ├── T-0011.prompt.md    # meta
│   └── T-0012.prompt.md    # render（无 LLM，仅记录）
├── artifacts/
│   ├── reviews/
│   │   ├── R1.round0.review.json
│   │   ├── R2.round0.review.json
│   │   ├── R3.round0.review.json
│   │   ├── R4.round0.review.json
│   │   ├── R1.round1.review.json
│   │   ├── R2.round1.review.json
│   │   ├── R3.round1.review.json
│   │   └── R4.round1.review.json
│   ├── rebuttals/
│   │   ├── A1.round1.rebuttal.json
│   │   └── A1.round2.rebuttal.json
│   ├── ledger.json
│   └── meta_review.json
├── logs/
│   ├── events.jsonl
│   └── usage.json
├── cache/
│   └── <prompt-hash>.json
└── output/
    ├── report.md
    └── report.json
```

## state.json 片段

```json
{
  "schema_version": "hpe/v2",
  "run_id": "20261105-120000-a1b2c3",
  "stage": "reviewing",
  "round": 0,
  "tasks": {
    "T-0001": {"kind": "digest", "status": "done", "output": "digest/digest.json"},
    "T-0002": {"kind": "reviewer_report", "status": "done", "actor": "R1"},
    "T-0003": {"kind": "reviewer_report", "status": "done", "actor": "R2"},
    "T-0004": {"kind": "reviewer_report", "status": "pending", "actor": "R3"},
    "T-0005": {"kind": "reviewer_report", "status": "pending", "actor": "R4"}
  },
  "coverage": {
    "reviewers_expected": 4,
    "reviewers_done": 2
  },
  "budget": {
    "spent_usd": 0.18,
    "max_usd": 3.0
  },
  "updated_at": "2026-11-05T12:05:00Z"
}
```

## report.md 结构示例

```markdown
# 审稿报告：Long-Tailed Image Classification under Low Labeling Budgets

> HPE v2 · run 20261105-120000-a1b2c3 · 2026-11-05
> 本报告由模型辅助生成，仅供研究/写作参考，不构成同行评审结论。

## 0. 裁决摘要
- 推荐：Major Revision
- 政策决定：Major Revision
- 综合分：5.1 / 10（median 5.0）
- 置信度：0.76
- 未解决：blocking 0 · major 2 · minor 4
- 审稿覆盖：4/4

## 1. 论文概要
...

## 2. 评分矩阵
| Reviewer | Overall | Novelty | Soundness | Evidence | Repro | Clarity | Significance | Recommendation |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| R1 | 5.0 | ... |
| R2 | 5.0 | ... |

## 3. 主要问题（必须修改）
### I-001 缺少与最强基线的同算力比较
- 严重度：major
- 状态：partially_resolved
- 提出者：R1, R2, R3
- 证据：第 8 页 Table 2 ...
- 作者回应：...
- 编辑判断：...

## 4. 次要问题
...

## 5. 优点
...

## 6. 辩论过程（匿名）
### 第 1 轮
- Reviewer A：...
- Reviewer B：...
- 作者回应：...

## 7. 分歧与不确定性
...

## 8. 修订清单
- [ ] I-001 补充同算力基线
- [ ] I-002 增加 stage-2 消融
- [ ] I-003 报告 3 个种子的均值/方差

## 9. 局限与免责声明
...

## 附录 A：证据索引
| Issue | Page | Section | Quote |
...

## 附录 B：运行清单
- 模型：...
- Prompt 版本：2.0.0
- Token：...
- 费用：...
```
