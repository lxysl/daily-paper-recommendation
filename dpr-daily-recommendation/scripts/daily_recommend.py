#!/usr/bin/env python3
"""Daily Paper Recommendation collector and applier.

The script intentionally uses arXiv's public announcement pages for daily
enumeration, arXiv's Atom API for metadata, and direct DeepXiv HTTP endpoints
for progressive enrichment. This avoids importing the DeepXiv CLI package,
which can initialize optional agent dependencies during import in some editable
installations.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import os
import re
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from subprocess import PIPE, run
from typing import Any, Iterable


TARGET_CATEGORIES = ("cs.AI", "cs.LG", "cs.CV", "cs.RO")
ARXIV_API_URL = "https://export.arxiv.org/api/query"
ARXIV_LIST_URL = "https://arxiv.org/list/{category}/pastweek"
ARXIV_SOURCE_URL = "https://arxiv.org/e-print/{arxiv_id}"
DEEPXIV_BASE_URL = "https://data.rag.ac.cn"
DEEPXIV_TRENDING_URL = "https://api.rag.ac.cn/trending_arxiv_papers/api/trending"
BEIJING_TZ = dt.timezone(dt.timedelta(hours=8), name="Asia/Shanghai")
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
ARXIV_ID_RE = re.compile(r"(?P<id>(?:\d{4}\.\d{4,5})(?:v\d+)?|[a-z-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)")
ANNOUNCEMENT_DATE_RE = re.compile(r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})")
MONTH_BY_ABBR = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}
TABLE_HEADER = "| Date | Paper | Link | One-line Summary |"
TABLE_SEPARATOR = "| --- | --- | --- | --- |"
LIMITED_APPLICATION_PATTERNS = (
    ("remote sensing", ("remote sensing", "satellite", "hyperspectral", "遥感", "SAR image")),
    ("medicine", ("medical", "clinical", "biomedical", "radiology", "pathology", "histology", "医学", "医疗")),
    ("low-resource language", ("low-resource", "low resource", "under-resourced", "minority language", "小语种")),
)
DIRECT_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg"}
CONVERTIBLE_IMAGE_EXTENSIONS = {".pdf", ".eps", ".ps"}
SOURCE_IMAGE_EXTENSIONS = DIRECT_IMAGE_EXTENSIONS | CONVERTIBLE_IMAGE_EXTENSIONS
SOURCE_ARCHIVE_NAMES = (
    "source.tar.gz",
    "source.tgz",
    "source.tar",
    "source.zip",
    "source.tex",
    "source.bin",
)
SOURCE_MAX_DOWNLOAD_BYTES = 250 * 1024 * 1024
SOURCE_MAX_EXTRACTED_BYTES = 250 * 1024 * 1024
SOURCE_MAX_EXTRACTED_FILES = 5000
MIN_SOURCE_FIGURE_SCORE = 3
MODEL_FIGURE_TERMS = {
    "architecture": 4,
    "framework": 4,
    "pipeline": 4,
    "overview": 3,
    "structure": 3,
    "model": 3,
    "method": 2,
    "network": 2,
    "module": 2,
    "encoder": 2,
    "decoder": 2,
    "transformer": 2,
    "system": 2,
    "workflow": 2,
    "模型": 4,
    "架构": 4,
    "结构": 3,
    "框架": 3,
    "流程": 3,
    "方法": 2,
}
RESULT_FIGURE_TERMS = {
    "qualitative": 4,
    "results": 4,
    "result": 3,
    "comparison": 3,
    "visualization": 3,
    "visualisation": 3,
    "performance": 3,
    "benchmark": 3,
    "ablation": 3,
    "experiment": 2,
    "examples": 2,
    "example": 2,
    "output": 2,
    "prediction": 2,
    "reconstruction": 2,
    "generation": 2,
    "效果": 4,
    "结果": 4,
    "对比": 3,
    "可视化": 3,
    "实验": 2,
}
FIGURE_KIND_LABELS = {
    "model": "模型/结构图",
    "result": "效果/结果图",
}


class DPRError(RuntimeError):
    """Raised for expected DPR workflow failures."""


class DeepXivNotIndexed(DPRError):
    """Raised when DeepXiv has not indexed a paper yet."""


class DeepXivAuthError(DPRError):
    """Raised when DeepXiv authentication fails."""


class DeepXivRateLimitError(DPRError):
    """Raised when DeepXiv reports daily limit exhaustion."""


@dataclass
class Paper:
    arxiv_id: str
    title: str
    abstract: str
    authors: list[str]
    categories: list[str]
    primary_category: str
    published: str
    updated: str
    link: str
    summary: str = ""
    brief: dict[str, Any] | None = None
    head: dict[str, Any] | None = None
    source: str = "arxiv"
    tags: list[str] = field(default_factory=list)

    def display_summary(self) -> str:
        if self.brief:
            tldr = self.brief.get("tldr") or self.brief.get("summary")
            if tldr:
                return normalize_space(str(tldr))
        return normalize_space(self.abstract or self.summary)


@dataclass
class Topic:
    slug: str
    name: str
    description: str
    path: Path
    seed_text: str
    include_keywords: list[str]
    existing_ids: set[str]


@dataclass
class SourceFigureCandidate:
    source_path: Path
    tex_path: Path | None
    graphics_ref: str
    caption: str
    order: int
    model_score: int = 0
    result_score: int = 0


@dataclass
class SelectedSourceFigure:
    kind: str
    candidate: SourceFigureCandidate
    score: int


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_person_or_org(value: str) -> str:
    text = normalize_space(value).lower()
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", text)
    return normalize_space(text)


def normalize_arxiv_id(value: str) -> str:
    match = ARXIV_ID_RE.search(value.strip())
    if not match:
        raise ValueError(f"Invalid arXiv ID or URL: {value}")
    arxiv_id = match.group("id")
    return re.sub(r"v\d+$", "", arxiv_id)


def slugify(value: str) -> str:
    text = normalize_space(value).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    if not text:
        raise ValueError(f"Cannot derive slug from topic name: {value}")
    return text


def parse_alias_markdown(path: Path) -> dict[str, set[str]]:
    if not path.exists():
        return {}
    result: dict[str, set[str]] = {}
    in_fence = False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not line or line.startswith("#") or line.startswith("|"):
            continue
        line = re.sub(r"^[-*+]\s+", "", line)
        line = re.sub(r"^\d+[.)]\s+", "", line)
        if not line:
            continue
        parts = [normalize_space(part) for part in line.split("|") if normalize_space(part)]
        if not parts:
            continue
        canonical = parts[0]
        aliases = {canonical, *parts[1:]}
        result[canonical] = {normalize_person_or_org(alias) for alias in aliases}
    return result


def extract_markdown_links(text: str) -> set[str]:
    return {normalize_arxiv_id(match.group(0)) for match in ARXIV_ID_RE.finditer(text)}


def parse_include_keywords(lines: list[str]) -> list[str]:
    keywords: list[str] = []
    in_keywords = False
    for raw_line in lines:
        line = raw_line.strip()
        if re.fullmatch(r"include_keywords\s*:", line, flags=re.IGNORECASE):
            in_keywords = True
            continue
        if not in_keywords:
            continue
        if not line:
            break
        if line.startswith("|") or line.startswith("#"):
            break
        match = re.match(r"^[-*+]\s+(.+)$", line)
        if not match:
            break
        keyword = normalize_space(match.group(1).strip("`\"'"))
        if keyword and keyword not in keywords:
            keywords.append(keyword)
    return keywords


def parse_topic_file(path: Path) -> Topic:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    name = path.stem
    for line in lines:
        if line.startswith("# "):
            name = normalize_space(line[2:])
            break

    include_keywords = parse_include_keywords(lines)
    description_lines: list[str] = []
    in_table = False
    in_keywords = False
    for raw_line in lines:
        line = raw_line.strip()
        if line.startswith("| Date |"):
            in_table = True
        if re.fullmatch(r"include_keywords\s*:", line, flags=re.IGNORECASE):
            in_keywords = True
            continue
        if in_keywords:
            if not line:
                in_keywords = False
            continue
        if in_table or not line or line.startswith("#"):
            continue
        description_lines.append(line)
    description = normalize_space(" ".join(description_lines))
    seed_text = normalize_space(f"{name} {description} {' '.join(include_keywords)}")
    return Topic(
        slug=path.stem,
        name=name,
        description=description,
        path=path,
        seed_text=seed_text,
        include_keywords=include_keywords,
        existing_ids=extract_markdown_links(text),
    )


def load_topics(workspace: Path) -> list[Topic]:
    topics_dir = workspace / "topics"
    if not topics_dir.exists():
        return []
    return [parse_topic_file(path) for path in sorted(topics_dir.glob("*.md"))]


def beijing_report_date(date_arg: str | None) -> dt.date:
    if date_arg:
        return dt.date.fromisoformat(date_arg)
    return dt.datetime.now(BEIJING_TZ).date() - dt.timedelta(days=1)


def beijing_day_to_utc_range(report_date: dt.date) -> tuple[dt.datetime, dt.datetime]:
    start_bj = dt.datetime.combine(report_date, dt.time.min, tzinfo=BEIJING_TZ)
    end_bj = start_bj + dt.timedelta(days=1)
    return start_bj.astimezone(dt.timezone.utc), end_bj.astimezone(dt.timezone.utc)


def arxiv_submitted_range(report_date: dt.date) -> tuple[str, str]:
    start_utc, end_utc = beijing_day_to_utc_range(report_date)
    return start_utc.strftime("%Y%m%d%H%M"), end_utc.strftime("%Y%m%d%H%M")


def http_get_text(url: str, timeout: int = 60) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "dpr-daily-recommendation/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        raise DPRError(f"HTTP {error.code} while requesting {url}") from error
    except urllib.error.URLError as error:
        raise DPRError(f"Network error while requesting {url}: {error.reason}") from error


def http_get_json(url: str, params: dict[str, Any], token: str | None = None, timeout: int = 60) -> dict[str, Any]:
    query = urllib.parse.urlencode({key: value for key, value in params.items() if value is not None})
    request = urllib.request.Request(f"{url}?{query}", headers={"User-Agent": "dpr-daily-recommendation/0.1"})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 401:
            raise DeepXivAuthError("DeepXiv token is missing or invalid.") from error
        if error.code == 404:
            raise DeepXivNotIndexed("DeepXiv has not indexed this paper yet.") from error
        if error.code == 429:
            detail = error.read().decode("utf-8", errors="replace").strip()
            message = "DeepXiv rate limit reached."
            if detail:
                message = f"{message} Response: {concise_summary(detail, max_chars=500)}"
            raise DeepXivRateLimitError(message) from error
        raise DPRError(f"HTTP {error.code} while requesting {url}") from error
    except urllib.error.URLError as error:
        raise DPRError(f"Network error while requesting {url}: {error.reason}") from error
    except json.JSONDecodeError as error:
        raise DPRError(f"Invalid JSON response from {url}") from error


def download_binary_to_path(url: str, path: Path, timeout: int = 120, max_bytes: int = SOURCE_MAX_DOWNLOAD_BYTES) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "dpr-daily-recommendation/0.1"})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, tmp_path.open("wb") as output:
            total = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise DPRError(f"Downloaded source exceeds {max_bytes} bytes: {url}")
                output.write(chunk)
        tmp_path.replace(path)
    except urllib.error.HTTPError as error:
        raise DPRError(f"HTTP {error.code} while requesting {url}") from error
    except urllib.error.URLError as error:
        raise DPRError(f"Network error while requesting {url}: {error.reason}") from error
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def get_deepxiv_token(workspace: Path) -> str:
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
    raise DPRError("DEEPXIV_TOKEN is required. Set it in the environment, ./.env, or ~/.env.")


class DeepXivClient:
    def __init__(self, token: str, cache_dir: Path, timeout: int = 60) -> None:
        self.token = token
        self.cache_dir = cache_dir
        self.timeout = timeout

    def cached_json(self, name: str, fetcher: Any) -> dict[str, Any]:
        path = self.cache_dir / name
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        data = fetcher()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return data

    def brief(self, arxiv_id: str) -> dict[str, Any]:
        return self.cached_json(
            f"brief/{arxiv_id}.json",
            lambda: http_get_json(
                f"{DEEPXIV_BASE_URL}/arxiv/",
                {"arxiv_id": arxiv_id, "type": "brief"},
                token=self.token,
                timeout=self.timeout,
            ),
        )

    def head(self, arxiv_id: str) -> dict[str, Any]:
        return self.cached_json(
            f"head/{arxiv_id}.json",
            lambda: http_get_json(
                f"{DEEPXIV_BASE_URL}/arxiv/",
                {"arxiv_id": arxiv_id, "type": "head"},
                token=self.token,
                timeout=self.timeout,
            ),
        )

    def search(self, query: str, date_from: str, date_to: str, categories: list[str]) -> list[dict[str, Any]]:
        params = {
            "type": "retrieve",
            "query": query,
            "size": 30,
            "offset": 0,
            "categories": ",".join(categories),
            "date_from": date_from,
            "date_to": date_to,
        }
        cache_name = "-".join(
            part
            for part in (
                slugify(query)[:60],
                date_from,
                date_to,
                slugify("-".join(categories))[:30],
            )
            if part
        )
        data = self.cached_json(
            f"search/{cache_name}.json",
            lambda: http_get_json(f"{DEEPXIV_BASE_URL}/arxiv/", params, token=self.token, timeout=self.timeout),
        )
        return normalize_search_results(data)

    def trending(self, days: int = 7, limit: int = 50) -> list[dict[str, Any]]:
        data = self.cached_json(
            f"trending/{days}-{limit}.json",
            lambda: http_get_json(DEEPXIV_TRENDING_URL, {"days": days, "limit": limit}, timeout=self.timeout),
        )
        if "data" in data and isinstance(data["data"], dict):
            return list(data["data"].get("papers", []))
        return list(data.get("papers", []))


def normalize_search_results(data: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(data.get("result"), list):
        return list(data["result"])
    if isinstance(data.get("results"), list):
        return list(data["results"])
    return []


def parse_arxiv_entry(entry: ET.Element) -> Paper:
    def text(path: str) -> str:
        node = entry.find(path, ATOM_NS)
        return normalize_space(node.text if node is not None and node.text else "")

    raw_id = text("atom:id")
    arxiv_id = normalize_arxiv_id(raw_id)
    authors = [normalize_space(author.findtext("atom:name", default="", namespaces=ATOM_NS)) for author in entry.findall("atom:author", ATOM_NS)]
    categories = [category.attrib.get("term", "") for category in entry.findall("atom:category", ATOM_NS)]
    primary = entry.find("arxiv:primary_category", ATOM_NS)
    primary_category = primary.attrib.get("term", categories[0] if categories else "")
    link = f"https://arxiv.org/abs/{arxiv_id}"
    return Paper(
        arxiv_id=arxiv_id,
        title=text("atom:title"),
        abstract=text("atom:summary"),
        authors=[author for author in authors if author],
        categories=[category for category in categories if category],
        primary_category=primary_category,
        published=text("atom:published"),
        updated=text("atom:updated"),
        link=link,
        summary=text("atom:summary"),
    )


def parse_arxiv_announcement_date(text: str) -> dt.date | None:
    match = ANNOUNCEMENT_DATE_RE.search(normalize_space(text))
    if not match:
        return None
    day, month_name, year = match.groups()
    month = MONTH_BY_ABBR.get(month_name)
    if month is None:
        raise ValueError(f"Unknown arXiv announcement month: {month_name}")
    return dt.date(int(year), month, int(day))


class ArxivAnnouncementParser(HTMLParser):
    def __init__(self, target_date: dt.date) -> None:
        super().__init__(convert_charrefs=True)
        self.target_date = target_date
        self.current_date: dt.date | None = None
        self.ids: list[str] = []
        self.saw_any_date = False
        self._seen_ids: set[str] = set()
        self._in_h3 = False
        self._h3_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "h3":
            self._in_h3 = True
            self._h3_parts = []
            return
        if tag != "a" or self.current_date != self.target_date:
            return
        href = dict(attrs).get("href") or ""
        if not href.startswith("/abs/"):
            return
        arxiv_id = normalize_arxiv_id(href)
        if arxiv_id not in self._seen_ids:
            self._seen_ids.add(arxiv_id)
            self.ids.append(arxiv_id)

    def handle_data(self, data: str) -> None:
        if self._in_h3:
            self._h3_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "h3" or not self._in_h3:
            return
        self._in_h3 = False
        parsed = parse_arxiv_announcement_date("".join(self._h3_parts))
        if parsed is not None:
            self.saw_any_date = True
            self.current_date = parsed


def parse_announced_arxiv_ids(html_text: str, report_date: dt.date) -> list[str]:
    parser = ArxivAnnouncementParser(report_date)
    parser.feed(html_text)
    if not parser.saw_any_date:
        raise DPRError("Could not parse arXiv announcement dates from list page.")
    return parser.ids


def fetch_announced_arxiv_ids(category: str, report_date: dt.date, max_results: int) -> list[str]:
    params = {"show": max_results}
    quoted_category = urllib.parse.quote(category, safe=".")
    url = f"{ARXIV_LIST_URL.format(category=quoted_category)}?{urllib.parse.urlencode(params)}"
    return parse_announced_arxiv_ids(http_get_text(url), report_date)[:max_results]


def fetch_arxiv_id_metadata(arxiv_ids: list[str], sleep_seconds: float) -> list[Paper]:
    papers: list[Paper] = []
    chunk_size = 100
    for start_index in range(0, len(arxiv_ids), chunk_size):
        chunk = arxiv_ids[start_index : start_index + chunk_size]
        params = {"id_list": ",".join(chunk), "max_results": len(chunk)}
        url = f"{ARXIV_API_URL}?{urllib.parse.urlencode(params)}"
        root = ET.fromstring(http_get_text(url))
        by_id = {paper.arxiv_id: paper for paper in (parse_arxiv_entry(entry) for entry in root.findall("atom:entry", ATOM_NS))}
        missing = [arxiv_id for arxiv_id in chunk if arxiv_id not in by_id]
        if missing:
            raise DPRError(f"arXiv API did not return metadata for IDs: {', '.join(missing)}")
        papers.extend(by_id[arxiv_id] for arxiv_id in chunk)
        if sleep_seconds > 0 and start_index + chunk_size < len(arxiv_ids):
            time.sleep(sleep_seconds)
    return papers


def fetch_arxiv_category(category: str, report_date: dt.date, max_results: int, sleep_seconds: float) -> list[Paper]:
    arxiv_ids = fetch_announced_arxiv_ids(category, report_date, max_results)
    return fetch_arxiv_id_metadata(arxiv_ids, sleep_seconds)


def fetch_daily_arxiv(report_date: dt.date, max_per_category: int, sleep_seconds: float) -> list[Paper]:
    by_id: dict[str, Paper] = {}
    for category in TARGET_CATEGORIES:
        for paper in fetch_arxiv_category(category, report_date, max_per_category, sleep_seconds):
            if paper.arxiv_id not in by_id:
                by_id[paper.arxiv_id] = paper
            elif category not in by_id[paper.arxiv_id].tags:
                by_id[paper.arxiv_id].tags.append(category)
    return sorted(by_id.values(), key=lambda paper: paper.published, reverse=True)


def paper_published_date_span(papers: list[Paper], fallback_date: dt.date) -> tuple[str, str]:
    dates = [dt.date.fromisoformat(paper.published[:10]) for paper in papers if paper.published]
    if not dates:
        return str(fallback_date), str(fallback_date)
    return str(min(dates)), str(max(dates + [fallback_date]))


def paper_author_norms(paper: Paper) -> set[str]:
    norms = {normalize_person_or_org(author) for author in paper.authors}
    if paper.brief:
        for author in paper.brief.get("authors", []):
            if isinstance(author, dict):
                norms.add(normalize_person_or_org(str(author.get("name", ""))))
            else:
                norms.add(normalize_person_or_org(str(author)))
    return {norm for norm in norms if norm}


def match_aliases(values: set[str], watched: dict[str, set[str]]) -> list[str]:
    hits: list[str] = []
    for canonical, aliases in watched.items():
        if values & aliases:
            hits.append(canonical)
    return hits


def structured_org_values(paper: Paper) -> set[str]:
    values: set[str] = set()
    for source in (paper.brief, paper.head):
        if not source:
            continue
        for key in ("orgs", "institutions", "affiliations"):
            item = source.get(key)
            if isinstance(item, list):
                values.update(normalize_person_or_org(str(value)) for value in item)
            elif isinstance(item, str):
                values.add(normalize_person_or_org(item))
        for author in source.get("authors", []):
            if isinstance(author, dict):
                for key in ("orgs", "institutions", "affiliations", "affiliation"):
                    item = author.get(key)
                    if isinstance(item, list):
                        values.update(normalize_person_or_org(str(value)) for value in item)
                    elif isinstance(item, str):
                        values.add(normalize_person_or_org(item))
    return {value for value in values if value}


def detect_limited_application(paper: Paper) -> list[str]:
    text = f"{paper.title} {paper.abstract} {paper.display_summary()}".lower()
    labels: list[str] = []
    for label, patterns in LIMITED_APPLICATION_PATTERNS:
        if any(pattern.lower() in text for pattern in patterns):
            labels.append(label)
    return labels


def normalize_keyword_text(value: str) -> str:
    text = normalize_space(value).lower()
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", text)
    return normalize_space(text)


def contains_keyword(text: str, keyword: str) -> bool:
    normalized_text = f" {normalize_keyword_text(text)} "
    normalized_keyword = normalize_keyword_text(keyword)
    if not normalized_keyword:
        return False
    return f" {normalized_keyword} " in normalized_text


def is_strong_topic_anchor(keyword: str) -> bool:
    return len(normalize_keyword_text(keyword).split()) >= 2


def topic_score(paper: Paper, topic: Topic) -> int:
    text = f"{paper.title} {paper.abstract} {paper.display_summary()}".lower()
    matched = [keyword for keyword in topic.include_keywords if contains_keyword(text, keyword)]
    if not any(is_strong_topic_anchor(keyword) for keyword in matched):
        return 0
    return sum(max(1, len(normalize_keyword_text(keyword).split())) for keyword in matched)


def arxiv_id_from_deepxiv_item(item: dict[str, Any]) -> str | None:
    for key in ("arxiv_id", "id"):
        if item.get(key):
            return normalize_arxiv_id(str(item[key]))
    if item.get("url"):
        return normalize_arxiv_id(str(item["url"]))
    return None


def apply_brief_data(papers: list[Paper], client: DeepXivClient, sleep_seconds: float) -> None:
    for paper in papers:
        try:
            paper.brief = client.brief(paper.arxiv_id)
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
        except DeepXivNotIndexed:
            paper.tags.append("deepxiv-not-indexed")


def enrich_heads(papers_by_id: dict[str, Paper], arxiv_ids: Iterable[str], client: DeepXivClient, sleep_seconds: float) -> None:
    for arxiv_id in sorted(set(arxiv_ids)):
        paper = papers_by_id.get(arxiv_id)
        if not paper:
            continue
        try:
            paper.head = client.head(arxiv_id)
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
        except DeepXivNotIndexed:
            paper.tags.append("deepxiv-head-not-indexed")


def build_candidates(
    papers: list[Paper],
    topics: list[Topic],
    watched_authors: dict[str, set[str]],
    watched_orgs: dict[str, set[str]],
    client: DeepXivClient,
    report_date: dt.date,
    sleep_seconds: float,
) -> dict[str, Any]:
    papers_by_id = {paper.arxiv_id: paper for paper in papers}
    search_date_from, search_date_to = paper_published_date_span(papers, report_date)
    author_hits: dict[str, list[str]] = {}
    institution_candidates: dict[str, list[str]] = {}
    institution_search_ids: set[str] = set()
    topic_candidates: dict[str, list[dict[str, Any]]] = {topic.slug: [] for topic in topics}

    for paper in papers:
        matched_authors = match_aliases(paper_author_norms(paper), watched_authors)
        if matched_authors:
            author_hits[paper.arxiv_id] = matched_authors

    # Only fetch heads where it can change a decision.
    enrich_heads(papers_by_id, author_hits.keys(), client, sleep_seconds)

    for canonical in watched_orgs:
        for item in client.search(canonical, search_date_from, search_date_to, list(TARGET_CATEGORIES)):
            arxiv_id = arxiv_id_from_deepxiv_item(item)
            if arxiv_id and arxiv_id in papers_by_id:
                institution_search_ids.add(arxiv_id)
    enrich_heads(papers_by_id, institution_search_ids, client, sleep_seconds)

    for paper in papers:
        matched_orgs = match_aliases(structured_org_values(paper), watched_orgs)
        if matched_orgs:
            institution_candidates[paper.arxiv_id] = matched_orgs

    for topic in topics:
        for paper in papers:
            if paper.arxiv_id in topic.existing_ids:
                continue
            score = topic_score(paper, topic)
            if score >= 2:
                topic_candidates[topic.slug].append({"arxiv_id": paper.arxiv_id, "score": score, "source": "local-keyword"})
        if topic.description or topic.name:
            for item in client.search(topic.name, search_date_from, search_date_to, list(TARGET_CATEGORIES)):
                arxiv_id = arxiv_id_from_deepxiv_item(item)
                if arxiv_id and arxiv_id in papers_by_id and arxiv_id not in topic.existing_ids:
                    topic_candidates[topic.slug].append({"arxiv_id": arxiv_id, "score": item.get("score", 0), "source": "deepxiv-search"})

    trending_items = client.trending(days=7, limit=50)
    trending_ids = [arxiv_id_from_deepxiv_item(item) for item in trending_items]
    trending_ids = [arxiv_id for arxiv_id in trending_ids if arxiv_id]
    yesterday_trending = [arxiv_id for arxiv_id in trending_ids if arxiv_id in papers_by_id]
    spotlight_pool = yesterday_trending[:5]
    fallback_trending = [arxiv_id for arxiv_id in trending_ids if arxiv_id not in spotlight_pool][:5]

    head_ids = set(institution_candidates.keys()) | set(spotlight_pool) | set(fallback_trending)
    for candidates in topic_candidates.values():
        ranked = sorted(candidates, key=lambda item: float(item["score"]), reverse=True)[:12]
        head_ids.update(str(item["arxiv_id"]) for item in ranked)
    enrich_heads(papers_by_id, head_ids, client, sleep_seconds)

    limited = {paper.arxiv_id: detect_limited_application(paper) for paper in papers}
    limited = {arxiv_id: labels for arxiv_id, labels in limited.items() if labels}

    return {
        "author_hits": author_hits,
        "institution_hits": institution_candidates,
        "topic_candidates": topic_candidates,
        "spotlight_pool": spotlight_pool,
        "fallback_trending": fallback_trending,
        "limited_applications": limited,
    }


def paper_to_json(paper: Paper) -> dict[str, Any]:
    return {
        "arxiv_id": paper.arxiv_id,
        "title": paper.title,
        "abstract": paper.abstract,
        "authors": paper.authors,
        "categories": paper.categories,
        "primary_category": paper.primary_category,
        "published": paper.published,
        "updated": paper.updated,
        "link": paper.link,
        "summary": paper.display_summary(),
        "tags": paper.tags,
        "brief": paper.brief,
        "head": paper.head,
    }


def concise_summary(text: str, max_chars: int = 180) -> str:
    value = normalize_space(text)
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 1].rstrip() + "..."


def markdown_paper_line(paper: Paper, reason: str = "") -> str:
    summary = concise_summary(paper.display_summary() or paper.abstract)
    suffix = f" Reason: {reason}" if reason else ""
    return f"- [{paper.title}]({paper.link}) (`{paper.arxiv_id}`): {summary}{suffix}"


def write_review_report(
    report_path: Path,
    report_date: dt.date,
    papers: list[Paper],
    topics: list[Topic],
    candidates: dict[str, Any],
) -> None:
    papers_by_id = {paper.arxiv_id: paper for paper in papers}
    lines: list[str] = [
        f"# DPR Review Candidates - {report_date}",
        "",
        f"Total arXiv candidates: {len(papers)}",
        f"Categories: {', '.join(TARGET_CATEGORIES)}",
        "",
        "## Spotlight Candidates",
        "",
    ]
    for arxiv_id in candidates["spotlight_pool"]:
        lines.append(markdown_paper_line(papers_by_id[arxiv_id], "yesterday trending candidate"))
    for arxiv_id in candidates["fallback_trending"]:
        paper = papers_by_id.get(arxiv_id)
        if paper:
            lines.append(markdown_paper_line(paper, "7-day trending fallback"))
    if not candidates["spotlight_pool"] and not candidates["fallback_trending"]:
        lines.append("- No trending candidates found in cache.")

    lines.extend(["", "## Watched Author Hits", ""])
    for arxiv_id, names in sorted(candidates["author_hits"].items()):
        lines.append(markdown_paper_line(papers_by_id[arxiv_id], f"matched authors: {', '.join(names)}"))
    if not candidates["author_hits"]:
        lines.append("- No watched author hits.")

    lines.extend(["", "## Watched Institution Hits", ""])
    for arxiv_id, names in sorted(candidates["institution_hits"].items()):
        lines.append(markdown_paper_line(papers_by_id[arxiv_id], f"strong institution evidence: {', '.join(names)}"))
    if not candidates["institution_hits"]:
        lines.append("- No strong institution hits.")

    lines.extend(["", "## Topic Candidates For AI Review", ""])
    for topic in topics:
        lines.append(f"### {topic.name} (`{topic.slug}`)")
        ranked = sorted(candidates["topic_candidates"].get(topic.slug, []), key=lambda item: float(item["score"]), reverse=True)[:12]
        if not ranked:
            lines.append("- No topic candidates.")
            lines.append("")
            continue
        seen: set[str] = set()
        for item in ranked:
            arxiv_id = str(item["arxiv_id"])
            if arxiv_id in seen or arxiv_id not in papers_by_id:
                continue
            seen.add(arxiv_id)
            lines.append(markdown_paper_line(papers_by_id[arxiv_id], f"{item['source']} score={item['score']}"))
        lines.append("")

    lines.extend(["## Limited Application Flags", ""])
    for arxiv_id, labels in sorted(candidates["limited_applications"].items()):
        paper = papers_by_id[arxiv_id]
        lines.append(f"- `{arxiv_id}` {paper.title}: {', '.join(labels)}")
    if not candidates["limited_applications"]:
        lines.append("- No limited-application flags.")

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_decision_template(path: Path, candidates: dict[str, Any], topics: list[Topic]) -> None:
    template = {
        "spotlight": candidates["spotlight_pool"][:5],
        "author_hits": candidates["author_hits"],
        "institution_hits": candidates["institution_hits"],
        "topic_updates": {topic.slug: [] for topic in topics},
        "excluded": [],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(template, indent=2, ensure_ascii=False), encoding="utf-8")


def collect(args: argparse.Namespace) -> None:
    workspace = Path(args.workspace).resolve()
    report_date = beijing_report_date(args.date)
    cache_dir = workspace / "cache" / "deepxiv" / str(report_date)
    cache_dir.mkdir(parents=True, exist_ok=True)

    token = get_deepxiv_token(workspace)
    papers_cache = cache_dir / "arxiv-announced.json"
    if papers_cache.exists():
        papers = [Paper(**item) for item in json.loads(papers_cache.read_text(encoding="utf-8"))]
    else:
        papers = fetch_daily_arxiv(report_date, args.max_per_category, args.sleep_seconds)
        papers_cache.write_text(json.dumps([paper_to_json(paper) for paper in papers], indent=2, ensure_ascii=False), encoding="utf-8")

    client = DeepXivClient(token=token, cache_dir=cache_dir)
    apply_brief_data(papers, client, args.deepxiv_sleep_seconds)
    watched_authors = parse_alias_markdown(workspace / "authors.md")
    watched_orgs = parse_alias_markdown(workspace / "institutions.md")
    topics = load_topics(workspace)
    candidates = build_candidates(papers, topics, watched_authors, watched_orgs, client, report_date, args.deepxiv_sleep_seconds)

    papers_json = [paper_to_json(paper) for paper in papers]
    (cache_dir / "papers.enriched.json").write_text(json.dumps(papers_json, indent=2, ensure_ascii=False), encoding="utf-8")
    (cache_dir / "candidates.json").write_text(json.dumps(candidates, indent=2, ensure_ascii=False), encoding="utf-8")

    report_dir = workspace / "reports" / str(report_date)
    review_report = report_dir / "review.md"
    decision_template = report_dir / "decisions.template.json"
    write_review_report(review_report, report_date, papers, topics, candidates)
    write_decision_template(decision_template, candidates, topics)
    print(f"Review report: {review_report}")
    print(f"Decision template: {decision_template}")


def load_enriched_papers(workspace: Path, report_date: str) -> dict[str, dict[str, Any]]:
    path = workspace / "cache" / "deepxiv" / report_date / "papers.enriched.json"
    if not path.exists():
        raise DPRError(f"Missing enriched paper cache: {path}. Run collect first.")
    return {item["arxiv_id"]: item for item in json.loads(path.read_text(encoding="utf-8"))}


def safe_arxiv_id_path(arxiv_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", arxiv_id)


def safe_archive_relative_path(name: str) -> Path | None:
    cleaned = name.replace("\\", "/").strip()
    if not cleaned or cleaned.startswith("/") or re.match(r"^[A-Za-z]:", cleaned):
        return None
    parts: list[str] = []
    for part in cleaned.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            return None
        parts.append(part)
    if not parts:
        return None
    return Path(*parts)


def track_extracted_file(state: dict[str, int], size: int, name: str) -> None:
    if state["files"] + 1 > SOURCE_MAX_EXTRACTED_FILES:
        raise DPRError(f"Source archive has too many files while extracting {name}.")
    if state["bytes"] + max(size, 0) > SOURCE_MAX_EXTRACTED_BYTES:
        raise DPRError(f"Source archive exceeds {SOURCE_MAX_EXTRACTED_BYTES} extracted bytes.")
    state["files"] += 1
    state["bytes"] += max(size, 0)


def copy_fileobj_limited(source: Any, destination: Path, expected_size: int, state: dict[str, int], name: str) -> None:
    track_extracted_file(state, expected_size, name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as output:
        shutil.copyfileobj(source, output, length=1024 * 1024)


def extract_tar_safe(archive_path: Path, extract_dir: Path, warnings: list[str]) -> int:
    state = {"files": 0, "bytes": 0}
    with tarfile.open(archive_path, mode="r:*") as archive:
        for member in archive:
            if member.isdir():
                continue
            if not member.isfile():
                warnings.append(f"Skipped non-file tar member: {member.name}")
                continue
            relative_path = safe_archive_relative_path(member.name)
            if relative_path is None:
                warnings.append(f"Skipped unsafe tar path: {member.name}")
                continue
            member_file = archive.extractfile(member)
            if member_file is None:
                warnings.append(f"Could not read tar member: {member.name}")
                continue
            copy_fileobj_limited(member_file, extract_dir / relative_path, int(member.size or 0), state, member.name)
    return state["files"]


def extract_zip_safe(archive_path: Path, extract_dir: Path, warnings: list[str]) -> int:
    state = {"files": 0, "bytes": 0}
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            relative_path = safe_archive_relative_path(info.filename)
            if relative_path is None:
                warnings.append(f"Skipped unsafe zip path: {info.filename}")
                continue
            with archive.open(info) as member_file:
                copy_fileobj_limited(member_file, extract_dir / relative_path, int(info.file_size or 0), state, info.filename)
    return state["files"]


def extract_gzip_single_file(archive_path: Path, extract_dir: Path) -> int:
    destination = extract_dir / "source.tex"
    destination.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with gzip.open(archive_path, "rb") as source, destination.open("wb") as output:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > SOURCE_MAX_EXTRACTED_BYTES:
                raise DPRError(f"Source gzip exceeds {SOURCE_MAX_EXTRACTED_BYTES} extracted bytes.")
            output.write(chunk)
    return 1


def extract_plain_source_file(archive_path: Path, extract_dir: Path) -> int:
    destination = extract_dir / (archive_path.name if archive_path.suffix.lower() == ".tex" else "source.tex")
    destination.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with archive_path.open("rb") as source, destination.open("wb") as output:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > SOURCE_MAX_EXTRACTED_BYTES:
                raise DPRError(f"Source file exceeds {SOURCE_MAX_EXTRACTED_BYTES} bytes.")
            output.write(chunk)
    return 1


def unpack_source_archive(archive_path: Path, extract_dir: Path, warnings: list[str]) -> int:
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive_path):
        return extract_zip_safe(archive_path, extract_dir, warnings)
    try:
        return extract_tar_safe(archive_path, extract_dir, warnings)
    except tarfile.TarError:
        pass
    with archive_path.open("rb") as source:
        magic = source.read(2)
    if magic == b"\x1f\x8b":
        return extract_gzip_single_file(archive_path, extract_dir)
    return extract_plain_source_file(archive_path, extract_dir)


def find_cached_source_archive(source_cache_dir: Path) -> Path | None:
    for name in SOURCE_ARCHIVE_NAMES:
        candidate = source_cache_dir / name
        if candidate.is_file():
            return candidate
    if not source_cache_dir.exists():
        return None
    for candidate in sorted(source_cache_dir.iterdir()):
        if candidate.is_file() and candidate.name != ".extracted" and not candidate.name.endswith(".tmp"):
            return candidate
    return None


def prepare_arxiv_source_tree(arxiv_id: str, source_cache_root: Path, warnings: list[str]) -> tuple[Path, bool]:
    source_cache_dir = source_cache_root / safe_arxiv_id_path(arxiv_id)
    extract_dir = source_cache_dir / "source"
    marker_path = source_cache_dir / ".extracted"
    if marker_path.exists() and extract_dir.exists():
        return extract_dir, False

    archive_path = find_cached_source_archive(source_cache_dir)
    downloaded = False
    if archive_path is None:
        archive_path = source_cache_dir / "source.bin"
        download_binary_to_path(ARXIV_SOURCE_URL.format(arxiv_id=arxiv_id), archive_path)
        downloaded = True

    extracted_count = unpack_source_archive(archive_path, extract_dir, warnings)
    if extracted_count == 0:
        warnings.append("Source archive did not contain extractable files.")
    marker_path.write_text(json.dumps({"archive": archive_path.name, "files": extracted_count}, ensure_ascii=False), encoding="utf-8")
    return extract_dir, downloaded


def read_text_lossy(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def strip_tex_comments(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        output: list[str] = []
        escaped = False
        for char in line:
            if char == "%" and not escaped:
                break
            output.append(char)
            if char == "\\" and not escaped:
                escaped = True
            else:
                escaped = False
        lines.append("".join(output))
    return "\n".join(lines)


def read_balanced_group(text: str, start_index: int, open_char: str, close_char: str) -> tuple[str, int] | None:
    if start_index >= len(text) or text[start_index] != open_char:
        return None
    depth = 0
    escaped = False
    for index in range(start_index, len(text)):
        char = text[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return text[start_index + 1 : index], index + 1
    return None


def skip_tex_space(text: str, index: int) -> int:
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def extract_tex_command_arguments(text: str, command: str) -> list[str]:
    arguments: list[str] = []
    pattern = re.compile(rf"\\{re.escape(command)}\b")
    for match in pattern.finditer(text):
        index = skip_tex_space(text, match.end())
        if index < len(text) and text[index] == "[":
            optional = read_balanced_group(text, index, "[", "]")
            if optional is not None:
                index = skip_tex_space(text, optional[1])
        if index < len(text) and text[index] == "{":
            argument = read_balanced_group(text, index, "{", "}")
            if argument is not None:
                arguments.append(argument[0])
    return arguments


def normalize_tex_caption(text: str) -> str:
    value = text.replace("~", " ")
    value = re.sub(r"\\(?:cite|ref|label|url|href|vspace|hspace)\*?(?:\s*\[[^\]]*\])?\s*\{[^{}]*\}", " ", value)
    value = re.sub(r"\\[A-Za-z]+\*?(?:\s*\[[^\]]*\])?", " ", value)
    value = value.replace("{", " ").replace("}", " ")
    return normalize_space(value)


def decode_process_output_lossy(output: bytes | str | None) -> str:
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    return output.decode("utf-8", errors="replace")


def source_image_files(source_root: Path) -> list[Path]:
    return sorted(
        path
        for path in source_root.rglob("*")
        if path.is_file() and path.suffix.lower() in SOURCE_IMAGE_EXTENSIONS
    )


def graphics_lookup_keys(source_root: Path, image_path: Path) -> set[str]:
    relative = image_path.relative_to(source_root).as_posix().lower()
    keys = {relative, relative.removeprefix("./")}
    without_suffix = str(Path(relative).with_suffix(""))
    keys.add(without_suffix)
    keys.add(Path(relative).name)
    keys.add(Path(without_suffix).name)
    return keys


def resolve_graphics_reference(graphics_ref: str, source_root: Path, images: list[Path]) -> Path | None:
    cleaned = normalize_space(graphics_ref).strip("'\"")
    if not cleaned or any(char in cleaned for char in "\\{}$"):
        return None
    cleaned = cleaned.replace("\\", "/").removeprefix("./")
    lookup: dict[str, Path] = {}
    for image_path in images:
        for key in graphics_lookup_keys(source_root, image_path):
            lookup.setdefault(key, image_path)
    lowered = cleaned.lower()
    if lowered in lookup:
        return lookup[lowered]
    if Path(lowered).suffix:
        return lookup.get(Path(lowered).name)
    for extension in (".png", ".jpg", ".jpeg", ".gif", ".svg", ".pdf", ".eps", ".ps"):
        candidate = f"{lowered}{extension}"
        if candidate in lookup:
            return lookup[candidate]
        candidate_name = Path(candidate).name
        if candidate_name in lookup:
            return lookup[candidate_name]
    return None


def figure_blocks(tex_text: str) -> list[str]:
    pattern = re.compile(r"\\begin\{figure\*?\}(.*?)\\end\{figure\*?\}", flags=re.IGNORECASE | re.DOTALL)
    return [match.group(1) for match in pattern.finditer(tex_text)]


def score_figure_text(caption: str, path: Path, terms: dict[str, int]) -> int:
    text = normalize_keyword_text(f"{caption} {path.stem} {path.parent.name}")
    score = 0
    for term, weight in terms.items():
        if normalize_keyword_text(term) in text:
            score += weight
    return score


def discover_source_figure_candidates(source_root: Path, warnings: list[str]) -> list[SourceFigureCandidate]:
    images = source_image_files(source_root)
    if not images:
        warnings.append("No supported image files found in source.")
        return []

    tex_paths = sorted(source_root.rglob("*.tex"))
    if not tex_paths:
        warnings.append("No .tex files found in source.")
        return []

    candidates: list[SourceFigureCandidate] = []
    seen: set[tuple[Path, str]] = set()
    for tex_path in tex_paths:
        tex_text = strip_tex_comments(read_text_lossy(tex_path))
        blocks = figure_blocks(tex_text)
        if not blocks:
            blocks = [tex_text]
        for block in blocks:
            captions = extract_tex_command_arguments(block, "caption")
            caption = normalize_tex_caption(captions[0]) if captions else ""
            for graphics_ref in extract_tex_command_arguments(block, "includegraphics"):
                image_path = resolve_graphics_reference(graphics_ref, source_root, images)
                if image_path is None:
                    warnings.append(f"Could not resolve includegraphics reference: {graphics_ref}")
                    continue
                key = (image_path, caption)
                if key in seen:
                    continue
                seen.add(key)
                candidate = SourceFigureCandidate(
                    source_path=image_path,
                    tex_path=tex_path,
                    graphics_ref=graphics_ref,
                    caption=caption,
                    order=len(candidates),
                )
                candidate.model_score = score_figure_text(candidate.caption, candidate.source_path, MODEL_FIGURE_TERMS)
                candidate.result_score = score_figure_text(candidate.caption, candidate.source_path, RESULT_FIGURE_TERMS)
                candidates.append(candidate)
    if not candidates:
        warnings.append("No includegraphics references resolved to supported image files.")
    return candidates


def best_source_figure(
    candidates: list[SourceFigureCandidate],
    kind: str,
    excluded_paths: set[Path] | None = None,
) -> SelectedSourceFigure | None:
    excluded_paths = excluded_paths or set()
    score_attr = "model_score" if kind == "model" else "result_score"
    eligible = [candidate for candidate in candidates if candidate.source_path not in excluded_paths and getattr(candidate, score_attr) >= MIN_SOURCE_FIGURE_SCORE]
    if not eligible:
        return None
    candidate = max(eligible, key=lambda item: (getattr(item, score_attr), bool(item.caption), -item.order))
    return SelectedSourceFigure(kind=kind, candidate=candidate, score=getattr(candidate, score_attr))


def select_source_figures(candidates: list[SourceFigureCandidate], max_figures: int) -> list[SelectedSourceFigure]:
    if max_figures <= 0:
        return []
    model = best_source_figure(candidates, "model")
    result = best_source_figure(candidates, "result")
    if max_figures == 1:
        options = [item for item in (model, result) if item is not None]
        if not options:
            return []
        return [max(options, key=lambda item: (item.score, bool(item.candidate.caption), -item.candidate.order))]

    selected: list[SelectedSourceFigure] = []
    selected_paths: set[Path] = set()
    if model is not None:
        selected.append(model)
        selected_paths.add(model.candidate.source_path)
    result = best_source_figure(candidates, "result", selected_paths)
    if result is not None and len(selected) < max_figures:
        selected.append(result)
    if not selected:
        fallback = max(
            candidates,
            key=lambda item: (max(item.model_score, item.result_score), bool(item.caption), -item.order),
            default=None,
        )
        if fallback is not None and max(fallback.model_score, fallback.result_score) >= MIN_SOURCE_FIGURE_SCORE:
            kind = "model" if fallback.model_score >= fallback.result_score else "result"
            selected.append(SelectedSourceFigure(kind=kind, candidate=fallback, score=max(fallback.model_score, fallback.result_score)))
    return selected[:max_figures]


def convert_source_figure_with_gs(source_path: Path, output_path: Path, warnings: list[str]) -> bool:
    gs_path = shutil.which("gs")
    if not gs_path:
        warnings.append(f"Cannot convert {source_path.name}: gs is not available.")
        return False
    command = [
        gs_path,
        "-dSAFER",
        "-dBATCH",
        "-dNOPAUSE",
        "-dFirstPage=1",
        "-dLastPage=1",
        "-sDEVICE=pngalpha",
        "-r160",
        "-dEPSCrop",
        f"-sOutputFile={output_path}",
        str(source_path),
    ]
    completed = run(command, stdout=PIPE, stderr=PIPE, timeout=90)
    if completed.returncode != 0 or not output_path.exists():
        detail = normalize_space(
            "\n".join(
                part
                for part in (
                    decode_process_output_lossy(completed.stderr),
                    decode_process_output_lossy(completed.stdout),
                )
                if part
            )
        )
        warnings.append(f"Failed to convert {source_path.name} with gs: {concise_summary(detail, max_chars=300)}")
        return False
    return True


def export_selected_source_figures(
    arxiv_id: str,
    selections: list[SelectedSourceFigure],
    report_dir: Path,
    warnings: list[str],
) -> list[dict[str, Any]]:
    asset_dir = report_dir / "assets" / safe_arxiv_id_path(arxiv_id)
    exported: list[dict[str, Any]] = []
    used_names: set[str] = set()
    for selection in selections:
        source_path = selection.candidate.source_path
        source_extension = source_path.suffix.lower()
        base_name = "model-structure" if selection.kind == "model" else "effect-result"
        output_extension = source_extension if source_extension in DIRECT_IMAGE_EXTENSIONS else ".png"
        output_name = f"{base_name}{output_extension}"
        suffix_index = 2
        while output_name in used_names:
            output_name = f"{base_name}-{suffix_index}{output_extension}"
            suffix_index += 1
        used_names.add(output_name)
        output_path = asset_dir / output_name
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if source_extension in DIRECT_IMAGE_EXTENSIONS:
            shutil.copy2(source_path, output_path)
        elif source_extension in CONVERTIBLE_IMAGE_EXTENSIONS:
            if not convert_source_figure_with_gs(source_path, output_path, warnings):
                continue
        else:
            warnings.append(f"Unsupported selected image extension: {source_path.name}")
            continue
        exported.append(
            {
                "kind": selection.kind,
                "label": FIGURE_KIND_LABELS[selection.kind],
                "caption": selection.candidate.caption,
                "score": selection.score,
                "source_path": str(source_path),
                "tex_path": str(selection.candidate.tex_path) if selection.candidate.tex_path else None,
                "graphics_ref": selection.candidate.graphics_ref,
                "output_path": str(output_path),
                "markdown_path": output_path.relative_to(report_dir).as_posix(),
            }
        )
    return exported


def collect_source_figures(
    arxiv_ids: list[str],
    workspace: Path,
    report_date: str,
    max_figures_per_paper: int,
    sleep_seconds: float,
) -> dict[str, Any]:
    report_dir = workspace / "reports" / report_date
    source_cache_root = workspace / "cache" / "arxiv-source" / report_date
    result: dict[str, Any] = {
        "report_date": report_date,
        "source_cache": str(source_cache_root),
        "max_figures_per_paper": max_figures_per_paper,
        "papers": {},
    }
    for index, arxiv_id in enumerate(arxiv_ids):
        paper_result: dict[str, Any] = {"status": "pending", "figures": [], "warnings": []}
        warnings: list[str] = paper_result["warnings"]
        downloaded = False
        try:
            source_root, downloaded = prepare_arxiv_source_tree(arxiv_id, source_cache_root, warnings)
            candidates = discover_source_figure_candidates(source_root, warnings)
            selections = select_source_figures(candidates, max_figures_per_paper)
            if not selections:
                warnings.append("No high-confidence model/result figures selected.")
            exported = export_selected_source_figures(arxiv_id, selections, report_dir, warnings)
            paper_result["figures"] = exported
            paper_result["status"] = "ok" if exported else "no_figures"
        except Exception as error:  # Source figures are best-effort and must not block the DPR report.
            warnings.append(str(error))
            paper_result["status"] = "error"
        for warning in warnings:
            print(f"Warning: source figures {arxiv_id}: {warning}")
        result["papers"][arxiv_id] = paper_result
        if downloaded and sleep_seconds > 0 and index + 1 < len(arxiv_ids):
            time.sleep(sleep_seconds)
    return result


def recommended_arxiv_ids_from_decisions(decisions: dict[str, Any]) -> list[str]:
    excluded = {str(arxiv_id) for arxiv_id in decisions.get("excluded", [])}
    ordered: list[str] = []
    seen: set[str] = set()

    def add(arxiv_id: str) -> None:
        value = str(arxiv_id)
        if value in excluded or value in seen:
            return
        seen.add(value)
        ordered.append(value)

    for arxiv_id in decisions.get("spotlight", []):
        add(arxiv_id)
    for arxiv_id in decisions.get("author_hits", {}).keys():
        add(arxiv_id)
    for arxiv_id in decisions.get("institution_hits", {}).keys():
        add(arxiv_id)
    for arxiv_ids in decisions.get("topic_updates", {}).values():
        for arxiv_id in arxiv_ids:
            add(arxiv_id)
    return ordered


def validate_decisions(decisions: dict[str, Any], papers_by_id: dict[str, dict[str, Any]], topics: list[Topic]) -> None:
    required = {"spotlight", "author_hits", "institution_hits", "topic_updates", "excluded"}
    missing = required - set(decisions)
    if missing:
        raise DPRError(f"Decision file is missing required keys: {', '.join(sorted(missing))}")
    topic_slugs = {topic.slug for topic in topics}
    for key in ("spotlight", "excluded"):
        if not isinstance(decisions[key], list):
            raise DPRError(f"Decision key '{key}' must be a list.")
        for arxiv_id in decisions[key]:
            if arxiv_id not in papers_by_id:
                raise DPRError(f"Unknown arXiv ID in '{key}': {arxiv_id}")
    for key in ("author_hits", "institution_hits"):
        if not isinstance(decisions[key], dict):
            raise DPRError(f"Decision key '{key}' must be an object.")
        for arxiv_id in decisions[key]:
            if arxiv_id not in papers_by_id:
                raise DPRError(f"Unknown arXiv ID in '{key}': {arxiv_id}")
    if not isinstance(decisions["topic_updates"], dict):
        raise DPRError("Decision key 'topic_updates' must be an object.")
    for slug, arxiv_ids in decisions["topic_updates"].items():
        if slug not in topic_slugs:
            raise DPRError(f"Unknown topic slug in topic_updates: {slug}")
        if not isinstance(arxiv_ids, list):
            raise DPRError(f"topic_updates.{slug} must be a list.")
        for arxiv_id in arxiv_ids:
            if arxiv_id not in papers_by_id:
                raise DPRError(f"Unknown arXiv ID in topic_updates.{slug}: {arxiv_id}")


def escape_table_cell(value: str) -> str:
    return normalize_space(value).replace("|", "\\|")


def ensure_topic_table(text: str) -> str:
    if TABLE_HEADER in text:
        return text
    if text and not text.endswith("\n"):
        text += "\n"
    return text + f"\n{TABLE_HEADER}\n{TABLE_SEPARATOR}\n"


def append_topic_rows(topic: Topic, arxiv_ids: list[str], papers_by_id: dict[str, dict[str, Any]], report_date: str) -> int:
    text = ensure_topic_table(topic.path.read_text(encoding="utf-8"))
    existing = extract_markdown_links(text)
    rows: list[str] = []
    for arxiv_id in arxiv_ids:
        if arxiv_id in existing:
            continue
        paper = papers_by_id[arxiv_id]
        summary = concise_summary(str(paper.get("summary") or paper.get("abstract") or ""), max_chars=140)
        rows.append(
            f"| {report_date} | {escape_table_cell(str(paper['title']))} | https://arxiv.org/abs/{arxiv_id} | {escape_table_cell(summary)} |"
        )
    if rows:
        if not text.endswith("\n"):
            text += "\n"
        text += "\n".join(rows) + "\n"
        topic.path.write_text(text, encoding="utf-8")
    return len(rows)


def append_source_figure_lines(lines: list[str], arxiv_id: str, source_figures: dict[str, Any] | None) -> None:
    if not source_figures:
        return
    paper_source_figures = source_figures.get("papers", {}).get(arxiv_id, {})
    status = str(paper_source_figures.get("status") or "")
    warnings = [
        normalize_space(str(warning))
        for warning in paper_source_figures.get("warnings", [])
        if normalize_space(str(warning))
    ]
    if status == "error":
        detail = concise_summary("; ".join(warnings), max_chars=180) if warnings else "source figure extraction failed"
        lines.append(f"  <small>图片提取失败：{detail}</small>")
    elif status == "no_figures":
        detail = concise_summary("; ".join(warnings), max_chars=180) if warnings else "未选出高置信模型/结果图"
        lines.append(f"  <small>未展示论文图：{detail}</small>")
    paper_figures = paper_source_figures.get("figures", [])
    for figure in paper_figures:
        label = str(figure.get("label") or "论文图")
        caption = normalize_space(str(figure.get("caption") or ""))
        markdown_path = str(figure.get("markdown_path") or "")
        if not markdown_path:
            continue
        if caption:
            lines.append(f"  - {label}：{caption}")
        else:
            lines.append(f"  - {label}")
        lines.append(f"    ![{label}]({markdown_path})")


def section_lines(
    title: str,
    arxiv_ids: Iterable[str],
    papers_by_id: dict[str, dict[str, Any]],
    annotations: dict[str, Any] | None = None,
    source_figures: dict[str, Any] | None = None,
) -> list[str]:
    lines = [f"## {title}", ""]
    count = 0
    for arxiv_id in arxiv_ids:
        paper = papers_by_id[arxiv_id]
        note = ""
        if annotations and arxiv_id in annotations:
            value = annotations[arxiv_id]
            if isinstance(value, list):
                note = f" ({', '.join(str(item) for item in value)})"
            else:
                note = f" ({value})"
        lines.append(f"- [{paper['title']}](https://arxiv.org/abs/{arxiv_id}) `{arxiv_id}`{note}: {concise_summary(str(paper.get('summary') or paper.get('abstract') or ''))}")
        append_source_figure_lines(lines, arxiv_id, source_figures)
        count += 1
    if count == 0:
        lines.append("- None.")
    lines.append("")
    return lines


def write_final_report(
    path: Path,
    report_date: str,
    decisions: dict[str, Any],
    papers_by_id: dict[str, dict[str, Any]],
    topics: list[Topic],
    appended_counts: dict[str, int],
    spotlight_notes: dict[str, str] | None = None,
    source_figures: dict[str, Any] | None = None,
) -> None:
    lines: list[str] = [f"# DPR Daily Paper Recommendation - {report_date}", ""]
    lines.extend(section_lines("Spotlight", decisions["spotlight"], papers_by_id, spotlight_notes, source_figures))
    lines.extend(section_lines("Watched Authors", decisions["author_hits"].keys(), papers_by_id, decisions["author_hits"], source_figures))
    lines.extend(section_lines("Watched Institutions", decisions["institution_hits"].keys(), papers_by_id, decisions["institution_hits"], source_figures))
    lines.append("## Topic Updates")
    lines.append("")
    topic_by_slug = {topic.slug: topic for topic in topics}
    for slug, arxiv_ids in decisions["topic_updates"].items():
        topic = topic_by_slug[slug]
        lines.append(f"### {topic.name}")
        if not arxiv_ids:
            lines.append("- None.")
            lines.append("")
            continue
        for arxiv_id in arxiv_ids:
            paper = papers_by_id[arxiv_id]
            lines.append(f"- [{paper['title']}](https://arxiv.org/abs/{arxiv_id}) `{arxiv_id}`: {concise_summary(str(paper.get('summary') or paper.get('abstract') or ''))}")
            append_source_figure_lines(lines, arxiv_id, source_figures)
        lines.append(f"- Appended rows: {appended_counts.get(slug, 0)}")
        lines.append("")
    lines.extend(section_lines("Excluded", decisions["excluded"], papers_by_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def apply_decisions(args: argparse.Namespace) -> None:
    workspace = Path(args.workspace).resolve()
    report_date = str(beijing_report_date(args.date))
    papers_by_id = load_enriched_papers(workspace, report_date)
    topics = load_topics(workspace)
    decisions_path = Path(args.decisions)
    if not decisions_path.is_absolute():
        decisions_path = workspace / decisions_path
    decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
    validate_decisions(decisions, papers_by_id, topics)
    candidates_path = workspace / "cache" / "deepxiv" / report_date / "candidates.json"
    spotlight_notes: dict[str, str] = {}
    if candidates_path.exists():
        candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
        for arxiv_id in candidates.get("spotlight_pool", []):
            spotlight_notes[str(arxiv_id)] = "昨日高热度"
        for arxiv_id in candidates.get("fallback_trending", []):
            spotlight_notes[str(arxiv_id)] = "近 7 天热度补齐"
    topic_by_slug = {topic.slug: topic for topic in topics}
    appended_counts = {
        slug: append_topic_rows(topic_by_slug[slug], arxiv_ids, papers_by_id, report_date)
        for slug, arxiv_ids in decisions["topic_updates"].items()
    }
    report_dir = workspace / "reports" / report_date
    source_figures: dict[str, Any] | None = None
    if not args.skip_source_figures:
        recommended_ids = recommended_arxiv_ids_from_decisions(decisions)
        source_figures = collect_source_figures(
            recommended_ids,
            workspace,
            report_date,
            args.max_source_figures_per_paper,
            args.source_sleep_seconds,
        )
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "source-figures.json").write_text(json.dumps(source_figures, indent=2, ensure_ascii=False), encoding="utf-8")
    final_report = workspace / "reports" / report_date / "final.md"
    write_final_report(final_report, report_date, decisions, papers_by_id, topics, appended_counts, spotlight_notes, source_figures)
    print(f"Final report: {final_report}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect and apply DPR daily recommendations.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect_parser = subparsers.add_parser("collect", help="Collect yesterday's papers and write review artifacts.")
    collect_parser.add_argument("--workspace", default=".", help="Workspace root containing authors.md, institutions.md, and topics/.")
    collect_parser.add_argument("--date", default=None, help="Beijing report date, YYYY-MM-DD. Defaults to yesterday.")
    collect_parser.add_argument("--max-per-category", type=int, default=1000, help="Maximum arXiv papers per target category.")
    collect_parser.add_argument("--sleep-seconds", type=float, default=3.0, help="Delay between arXiv pages.")
    collect_parser.add_argument("--deepxiv-sleep-seconds", type=float, default=0.5, help="Delay between DeepXiv requests.")
    collect_parser.set_defaults(func=collect)

    apply_parser = subparsers.add_parser("apply", help="Apply AI-reviewed decisions and write final report/topic updates.")
    apply_parser.add_argument("--workspace", default=".", help="Workspace root.")
    apply_parser.add_argument("--date", default=None, help="Beijing report date, YYYY-MM-DD. Defaults to yesterday.")
    apply_parser.add_argument("--decisions", required=True, help="Path to decisions JSON.")
    apply_parser.add_argument("--skip-source-figures", action="store_true", help="Do not download arXiv sources or add source figures to the final report.")
    apply_parser.add_argument("--max-source-figures-per-paper", type=int, default=2, help="Maximum arXiv source figures to show per recommended paper.")
    apply_parser.add_argument("--source-sleep-seconds", type=float, default=1.0, help="Delay between arXiv source downloads.")
    apply_parser.set_defaults(func=apply_decisions)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except (DPRError, ValueError, ET.ParseError, json.JSONDecodeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
