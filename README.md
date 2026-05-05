# Daily Paper Recommendation

Daily Paper Recommendation (DPR) 是一个面向个人研究跟踪的 arXiv 每日论文推荐工作区。它每天从 arXiv announcement page 枚举 `cs.AI`、`cs.LG`、`cs.CV`、`cs.RO` 的新公告论文，再用 DeepXiv 做摘要、趋势、机构、作者和 topic 线索增强，最后由 LLM Agent 深读候选论文并生成中文推荐报告。

本项目包含两个 Codex skill：

- `dpr-daily-recommendation`：收集某一天的 arXiv 公告论文，生成 review artifacts、AI 决策模板、最终日报，并更新 topic tracker。
- `dpr-create-topic`：创建 `topics/*.md` 研究主题追踪文件，支持用 arXiv 种子论文初始化。

## 目录结构

```text
.
├── authors.md                         # 关注作者列表
├── institutions.md                    # 关注机构列表
├── topics/                            # 研究主题追踪表
├── reports/                           # 每日 review、决策、最终报告
├── cache/deepxiv/                     # arXiv/DeepXiv 缓存
├── dpr-daily-recommendation/
│   ├── SKILL.md
│   └── scripts/daily_recommend.py
└── dpr-create-topic/
    ├── SKILL.md
    └── scripts/create_topic.py
```

`cache/`、`reports/`、`topics/` 中的运行产物默认不加入 Git；仓库只保留 `.gitkeep` 以保存空目录。

## 安装 DeepXiv

本项目的脚本直接访问 arXiv 与 DeepXiv HTTP 接口，避免在工作流中导入 DeepXiv CLI 的可选 agent 依赖。但仍建议在同一个 Python 环境中安装 DeepXiv SDK，便于获取 token、手动排查和直接使用官方 CLI。

### 手动安装

推荐使用项目约定的 `academic` conda 环境：

```bash
conda activate academic
python -m pip install -U deepxiv-sdk
```

首次运行 DeepXiv CLI 会自动注册匿名 token，并保存到 `~/.env`：

```bash
deepxiv search "agentic memory" --limit 5
```

如果需要 MCP 或 DeepXiv 内置 research agent，安装完整依赖：

```bash
python -m pip install -U "deepxiv-sdk[all]"
```

本项目脚本需要能读到 `DEEPXIV_TOKEN`。可选方式：

```bash
export DEEPXIV_TOKEN="your_token"
```

或在项目根目录 `./.env` 或用户目录 `~/.env` 中写入：

```text
DEEPXIV_TOKEN=your_token
```

DeepXiv 官方文档说明，匿名自动 token 每天约 1,000 次请求；在 [data.rag.ac.cn/register](https://data.rag.ac.cn/register) 注册的 token 每天约 10,000 次请求。

### LLM Agent 一句话安装

把下面这句话发给 Codex、Claude Code 或其他 LLM Agent：

```text
请帮我安装 DeepXiv SDK；开始前先检查当前 Python/conda 环境，并明确告诉我将安装到哪个环境或目录，等我确认安装位置后，再运行 python -m pip install -U deepxiv-sdk。
```

如果你需要完整 DeepXiv agent/MCP 能力，把最后的安装命令改成：

```text
python -m pip install -U "deepxiv-sdk[all]"
```

自动安装时必须先确认安装位置，避免把包装进错误的 conda 环境或系统 Python。

## 快速开始

### 1. 配置关注列表

`authors.md` 每行一个关注作者，别名用 `|` 分隔：

```markdown
- Kaiming He | K. He
```

`institutions.md` 每行一个关注机构，别名同样用 `|` 分隔：

```markdown
- DeepMind | Google DeepMind
```

机构命中只使用 DeepXiv 返回的结构化 affiliation/org evidence，不会靠常识推断。

### 2. 创建 topic tracker

使用 skill：

```text
Use $dpr-create-topic to create a tracked topic for "Vision-Language-Action Models" with seed paper 2406.09246.
```

或直接运行脚本：

```bash
conda run -n academic python dpr-create-topic/scripts/create_topic.py create \
  --workspace . \
  --topic "Vision-Language-Action Models" \
  --description "关注视觉、语言与机器人动作统一建模的 VLA/WAM 研究。" \
  --include-keyword "vision-language-action" \
  --include-keyword "world action model" \
  --paper 2406.09246
```

脚本会创建 `topics/<slug>.md`，其中必须包含 `include_keywords`。每日推荐的本地 topic 匹配依赖这些关键词，尤其要求至少一个强多词 anchor phrase。

### 3. 推荐某天的候选论文

使用 skill：

```text
Use $dpr-daily-recommendation to collect and recommend papers for 2026-05-05.
```

不指定日期时，skill 会默认处理北京时间昨天对应的 arXiv announcement day。它会先运行本地 collect 脚本，收集 `cs.AI`、`cs.LG`、`cs.CV`、`cs.RO` 的公告论文，并用 DeepXiv brief/head/search/trending 信息增强缓存；然后读取 review report、候选 JSON 和完整 enriched cache，深读值得比较的论文；最后填写 `decisions.json`、应用决策、更新 topic tracker，并把最终推荐改写成中文报告。

候选较多时，skill 会把深读任务拆给 sub-agents 并行处理。sub-agents 只写证据笔记，主 Agent 负责最终取舍、`decisions.json`、topic 更新和报告综合。

skill 的主要输出包括：

```text
reports/YYYY-MM-DD/review.md                 # 初筛候选、作者/机构/topic/趋势命中
reports/YYYY-MM-DD/decisions.template.json   # 决策模板
reports/YYYY-MM-DD/decisions.json            # Agent 选择后的最终决策
reports/YYYY-MM-DD/final.md                  # 中文日报
reports/YYYY-MM-DD/deep-review-*.md          # 可选，sub-agent 或主 Agent 的深读证据
cache/deepxiv/YYYY-MM-DD/papers.enriched.json
cache/deepxiv/YYYY-MM-DD/candidates.json
topics/*.md                                  # 被选中的 topic 更新行
```

如果需要快速检查管线，可以让 Agent 使用 smoke test 参数；如果需要回填历史日期，直接在请求里写明日期。

## 故障排查

- `DEEPXIV_TOKEN is required`：设置 `DEEPXIV_TOKEN` 到环境变量、`./.env` 或 `~/.env`。
- `DeepXiv token is missing or invalid`：重新配置 token，例如 `deepxiv config --token YOUR_TOKEN`。
- `DeepXiv rate limit reached`：等待配额重置，或注册更高配额 token。
- `DeepXiv has not indexed this paper yet`：通常是索引延迟；收集脚本会回退到 arXiv metadata。
- arXiv 或 DeepXiv 网络错误：让脚本直接失败并查看原始错误，不要吞掉异常。

如果 DeepXiv 相关代码出错，先阅读官方文档：

- [DeepXiv README](https://github.com/DeepXiv/deepxiv_sdk/blob/main/README.md)
- [DeepXiv USAGE](https://github.com/DeepXiv/deepxiv_sdk/blob/main/USAGE.md)

## 测试

```bash
conda run -n academic python -m unittest discover -s tests
```

如果缺包或环境不对，先确认当前 conda 环境。这个项目约定使用 `academic` 环境。
