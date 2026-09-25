"""Web access: search providers, safe page fetching and readable-text extraction."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from jarvis.config import SearchSettings

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36 JARVIS-Assistant"
)
MAX_PAGE_BYTES = 3_000_000

# Mildly prefer sources that tend to be authoritative.
_AUTHORITY = {
    "wikipedia.org": 0.3, ".gov": 0.4, ".edu": 0.3, "who.int": 0.4, "europa.eu": 0.3, "nature.com": 0.3,
    "reuters.com": 0.3, "apnews.com": 0.3, "bbc.co.uk": 0.2, "bbc.com": 0.2, "github.com": 0.2,
    "stackoverflow.com": 0.2, "docs.python.org": 0.3, "developer.mozilla.org": 0.3, "rtings.com": 0.3,
    "notebookcheck.net": 0.3, "arstechnica.com": 0.2, "theverge.com": 0.1, "tomshardware.com": 0.2,
}
_LOW_VALUE = ("pinterest.", "facebook.com", "instagram.com", "tiktok.com", "quora.com")


class WebError(Exception):
    pass


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    source: str = ""

    @property
    def domain(self) -> str:
        return urlparse(self.url).netloc.lower().removeprefix("www.")

    def authority(self) -> float:
        domain = self.domain
        score = max((v for k, v in _AUTHORITY.items() if k in domain), default=0.0)
        return score - (0.5 if any(low in domain for low in _LOW_VALUE) else 0.0)

    def to_dict(self) -> dict[str, str]:
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


@dataclass
class Page:
    url: str
    title: str
    text: str
    links: list[str] = field(default_factory=list)


def _is_public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
            return False
    return True


class WebClient:
    def __init__(self, settings: SearchSettings, *, allow_private: bool = False,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        self._allow_private = allow_private
        self._client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-GB,en;q=0.9"},
            timeout=httpx.Timeout(15.0, connect=8.0),
            follow_redirects=True,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    # ── Search ──────────────────────────────────────────────────────────────
    async def search(self, query: str, max_results: int | None = None) -> list[SearchResult]:
        limit = max_results or self._settings.max_results
        provider = self._settings.provider
        if provider == "tavily" and self._settings.tavily_api_key:
            return await self._tavily(query, limit)
        if provider == "brave" and self._settings.brave_api_key:
            return await self._brave(query, limit)
        return await self._duckduckgo(query, limit)

    async def _duckduckgo(self, query: str, limit: int) -> list[SearchResult]:
        errors = []
        for endpoint in ("https://html.duckduckgo.com/html/", "https://lite.duckduckgo.com/lite/"):
            try:
                resp = await self._client.post(endpoint, data={"q": query, "kl": "wt-wt"})
            except httpx.HTTPError as exc:
                errors.append(f"{urlparse(endpoint).netloc}: {type(exc).__name__}")
                continue
            if resp.status_code != 200:
                errors.append(f"{urlparse(endpoint).netloc}: HTTP {resp.status_code}")
                continue
            results = self._parse_ddg(resp.text, limit)
            if results:
                return results
            errors.append(f"{urlparse(endpoint).netloc}: no parsable results (possibly rate-limited)")
        raise WebError("web search failed — " + "; ".join(errors) +
                       ". Configure JARVIS_SEARCH_PROVIDER=tavily or brave with an API key for reliability.")

    @staticmethod
    def _parse_ddg(html: str, limit: int) -> list[SearchResult]:
        soup = BeautifulSoup(html, "html.parser")
        results: list[SearchResult] = []
        anchors = soup.select("a.result__a") or soup.select("a.result-link")
        for anchor in anchors:
            href = str(anchor.get("href") or "")
            if "uddg=" in href:
                href = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
            if not href.startswith("http") or "duckduckgo.com/y.js" in href:
                continue
            container = anchor.find_parent(class_="result") or anchor.find_parent("tr")
            snippet_el = container.select_one(".result__snippet") if container else None
            if snippet_el is None and container is not None:
                nxt = container.find_next_sibling("tr")
                snippet_el = nxt.select_one(".result-snippet") if nxt else None
            results.append(SearchResult(anchor.get_text(" ", strip=True), href,
                                        snippet_el.get_text(" ", strip=True) if snippet_el else "", "duckduckgo"))
            if len(results) >= limit:
                break
        return results

    async def _tavily(self, query: str, limit: int) -> list[SearchResult]:
        key = self._settings.tavily_api_key.get_secret_value() if self._settings.tavily_api_key else ""
        try:
            resp = await self._client.post(
                "https://api.tavily.com/search",
                json={"query": query, "max_results": limit, "api_key": key},
                headers={"Authorization": f"Bearer {key}"},
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise WebError(f"Tavily search failed: {type(exc).__name__}") from exc
        return [SearchResult(r.get("title", ""), r.get("url", ""), r.get("content", "")[:400], "tavily")
                for r in resp.json().get("results", [])]

    async def _brave(self, query: str, limit: int) -> list[SearchResult]:
        key = self._settings.brave_api_key.get_secret_value() if self._settings.brave_api_key else ""
        try:
            resp = await self._client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": limit},
                headers={"X-Subscription-Token": key, "Accept": "application/json"},
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise WebError(f"Brave search failed: {type(exc).__name__}") from exc
        items = resp.json().get("web", {}).get("results", [])
        return [SearchResult(r.get("title", ""), r.get("url", ""), re.sub(r"<[^>]+>", "", r.get("description", "")),
                             "brave") for r in items]

    # ── Fetch ───────────────────────────────────────────────────────────────
    async def _check_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise WebError("only http(s) URLs can be fetched")
        if not self._allow_private and not await asyncio.to_thread(_is_public_host, parsed.hostname):
            raise WebError("refusing to fetch a private-network or unresolvable address")

    async def fetch(self, url: str, max_chars: int = 20_000) -> Page:
        # Redirects are followed by hand so every hop passes the private-network check.
        try:
            for _ in range(6):
                await self._check_url(url)
                request = self._client.build_request("GET", url)
                resp = await self._client.send(request, stream=True, follow_redirects=False)
                if resp.is_redirect and "location" in resp.headers:
                    await resp.aclose()
                    url = urljoin(url, resp.headers["location"])
                    continue
                break
            else:
                raise WebError("too many redirects")
        except httpx.HTTPError as exc:
            raise WebError(f"could not load the page: {type(exc).__name__}") from exc
        try:
            async with resp:
                if resp.status_code >= 400:
                    raise WebError(f"the site returned HTTP {resp.status_code}")
                content_type = resp.headers.get("content-type", "")
                if not any(t in content_type for t in ("html", "text", "xml", "json")):
                    raise WebError(f"unsupported content type: {content_type.split(';')[0] or 'unknown'}")
                body = b""
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_PAGE_BYTES:
                        break
                final_url = str(resp.url)
                encoding = resp.encoding or "utf-8"
        except httpx.HTTPError as exc:
            raise WebError(f"could not load the page: {type(exc).__name__}") from exc
        html = body.decode(encoding, errors="replace")
        if "html" not in content_type:
            return Page(final_url, final_url, html[:max_chars])
        title, text, links = extract_readable(html)
        return Page(final_url, title or final_url, text[:max_chars], links[:50])


def extract_readable(html: str) -> tuple[str, str, list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else ""
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form",
                     "iframe", "button"]):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    blocks = []
    for element in root.find_all(["h1", "h2", "h3", "p", "li", "td", "th", "pre", "blockquote"]):
        text = element.get_text(" ", strip=True)
        if len(text) >= 25 or element.name in {"h1", "h2", "h3", "td", "th"}:
            blocks.append(text)
    if not blocks:
        blocks = [root.get_text(" ", strip=True)]
    deduped: list[str] = []
    seen: set[str] = set()
    for block in blocks:
        if block not in seen:
            seen.add(block)
            deduped.append(block)
    links = [str(a["href"]) for a in root.find_all("a", href=True) if str(a["href"]).startswith("http")]
    return title, "\n".join(deduped), links


def relevant_passages(text: str, question: str, max_chars: int = 2500) -> str:
    """Pick the paragraphs of `text` that share the most terms with `question`."""
    terms = {t for t in re.findall(r"[a-z0-9]{3,}", question.lower())}
    paragraphs = [p for p in text.split("\n") if p.strip()]
    if not terms:
        return "\n".join(paragraphs)[:max_chars]
    scored = sorted(
        ((sum(p.lower().count(t) for t in terms) / (1 + len(p) / 400), i, p) for i, p in enumerate(paragraphs)),
        reverse=True,
    )
    chosen: list[tuple[int, str]] = []
    total = 0
    for score, index, paragraph in scored:
        if score <= 0 or total > max_chars:
            break
        chosen.append((index, paragraph))
        total += len(paragraph)
    if not chosen:
        return "\n".join(paragraphs)[:max_chars]
    return "\n".join(p for _, p in sorted(chosen))[:max_chars]


class MockWebClient(WebClient):
    """Deterministic fake web for mock mode and tests."""

    def __init__(self) -> None:  # noqa: D401 — no network client needed
        self.fetched: list[str] = []
        self.searched: list[str] = []

    async def close(self) -> None:
        return None

    async def search(self, query: str, max_results: int | None = None) -> list[SearchResult]:
        self.searched.append(query)
        slug = re.sub(r"\W+", "-", query.lower()).strip("-")[:40]
        return [
            SearchResult(f"{query.title()} — overview (simulated)", f"https://example.org/{slug}",
                         f"A simulated overview of {query}.", "mock"),
            SearchResult(f"{query.title()} compared (simulated)", f"https://reviews.example.com/{slug}",
                         f"Simulated comparison covering {query}.", "mock"),
            SearchResult(f"{query.title()} — encyclopedia (simulated)", f"https://en.wikipedia.org/wiki/{slug}",
                         f"Simulated encyclopedia entry on {query}.", "mock"),
        ][: max_results or 8]

    async def fetch(self, url: str, max_chars: int = 20_000) -> Page:
        self.fetched.append(url)
        return Page(url, f"Simulated page for {url}",
                    f"This is simulated page content for {url}. It exists only in mock mode.\n"
                    "Key point one: simulated data. Key point two: nothing here is real.")
