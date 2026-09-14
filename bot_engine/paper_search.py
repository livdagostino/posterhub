import hashlib
import json
import logging
import re
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from urllib.parse import quote_plus, unquote, urlparse

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PosterHub/1.0; +https://posterhub.ing.unimore.it)"}
REQUEST_TIMEOUT = 10
ARXIV_MIN_INTERVAL = 3
ARXIV_MAX_WAIT_SLOTS = 10
UNAVAILABLE_TTL = 30
FEED_CACHE_TTL = 86400
EMPTY_FEED_CACHE_TTL = 300
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
UNAVAILABLE_STATUSES = {401, 403, 429, 500, 502, 503, 504}
MIN_RETRY_DELAY = 3
MAX_RETRY_DELAY = 10

ATOM_NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
STOPWORDS = {"a", "an", "and", "for", "of", "the", "to", "in", "on", "with", "using", "via", "into", "from"}
SPRINGER_HOST = "link.springer.com"

EXACT_MATCH_SCORE = 1.0
STRONG_SIMILARITY = 0.75
NAMED_PROJECT_SIMILARITY = 0.5
MIN_SHARED_WORDS = 3

_ARXIV_ID = r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?"
_ARXIV_VERSION = re.compile(r"v\d+$", re.I)
_ARXIV_HOSTS = {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}
_DOI_HOSTS = {"doi.org", "dx.doi.org"}
_arxiv_lock = threading.Lock()
_last_arxiv_request = 0.0


def _cache_get(key):
    try:
        return cache.get(key)
    except Exception as e:
        logger.warning("Cache read failed for %s: %s", key, type(e).__name__)
        return None


def _cache_set(key, value, timeout):
    try:
        cache.set(key, value, timeout=timeout)
    except Exception as e:
        logger.warning("Cache write failed for %s: %s", key, type(e).__name__)


def _cache_add(key, timeout):
    try:
        return cache.add(key, True, timeout=timeout)
    except Exception as e:
        logger.warning("Cache add failed for %s: %s", key, type(e).__name__)
        raise


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _strip_version(identifier):
    return _ARXIV_VERSION.sub("", identifier)


def _arxiv_id(value):
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if re.fullmatch(_ARXIV_ID, value, re.I):
        return _strip_version(value)
    value = re.sub(r"^arxiv:\s*", "", value, flags=re.I)
    if re.fullmatch(_ARXIV_ID, value, re.I):
        return _strip_version(value)
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        return ""
    path = unquote(parsed.path)
    if parsed.hostname in _ARXIV_HOSTS:
        match = re.fullmatch(r"/(?:abs|pdf|html)/(" + _ARXIV_ID + r")(?:\.pdf)?/?", path, re.I)
    elif parsed.hostname in _DOI_HOSTS:
        match = re.fullmatch(r"/10\.48550/arxiv\.(" + _ARXIV_ID + r")", path, re.I)
    else:
        return ""
    return _strip_version(match.group(1)) if match else ""


def _clean_title(title):
    title = unicodedata.normalize("NFKC", title if isinstance(title, str) else "")
    title = re.sub(r"\[(?:PDF|HTML|CITATION)\]\s*", "", title, flags=re.I)
    return " ".join(re.sub(r"[‐‑‒–—−]", "-", title).split())


def _acronym(title):
    prefix = _clean_title(title).split(":", 1)[0]
    if not re.fullmatch(r"[\w-]{3,32}", prefix):
        return ""
    if any(c.isupper() for c in prefix[1:]) or any(c.isdigit() for c in prefix):
        return prefix
    return ""


def _title_words(title):
    return set(re.findall(r"\w{2,}", _clean_title(title).lower())) - STOPWORDS


def _title_similarity(query, candidate_title):
    q_words, c_words = _title_words(query), _title_words(candidate_title)
    if not q_words or not c_words:
        return 0.0
    return 2 * len(q_words & c_words) / (len(q_words) + len(c_words))


def _compact(value):
    return re.sub(r"\W", "", value).lower()


def _match_score(title, candidate):
    q_words, c_words = _title_words(title), _title_words(candidate)
    if not q_words or not c_words:
        return 0.0
    q_name, c_name = _acronym(title), _acronym(candidate)
    named_mismatch = q_name and c_name and _compact(q_name) != _compact(c_name)
    if named_mismatch:
        return 0.0
    if q_words == c_words:
        return EXACT_MATCH_SCORE
    score = _title_similarity(title, candidate)
    if len(q_words & c_words) < MIN_SHARED_WORDS:
        return 0.0
    if score >= STRONG_SIMILARITY:
        return score
    if q_name and c_name and score >= NAMED_PROJECT_SIMILARITY:
        return score
    return 0.0


def _best_match(title, candidates):
    ranked = sorted(((_match_score(title, p.get("title", "")), p) for p in candidates),
                    key=lambda pair: pair[0], reverse=True)
    for score, paper in ranked:
        if score and paper.get("paper_url"):
            return paper
    return None


def _paper(**fields):
    result = {
        "title": "", "paper_url": "", "pdf_url": "", "arxiv_id": "", "doi": "",
        "authors": "", "abstract": "", "year": None, "_blocked": False, "source": "",
    }
    result.update(fields)
    return result


def _arxiv_paper(identifier, **fields):
    return _paper(
        paper_url=f"https://arxiv.org/abs/{identifier}",
        pdf_url=f"https://arxiv.org/pdf/{identifier}",
        arxiv_id=identifier,
        **fields,
    )


def _year_from(value):
    return int(value[:4]) if value[:4].isdigit() else None


def _request_search(source, url, *, params=None, headers=None, anonymous_fallback=False):
    headers = dict(headers or HEADERS)
    cooldown = "paper-search:unavailable:" + _digest(source + str(headers.get("x-api-key", "")))
    if _cache_get(cooldown):
        logger.info("%s temporarily unavailable; continuing with other sources", source)
        return None

    def unavailable():
        _cache_set(cooldown, True, UNAVAILABLE_TTL)
        return None

    for attempt in range(2):
        try:
            response = requests.get(url, params=params, headers=dict(headers), timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            logger.warning("%s request failed (%s), attempt %d/2", source, type(exc).__name__, attempt + 1)
            if attempt == 0:
                time.sleep(MIN_RETRY_DELAY)
                continue
            return unavailable()
        if response.status_code == 200:
            return response
        status = response.status_code
        retry_after = response.headers.get("Retry-After", MIN_RETRY_DELAY)
        response.close()
        logger.warning("%s returned HTTP %s, attempt %d/2", source, status, attempt + 1)
        if anonymous_fallback and status in {401, 403} and "x-api-key" in headers and attempt == 0:
            logger.warning("Semantic Scholar rejected the configured API key; trying public access")
            headers.pop("x-api-key")
            continue
        if status in RETRYABLE_STATUSES and attempt == 0:
            time.sleep(_retry_delay(retry_after))
            continue
        if status in UNAVAILABLE_STATUSES:
            return unavailable()
        return None
    return None


def _retry_delay(retry_after):
    try:
        return min(MAX_RETRY_DELAY, max(MIN_RETRY_DELAY, float(retry_after)))
    except (TypeError, ValueError):
        return MIN_RETRY_DELAY


def _wait_for_arxiv():
    global _last_arxiv_request
    try:
        for _ in range(ARXIV_MAX_WAIT_SLOTS):
            if _cache_add("paper-search:arxiv:request-slot", ARXIV_MIN_INTERVAL):
                _last_arxiv_request = time.monotonic()
                return True
            time.sleep(ARXIV_MIN_INTERVAL)
        logger.warning("arXiv lookup deferred: request slots are busy")
        return False
    except Exception:
        time.sleep(max(0, ARXIV_MIN_INTERVAL - (time.monotonic() - _last_arxiv_request)))
        _last_arxiv_request = time.monotonic()
        return True


def _arxiv_feed(params):
    key = "paper-search:arxiv:v1:" + _digest(json.dumps(params, sort_keys=True))
    cached = _cache_get(key)
    if cached is not None:
        return cached
    with _arxiv_lock:
        if not _wait_for_arxiv():
            return []
        response = _request_search("arXiv", "https://export.arxiv.org/api/query", params=params)
    if response is None:
        return []
    try:
        root = ET.fromstring(response.content)
        if root.tag != "{http://www.w3.org/2005/Atom}feed":
            raise ValueError("Expected an Atom feed")
        results = []
        for entry in root.findall("a:entry", ATOM_NS):
            entry_id = entry.findtext("a:id", "", ATOM_NS)
            if "/api/errors" in entry_id:
                raise ValueError("arXiv API error entry")
            identifier = _arxiv_id(entry_id)
            if not identifier:
                continue
            results.append(_arxiv_paper(
                identifier,
                title=_clean_title(entry.findtext("a:title", "", ATOM_NS)),
                doi=entry.findtext("arxiv:doi", "", ATOM_NS),
                authors=", ".join(a.text.strip() for a in entry.findall("a:author/a:name", ATOM_NS) if a.text),
                abstract=" ".join(entry.findtext("a:summary", "", ATOM_NS).split()),
                year=_year_from(entry.findtext("a:published", "", ATOM_NS)),
                source="arxiv",
            ))
        logger.info("arXiv lookup returned %d candidates", len(results))
        _cache_set(key, results, FEED_CACHE_TTL if results else EMPTY_FEED_CACHE_TTL)
        return results
    except (ET.ParseError, ValueError, AttributeError):
        logger.warning("arXiv returned an invalid metadata response")
        return []
    finally:
        response.close()


def _get_arxiv_paper(identifier):
    identifier = _arxiv_id(identifier)
    if not identifier:
        return None
    for paper in _arxiv_feed({"id_list": identifier, "max_results": 1}):
        if paper["arxiv_id"] == identifier:
            return paper
    response = _request_search("arXiv abstract", f"https://arxiv.org/abs/{identifier}")
    if response is None:
        return None
    try:
        soup = BeautifulSoup(response.text, "html.parser")
        title = soup.find("meta", attrs={"name": "citation_title"})
        if not title or not title.get("content"):
            return None
        date = soup.find("meta", attrs={"name": "citation_date"})
        abstract = soup.select_one("blockquote.abstract")
        return _arxiv_paper(
            identifier,
            title=_clean_title(title["content"]),
            source="arxiv_page",
            authors=", ".join(a["content"] for a in soup.select('meta[name="citation_author"][content]')),
            abstract=re.sub(r"^Abstract:\s*", "", abstract.get_text(" ", strip=True)) if abstract else "",
            year=_year_from(date.get("content", "") if date else ""),
        )
    finally:
        response.close()


def _search_arxiv(query, limit=5):
    title = _clean_title(query).replace('"', " ")
    if not title:
        return []
    terms = [f'ti:"{title}"']
    acronym = _acronym(title)
    if acronym and acronym != title:
        terms.append(f'ti:"{acronym}"')
    words = sorted(_title_words(title), key=lambda w: (-len(w), w))[:4]
    if len(words) >= MIN_SHARED_WORDS:
        terms.append("(" + " AND ".join(f"ti:{w}" for w in words) + ")")
    results = _arxiv_feed({"search_query": " OR ".join(terms), "max_results": limit})
    if _best_match(title, results):
        return results
    response = _request_search(
        "arXiv web search", "https://arxiv.org/search/",
        params={"query": acronym or title, "searchtype": "title", "abstracts": "show"},
    )
    if response is None:
        return results
    try:
        soup = BeautifulSoup(response.text, "html.parser")
        for item in soup.select("li.arxiv-result")[:limit]:
            link = item.select_one("p.list-title a[href]")
            heading = item.select_one("p.title")
            identifier = _arxiv_id(link["href"]) if link else ""
            if not identifier or not heading:
                continue
            results.append(_arxiv_paper(
                identifier,
                title=_clean_title(heading.get_text(" ", strip=True)),
                authors=", ".join(a.get_text(" ", strip=True) for a in item.select("p.authors a")),
                source="arxiv_web",
            ))
        logger.info("arXiv web search returned %d candidates", len(results))
        return results
    finally:
        response.close()


def _normalize_result(paper):
    paper = dict(paper)
    identifier = _arxiv_id(paper.get("paper_url")) or _arxiv_id(paper.get("pdf_url"))
    if identifier:
        paper.update(paper_url=f"https://arxiv.org/abs/{identifier}",
                     pdf_url=f"https://arxiv.org/pdf/{identifier}", arxiv_id=identifier)
    paper["_blocked"] = urlparse(paper.get("paper_url", "")).hostname == SPRINGER_HOST
    return paper


def _search_semantic_scholar(query, limit=5):
    headers = dict(HEADERS)
    key = getattr(settings, "SEMANTIC_SCHOLAR_API_KEY", "")
    if key:
        headers["x-api-key"] = key
    response = _request_search(
        "Semantic Scholar", "https://api.semanticscholar.org/graph/v1/paper/search",
        params={"query": query, "fields": "title,url,authors,externalIds,openAccessPdf,year,abstract", "limit": limit},
        headers=headers, anonymous_fallback=True,
    )
    if response is None:
        return []
    try:
        results = []
        for paper in response.json().get("data", []):
            ext = paper.get("externalIds") or {}
            identifier = _arxiv_id(ext.get("ArXiv", ""))
            results.append(_normalize_result(_paper(
                title=paper.get("title", ""),
                paper_url=f"https://arxiv.org/abs/{identifier}" if identifier else (paper.get("url") or ""),
                pdf_url=(paper.get("openAccessPdf") or {}).get("url") or "",
                authors=", ".join(a["name"] for a in (paper.get("authors") or []) if a.get("name")),
                arxiv_id=identifier,
                doi=ext.get("DOI") or "",
                abstract=paper.get("abstract") or "",
                year=paper.get("year"),
                source="semantic_scholar",
            )))
        logger.info("Semantic Scholar returned %d candidates for %r", len(results), query)
        return results
    except (ValueError, TypeError, AttributeError):
        logger.warning("Semantic Scholar returned invalid metadata")
        return []
    finally:
        response.close()


def _search_google_scholar(query, limit=5):
    response = _request_search("Google Scholar", f"https://scholar.google.com/scholar?q={quote_plus(query)}")
    if response is None:
        return []
    try:
        soup = BeautifulSoup(response.text, "html.parser")
        if soup.select_one('#gs_captcha_ccl, form[action*="Captcha"], form[action*="/sorry/"]'):
            logger.warning("Google Scholar returned a challenge page")
            return []
        results = []
        for card in soup.select(".gs_r.gs_or")[:limit]:
            anchor = card.select_one(".gs_ri .gs_rt a")
            if not anchor or not anchor.get("href", "").startswith(("http://", "https://")):
                continue
            pdf = card.select_one(".gs_or_ggsm a")
            results.append(_normalize_result(_paper(
                title=_clean_title(anchor.get_text(" ", strip=True)),
                paper_url=anchor["href"],
                pdf_url=pdf.get("href", "") if pdf else "",
                source="google_scholar",
            )))
        logger.info("Google Scholar returned %d candidates for %r", len(results), query)
        return results
    finally:
        response.close()


def search_paper(query, *, query_hint="", arxiv_id=""):
    title = _clean_title(query)
    if arxiv_id:
        paper = _get_arxiv_paper(arxiv_id)
        if paper and (not title or _match_score(title, paper["title"])):
            return paper
    if not title:
        return None
    paper = _best_match(title, _search_arxiv(title))
    if paper:
        logger.info("Paper matched via arXiv: %s", paper["paper_url"])
        return paper
    acronym = _acronym(title)
    variants = list(dict.fromkeys(q for q in (title, query_hint, f'"{acronym}"' if acronym else "") if q))
    blocked = None
    for source in (_search_semantic_scholar, _search_google_scholar):
        for variant in variants[:3]:
            paper = _best_match(title, source(variant))
            if paper and not paper.get("_blocked"):
                logger.info("Paper matched via %s: %s", paper.get("source"), paper["paper_url"])
                return paper
            blocked = blocked or paper
    if not blocked:
        logger.warning("No matching paper found after all bibliographic sources for %r", title)
    return blocked


def find_paper_from_github(repo_url, title):
    parsed = urlparse(repo_url or "")
    if parsed.hostname != "github.com" or not re.fullmatch(r"/[\w.-]+/[\w.-]+/?", parsed.path):
        return None
    response = _request_search("GitHub paper references", "https://github.com" + parsed.path)
    if response is None:
        return None
    try:
        soup = BeautifulSoup(response.text, "html.parser")
        readme = soup.select_one("article.markdown-body") or soup
        identifiers = list(dict.fromkeys(identifier for a in readme.select("a[href]")
                                         if (identifier := _arxiv_id(a["href"]))))
        for identifier in identifiers[:3]:
            paper = _get_arxiv_paper(identifier)
            if paper and _match_score(title, paper["title"]):
                logger.info("Paper recovered from GitHub project: %s", paper["paper_url"])
                return paper
    finally:
        response.close()
    return None
