# Domain card: AI / CS / ICT

Applies to AI, machine learning, computer science, communications, networking, systems, HCI and related ICT work. The six HPE dimensions remain the scoring frame; this card tells you what to look for in this domain.

## What counts as evidence

- A claim about performance needs a dataset/benchmark, split protocol, metric definition, baseline, and a matched compute/training budget.
- A claim about novelty needs the closest categories of prior work and a concrete difference, not a self-declared "first".
- A claim about robustness/generalization needs distribution shift, OOD/edge cases, seeds, variance or a clear statement that it is not tested.
- A claim about efficiency needs latency/throughput/FLOPs/memory measured on stated hardware, not just parameter counts.
- A claim about reproducibility needs code/data availability, hyperparameters, seeds, environment and evaluation script.

## Common loopholes

1. **Benchmark confounds**: different training budgets, backbones, tokenizers, prompt formats or evaluation subsets across methods.
2. **Selection bias**: best checkpoint/seed/prompt chosen on the test set; "best of N" reported without N.
3. **Statistical weakness**: single run, no variance, small margin, no paired test, no effect size.
4. **Ablation gaps**: each contribution not isolated; component interactions hidden.
5. **Leakage**: train/test overlap, pretraining contamination, target leakage, tuning on test.
6. **Metric mismatch**: metric does not measure the claimed capability (e.g. accuracy for calibration, retrieval recall for reasoning).
7. **Novelty inflation**: new combination/framework called a new paradigm; baseline is a strawman.
8. **Reproducibility theater**: code link missing, data restricted, seeds absent, prompt omitted.
9. **Safety/ethics afterthought**: dual-use or privacy risk asserted away without a concrete path.
10. **LLM-specific**: prompt sensitivity, judge-model bias, contamination, cost/latency at deployment, non-determinism.

## Review focus

- State the **technical highlight** (what is genuinely new in the mechanism) separately from the **empirical highlight** (what the numbers show).
- For every major issue, name the exact table/figure/page and the missing comparison or statistic.
- Do not demand experiments from a purely theoretical paper; instead demand a proof, counterexample, or explicit scope condition.
- For a survey/review, evaluate coverage protocol, taxonomy validity, selection criteria and synthesis, not "no new method".
