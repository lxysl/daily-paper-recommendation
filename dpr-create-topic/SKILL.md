---
name: dpr-create-topic
description: Create a Daily Paper Recommendation topic markdown tracker under ./topics from a topic name, optional description, and optional arXiv seed papers. Use when the user asks to add, initialize, create, or track a DPR topic for future daily recommendations.
---

# DPR Create Topic

## Workflow

Use the `academic` conda environment and the bundled script.

1. Extract the topic display name from the user request.
2. If the user gives arXiv URLs or IDs, pass each one with repeated `--paper`.
3. Draft a concise Chinese topic description if the user did not provide one. Base it on the topic name and any seed paper titles/abstracts you inspect.
4. Design an explicit `include_keywords` list for future daily matching. Include 5-12 English phrases that are specific to the topic, and include at least one strong anchor phrase with two or more words. Prefer phrases such as method names, task names, or distinctive paradigm names; avoid broad one-word terms such as `learning`, `model`, `vision`, `language`, `reasoning`, `agent`, `image`, `multimodal`, or `benchmark` unless they are part of a more specific phrase.
5. Run:

```bash
conda run -n academic python dpr-create-topic/scripts/create_topic.py create --workspace . --topic "TOPIC NAME" --description "中文描述" --include-keyword "specific phrase" --include-keyword "another anchor phrase" --paper 2409.05591
```

Omit `--paper` when there are no seed papers. Repeat `--include-keyword` for every keyword phrase, or pass a comma-separated list. Use `--slug` only if the user explicitly wants a different filename.

## Output Contract

The script creates `topics/<slug>.md` with:

```markdown
# Topic Name

中文描述

include_keywords:
- specific phrase
- another anchor phrase

| Date | Paper | Link | One-line Summary |
| --- | --- | --- | --- |
```

Seed papers are added as initial rows. The paper title stays in English when appropriate; the summary should be concise and may come from DeepXiv TLDR or arXiv abstract.

## Rules

- Do not overwrite an existing topic unless the user explicitly asks for replacement; the script fails on conflicts by default.
- Validate every seed paper as an arXiv ID or URL before creating the topic.
- If DeepXiv brief data is unavailable, use arXiv metadata as fallback.
- Keep the topic description short enough to be useful as future matching context.
- Always create an `include_keywords` block. These keywords are the local matching contract for `dpr-daily-recommendation`; do not rely on historical table rows to define a topic.
- Include at least one strong anchor phrase of two or more words. Strong anchors should capture the topic semantics, e.g. `thinking with images`, `visual tool use`, `on-policy distillation`, or `world action model`.
- Avoid generic keyword lists that would match most AI/CV/LG papers.
- After creation, mention the created path and any seed rows included.
- If DeepXiv-related code fails, read the official DeepXiv documentation at https://github.com/DeepXiv/deepxiv_sdk/blob/main/README.md and https://github.com/DeepXiv/deepxiv_sdk/blob/main/USAGE.md before debugging further.
