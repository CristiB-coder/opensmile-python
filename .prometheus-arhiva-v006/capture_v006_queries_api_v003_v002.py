#!/usr/bin/env python3
"""Official structured API capture for ARHIVA V006 V003.

No article pages or full-text endpoints are used. Raw API bytes are preserved
before parsing. Semantic screening and DLREG execution are out of scope.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

QUERIES = [
    '"dismissed as noise"',
    '"dismissed as an artifact"',
    '"dismissed as artifact"',
    '"regarded as contamination"',
    '"considered contamination"',
    '"treated as noise"',
    '"viewed as noise"',
    '"thought to be an artifact"',
    '"thought to be contamination"',
    '"written off as noise"',
    '"ignored as noise"',
    '"considered an outlier"',
]
USER_AGENT = "PROMETHEUS-Arhiva-V006/1.0 structured-metadata-capture"
TIMEOUT = 45
ATOM = {
    "a": "http://www.w3.org/2005/Atom",
    "o": "http://a9.com/-/spec/opensearch/1.1/",
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def collapse(text: str | None) -> str:
    return " ".join((text or "").split())


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_europe_pmc_url(query: str, page_size: int = 100) -> str:
    params = {
        "query": f"{query} AND OPEN_ACCESS:Y",
        "resultType": "core",
        "format": "json",
        "pageSize": str(page_size),
        "synonym": "false",
        "cursorMark": "*",
    }
    return (
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search?"
        + urllib.parse.urlencode(params)
    )


def build_arxiv_url(query: str, max_results: int = 100) -> str:
    params = {
        "search_query": f"all:{query}",
        "start": "0",
        "max_results": str(max_results),
        "sortBy": "relevance",
        "sortOrder": "descending",
    }
    return "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)


def exact_date(value: Any) -> tuple[str | None, str]:
    if not isinstance(value, str) or not value.strip():
        return None, "MISSING"
    text = value.strip()
    if (
        len(text) >= 10
        and text[4] == "-"
        and text[7] == "-"
        and text[:4].isdigit()
        and text[5:7].isdigit()
        and text[8:10].isdigit()
    ):
        return text[:10], "EXACT_DAY"
    return None, "NOT_EXACT_DAY"


def parse_europe_pmc(
    data: bytes, query_index: int, query: str, raw_sha: str
) -> list[dict[str, Any]]:
    value = json.loads(data.decode("utf-8"))
    results = ((value.get("resultList") or {}).get("result") or [])
    output: list[dict[str, Any]] = []
    for rank, row in enumerate(results, start=1):
        pmcid = row.get("pmcid")
        ext_id = row.get("id") or row.get("extId")
        source = row.get("source") or row.get("src")
        canonical = (
            f"PMC{str(pmcid).removeprefix('PMC')}"
            if pmcid
            else f"{source}:{ext_id}"
        )
        date_value = (
            row.get("firstPublicationDate")
            or row.get("electronicPublicationDate")
            or ((row.get("journalInfo") or {}).get("printPublicationDate"))
        )
        publication_date, publication_status = exact_date(date_value)
        abstract = collapse(row.get("abstractText"))
        output.append(
            {
                "capture_id": f"V003-Q{query_index:02d}-S01-R{rank:03d}",
                "query_index": query_index,
                "query_literal": query,
                "source_index": 1,
                "source": "EUROPE_PMC_OPEN_ACCESS",
                "result_rank": rank,
                "canonical_article_id": canonical,
                "title": collapse(row.get("title")),
                "screening_text": abstract or None,
                "screening_text_source": (
                    "ABSTRACT_TEXT" if abstract else "MISSING"
                ),
                "publication_date_exact": publication_date,
                "publication_date_status": publication_status,
                "publication_metadata_raw": date_value,
                "authors": (
                    collapse(row.get("authorString")).split(", ")
                    if row.get("authorString")
                    else []
                ),
                "canonical_url": (
                    f"https://europepmc.org/article/{source}/{ext_id}"
                    if source and ext_id
                    else None
                ),
                "doi": row.get("doi"),
                "pmcid": pmcid,
                "arxiv_id": None,
                "raw_response_sha256": raw_sha,
            }
        )
    return output


def parse_arxiv(
    data: bytes, query_index: int, query: str, raw_sha: str
) -> list[dict[str, Any]]:
    root = ET.fromstring(data)
    output: list[dict[str, Any]] = []
    for rank, entry in enumerate(root.findall("a:entry", ATOM), start=1):
        identifier = collapse(
            entry.findtext("a:id", default="", namespaces=ATOM)
        )
        arxiv_id = (
            identifier.rsplit("/abs/", 1)[-1]
            if "/abs/" in identifier
            else identifier
        )
        published = collapse(
            entry.findtext("a:published", default="", namespaces=ATOM)
        )
        publication_date, publication_status = exact_date(published)
        authors = [
            collapse(node.text)
            for node in entry.findall("a:author/a:name", ATOM)
        ]
        summary = collapse(
            entry.findtext("a:summary", default="", namespaces=ATOM)
        )
        output.append(
            {
                "capture_id": f"V003-Q{query_index:02d}-S02-R{rank:03d}",
                "query_index": query_index,
                "query_literal": query,
                "source_index": 2,
                "source": "ARXIV",
                "result_rank": rank,
                "canonical_article_id": f"ARXIV:{arxiv_id}",
                "title": collapse(
                    entry.findtext("a:title", default="", namespaces=ATOM)
                ),
                "screening_text": summary or None,
                "screening_text_source": (
                    "ATOM_SUMMARY" if summary else "MISSING"
                ),
                "publication_date_exact": publication_date,
                "publication_date_status": publication_status,
                "publication_metadata_raw": published or None,
                "authors": authors,
                "canonical_url": identifier or None,
                "doi": None,
                "pmcid": None,
                "arxiv_id": arxiv_id,
                "raw_response_sha256": raw_sha,
            }
        )
    return output


def fetch(url: str) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": (
                "application/json, application/atom+xml, "
                "application/xml;q=0.9, */*;q=0.1"
            ),
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return int(response.status), dict(response.headers.items()), response.read()


def write_capture(
    root: Path,
    capture_id: str,
    url: str,
    status: int | None,
    headers: dict[str, str],
    body: bytes | None,
    error: str | None,
    query_index: int,
    query: str,
    source_index: int,
    source: str,
) -> dict[str, Any]:
    folder = root / capture_id
    folder.mkdir(parents=True, exist_ok=False)
    body = body or b""
    (folder / "response.raw").write_bytes(body)
    metadata = {
        "capture_id": capture_id,
        "query_index": query_index,
        "query_literal": query,
        "source_index": source_index,
        "source": source,
        "request_url": url,
        "request_timestamp_utc": now_utc(),
        "http_status": status,
        "response_headers": headers,
        "raw_response_sha256": sha256_bytes(body),
        "raw_response_size_bytes": len(body),
        "error": error,
        "article_page_opened": False,
        "full_text_endpoint_used": False,
    }
    (folder / "metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return metadata


def capture_all(output: Path, preflight: bool = False) -> dict[str, Any]:
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)
    records: list[dict[str, Any]] = []
    structured: list[dict[str, Any]] = []
    plan: list[tuple[int, str, int, str, str, float]] = []
    for query_index, query in enumerate(QUERIES, start=1):
        plan.append(
            (
                query_index,
                query,
                1,
                "EUROPE_PMC_OPEN_ACCESS",
                build_europe_pmc_url(query),
                1.0,
            )
        )
        plan.append(
            (
                query_index,
                query,
                2,
                "ARXIV",
                build_arxiv_url(query),
                3.0,
            )
        )
    if preflight:
        plan = [plan[0], plan[1]]

    for ordinal, (query_index, query, source_index, source, url, delay) in enumerate(
        plan, start=1
    ):
        capture_id = f"V003-Q{query_index:02d}-S{source_index:02d}"
        status: int | None = None
        headers: dict[str, str] = {}
        body: bytes | None = None
        error: str | None = None
        try:
            status, headers, body = fetch(url)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        metadata = write_capture(
            output,
            capture_id,
            url,
            status,
            headers,
            body,
            error,
            query_index,
            query,
            source_index,
            source,
        )
        records.append(metadata)
        if error is None and status == 200 and body is not None:
            try:
                parsed = (
                    parse_europe_pmc(
                        body,
                        query_index,
                        query,
                        metadata["raw_response_sha256"],
                    )
                    if source_index == 1
                    else parse_arxiv(
                        body,
                        query_index,
                        query,
                        metadata["raw_response_sha256"],
                    )
                )
                structured.extend(parsed)
                metadata["parse_status"] = "PASS"
                metadata["parsed_result_count"] = len(parsed)
            except Exception as exc:
                metadata["parse_status"] = "FAIL"
                metadata["parse_error"] = f"{type(exc).__name__}: {exc}"
        else:
            metadata["parse_status"] = "NOT_ATTEMPTED"
            metadata["parsed_result_count"] = 0
        (output / capture_id / "metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if ordinal < len(plan):
            time.sleep(delay)

    register = {
        "artifact_id": (
            "PROMETHEUS_ARHIVA_V006_OFFICIAL_API_CAPTURE_REGISTER_0001_V003"
        ),
        "status": "PREFLIGHT_COMPLETE" if preflight else "CAPTURE_COMPLETE",
        "preflight": preflight,
        "capture_records": records,
        "structured_results": structured,
        "summary": {
            "requests": len(records),
            "successful_http_200": sum(
                record["http_status"] == 200 for record in records
            ),
            "errors": sum(record["error"] is not None for record in records),
            "parsed_results": len(structured),
            "article_pages_opened": 0,
            "dlreg_runs": 0,
            "gold_labels_created": 0,
        },
        "epistemic_effect": "NONE",
    }
    (output / "capture_register.json").write_text(
        json.dumps(register, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return register


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    result = capture_all(args.output_dir.resolve(), args.preflight)
    print(json.dumps(result["summary"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
