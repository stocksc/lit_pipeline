# Model comparison review: triage and deep read

Run on 2026-10-01. Three papers (FairGBM 2209.07850v5, LDA search 2512.23943v2, STRATA 2504.21259v2) went through production's triage and deep-read prompts with four models. Triage ran three times per model, and each model's deep read was given its own first triage. All 48 calls succeeded, for $1.75 in total. The side-by-side outputs are in `report.html`.

## Bottom line

- **Scores don't separate the models on these papers.** Every triage run scored 8–10, and every deep read 8 or 9, matching production's earlier Opus 5 scores (9, 9, 8). The differences are in accuracy and in what each narrative notices.
- **Haiku 4.5 made the most errors.** Its deep reads have three unsupported claims: a misread speedup, an invented regulator, and invented per-state numbers. The others had at most one small slip each: Fable 5.1 overstated one limitation, Opus 5.5 added one detail from outside knowledge, and I found none from Sonnet 5.5.
- **The stronger models catch the practitioner issues Haiku misses.** Fable 5.1 caught the most, including the single most lending-relevant caveat in the LDA paper.
- **Opus 5.5 is the best-value deep-read model.** It made no errors, gave the sharpest lending-law framing, and costs 26% less than production's Opus 5 on the same papers.
- **Not tested here: which borderline papers triage should send to a deep read.** All three papers are clear positives.

## Scores

Triage runs → deep-read score. "Production" is the earlier Haiku 4.5 → Opus 5 run.

| Paper | Production | Haiku 4.5 | Sonnet 5.5 | Opus 5.5 | Fable 5.1 |
|---|---|---|---|---|---|
| FairGBM | 9 → 9 | 9 9 9 → 9 | 9 9 10 → 9 | 10 10 9 → 9 | 10 10 10 → 9 |
| LDA search | 9 → 9 | 9 9 9 → 9 | 10 10 10 → 9 | 9 9 9 → 8 | 9 10 10 → 9 |
| STRATA | 9 → 8 | 9 9 9 → 8 | 9 9 9 → 8 | 9 9 8 → 8 | 9 9 9 → 8 |

## Accuracy

I checked the numbers, named entities (regulators, datasets, venues) and scope claims in all twelve deep reads against the paper text the models were given.

| Model | Paper | Claim | What the paper says |
|---|---|---|---|
| Haiku 4.5 | FairGBM | "2-21x faster than baselines" | Misreads Table 2. 21.4 is a baseline's training time; ×2.4 is FairGBM's cost *relative to plain LightGBM* (slower). The text says FairGBM trains in under a tenth of EG's time. |
| Haiku 4.5 | LDA | Regulatory context includes FHFA | FHFA never appears in the paper. It's in your interests text. |
| Haiku 4.5 | STRATA | "Hawaii 69.5% accuracy vs. 95%+ elsewhere" | Not in its input. Per-state results are in an appendix that is trimmed along with the references; the text only says Hawaii was excluded from the state rankings. |
| Haiku 4.5 | LDA | Can't extend to other fairness metrics "without redesign" | The paper doesn't discuss this; it's unsupported speculation. |
| Fable 5.1 | LDA | Covers only seed/split retraining, "not feature, hyperparameter, or constraint-based alternatives" | Overstated. The framework lets the training procedure sample hyperparameters or even choose among algorithms; the real restriction is iid, non-adaptive sampling, which Fable lists separately. |
| Opus 5.5 | FairGBM | "scikit-learn-style interface" | Not in the text. It's true of the released package, so it comes from outside knowledge. |

Haiku also misframes what papers are:
- It files LDA search ("tackles debiasing through model multiplicity") and STRATA ("novel debiasing approach") under debiasing, which inflates their fit with your top interest. The other three models say explicitly that neither is a debiasing method.
- Its STRATA rationale says the paper "provides production-ready code," although its own limitations note that the training code and weights are withheld.
- Some of its limitations are the generic critique the prompt asks it to avoid, e.g. "no comparison with recent LLM-based fairness methods" and "code not fully validated in review."

Claims that checked out (a sample): FairGBM's ~10× speedup over EG, its 500K-instance fraud dataset, and binary groups in experiments with multi-group support in the method. LDA's 3,000-row subsamples, the "about 60 models" finding, and Algorithm 2 not helping. STRATA's 19.7M voter records, its 3.8%/3.4% NHPI/AIAN recall, 5.6% "Other" recall (computed from the confusion matrix), and the 99.52% near-ceiling outcome.

## What each model caught

✓ = raised · – = not raised · ✗ = got wrong

**FairGBM**

| Point | Haiku | Sonnet | Opus | Fable |
|---|---|---|---|---|
| Code is released | ✓ | ✓ | ✓ | ✓ |
| Needs the protected attribute at training time | – | ✓ | ✓ | ✓ |
| ...framed as a disparate-treatment / ECOA–Reg B risk in US credit | – | – | ✓ | – |
| ...needed at training only, so most lenders would need imputed race (BISG) | – | ✓ | – | ✓ |
| Real-world test is fraud detection; public benchmarks are census data, not credit | – | – | ✓ | ✓ |
| Guarantees hold for the randomized classifier, not the deployed last iterate | ✓ | – | ✓ | ✓ |
| Fraud dataset is proprietary | ✓ | ✓ | ✓ | ✓ |
| LightGBM fork may lag upstream / complicate model governance | – | ✓ | – | ✓ |
| No adverse-action explainability | – | – | ✓ | – |
| Useful for generating LDA-search candidates quickly | – | – | ✓ | – |

**LDA search**

| Point | Haiku | Sonnet | Opus | Fable |
|---|---|---|---|---|
| A stopping rule, not a debiasing method | ✗ | ✓ | ✓ | ✓ |
| 3,000-row subsamples may not transfer to industry data | ✓ | ✓ | ✓ | ✓ |
| Firm must pick the cost threshold γ | ✓ | ✓ | ✓ | ✓ |
| Assumes iid, non-adaptive search | ✓ | – | ✓ | ✓ |
| Bound is loose (overshoots by tens of models) | – | ✓ | ✓ | ✓ |
| The data-driven variant (Algorithm 2) didn't improve the bounds | – | – | ✓ | ✓ |
| **Bound performs worse for logistic regression and on HMDA** | – | – | – | ✓ |
| Lets you back out a firm's implied cost from where it stopped | – | – | – | ✓ |
| Industry data: lower retraining variance and higher γ | – | – | – | ✓ |

**STRATA**

| Point | Haiku | Sonnet | Opus | Fable |
|---|---|---|---|---|
| Training code and weights withheld; can't retrain | ✓ | ✓ | ✓ | ✓ |
| Inference only through a commercial AWS Marketplace license | – | ✓ | ✓ | ✓ |
| Authors are the vendor | – | ✓ | ✓ | ✓ |
| PPP validation is in-source (PPP also used in training) | in rationale only | ✓ | ✓ | ✓ |
| Near-zero recall for AIAN/NHPI | ✓ | ✓ | ✓ | ✓ |
| Use summed probabilities, not hard labels, for disparity estimates | – | ✓ | ✓ | ✓ |
| Imputation, not debiasing | ✗ | ✓ | ✓ | – |
| Downstream disparity demo uses a near-ceiling outcome (99.5%) | – | ✓ | – | – |

The row I'd weight most is the LDA caveat in bold: per the paper, the stopping rule "appears to perform worse for logistic regression on all datasets and all methods on HMDA." That is close to the standard fair-lending setup, and only Fable mentioned it.

## Triage

- **Scores are stable and nearly identical.** All 36 runs were 8–10, and no model varied by more than a point on a paper. Haiku was the most consistent (9 on every run).
- **The newer models reason about your rubric.** They check the "perfect 10" criteria explicitly ("not a debiasing method," "the abstract mentions no code"). Haiku's rationales are generic, and it called STRATA a debiasing approach.
- **Outside knowledge leaked into triage on the oldest paper.** Triage sees only the abstract, which for FairGBM (2022) never mentions code. Sonnet, Opus and Fable all credited it with an open-source implementation anyway, and Opus and Fable cited that in giving it 10s. They didn't do this for the 2025 papers, saying instead that the abstracts don't mention code. So these models likely know FairGBM from training data. Production triages papers from the past week, which no model can have seen, so this sample probably flatters the newer models' triage.
- **Thinking barely registered.** Triage output was 102–191 tokens for every model, about the size of the answer alone. The thinking models chose to think very little on this task.

## Length (targets: summary ~100 words, bullets ~50 words)

| Model | Summary words | Relevance words | Limitations words |
|---|---|---|---|
| Haiku 4.5 | 83 / 92 / 81 | 66 / 57 / 51 | 77 / 98 / 67 |
| Sonnet 5.5 | 93 / 104 / 108 | 49 / 47 / 40 | 56 / 67 / 69 |
| Opus 5.5 | 105 / 116 / 129 | 48 / 52 / 58 | 57 / 65 / 77 |
| Fable 5.1 | 109 / 148 / 119 | 46 / 57 / 48 | 72 / 101 / 50 |

Sonnet 5.5 stays closest to the targets. Fable runs longest on summaries. Limitations run over the target for every model.

## Cost

Per-call averages from this run. Monthly figures use production's September volume (66 triage calls, 27 deep reads).

| Configuration | Triage / call | Deep read / call | Per month |
|---|---|---|---|
| Haiku 4.5 only | $0.0018 | $0.020 | $0.66 |
| Haiku triage + Sonnet 5.5 deep read | $0.0018 | $0.053 | $1.55 |
| Haiku triage + Opus 5.5 deep read | $0.0018 | $0.108 | $3.03 |
| **Current: Haiku triage + Opus 5 deep read** | $0.0018 | $0.145* | $4.03 |
| Haiku triage + Fable 5.1 deep read | $0.0018 | $0.285 | $7.81 |
| Fable 5.1 for both | $0.023 | $0.285 | $9.24 |

\* Production's recorded Opus 5 deep-read costs for these same three papers.

## Recommendations (for discussion)

- **Deep read: move to Opus 5.5 as planned.** On these papers it had no errors and the best lending-specific judgment, at ~$1/month less than today.
- **Consider Fable 5.1 if its extra catches matter to you.** It found the most, and all its specific claims checked out apart from one overstated limitation. It costs about $4.80/month more than Opus 5.5 at current volume.
- **Sonnet 5.5 is a credible budget option.** It had no errors, was concise, and was the only model to notice STRATA's near-ceiling demo. It costs half as much as Opus 5.5.
- **Don't use Haiku for deep reads.** It invented specific numbers.
- **Triage: no evidence here to move off Haiku.** The test that would settle it is a triage-only run on borderline papers (ones Haiku scored 5–7). It would show whether a stronger model changes which papers get deep-read, and it would cost well under a dollar.
- **Caveats:** three papers, one deep read per model, and at least one paper the models evidently already knew.

## Your notes

_Add your read here: anything the models caught or missed that I haven't listed, and whether Opus's ECOA/Reg B framing and Fable's extra depth match what you'd want in the digest._
