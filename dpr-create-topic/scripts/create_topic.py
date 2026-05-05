#!/usr/bin/env python3
"""Create DPR topic markdown trackers."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


ARXIV_API_URL = "https://export.arxiv.org/api/query"
DEEPXIV_BASE_URL = "https://data.rag.ac.cn"
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
ARXIV_ID_RE = re.compile(r"(?P<id>(?:\d{4}\.\d{4,5})(?:v\d+)?|[a-z-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)")
TABLE_HEADER = "| Date | Paper | Link | One-line Summary |"
TABLE_SEPARATOR = "| --- | --- | --- | --- |"


class TopicError(RuntimeError):
    """Raised for expected topic-creation failures."""


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_arxiv_id(value: str) -> str:
    match = ARXIV_ID_RE.search(value.strip())
    if not match:
        raise ValueError(f"Invalid arXiv ID or URL: {value}")
    return re.sub(r"v\d+$", "", match.group("id"))


def slugify(value: str) -> str:
    text = normalize_space(value).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    if not text:
        raise ValueError(f"Cannot derive slug from topic name: {value}")
    return text


def get_deepxiv_token(workspace: Path) -> str | None:
    if os.environ.get("DEEPXIV_TOKEN"):
        return str(os.environ["DEEPXIV_TOKEN"])
    for env_path in (workspace / ".env", Path.home() / ".env"):
        if not env_path.exists():
            continue
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line.startswith("DEEPXIV_TOKEN="):
                token = line.split("=", 1)[1].strip().strip("\"'")
                if token:
                    return token
    return None


def http_get_text(url: str, timeout: int = 60) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "dpr-create-topic/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        raise TopicError(f"HTTP {error.code} while requesting {url}") from error
    except urllib.error.URLError as error:
        raise TopicError(f"Network error while requesting {url}: {error.reason}") from error


def http_get_json(url: str, params: dict[str, Any], token: str | None, timeout: int = 60) -> dict[str, Any]:
    query = urllib.parse.urlencode({key: value for key, value in params.items() if value is not None})
    request = urllib.request.Request(f"{url}?{query}", headers={"User-Agent": "dpr-create-topic/0.1"})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return {}
        raise TopicError(f"HTTP {error.code} while requesting {url}") from error
    except urllib.error.URLError as error:
        raise TopicError(f"Network error while requesting {url}: {error.reason}") from error
    except json.JSONDecodeError as error:
        raise TopicError(f"Invalid JSON response from {url}") from error


def fetch_arxiv_metadata(arxiv_id: str) -> dict[str, Any]:
    params = {"id_list": arxiv_id, "max_results": 1}
    xml_text = http_get_text(f"{ARXIV_API_URL}?{urllib.parse.urlencode(params)}")
    root = ET.fromstring(xml_text)
    entry = root.find("atom:entry", ATOM_NS)
    if entry is None:
        raise TopicError(f"arXiv paper not found: {arxiv_id}")

    def text(path: str) -> str:
        node = entry.find(path, ATOM_NS)
        return normalize_space(node.text if node is not None and node.text else "")

    return {
        "arxiv_id": arxiv_id,
        "title": text("atom:title"),
        "abstract": text("atom:summary"),
        "published": text("atom:published"),
        "link": f"https://arxiv.org/abs/{arxiv_id}",
    }


def fetch_deepxiv_brief(arxiv_id: str, token: str | None) -> dict[str, Any]:
    if not token:
        return {}
    return http_get_json(
        f"{DEEPXIV_BASE_URL}/arxiv/",
        {"arxiv_id": arxiv_id, "type": "brief"},
        token=token,
    )


def concise_summary(text: str, max_chars: int = 140) -> str:
    value = normalize_space(text)
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 1].rstrip() + "..."


def escape_table_cell(value: str) -> str:
    return normalize_space(value).replace("|", "\\|")


def parse_include_keyword_args(values: list[str]) -> list[str]:
    keywords: list[str] = []
    for raw_value in values:
        for part in raw_value.split(","):
            keyword = normalize_space(part.strip("`\"'"))
            if keyword and keyword not in keywords:
                keywords.append(keyword)
    return keywords


def row_for_paper(paper: dict[str, Any]) -> str:
    published = str(paper.get("published") or paper.get("publish_at") or "")[:10] or str(dt.date.today())
    arxiv_id = str(paper["arxiv_id"])
    summary = concise_summary(str(paper.get("tldr") or paper.get("abstract") or ""))
    return f"| {published} | {escape_table_cell(str(paper['title']))} | https://arxiv.org/abs/{arxiv_id} | {escape_table_cell(summary)} |"


def merge_paper_metadata(arxiv_data: dict[str, Any], brief: dict[str, Any]) -> dict[str, Any]:
    merged = dict(arxiv_data)
    if brief:
        merged.update({key: value for key, value in brief.items() if value})
        merged["arxiv_id"] = arxiv_data["arxiv_id"]
        merged["title"] = brief.get("title") or arxiv_data["title"]
        merged["abstract"] = arxiv_data.get("abstract", "")
    return merged


def create_topic(args: argparse.Namespace) -> None:
    workspace = Path(args.workspace).resolve()
    topics_dir = workspace / "topics"
    topics_dir.mkdir(parents=True, exist_ok=True)
    slug = args.slug or slugify(args.topic)
    topic_path = topics_dir / f"{slug}.md"
    if topic_path.exists() and not args.force:
        raise TopicError(f"Topic already exists: {topic_path}")

    token = get_deepxiv_token(workspace)
    papers: list[dict[str, Any]] = []
    for raw_paper in args.paper:
        arxiv_id = normalize_arxiv_id(raw_paper)
        arxiv_data = fetch_arxiv_metadata(arxiv_id)
        brief = fetch_deepxiv_brief(arxiv_id, token)
        papers.append(merge_paper_metadata(arxiv_data, brief))

    description = normalize_space(args.description or "")
    if not description:
        raise TopicError("Description is required. Let the skill draft a concise Chinese description before calling this script.")
    include_keywords = parse_include_keyword_args(args.include_keyword)
    if not include_keywords:
        raise TopicError("At least one include keyword is required. Let the skill design explicit include_keywords before calling this script.")

    lines = [
        f"# {args.topic}",
        "",
        description,
        "",
        "include_keywords:",
    ]
    lines.extend(f"- {keyword}" for keyword in include_keywords)
    lines.extend([
        "",
        TABLE_HEADER,
        TABLE_SEPARATOR,
    ])
    lines.extend(row_for_paper(paper) for paper in papers)
    topic_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"Topic created: {topic_path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a DPR topic markdown file.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create_parser = subparsers.add_parser("create", help="Create a topic tracker.")
    create_parser.add_argument("--workspace", default=".", help="Workspace root.")
    create_parser.add_argument("--topic", required=True, help="Topic display name.")
    create_parser.add_argument("--slug", default=None, help="Optional topic slug. Defaults to topic-derived slug.")
    create_parser.add_argument("--description", default=None, help="Topic description. Required by the script.")
    create_parser.add_argument("--include-keyword", action="append", default=[], help="Topic include keyword or comma-separated keywords. Repeatable.")
    create_parser.add_argument("--paper", action="append", default=[], help="Optional arXiv ID or URL. Repeatable.")
    create_parser.add_argument("--force", action="store_true", help="Overwrite an existing topic file.")
    create_parser.set_defaults(func=create_topic)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except (TopicError, ValueError, ET.ParseError, json.JSONDecodeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
