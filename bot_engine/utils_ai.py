import base64
import io
import json
import logging
import os
import re
import threading
from urllib.parse import quote_plus, urljoin

import requests
import pypdf
from bs4 import BeautifulSoup
from openai import OpenAI
from django.conf import settings

from .paper_search import (
    _arxiv_id, _best_match, _get_arxiv_paper,
    _search_arxiv, _search_google_scholar, _title_similarity,
    find_paper_from_github, search_paper,
)

from .prompts import (
    POSTER_PROMPT,
    WHY_USEFUL_PROMPT,
    DESCRIPTION_FROM_PDF_PROMPT,
    DESCRIPTION_FROM_SCRAPE_PROMPT,
    DESCRIPTION_FROM_POSTER_PROMPT,
)

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0"
    )
}

CONNECT_TIMEOUT = 5
PAGE_TIMEOUT = (CONNECT_TIMEOUT, 15)
PROBE_TIMEOUT = (CONNECT_TIMEOUT, 8)
DOWNLOAD_TIMEOUT = (CONNECT_TIMEOUT, 30)
MAX_PDF_BYTES = 50 * 1024 * 1024
MAX_HTML_BYTES = 8 * 1024 * 1024
MAX_PDF_TEXT_CHARS = 400_000
GITHUB_API_TIMEOUT = (CONNECT_TIMEOUT, 10)
OPENAI_MODEL = "gpt-4o"
PDF_MAGIC = b"%PDF"

VALID_SUBFIELDS = {
    "artificial_intelligence", "machine_learning", "deep_learning",
    "reinforcement_learning", "nlp", "expert_systems",
    "knowledge_representation", "generative_model", "continual_learning",
    "foundation_model", "model_merging", "mil",
    "computer_vision", "medical_imaging", "image_processing",
    "computer_graphics", "augmented_reality", "virtual_reality",
    "segmentation", "classification", "vision_text",
    "mri", "cbct", "pet", "xray", "wsi", "ct", "us",
    "distributed_systems", "embedded_systems", "computer_architecture",
    "operating_systems", "parallel_computing", "hpc", "dependable_systems",
    "cybersecurity", "cryptography", "blockchain", "network_security",
    "iot", "edge_computing",
    "data_mining", "big_data", "dbms", "information_retrieval",
    "multimodal", "missing_modalities", "report", "challenge",
    "software_engineering", "algorithm_design", "computational_complexity",
    "formal_methods", "software_testing",
    "cloud_computing", "quantum_computing", "neuromorphic_computing",
    "mobile_computing", "wearable_computing", "pervasive_computing",
    "robotics", "autonomous_systems", "bioinformatics",
    "computational_biology", "hci", "speech_recognition", "signal_processing",
    "smart_grids", "cyber_physical_systems", "brain", "abdomen", "maxillofacial",
}

_SS_API_KEY = getattr(settings, "SEMANTIC_SCHOLAR_API_KEY", None) or ""
_openai_key = getattr(settings, "OPENAI_API_KEY", None) or ""
API_KEY_CONFIGURED = bool(_openai_key.strip())
_openai_state = threading.local()
_pdf_cache = threading.local()


def _openai_client():
    if not API_KEY_CONFIGURED:
        return None
    pid = os.getpid()
    if getattr(_openai_state, "pid", None) != pid:
        _openai_state.client = OpenAI(api_key=_openai_key)
        _openai_state.pid = pid
    return _openai_state.client


def _ss_headers():
    return {"x-api-key": _SS_API_KEY} if _SS_API_KEY else {}


def _github_headers():
    headers = {"Accept": "application/vnd.github+json"}
    token = (getattr(settings, "GITHUB_TOKEN", "") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _http_get(url, *, timeout=PAGE_TIMEOUT, headers=None, params=None, stream=False):
    return requests.get(
        url,
        headers={**HEADERS, **(headers or {})},
        params=params,
        timeout=timeout,
        allow_redirects=True,
        stream=stream,
    )


def _http_head(url, *, timeout=PROBE_TIMEOUT, headers=None):
    return requests.head(
        url,
        headers={**HEADERS, **(headers or {})},
        timeout=timeout,
        allow_redirects=True,
    )


def _read_capped(response, limit):
    body = bytearray()
    for chunk in response.iter_content(65536):
        body.extend(chunk)
        if len(body) > limit:
            return None
    return bytes(body)


def _content_type(response):
    return response.headers.get("Content-Type", "").lower()


def _fetch_html(url, *, timeout=PAGE_TIMEOUT):
    if not url:
        return None, ""
    try:
        response = _http_get(url, timeout=timeout, stream=True)
    except requests.RequestException as e:
        logger.debug("Page fetch failed for %s: %s", url, e)
        return None, ""
    try:
        if response.status_code != 200:
            return None, ""
        body = _read_capped(response, MAX_HTML_BYTES)
        if body is None:
            logger.info("Page exceeds size limit, skipped: %s", url)
            return None, ""
        return BeautifulSoup(body, "html.parser"), response.url
    except requests.RequestException as e:
        logger.debug("Page read failed for %s: %s", url, e)
        return None, ""
    finally:
        response.close()


def _fetch_text(url, *, timeout=PAGE_TIMEOUT, html_only=True):
    if not url:
        return ""
    try:
        response = _http_get(url, timeout=timeout, stream=True)
    except requests.RequestException as e:
        logger.debug("Text fetch failed for %s: %s", url, e)
        return ""
    try:
        if response.status_code != 200:
            return ""
        if html_only and "html" not in _content_type(response):
            return ""
        body = _read_capped(response, MAX_HTML_BYTES)
        if body is None:
            logger.info("Page exceeds size limit, skipped: %s", url)
            return ""
        return body.decode(response.encoding or "utf-8", errors="replace")
    except requests.RequestException as e:
        logger.debug("Text read failed for %s: %s", url, e)
        return ""
    finally:
        response.close()


def _vision_request(prompt, image_path, max_tokens, temperature):
    return {
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": _encode_image_to_base64(image_path), "detail": "high"}},
            ],
        }],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }


def _text_request(system_prompt, user_content, max_tokens, temperature):
    return {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }


def _complete(request_kwargs, purpose):
    client = _openai_client()
    if client is None:
        return None
    try:
        response = client.chat.completions.create(model=OPENAI_MODEL, **request_kwargs)
    except Exception as e:
        logger.warning("OpenAI %s failed: %s", purpose, type(e).__name__)
        return None
    try:
        return (response.choices[0].message.content or "").strip()
    except (AttributeError, IndexError, TypeError):
        logger.warning("OpenAI %s returned an unusable response", purpose)
        return None


def _slugify(s):
    return str(s).strip().lower().replace(" ", "_").replace("-", "_")


def _parse_subfields(raw_subfields):
    if isinstance(raw_subfields, str):
        items = [_slugify(s) for s in raw_subfields.split(",") if s.strip()]
    elif isinstance(raw_subfields, list):
        items = [_slugify(s) for s in raw_subfields if s]
    else:
        return ""
    seen, valid = set(), []
    for s in items:
        if s in VALID_SUBFIELDS and s not in seen:
            seen.add(s)
            valid.append(s)
    return ",".join(valid)


def _fallback():
    return {
        "_ai_error":          True,
        "is_research_poster": True,
        "title":              "Analysis Failed",
        "authors":            "",
        "conference":         "",
        "year":               "",
        "institution":        "",
        "subfields":          "",
        "search_query":       "",
        "github_query":       "",
    }


def _url_exists(url):
    try:
        return _http_head(url, timeout=PROBE_TIMEOUT).status_code == 200
    except requests.RequestException:
        return False


def _resolve_url(href, base_url):
    return urljoin(base_url, href)


def _encode_image_to_base64(image_path):
    ext = str(image_path).rsplit(".", 1)[-1].lower()
    mime = {"png": "image/png", "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/jpeg")
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def extract_poster_info(image_path):
    try:
        request_kwargs = _vision_request(POSTER_PROMPT, image_path, 1024, 0.2)
    except OSError as e:
        logger.error("Poster image unreadable at %s: %s", image_path, e)
        return _fallback()
    text = _complete(request_kwargs, "poster extraction")
    if text is None:
        return _fallback()
    try:
        return json.loads(text.replace("```json", "").replace("```", "").strip())
    except (ValueError, TypeError):
        logger.warning("Poster extraction returned malformed JSON")
        return _fallback()


def _looks_like_pdf(content_type, prefix):
    if prefix.startswith(PDF_MAGIC):
        return True
    return "pdf" in content_type and not prefix.lstrip()[:15].lower().startswith(b"<!doctype")


def _is_valid_pdf_url(url):
    if not url:
        return False
    if "doi.org/" in url:
        return False
    try:
        with _http_head(url) as response:
            if response.status_code == 200 and "pdf" in _content_type(response):
                return True
    except requests.RequestException:
        pass
    try:
        response = _http_get(url, timeout=PROBE_TIMEOUT, stream=True)
    except requests.RequestException:
        return False
    try:
        content_type = _content_type(response)
        if response.status_code != 200:
            logger.info("PDF candidate rejected: HTTP %s, url=%s", response.status_code, url)
            return False
        prefix = next(response.iter_content(1024), b"")
        if _looks_like_pdf(content_type, prefix):
            return True
        logger.info("PDF candidate rejected: type=%s, url=%s", content_type, url)
        return False
    except requests.RequestException:
        return False
    finally:
        response.close()


def _find_pdf_url(soup, base_url):
    strategies = [
        lambda s: s.find("meta", {"name": "citation_pdf_url"}),
        lambda s: s.find("a", href=re.compile(r"\.pdf($|\?)", re.I)),
        lambda s: s.find("a", href=re.compile(r"arxiv\.org/pdf/", re.I)),
        lambda s: s.find("a", string=re.compile(r"\bPDF\b|Download\s+PDF", re.I)),
        lambda s: s.find("a", href=re.compile(r"stamp\.jsp", re.I)),
        lambda s: s.find("iframe", src=re.compile(r"\.pdf", re.I)),
    ]
    for strategy in strategies:
        el = strategy(soup)
        if not el:
            continue
        if el.name == "meta":
            href = el.get("content", "")
        elif el.name == "iframe":
            href = el.get("src", "")
        else:
            href = el.get("href", "")
        if href:
            return _resolve_url(href, base_url)
    return ""


def _find_pdf_via_arxiv(title):
    paper = _best_match(title, _search_arxiv(title)) if title else None
    return paper.get("pdf_url", "") if paper else ""


def _get_pdf_from_stamp(stamp_url):
    soup, final_url = _fetch_html(stamp_url)
    if soup is None:
        return ""
    iframe = soup.find("iframe")
    src = iframe.get("src", "") if iframe else ""
    return _resolve_url(src, final_url or "https://ieeexplore.ieee.org/") if src else ""


def _get_ieee_pdf(doi):
    try:
        response = _http_get(f"https://doi.org/{doi}", stream=True)
    except requests.RequestException as e:
        logger.debug("IEEE PDF extraction failed for DOI %s: %s", doi, e)
        return ""
    try:
        if response.status_code != 200:
            return ""
        m = re.search(r"/document/(\d+)", response.url)
    finally:
        response.close()
    if not m:
        return ""
    return _get_pdf_from_stamp(
        f"https://ieeexplore.ieee.org/stamp/stamp.jsp?tp=&arnumber={m.group(1)}"
    )


def _get_pdf_from_page(page_url):
    if not page_url:
        return ""
    if page_url.lower().endswith(".pdf"):
        return page_url if _is_valid_pdf_url(page_url) else ""
    identifier = _arxiv_id(page_url)
    if identifier:
        pdf = f"https://arxiv.org/pdf/{identifier}"
        if _is_valid_pdf_url(pdf):
            return pdf
    soup, final_url = _fetch_html(page_url)
    if soup is None:
        return ""
    pdf = _find_pdf_url(soup, final_url)
    if not pdf:
        return ""
    if _is_valid_pdf_url(pdf):
        return pdf
    if "stamp.jsp" in pdf:
        return _get_pdf_from_stamp(pdf)
    return ""


PUBLISHER_PDF_TEMPLATES = (
    (r"^10\.1007/", "https://link.springer.com/content/pdf/{doi}.pdf"),
    (r"^10\.1145/", "https://dl.acm.org/doi/pdf/{doi}"),
)


def _get_pdf_from_doi(doi):
    if not doi:
        return ""
    if re.match(r"^10\.1109/", doi):
        pdf = _get_ieee_pdf(doi)
        if pdf:
            return pdf
    for prefix, template in PUBLISHER_PDF_TEMPLATES:
        if re.match(prefix, doi):
            url = template.format(doi=doi)
            if _is_valid_pdf_url(url):
                return url
    soup, final_url = _fetch_html(f"https://doi.org/{doi}")
    if soup is None:
        return ""
    return _find_pdf_url(soup, final_url)


def _find_pdf_via_google_scholar(title):
    if not title:
        return ""
    candidates = _search_google_scholar(title, limit=5)
    ranked = sorted(candidates, key=lambda p: _title_similarity(title, p.get("title", "")), reverse=True)
    for result in ranked:
        if not _best_match(title, [result]):
            continue
        pdf = result.get("pdf_url", "")
        if pdf and _is_valid_pdf_url(pdf):
            return pdf
    return ""


def _find_real_pdf(pdf_url_hint="", paper_link="", doi="", title=""):
    if pdf_url_hint and _is_valid_pdf_url(pdf_url_hint):
        return pdf_url_hint
    if paper_link:
        pdf = _get_pdf_from_page(paper_link)
        if pdf:
            return pdf
    if doi:
        pdf = _get_pdf_from_doi(doi)
        if pdf:
            return pdf
    if title:
        pdf = _find_pdf_via_arxiv(title)
        if pdf:
            return pdf
        pdf = _find_pdf_via_google_scholar(title)
        if pdf:
            return pdf
    return ""


def _extract_github_from_annotations(reader):
    urls = []
    for page in reader.pages:
        try:
            annots = page.get("/Annots")
            if not annots:
                continue
            if hasattr(annots, "get_object"):
                annots = annots.get_object()
            for annot in annots:
                annot_obj = annot.get_object() if hasattr(annot, "get_object") else annot
                if annot_obj.get("/Subtype") != "/Link":
                    continue
                action = annot_obj.get("/A")
                if not action:
                    continue
                if hasattr(action, "get_object"):
                    action = action.get_object()
                uri = action.get("/URI", "")
                if isinstance(uri, str) and "github.com" in uri.lower():
                    urls.append(uri)
        except (pypdf.errors.PyPdfError, AttributeError, TypeError, KeyError, ValueError) as e:
            logger.debug("Unreadable PDF annotations skipped: %s", type(e).__name__)
            continue
    return urls


def _normalize_github_urls_in_text(text):
    text = re.sub(r"(github\.com/[\w\-\.]+/)\s+([\w\-\.]+)", r"\1\2", text)
    text = re.sub(r"(github\.com/[\w\-\.]*)\s+([\w\-\.]*/)\s*(\S+)", r"\1\2\3", text)
    return text


def _fix_duplicated_name(url):
    parts = url.split("/")
    name = parts[-1]
    owner = parts[-2]
    for length in range(4, len(name) // 2 + 1):
        prefix = name[:length]
        if name.startswith(prefix + prefix):
            fixed = f"https://github.com/{owner}/{prefix}"
            if _url_exists(fixed):
                return fixed
    return None


def _first_valid_github(text):
    repo_pat   = r'github\.com/([\w\-\.]+/[\w\-\.]+?)(?:/|\s|$|\)|\]|:|,|"|\'|\\|\.(?:\s|$))'
    org_pat    = r'github\.com/([\w\-\.]+?)(?:/|\s|$|\)|\]|:|,|"|\'|\\|\.(?:\s|$))'
    skip_parts = {"issues", "pulls", "wiki", "releases", "actions", "blob", "tree"}
    skip_orgs  = {
        "about", "features", "pricing", "enterprise", "settings",
        "login", "signup", "join", "explore", "topics", "trending",
        "collections", "sponsors", "marketplace", "security",
        "notifications", "new", "organizations", "orgs",
    }

    seen_repos = []
    for match in re.findall(repo_pat, text, re.I):
        repo  = match.strip().rstrip("/").rstrip(".")
        parts = repo.split("/")
        if len(parts) == 2 and parts[0] and parts[1].lower() not in skip_parts:
            url = f"https://github.com/{repo}"
            if url not in seen_repos:
                seen_repos.append(url)

    for url in seen_repos:
        if _url_exists(url):
            return url
        fixed = _fix_duplicated_name(url)
        if fixed:
            return fixed

    seen_orgs = []
    for match in re.findall(org_pat, text, re.I):
        name = match.strip().rstrip("/").rstrip(".")
        if not name or name.lower() in skip_orgs or "/" in name:
            continue
        url = f"https://github.com/{name}"
        if url not in seen_orgs and url not in seen_repos:
            seen_orgs.append(url)

    for url in seen_orgs:
        if _url_exists(url):
            return url

    return ""


def _download_pdf_safe(url, timeout=DOWNLOAD_TIMEOUT):
    cached = getattr(_pdf_cache, "entry", None)
    if cached and cached[0] == url:
        return cached[1]
    result = _fetch_pdf(url, timeout)
    _pdf_cache.entry = (url, result)
    return result


def _fetch_pdf(url, timeout):
    try:
        response = _http_get(url, timeout=timeout, stream=True)
    except requests.RequestException as e:
        logger.warning("PDF download failed for %s: %s", url, type(e).__name__)
        return None
    try:
        if response.status_code != 200:
            logger.warning("PDF download returned HTTP %s: %s", response.status_code, url)
            return None
        declared = response.headers.get("Content-Length", "")
        if declared.isdigit() and int(declared) > MAX_PDF_BYTES:
            logger.warning("PDF exceeds download size limit: %s", url)
            return None
        content = _read_capped(response, MAX_PDF_BYTES)
        if content is None:
            logger.warning("PDF exceeds download size limit: %s", url)
            return None
        return content, _content_type(response), response.url
    except requests.RequestException as e:
        logger.warning("PDF download interrupted for %s: %s", url, type(e).__name__)
        return None
    finally:
        response.close()


def _clear_pdf_cache():
    _pdf_cache.entry = None


def _read_pdf(pdf_url):
    result = _download_pdf_safe(pdf_url)
    if result is None:
        return None
    content, content_type, final_url = result
    if not content.startswith(PDF_MAGIC) and "html" in content_type:
        soup = BeautifulSoup(content, "html.parser")
        real_pdf = _find_pdf_url(soup, final_url)
        if not real_pdf or real_pdf == pdf_url:
            return None
        nested = _download_pdf_safe(real_pdf)
        if nested is None:
            return None
        content = nested[0]
    if not content.startswith(PDF_MAGIC):
        logger.info("Discarded non-PDF payload from %s", pdf_url)
        return None
    try:
        return pypdf.PdfReader(io.BytesIO(content))
    except (pypdf.errors.PyPdfError, ValueError, OSError, RecursionError) as e:
        logger.info("Unreadable PDF at %s: %s", pdf_url, type(e).__name__)
        return None


def _find_github_in_pdf(pdf_url):
    if not pdf_url:
        return ""
    reader = _read_pdf(pdf_url)
    if reader is None:
        return ""
    for url in _extract_github_from_annotations(reader):
        found = _first_valid_github(url)
        if found:
            return found
    text = _pdf_text(reader)
    if not text:
        return ""
    return _first_valid_github(_normalize_github_urls_in_text(text))


def _pdf_text(reader, max_pages=None):
    pages = reader.pages if max_pages is None else reader.pages[:max_pages]
    collected, length = [], 0
    for page in pages:
        try:
            extracted = page.extract_text() or ""
        except (pypdf.errors.PyPdfError, ValueError, TypeError, KeyError, RecursionError) as e:
            logger.debug("Unreadable PDF page skipped: %s", type(e).__name__)
            continue
        collected.append(extracted)
        length += len(extracted)
        if length >= MAX_PDF_TEXT_CHARS:
            break
    return "\n".join(collected)[:MAX_PDF_TEXT_CHARS]


def _search_github_api(title, github_query=""):
    if not github_query or not github_query.strip():
        return ""
    gq = github_query.strip().lower()
    if not title:
        return ""
    title_tokens = {w.lower() for w in re.findall(r"[\w\-]+", title)}
    if gq not in title_tokens:
        return ""
    try:
        response = _http_get(
            "https://api.github.com/search/repositories",
            params={"q": gq, "sort": "best-match", "per_page": 5},
            headers=_github_headers(),
            timeout=GITHUB_API_TIMEOUT,
        )
    except requests.RequestException as e:
        logger.info("GitHub repository search failed: %s", type(e).__name__)
        return ""
    try:
        if response.status_code != 200:
            logger.info("GitHub repository search returned HTTP %s", response.status_code)
            return ""
        items = response.json().get("items", [])
    except (ValueError, AttributeError):
        logger.info("GitHub repository search returned malformed results")
        return ""
    finally:
        response.close()
    if not isinstance(items, list):
        return ""
    for item in items:
        if not isinstance(item, dict):
            continue
        if (item.get("name") or "").strip().lower() == gq:
            return item.get("html_url", "")
    return ""


def _scrape_page_for_github(url):
    text = _fetch_text(url)
    return _first_valid_github(text) if text else ""


def find_github_repo(pdf_url="", title="", github_query="", paper_url="", doi=""):
    if pdf_url:
        url = _find_github_in_pdf(pdf_url)
        if url:
            return url

    pages_to_try = []
    if doi:
        pages_to_try.append(f"https://doi.org/{doi}")
    if paper_url:
        pages_to_try.append(paper_url)
    for page in pages_to_try:
        url = _scrape_page_for_github(page)
        if url:
            return url

    if title or github_query:
        url = _search_github_api(title, github_query=github_query)
        if url:
            return url

    return ""


def _extract_authors_from_html(soup):
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (ValueError, TypeError):
            continue
        for item in (data if isinstance(data, list) else [data]):
            if not isinstance(item, dict):
                continue
            raw = item.get("author", [])
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
            if isinstance(raw, list):
                names = [
                    a.get("name", "") if isinstance(a, dict) else str(a)
                    for a in raw
                ]
                names = [n for n in names if n.strip()]
                if names:
                    return ", ".join(names)

    tags = soup.find_all(
        "meta",
        attrs={"name": re.compile(r"^(author|citation_author|dc\.creator)$", re.I)},
    )
    if tags:
        names = [t["content"].strip() for t in tags if t.get("content", "").strip()]
        if names:
            return ", ".join(names)

    og = soup.find("meta", attrs={"property": re.compile(r"author", re.I)})
    if og and og.get("content", "").strip():
        return og["content"].strip()

    return ""


def _authors_from_arxiv(arxiv_id):
    paper = _get_arxiv_paper(arxiv_id)
    return paper.get("authors", "") if paper else ""


def _authors_from_semantic_scholar(identifier):
    try:
        response = _http_get(
            f"https://api.semanticscholar.org/graph/v1/paper/{quote_plus(identifier)}",
            params={"fields": "authors"},
            headers=_ss_headers(),
            timeout=GITHUB_API_TIMEOUT,
        )
    except requests.RequestException as e:
        logger.debug("Semantic Scholar author lookup failed: %s", type(e).__name__)
        return ""
    try:
        if response.status_code != 200:
            return ""
        names = [a["name"] for a in response.json().get("authors", []) if a.get("name")]
        return ", ".join(names) if names else ""
    except (ValueError, AttributeError, TypeError):
        logger.debug("Semantic Scholar returned malformed author metadata")
        return ""
    finally:
        response.close()


DOI_PATTERN = re.compile(r"(10\.\d{4,9}/[\-\._();/:A-Z0-9]+)", re.I)


def fetch_authors(paper_url, title=""):
    if not paper_url and not title:
        return ""

    identifier = _arxiv_id(paper_url)
    if identifier:
        result = _authors_from_arxiv(identifier)
        if result:
            return result

    doi_m = DOI_PATTERN.search(paper_url or "")
    if doi_m:
        result = _authors_from_semantic_scholar(f"DOI:{doi_m.group(1)}")
        if result:
            return result

    if paper_url:
        page = _fetch_text(paper_url, timeout=GITHUB_API_TIMEOUT, html_only=False)
        if page:
            result = _extract_authors_from_html(BeautifulSoup(page, "html.parser"))
            if result:
                return result
            if not doi_m:
                doi_in_page = DOI_PATTERN.search(page)
                if doi_in_page:
                    result = _authors_from_semantic_scholar(f"DOI:{doi_in_page.group(1)}")
                    if result:
                        return result

    if title and title.strip():
        paper = search_paper(title)
        if paper:
            return paper.get("authors", "")

    return ""


def _extract_text_from_pdf(pdf_url, max_pages=8):
    if not pdf_url:
        return ""
    reader = _read_pdf(pdf_url)
    if reader is None:
        return ""
    text = _pdf_text(reader, max_pages=max_pages)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _generate_description_from_pdf(pdf_url):
    pdf_text = _extract_text_from_pdf(pdf_url)
    if not pdf_text or len(pdf_text) < 200:
        return ""
    return _complete(
        _text_request(DESCRIPTION_FROM_PDF_PROMPT, pdf_text[:12000], 300, 0.3),
        "description from PDF",
    ) or ""


def _shorten_scraped_description(raw_text):
    if not raw_text or not raw_text.strip():
        return ""
    if len(raw_text.split()) <= 100:
        return raw_text
    shortened = _complete(
        _text_request(DESCRIPTION_FROM_SCRAPE_PROMPT, raw_text[:6000], 250, 0.3),
        "description from page",
    )
    return shortened or raw_text


ABSTRACT_SELECTORS = (
    "#Abs1-content", "#Abs1 p", ".c-article-section__content",
    ".abstract-content", ".abstractSection", ".abstract",
    "#abstract", "[class*='abstract']", ".paper-abstract", ".article-abstract",
)

ABSTRACT_META_TAGS = (
    {"name": "citation_abstract"},
    {"name": "DC.description"},
    {"name": "description"},
    {"property": "og:description"},
)

ABSTRACT_PREFIX = re.compile(r"^Abstract:?\s*", re.I)


def _scrape_raw_from_site(paper_url):
    soup, _ = _fetch_html(paper_url)
    if soup is None:
        return ""

    bq = soup.find("blockquote", class_="abstract")
    if bq:
        text = ABSTRACT_PREFIX.sub("", bq.get_text(separator=" ").strip())
        if text:
            return text

    for css in ABSTRACT_SELECTORS:
        el = soup.select_one(css)
        if el:
            text = ABSTRACT_PREFIX.sub("", el.get_text(separator=" ").strip())
            if len(text) > 80:
                return text

    for attrs in ABSTRACT_META_TAGS:
        tag = soup.find("meta", attrs)
        if tag and tag.get("content", "").strip():
            return tag["content"].strip()

    return ""


def _scrape_description_from_site(paper_url):
    return _shorten_scraped_description(_scrape_raw_from_site(paper_url))


def _generate_description_from_poster(image_path):
    try:
        request_kwargs = _vision_request(DESCRIPTION_FROM_POSTER_PROMPT, image_path, 300, 0.3)
    except OSError as e:
        logger.warning("Poster image unreadable at %s: %s", image_path, e)
        return ""
    return _complete(request_kwargs, "summary from poster") or ""


def generate_why_useful(summary="", user_notes="", user_tags="", research_interests=""):
    sections = (
        ("Research group interests", research_interests),
        ("Abstract/summary",         summary),
        ("User notes",               user_notes),
        ("User tags",                user_tags),
    )
    parts = [f"{label}:\n{value.strip()}" for label, value in sections if value and value.strip()]
    if not parts:
        return ""
    return _complete(
        _text_request(WHY_USEFUL_PROMPT, "\n\n".join(parts), 150, 0.3),
        "why-useful generation",
    ) or ""


def _resolve_year(ai_year, paper_result, paper_link):
    if ai_year and str(ai_year).strip().isdigit():
        return int(str(ai_year).strip()[:4])

    if paper_result and paper_result.get("year"):
        return paper_result["year"]

    if paper_link:
        arxiv_match = re.search(r'arxiv\.org/(?:abs|pdf)/(\d{2})\d{2}\.\d+', paper_link, re.I)
        if arxiv_match:
            return 2000 + int(arxiv_match.group(1))

    return ""


def _unique_authors(raw):
    seen, unique = set(), []
    for author in (a.strip() for a in (raw or "").split(",") if a.strip()):
        key = author.lower()
        if key not in seen:
            seen.add(key)
            unique.append(author)
    return ", ".join(unique)


def _empty_poster_result():
    return {
        "is_research_poster": False,
        "title":       "",
        "authors":     "",
        "summary":     "",
        "subfields":   "",
        "paper_link":  "",
        "github_link": "",
        "publication_year": "",
        "notes":       "",
    }


def analyze_and_enrich(image_path, overrides=None):
    _clear_pdf_cache()
    try:
        return _analyze_and_enrich(image_path, overrides)
    finally:
        _clear_pdf_cache()


def _analyze_and_enrich(image_path, overrides):
    overrides = overrides or {}
    info = extract_poster_info(image_path)

    if info.get("_ai_error"):
        return info

    if not info.get("is_research_poster"):
        logger.info("Image is not a research poster, skipping enrichment.")
        return _empty_poster_result()

    title        = info.get("title", "")
    search_query = info.get("search_query", "")

    user_paper_link = overrides.get("paper_link", "")

    paper_result = None
    if not user_paper_link:
        paper_result = search_paper(
            title, query_hint=search_query, arxiv_id=info.get("arxiv_id", ""),
        )

    paper_link        = user_paper_link
    pdf_url_hint      = ""
    doi               = ""
    authors_from_api  = ""
    abstract_from_api = ""

    if paper_result:
        paper_link        = paper_link or paper_result.get("paper_url", "")
        pdf_url_hint      = paper_result.get("pdf_url", "")
        doi               = paper_result.get("doi", "")
        authors_from_api  = paper_result.get("authors", "")
        abstract_from_api = paper_result.get("abstract", "")

    pdf_url = _find_real_pdf(
        pdf_url_hint=pdf_url_hint,
        paper_link=paper_link,
        doi=doi,
        title=title,
    )

    if not paper_link and pdf_url:
        identifier = _arxiv_id(pdf_url)
        paper_link = f"https://arxiv.org/abs/{identifier}" if identifier else pdf_url
        logger.info("Recovered paper link from PDF fallback: %s", paper_link)

    github_url = overrides.get("github_link", "") or find_github_repo(
        pdf_url=pdf_url,
        title=title,
        github_query=info.get("github_query", ""),
        paper_url=paper_link,
        doi=doi,
    )

    if not paper_link and github_url:
        paper_result = find_paper_from_github(github_url, title)
        if paper_result:
            paper_link = paper_result["paper_url"]
            pdf_url = paper_result.get("pdf_url", "")
            doi = paper_result.get("doi", "")
            authors_from_api = paper_result.get("authors", "")
            abstract_from_api = paper_result.get("abstract", "")

    authors_raw = authors_from_api
    authors_source = "paper_metadata" if authors_raw else ""
    if not authors_raw and paper_link:
        authors_raw = fetch_authors(paper_link, title=title)
        if authors_raw:
            authors_source = "linked_paper"
    if not authors_raw:
        authors_raw = info.get("authors", "") or ""
        authors_source = "poster_image"
    logger.info("Author resolution for %r: source=%s", title, authors_source)

    description_fns = (
        lambda: _generate_description_from_pdf(pdf_url) if pdf_url else "",
        lambda: _generate_description_from_pdf(pdf_url_hint) if (pdf_url_hint and pdf_url_hint != pdf_url) else "",
        lambda: _scrape_description_from_site(paper_link) if paper_link else "",
        lambda: _scrape_description_from_site(f"https://doi.org/{doi}") if doi else "",
        lambda: abstract_from_api or "",
        lambda: _generate_description_from_poster(image_path),
    )
    description = ""
    for fn in description_fns:
        description = fn() or ""
        if description:
            break

    logger.info("Enrichment complete for %r: paper=%s pdf=%s github=%s summary=%s",
                title, paper_link or "not_found", pdf_url or "not_found",
                github_url or "not_found", bool(description))
    return {
        "is_research_poster": info.get("is_research_poster", True),
        "title":       info.get("title", "Untitled"),
        "authors":     _unique_authors(authors_raw),
        "summary":     description,
        "subfields":   _parse_subfields(info.get("subfields", [])),
        "paper_link":  paper_link,
        "github_link": github_url,
        "publication_year": _resolve_year(info.get("year", ""), paper_result, paper_link),
        "conference":  (info.get("conference") or "").strip(),
        "notes": (
            f"Auto-extracted by AI. "
            f"Conference: {info.get('conference', 'N/A')}, "
            f"Institution: {info.get('institution', 'N/A')}"
        ),
    }
