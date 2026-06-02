---
name: dpr-daily-recommendation
description: Collect and review the previous arXiv announcement day's papers for Daily Paper Recommendation using arXiv announcement enumeration plus DeepXiv enrichment. Use when the user asks to run a daily paper recommender, generate a DPR report, inspect yesterday's AI/LG/CV/RO papers, update topic markdown trackers, or manually run the local DPR recommendation workflow.
---

# DPR Daily Recommendation

## Workflow

Use the `academic` conda environment. Do not call the `deepxiv` CLI directly; the local CLI may import optional agent code and trigger `tiktoken` network initialization. Run the bundled Python script instead.

1. Collect candidates for the previous arXiv announcement day, not the previous submission-date window:

```bash
conda run -n academic python dpr-daily-recommendation/scripts/daily_recommend.py collect --workspace .
```

Use `--date YYYY-MM-DD` to backfill an arXiv announcement-date report. Use `--max-per-category 5` only for a quick smoke test.

2. Use `reports/YYYY-MM-DD/` as the report working directory for that day. Read `reports/YYYY-MM-DD/review.md`, `cache/deepxiv/YYYY-MM-DD/candidates.json`, and the full enriched paper cache. Review every collected paper's title, category, abstract, and TLDR/brief before making any final recommendation.

3. Build and refine a deep-review candidate pool iteratively:

- Start with a broad provisional pool from trending papers, topic matches, watched authors/institutions, and any abstracts that look important, broadly useful, surprising, or likely to interest the reader.
- Pull longer content for promising papers: DeepXiv raw full text when available, otherwise key sections such as Introduction, Method, Experiments, Results, Discussion, or preview/head summaries.
- Reading a paper deeply does not mean it should appear in the final daily report. Treat deep reads as evidence for later comparison.
- After each reading pass, compare new candidates against the current weakest recommended papers. Add newly valuable papers and remove papers that are less novel, less useful, too narrow, mostly position-only, weakly supported, or redundant with stronger candidates.
- Repeat the loop until additional candidates are unlikely to change the final set. The final report should contain only papers that remain valuable after this cross-comparison.
- Use sub-agents for deep review when the collected candidate set is larger than a quick smoke test or when the review pool is large enough to benefit from parallel reading. Split candidate reading across disjoint paper sets and ask sub-agents to write concise evidence notes or subfiles under `reports/YYYY-MM-DD/`, not final recommendations; the main agent remains responsible for final selection and synthesis.

4. Make AI decisions by copying `reports/YYYY-MM-DD/decisions.template.json` to `reports/YYYY-MM-DD/decisions.json` and editing these keys:

```json
{
  "spotlight": [],
  "author_hits": {},
  "institution_hits": {},
  "topic_updates": {},
  "excluded": []
}
```

Keep arXiv IDs only. Put topic updates under the existing topic slug from `topics/*.md`.

5. Apply decisions:

```bash
conda run -n academic python dpr-daily-recommendation/scripts/daily_recommend.py apply --workspace . --date YYYY-MM-DD --decisions reports/YYYY-MM-DD/decisions.json
```

By default, `apply` also downloads arXiv source only for the final recommended papers, extracts high-confidence model/structure and result/effect figures, writes assets under `reports/YYYY-MM-DD/assets/<arxiv-id>/`, and records status in `reports/YYYY-MM-DD/source-figures.json`. Use `--skip-source-figures` to disable this, `--max-source-figures-per-paper` to change the per-paper limit, and `--source-sleep-seconds` to control arXiv source download pacing. Source figure failures are warnings and must not block the final report. Keep source-figure failures separate from paper caveats or limitations; if a recommended paper has a figure extraction failure, mention it as a small note directly below that paper's summary, e.g. `<small>图片提取失败：...</small>`.

6. Check `reports/YYYY-MM-DD/final.md`, `reports/YYYY-MM-DD/source-figures.json`, and the changed `topics/*.md` rows. Write the user-facing DPR report in Chinese, including the final recommendation text, selected model/result figures when available, downgrade/exclusion notes, caveats, and the final response summary to the user. If the generated report is too terse, manually rewrite it from the deep-review evidence instead of leaving abstract snippets. When rewriting, do not treat source-figure extraction failures as paper limitations; keep the limitation/caveat sentence about the paper itself, and place figure extraction status as a small note below the recommendation summary. Keep filenames, JSON keys, commands, arXiv IDs, and technical terms unchanged where appropriate. Keep all day-specific review notes, sub-agent notes, decision files, source-figure records, and final reports under `reports/YYYY-MM-DD/`.

## Subagent Deep Review

After collection succeeds and `reports/YYYY-MM-DD/review.md`, `cache/deepxiv/YYYY-MM-DD/candidates.json`, and `cache/deepxiv/YYYY-MM-DD/papers.enriched.json` exist, deep-review candidate reading must use sub-agents unless the user explicitly says not to use sub-agents for this session.

Creating sub-agents is an internal Codex collaboration step, not a system permission operation. Do not ask the user to approve or confirm sub-agent spawning when this skill's delegation criteria are met. Treat use of this skill plus the criteria below as authorization to follow the sub-agent workflow. The required user-facing update is a notice of delegation, not a consent prompt.

Use sub-agents whenever either condition is true:

- The enriched candidate set has more than 25 papers.
- The provisional deep-review pool has more than 8 papers after the main agent's first pass through the review report, candidates JSON, and enriched cache.

Before spawning sub-agents, tell the user that sub-agents are being used and which paper groups or review slices are being delegated. Do not phrase this as a request for permission. The parent agent must own the final recommendation decisions, `decisions.json`, topic updates, and final report synthesis.

If sub-agents cannot be spawned because the current environment or tool policy blocks them, stop before deep-review reading, explain the blocker, and ask for explicit user direction before continuing sequentially. No silent sequential fallback: only an explicit user instruction such as "do not use sub-agents" or "run this sequentially" authorizes a normal sequential deep-review path.

Default delegation flow:

1. Parent runs collection and reads the review report, candidates JSON, and enriched cache.
2. Parent creates a provisional deep-review pool from trending papers, topic candidates, watched author/institution hits, and promising abstracts/TLDRs.
3. Parent splits the pool into disjoint paper groups by topic, evidence type, or arXiv ID ranges.
4. Parent spawns sub-agents for each group and asks for concise evidence notes under `reports/YYYY-MM-DD/`.
5. Sub-agents read assigned papers only, pull longer content when useful, compare within their assigned group, and write evidence notes.
6. Parent reads all sub-agent notes, performs cross-group comparison, chooses final recommendations, writes `decisions.json`, applies decisions, and rewrites the final report in Chinese.

Subagent write boundary: sub-agents may write only their assigned evidence-note files under `reports/YYYY-MM-DD/`. Do not let sub-agents edit `decisions.json`, `final.md`, `topics/*.md`, shared caches, or scripts. This keeps final selection and topic-tracker provenance centralized.

Subagent handoff contract:

- Give each sub-agent a disjoint list of arXiv IDs and the absolute paths to `review.md`, `candidates.json`, and `papers.enriched.json`.
- Tell each sub-agent which perspective to prioritize, such as RAG/retrieval, agent workflows, OPD/RL, unified multimodal models, multimodal fusion, TabPFN/tabular methods, or broad AI systems. Do not use robotics, robot manipulation, VLA, or WAM as default review slices unless the user explicitly requests them for that run.
- Ask each sub-agent to inspect title, category, abstract, TLDR/brief, and available head/section/raw/preview content for assigned papers.
- Ask each sub-agent to compare assigned papers against each other and identify which papers deserve promotion, downgrade, or exclusion.
- Tell each sub-agent to write a concise markdown evidence note under `reports/YYYY-MM-DD/`, with arXiv IDs, recommendation strength, evidence, caveats, and whether the paper should update a topic tracker.
- Tell each sub-agent not to make final recommendations for the whole day and not to edit decision or topic files.

Use this template for each sub-agent:

```text
Deep-review this assigned DPR paper group.

Report date: YYYY-MM-DD
Workspace: /absolute/path/to/workspace
Review report: /absolute/path/to/reports/YYYY-MM-DD/review.md
Candidates JSON: /absolute/path/to/cache/deepxiv/YYYY-MM-DD/candidates.json
Enriched cache: /absolute/path/to/cache/deepxiv/YYYY-MM-DD/papers.enriched.json
Assigned arXiv IDs:
- <id>
- <id>

Perspective to prioritize: <topic or review angle>

Read each assigned paper's title, category, abstract, TLDR/brief, and available head/section/raw/preview content. Pull longer content when it can change the recommendation. Compare papers within this group and identify promote/downgrade/exclude decisions with evidence and caveats.

Write only this evidence note:
/absolute/path/to/reports/YYYY-MM-DD/<note-name>.md

Do not edit decisions.json, final.md, topics/*.md, caches, or scripts. The parent agent owns final selection and synthesis.
```

## Inputs

- `authors.md`: one watched author per line; aliases use `|`, e.g. `- Kaiming He | K. He`.
- `institutions.md`: one watched institution per line; aliases use `|`. Institution matches require strong structured affiliation/org evidence.
- `topics/*.md`: one topic tracker per file with H1, description, an explicit `include_keywords` list, and table columns `Date | Paper | Link | One-line Summary`.

## Review Rules

- Review all collected paper abstracts/TLDRs before making recommendations. Do not recommend from the review artifact alone.
- The deep-read set is a working candidate pool, not the final report. A deeply read paper can still be dropped, and an initially overlooked abstract can still be promoted after further reading.
- Do not download arXiv source for the full candidate set. Fetch source figures only after `decisions.json` identifies the final recommended papers.
- For papers that look potentially important, broadly useful, surprising, or likely to interest the reader, pull longer paper content such as raw full text, preview, or key sections and read it before final selection.
- Selection must be iterative. Repeatedly add promising new papers, drop weaker old candidates, and compare papers against the current final cutoff until the recommendation set is stable.
- There is no fixed number of final recommendations. Keep only papers you judge high-value after comparison; do not pad the report to a target count, and do not keep a paper merely because it was deep-read.
- Write all final user-facing DPR output in Chinese, in your own words. This includes `final.md`, selected-paper recommendations, downgrade/exclusion explanations, caveats, and the final response summary to the user. Explain what each selected paper is really about, why it matters, what evidence supports the recommendation, and the key caveats or limitations. Paper caveats and limitations must describe the paper itself, not operational issues such as source-figure extraction. Prefer clarity and usefulness over brevity.
- Prefer yesterday's trending candidates when quality is comparable; use 7-day trending fallback only when yesterday does not provide enough notable papers and clearly label that in the final report.
- Exclude or downgrade limited-application papers such as remote sensing, medicine, and low-resource language unless they match a user topic. If they match a watched author or strong institution, keep them in that section with a short caveat. Prioritize multimodal models that are not primarily large-model/LLM papers, multimodal fusion, and TabPFN/tabular methods. Do not prioritize robotics, robot manipulation, VLA, or WAM papers; exclude or downgrade them by default unless the user explicitly requests that area for the current run or they are unusually central to a non-robotics multimodal/fusion question.
- Author hits are exact normalized alias matches. Do not infer a watched author from institution or lab context.
- Institution hits require structured affiliation/org/institution evidence. Do not infer institution membership from common author knowledge.
- Topic updates should only include papers worth appending to the topic tracker. Local topic matching uses the explicit `include_keywords` list and requires at least one strong multi-word anchor phrase match; avoid weak keyword-only matches unless the abstract/TLDR clearly fits.

## Failure Handling

- If `DEEPXIV_TOKEN` is missing, stop and tell the user to set it in the environment, `./.env`, or `~/.env`.
- If DeepXiv returns 404 for a paper, treat it as indexing delay; the script falls back to arXiv metadata.
- If authentication, rate-limit, arXiv parsing, or network errors occur, let the script fail visibly and report the error. Do not hide failures with broad exception handling.
- If DeepXiv-related code fails, read the official DeepXiv documentation at https://github.com/DeepXiv/deepxiv_sdk/blob/main/README.md and https://github.com/DeepXiv/deepxiv_sdk/blob/main/USAGE.md before debugging further.
