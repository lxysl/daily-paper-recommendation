# Installation Guide for Agents

## Requirements

- Python 3.10+

## Install

- Clone this repo to any temporary location. Copy the `dpr-create-topic/` and `dpr-daily-recommendation/` folders inside into `~/.claude/skills/` or `~/.codex/skills/` based on which tool you are in (Claude Code or Codex).
- Install DeepXiv SDK. Before starting, check the current Python/conda environment and ask user for which environment or directory to install into. After confirmed the installation location, run python -m pip install -U deepxiv-sdk.

## Verify

After install or update, ask the user to restart Claude Code or Codex.

## Uninstall

Delete `dpr-create-topic/` and `dpr-daily-recommendation/` from `~/.claude/skills/` or `~/.codex/skills/`. Ask the user to restart Claude Code or Codex.