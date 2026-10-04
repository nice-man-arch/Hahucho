"""Adapter for the documented Miruro Native API hosted at api.hiyori.tv."""
import json
import logging
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from .base import Provider, ProviderError

BASE = "https://api.hiyori.tv"
log = logging.getLogger("hakucho")


def _anilist_id(value):
    value = str(value or "")
    if not re.fullmatch(r"[1-9][0-9]{0,9}", value):
        raise ProviderError("Invalid AniList ID", "invalid_input")
    return value


def _records(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("results", "items", "data", "searchResults"):
            if isinstance(value.get(key), list):
                return value[key]
    return []


class HiyoriProvider(Provider):
    name = "hiyori"

    def _json(self, path, params=None):
        url = BASE + path + (("?" + urlencode(params)) if params else "")
        try:
            req = Request(url, headers={"Accept": "application/json", "User-Agent": "Hakucho/1.0"})
            with urlopen(req, timeout=16) as response:
                if response.status != 200:
                    raise ProviderError(f"Hiyori returned HTTP {response.status}", "provider_unavailable")
                payload = json.loads(response.read(12_000_000).decode("utf-8"))
                if not isinstance(payload, (dict, list)):
                    raise ProviderError("Hiyori returned an invalid response", "provider_unavailable")
                return payload
        except HTTPError as exc:
            if exc.code in (403, 429):
                raise ProviderError(f"Hiyori blocked or rate-limited this request (HTTP {exc.code})", "provider_blocked") from exc
            if exc.code == 404:
                raise ProviderError("Hiyori could not find this anime or episode", "anime_unavailable") from exc
            raise ProviderError(f"Hiyori returned HTTP {exc.code}", "provider_unavailable") from exc
        except (URLError, TimeoutError) as exc:
            raise ProviderError(f"Network failure contacting Hiyori: {exc}", "network_failure") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderError("Hiyori returned invalid JSON", "provider_unavailable") from exc

    def _search_anilist(self, query):
        gql = """
        query ($search: String) {
          Page(page: 1, perPage: 50) {
            media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
              id
              idMal
              title { romaji english userPreferred }
              coverImage { extraLarge large medium }
              seasonYear
              averageScore
              description(asHtml: false)
              synonyms
              genres
              format
              status
              episodes
            }
          }
        }
        """
        req = Request("https://graphql.anilist.co",
                      data=json.dumps({"query": gql, "variables": {"search": query}}).encode("utf-8"),
                      headers={"Content-Type": "application/json", "User-Agent": "Hakucho/1.0", "Accept": "application/json"})
        with urlopen(req, timeout=3.5) as response:
            payload = json.loads(response.read(10_000_000).decode("utf-8"))
            media_list = (payload.get("data") or {}).get("Page", {}).get("media") or []
            items = []
            for m in media_list:
                aid = m.get("id")
                if not aid:
                    continue
                titles = m.get("title") or {}
                title = titles.get("english") or titles.get("romaji") or titles.get("userPreferred") or ""
                cover_dict = m.get("coverImage") or {}
                cover = cover_dict.get("extraLarge") or cover_dict.get("large") or cover_dict.get("medium") or ""
                score = m.get("averageScore")
                rating = f"{score/10:.1f}" if score else "-"
                items.append({
                    "id": f"hiyori:{aid}",
                    "provider": self.name,
                    "title": title,
                    "cover": cover,
                    "year": str(m.get("seasonYear") or ""),
                    "rating": rating,
                    "description": m.get("description") or "",
                    "anilist_id": int(aid),
                    "mal_id": str(m.get("idMal")) if m.get("idMal") is not None else None,
                    "synonyms": m.get("synonyms") or [],
                })
            return items

    def trending(self, page=1, per_page=50):
        gql = """
        query ($page: Int, $perPage: Int) {
          Page(page: $page, perPage: $perPage) {
            media(type: ANIME, sort: TRENDING_DESC) {
              id
              idMal
              title { romaji english userPreferred }
              coverImage { extraLarge large medium }
              seasonYear
              averageScore
              description(asHtml: false)
              genres
              format
              status
              episodes
            }
          }
        }
        """
        try:
            req = Request("https://graphql.anilist.co",
                          data=json.dumps({"query": gql, "variables": {"page": page, "perPage": per_page}}).encode("utf-8"),
                          headers={"Content-Type": "application/json", "User-Agent": "Hakucho/1.0", "Accept": "application/json"})
            with urlopen(req, timeout=3.5) as response:
                payload = json.loads(response.read(10_000_000).decode("utf-8"))
                media_list = (payload.get("data") or {}).get("Page", {}).get("media") or []
                items = []
                for m in media_list:
                    aid = m.get("id")
                    if not aid:
                        continue
                    titles = m.get("title") or {}
                    title = titles.get("english") or titles.get("romaji") or titles.get("userPreferred") or ""
                    cover_dict = m.get("coverImage") or {}
                    cover = cover_dict.get("extraLarge") or cover_dict.get("large") or cover_dict.get("medium") or ""
                    score = m.get("averageScore")
                    rating = f"{score/10:.1f}" if score else "-"
                    items.append({
                        "id": f"hiyori:{aid}",
                        "provider": self.name,
                        "title": title,
                        "cover": cover,
                        "year": str(m.get("seasonYear") or ""),
                        "rating": rating,
                        "description": m.get("description") or "",
                        "anilist_id": int(aid),
                        "mal_id": str(m.get("idMal")) if m.get("idMal") is not None else None,
                    })
                return items
        except Exception as e:
            log.warning("AniList trending failed: %s", e)
            return []

    def genre(self, genre_name, page=1, per_page=50):
        gql = """
        query ($genre: String, $page: Int, $perPage: Int) {
          Page(page: $page, perPage: $perPage) {
            media(genre: $genre, type: ANIME, sort: POPULARITY_DESC) {
              id
              idMal
              title { romaji english userPreferred }
              coverImage { extraLarge large medium }
              seasonYear
              averageScore
              description(asHtml: false)
              genres
              format
              status
              episodes
            }
          }
        }
        """
        try:
            req = Request("https://graphql.anilist.co",
                          data=json.dumps({"query": gql, "variables": {"genre": genre_name, "page": page, "perPage": per_page}}).encode("utf-8"),
                          headers={"Content-Type": "application/json", "User-Agent": "Hakucho/1.0", "Accept": "application/json"})
            with urlopen(req, timeout=3.5) as response:
                payload = json.loads(response.read(10_000_000).decode("utf-8"))
                media_list = (payload.get("data") or {}).get("Page", {}).get("media") or []
                items = []
                for m in media_list:
                    aid = m.get("id")
                    if not aid:
                        continue
                    titles = m.get("title") or {}
                    title = titles.get("english") or titles.get("romaji") or titles.get("userPreferred") or ""
                    cover_dict = m.get("coverImage") or {}
                    cover = cover_dict.get("extraLarge") or cover_dict.get("large") or cover_dict.get("medium") or ""
                    score = m.get("averageScore")
                    rating = f"{score/10:.1f}" if score else "-"
                    items.append({
                        "id": f"hiyori:{aid}",
                        "provider": self.name,
                        "title": title,
                        "cover": cover,
                        "year": str(m.get("seasonYear") or ""),
                        "rating": rating,
                        "description": m.get("description") or "",
                        "anilist_id": int(aid),
                        "mal_id": str(m.get("idMal")) if m.get("idMal") is not None else None,
                    })
                return items
        except Exception as e:
            log.warning("AniList genre failed: %s", e)
            return []

    def search(self, query):
        try:
            items = self._search_anilist(query)
            if items:
                return items
        except Exception as exc:
            log.warning("AniList GraphQL search failed, falling back to Hiyori REST: %s", exc)
        payload = self._json("/search", {"query": query, "page": 1, "per_page": 20})
        items = []
        for row in _records(payload):
            aid = row.get("id") or row.get("anilistId") or row.get("anilist_id")
            if not str(aid or "").isdigit():
                continue
            titles = row.get("title") or {}
            if isinstance(titles, str):
                title = titles
            else:
                title = (titles.get("english") or titles.get("romaji") or titles.get("userPreferred")
                         or row.get("title_english") or row.get("title_romaji") or row.get("name") or "")
            cover = row.get("coverImage") or row.get("cover") or row.get("poster") or row.get("image") or ""
            if isinstance(cover, dict):
                cover = cover.get("large") or cover.get("extraLarge") or cover.get("url") or ""
            ids = row.get("external_ids") or {}
            items.append({"id": f"hiyori:{aid}", "provider": self.name, "title": title,
                          "cover": cover, "year": row.get("seasonYear") or row.get("year") or "",
                          "description": row.get("description") or "", "anilist_id": int(aid),
                          "mal_id": row.get("idMal") or ids.get("mal_id") or ids.get("malId"),
                          "synonyms": row.get("synonyms") or []})
        return items

    def details(self, anime_id):
        aid = _anilist_id(str(anime_id).removeprefix("hiyori:"))
        try:
            gql = """
            query ($id: Int) {
              Media(id: $id, type: ANIME) {
                id
                idMal
                title { romaji english userPreferred }
                coverImage { extraLarge large }
                seasonYear
                genres
                duration
                format
                status
                episodes
                description(asHtml: false)
                synonyms
              }
            }
            """
            req = Request("https://graphql.anilist.co",
                          data=json.dumps({"query": gql, "variables": {"id": int(aid)}}).encode("utf-8"),
                          headers={"Content-Type": "application/json", "User-Agent": "Hakucho/1.0", "Accept": "application/json"})
            with urlopen(req, timeout=3.5) as response:
                payload = json.loads(response.read(10_000_000).decode("utf-8"))
                m = (payload.get("data") or {}).get("Media")
                if m:
                    titles = m.get("title") or {}
                    title = titles.get("english") or titles.get("romaji") or titles.get("userPreferred") or ""
                    cover_dict = m.get("coverImage") or {}
                    cover = cover_dict.get("extraLarge") or cover_dict.get("large") or ""
                    return {"id": f"hiyori:{aid}", "provider": self.name, "title": title,
                            "description": m.get("description") or "", "coverUrl": cover,
                            "cover": {"url": cover}, "year": str(m.get("seasonYear") or ""),
                            "genre": ", ".join(m.get("genres") or []), "duration": f"{m.get('duration')} min" if m.get("duration") else "",
                            "anilist_id": int(aid), "mal_id": m.get("idMal"),
                            "synonyms": m.get("synonyms") or [], "externalLinks": [],
                            "format": m.get("format"), "status": m.get("status"),
                            "episodes_count": m.get("episodes")}
        except Exception as exc:
            log.warning("AniList GraphQL details failed for %s, falling back: %s", aid, exc)
        data = self._json("/info/" + aid)
        titles = data.get("title") or {}
        title = titles if isinstance(titles, str) else (titles.get("english") or titles.get("romaji") or titles.get("userPreferred") or "")
        cover = data.get("coverImage") or data.get("cover") or {}
        if isinstance(cover, dict):
            cover = cover.get("extraLarge") or cover.get("large") or ""
        return {"id": f"hiyori:{aid}", "provider": self.name, "title": title,
                "description": data.get("description") or "", "coverUrl": cover,
                "cover": {"url": cover}, "year": data.get("seasonYear") or "",
                "genre": ", ".join(data.get("genres") or []), "duration": data.get("duration") or "",
                "anilist_id": int(aid), "mal_id": data.get("idMal"),
                "synonyms": data.get("synonyms") or [], "externalLinks": data.get("externalLinks") or [],
                "format": data.get("format"), "status": data.get("status"),
                "episodes_count": data.get("episodes")}

    def episodes(self, anime_id):
        aid = _anilist_id(str(anime_id).removeprefix("hiyori:"))
        payload = self._json("/episodes/" + aid)
        mappings = payload.get("mappings") or {}
        raw_providers = payload.get("providers") or {}
        by_number = {}
        for server, provider_data in raw_providers.items():
            episodes = provider_data.get("episodes", provider_data) if isinstance(provider_data, dict) else {}
            if not isinstance(episodes, dict):
                continue
            for language, rows in episodes.items():
                if language not in ("sub", "dub") or not isinstance(rows, list):
                    continue
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    exact_id = row.get("id")
                    number = row.get("number")
                    if not exact_id or not isinstance(number, (str, int, float)):
                        continue
                    # Hiyori documents these ids as full watch/{provider}/{anilistId}/{category}/{slug} paths.
                    if not re.fullmatch(r"watch/[A-Za-z0-9_-]+/[0-9]+/(?:sub|dub)/[A-Za-z0-9._~-]+", str(exact_id)):
                        continue
                    key = str(number)
                    ep = by_number.setdefault(key, {"id": f"hiyori-episode:{aid}:{key}", "number": key,
                        "title": row.get("title") or f"Episode {key}", "filler": bool(row.get("filler")),
                        "servers": [], "anilist_id": int(aid), "mal_id": mappings.get("malId")})
                    ep["servers"].append({"provider": self.name, "server": str(server), "language": language,
                        "episode_id": exact_id, "title": row.get("title") or f"Episode {key}",
                        "image": row.get("image") or "", "duration": row.get("duration"),
                        "available": True})
        return sorted(by_number.values(), key=lambda ep: (float(ep["number"]) if ep["number"].replace(".", "", 1).isdigit() else 10**9, ep["number"]))

    def sources(self, episode_id, language="sub"):
        if language not in ("sub", "dub"):
            raise ProviderError("language must be sub or dub", "invalid_input")
        match = re.fullmatch(r"watch/[A-Za-z0-9_-]+/[0-9]+/(sub|dub)/[A-Za-z0-9._~-]+", str(episode_id or ""))
        if not match:
            raise ProviderError("Invalid Hiyori episode reference", "invalid_input")
        if match.group(1) != language:
            raise ProviderError("The selected server does not provide that language", "episode_unavailable")
        request_path = "/" + episode_id
        log.debug("hiyori exact source URL: %s%s", BASE, request_path)
        payload = self._json(request_path)
        rows = payload.get("streams") or []
        subs = payload.get("subtitles") or []
        if isinstance(subs, dict):
            subs = [subs]
        subtitles = []
        for item in subs:
            if isinstance(item, dict) and _trusted_https(item.get("file") or item.get("url")):
                subtitles.append({"url": item.get("file") or item.get("url"), "label": item.get("label") or item.get("lang") or "Subtitle", "kind": item.get("kind") or "subtitles"})
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = row.get("url")
            if not _trusted_https(url):
                continue
            ref = row.get("referer") or payload.get("referer") or ""
            out.append({"url": url, "resourceLink": url, "link": url,
                        "quality": row.get("quality") or "Auto", "resolution": _quality_num(row.get("quality")),
                        "type": row.get("type") or "hls", "language": language, "server": episode_id.split("/")[1],
                        "provider": self.name, "subtitles": subtitles,
                        "intro": payload.get("intro"), "outro": payload.get("outro"), "referer": ref})
        if not out:
            raise ProviderError("Hiyori returned no playable streams for this server", "no_stream")
        return sorted(out, key=lambda item: item["resolution"], reverse=True)


def _quality_num(value):
    match = re.search(r"(\d{3,4})p", str(value or ""), re.I)
    return int(match.group(1)) if match else 0


def _trusted_https(value):
    if not isinstance(value, str): return False
    try: parsed=urlparse(value); port=parsed.port
    except ValueError: return False
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or port not in (None,443): return False
    host=parsed.hostname.lower()
    return host not in {"localhost","localhost.localdomain"} and not host.endswith((".localhost",".local"))
