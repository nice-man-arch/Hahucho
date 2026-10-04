"""HiAnime adapter. Uses its current search, episode, and server APIs."""
from html.parser import HTMLParser
from html import unescape
import json
import logging
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse
from urllib.request import Request, urlopen

from .base import Provider, ProviderError

BASE = "https://hianime.at"
log = logging.getLogger("hakucho")
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
           "X-Requested-With": "XMLHttpRequest", "Referer": BASE + "/"}


def _attr(tag: str, name: str) -> str:
    m = re.search(r'\b' + re.escape(name) + r'=["\']([^"\']*)', tag, re.I)
    return m.group(1) if m else ""


def _safe_url(url: str, hosts: set[str] | None = None) -> str:
    p = urlparse(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.port not in (None, 443):
        return ""
    if hosts and p.hostname.lower() not in hosts:
        return ""
    return url


def _safe_cover_url(url: str) -> str:
    if not url or not isinstance(url, str):
        return ""
    p = urlparse(url)
    if p.scheme in ("http", "https") and p.hostname:
        return url
    return ""


def _media_url(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    allowed = host.endswith(".dramahot.top") or host.endswith(".drama1.cfd")
    return _safe_url(url) if allowed else ""


class HiAnimeProvider(Provider):
    name = "hianime"

    def _get(self, url: str, referer: str | None = None, timeout: float = 6.0) -> bytes:
        if not _safe_url(url):
            raise ProviderError("Provider returned an unsafe URL", "provider_unavailable")
        headers = dict(HEADERS)
        if referer:
            headers["Referer"] = referer
        try:
            with urlopen(Request(url, headers=headers), timeout=timeout) as r:
                body = r.read(8_000_000)
                if r.status == 403 or b"Just a moment" in body[:5000]:
                    raise ProviderError("HiAnime is blocking this network (Cloudflare challenge)", "cloudflare_blocked")
                return body
        except HTTPError as e:
            if e.code in (403, 503):
                body = e.read(1_000_000).lower()
                if b"cloudflare" in body or b"just a moment" in body:
                    raise ProviderError("HiAnime is blocking this network (Cloudflare challenge)", "cloudflare_blocked") from e
                raise ProviderError(f"HiAnime rejected or could not serve the request (HTTP {e.code})", "provider_unavailable") from e
            if e.code == 404:
                kind = "episode_unavailable" if "/api/theme/episode/" in url else "anime_unavailable"
                raise ProviderError(f"HiAnime could not find that {'episode' if kind == 'episode_unavailable' else 'anime'} (HTTP 404)", kind) from e
            raise ProviderError(f"HiAnime returned HTTP {e.code}", "provider_unavailable") from e
        except URLError as e:
            raise ProviderError(f"Network failure contacting HiAnime: {e.reason}", "network_failure") from e
        except TimeoutError as e:
            raise ProviderError("Network timeout contacting HiAnime", "network_failure") from e

    def _text(self, url: str, timeout: float = 6.0) -> str:
        return self._get(url, timeout=timeout).decode("utf-8", "replace")

    def _parse_film_html(self, html: str) -> list[dict]:
        out, seen = [], set()
        for m in re.finditer(r"(?:<div[^>]*class=\"[^\"]*flw-item[^\"]*\"[^>]*>|<div[^>]*class=\"film-poster\"[^>]*>).*?<h3[^>]*class=\"[^\"]*film-name[^\"]*\"[^>]*>.*?</h3>", html, re.S):
            chunk = m.group(0)
            a = re.search(r"<h3[^>]*class=\"[^\"]*film-name[^\"]*\"[^>]*>\s*<a\b([^>]*)>(.*?)</a>", chunk, re.S)
            if not a:
                continue
            tag_attrs, title_html = a.group(1), a.group(2)
            href = _attr("<a " + tag_attrs + ">", "href")
            title = unescape(re.sub(r"<[^>]+>", "", title_html)).strip()
            full_href = urljoin(BASE, href)
            aid = urlparse(full_href).path.rstrip("/").rsplit("/", 1)[-1]
            if not aid or aid in seen or not title:
                continue
            seen.add(aid)

            poster_m = re.search(r"<img\b[^>]*(?:data-src|src)=[\"']([^\"']+)[\"']", chunk, re.I)
            raw_cover = poster_m.group(1) if poster_m else ""
            cover = urljoin(BASE, raw_cover) if raw_cover else ""
            if not _safe_cover_url(cover):
                cover = ""
            out.append({"id": "hianime:" + aid, "provider": self.name, "title": title, "cover": cover, "year": "", "rating": None})
            if len(out) >= 60:
                break
        return out

    def search(self, query: str) -> list[dict]:
        html = self._text(f"{BASE}/search?keyword={quote(query)}", timeout=5.0)
        return self._parse_film_html(html)

    def trending(self) -> list[dict]:
        html = self._text(f"{BASE}/top-airing", timeout=5.0)
        return self._parse_film_html(html)

    def genre(self, genre_name: str) -> list[dict]:
        slug = genre_name.strip().lower().replace(" ", "-")
        try:
            html = self._text(f"{BASE}/genres/{quote(slug)}", timeout=5.0)
            return self._parse_film_html(html)
        except Exception:
            return self.search(genre_name)

    def details(self, anime_id: str) -> dict:
        slug = _anime_id(str(anime_id).removeprefix("hianime:"))
        html = self._text(f"{BASE}/{quote(slug, safe='-')}", timeout=6.0)
        title = ""
        m = re.search(r'<h2[^>]*class="[^" ]*film-name[^" ]*[^>]*>(.*?)</h2>', html, re.I | re.S)
        if m:
            title = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        title = title or (re.search(r'<title>(.*?)</title>', html, re.I | re.S) or [None, slug])[1].split(" - ")[0].strip()
        desc = ""
        m = re.search(r'<div[^>]*class="[^\"]*film-description[^\"]*"[^>]*>(.*?)</div>', html, re.I | re.S)
        if m:
            desc = unescape(re.sub(r"<[^>]+>", " ", m.group(1))).strip()
        poster = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)', html, re.I) or re.search(r'<img\b[^>]*class="[^"]*film-poster-img[^\"]*"[^>]*(?:data-src|src)=["\']([^"\']+)', html, re.I)
        cover = _safe_cover_url(urljoin(BASE, poster.group(1))) if poster else ""
        mal = re.search(r'myanimelist\.net/anime/(\d+)', html, re.I)
        pos = html.find("film-name")
        header_chunk = html[pos:pos+4000] if pos != -1 else ""
        has_sub = "tick-sub" in header_chunk if header_chunk else True
        has_dub = "tick-dub" in header_chunk
        return {"id": "hianime:" + slug, "provider": self.name, "title": title, "description": desc, "intro": desc[:280], "cover": {"url": cover}, "coverUrl": cover, "year": "", "genre": "", "mal_id": int(mal.group(1)) if mal else None, "anilist_id": None, "has_sub": has_sub, "has_dub": has_dub}

    def episodes(self, anime_id: str) -> list[dict]:
        aid = _anime_id(str(anime_id).removeprefix("hianime:"))
        num = re.search(r"-(\d+)$", aid).group(1)
        payload = json.loads(self._get(f"{BASE}/api/theme/episode/list/{num}").decode())
        html = payload.get("html", "").replace("\\/", "/")
        has_dub = False
        try:
            main_html = self._text(f"{BASE}/{aid}", timeout=4.0)
            pos = main_html.find("film-name")
            header_chunk = main_html[pos:pos+4000] if pos != -1 else ""
            has_dub = "tick-dub" in header_chunk
        except Exception:
            has_dub = False
        out = []
        for tag in re.findall(r'<a\b[^>]*class="[^"]*ep-item[^"]*"[^>]*>', html, re.I):
            number, eid = _attr(tag, "data-number"), _attr(tag, "data-id")
            if number and eid:
                servers = [
                    {"server": "zokoanime", "provider": self.name, "language": "sub", "episode_id": eid, "title": f"Episode {number}"}
                ]
                if has_dub:
                    servers.append(
                        {"server": "zokoanime", "provider": self.name, "language": "dub", "episode_id": eid, "title": f"Episode {number}"}
                    )
                out.append({"id": eid, "number": number, "title": f"Episode {number}", "filler": False,
                            "anime_id": "hianime:" + aid, "servers": servers})
        if not out and payload.get("totalItems", 0):
            raise ProviderError("HiAnime changed its episode response format", "provider_unavailable")
        return out

    def sources(self, episode_id: str, language: str = "sub") -> list[dict]:
        if not episode_id.isdigit():
            raise ProviderError("Invalid episode ID", "invalid_input")
        lang = "dub" if language.lower() in ("dub", "eng", "en") else "sub"
        log.debug("hianime exact source request: %s/api/theme/episode/servers?episodeId=%s language=%s", BASE, episode_id, lang)
        payload = json.loads(self._get(f"{BASE}/api/theme/episode/servers?episodeId={episode_id}").decode())
        html = payload.get("html", "").replace("\\/", "/")
        h = None
        for tag in re.findall(r'<div\b[^>]*class="[^"]*server-item[^"]*"[^>]*>', html, re.I):
            if _attr(tag, "data-type").lower() == lang and _attr(tag, "data-server-name").lower() == "zokoanime":
                h = _attr(tag, "data-hash")
                break
        if not h:
            raise ProviderError(f"No {lang} source available for this episode", "no_stream")
        import base64
        try:
            embed = base64.b64decode(h).decode()
        except Exception as e:
            raise ProviderError("HiAnime returned an invalid source reference", "provider_unavailable") from e
        embed = _safe_url(embed, {"zokoanime.video"})
        if not embed:
            raise ProviderError("HiAnime returned an untrusted player URL", "provider_unavailable")
        log.debug("hianime resolved embed URL episode_id=%s url=%s", episode_id, embed)
        page = self._text(embed)
        m = re.search(r'window\.__P="([^" ]+)"', page)
        if not m:
            raise ProviderError("The ZokoAnime player configuration was not found", "no_stream")
        try:
            blob = base64.b64decode(m.group(1))
            key = b"otaku-embed-v1"
            config = json.loads(bytes(b ^ key[i % len(key)] for i, b in enumerate(blob)))
        except Exception as e:
            raise ProviderError("Could not decode the current player configuration", "provider_unavailable") from e
        master = _media_url(config.get("src", ""))
        if not master:
            raise ProviderError("Player did not return a safe HLS URL", "no_stream")
        subtitles = []
        for sub in config.get("subtitles", []):
            u = _media_url(urljoin(embed, sub.get("src", "")))
            if u:
                subtitles.append(u)
        referer = urlparse(embed)
        player_referer = f"{referer.scheme}://{referer.netloc}/"
        return [{"quality": "1080p", "resolution": 1080, "url": master, "resourceLink": master, "link": master, "subtitles": subtitles, "language": lang, "referer": player_referer}]


def _anime_id(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*-\d+", value or ""):
        raise ProviderError("Invalid anime ID", "invalid_input")
    return value
