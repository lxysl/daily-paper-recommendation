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
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable


TARGET_CATEGORIES = ("cs.AI", "cs.LG", "cs.CV", "cs.RO")
ARXIV_API_URL = "https://export.arxiv.org/api/query"
ARXIV_LIST_URL = "https://arxiv.org/list/{category}/pastweek"
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


def section_lines(title: str, arxiv_ids: Iterable[str], papers_by_id: dict[str, dict[str, Any]], annotations: dict[str, Any] | None = None) -> list[str]:
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
) -> None:
    lines: list[str] = [f"# DPR Daily Paper Recommendation - {report_date}", ""]
    lines.extend(section_lines("Spotlight", decisions["spotlight"], papers_by_id, spotlight_notes))
    lines.extend(section_lines("Watched Authors", decisions["author_hits"].keys(), papers_by_id, decisions["author_hits"]))
    lines.extend(section_lines("Watched Institutions", decisions["institution_hits"].keys(), papers_by_id, decisions["institution_hits"]))
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
    final_report = workspace / "reports" / report_date / "final.md"
    write_final_report(final_report, report_date, decisions, papers_by_id, topics, appended_counts, spotlight_notes)
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
