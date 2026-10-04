#!/usr/bin/env python3
"""Small local JSON API for Hakuchō. No web framework or third-party modules required."""
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
import socket
import select
import threading
import shutil
import hashlib
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FutureTimeoutError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Omarchy watches plugin trees for edits; don't create __pycache__ files inside it.
sys.dont_write_bytecode = True

from providers import HiAnimeProvider, HiyoriProvider
from providers.base import ProviderError
from cache import Cache
import downloads

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s hakucho: %(message)s")
log = logging.getLogger("hakucho")
VERSION = "2.1.0"
DEBUG_SOURCES = (os.environ.get("HAKUCHO_DEBUG_SOURCES") or os.environ.get("ANIMECHY_DEBUG_SOURCES", "")).lower() in ("1", "true", "yes", "on")
if DEBUG_SOURCES:
    log.setLevel(logging.DEBUG)
DATA = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "hakucho" / "hakucho.sqlite3"
OLD_DATA = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "animechy" / "animechy.sqlite3"
if not DATA.exists() and OLD_DATA.exists():
    try:
        DATA.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(OLD_DATA, DATA)
    except Exception:
        pass
cache = Cache(DATA)
providers = [HiAnimeProvider(), HiyoriProvider()]
live_sources = {}
_saved_settings = cache.settings()
downloads.configure(cache, _saved_settings.get("download_location"), _saved_settings.get("download_max_simultaneous", 2))

COVER_CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "hakucho" / "covers"
COVER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_cover_pool = ThreadPoolExecutor(max_workers=16)


def _download_cover(url, filepath):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"}
        if "hianime" in str(url) or "anipixcdn" in str(url):
            headers["Referer"] = "https://hianime.to/"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=6) as r:
            data = r.read()
            if data and len(data) > 100:
                tmp = filepath.with_suffix(".tmp")
                tmp.write_bytes(data)
                tmp.replace(filepath)
    except Exception:
        pass


def resolve_cover_for_item(item):
    if not isinstance(item, dict):
        return ""
    cover = item.get("cover") or item.get("coverUrl") or ""
    if isinstance(cover, dict):
        cover = cover.get("url") or cover.get("extraLarge") or cover.get("large") or cover.get("medium") or ""
    cover = str(cover or "").strip()
    if cover.startswith("{") and ("'url':" in cover or '"url":' in cover):
        try:
            cj = json.loads(cover.replace("'", '"'))
            if isinstance(cj, dict): cover = str(cj.get("url") or "")
        except Exception:
            pass
    if not cover or not cover.startswith("http"):
        aid = item.get("id") or item.get("anime_id")
        if aid:
            cached = cache.get("details:" + str(aid))
            if cached and isinstance(cached, dict):
                c = cached.get("cover")
                if isinstance(c, dict): cover = c.get("url") or c.get("extraLarge") or c.get("large") or ""
                elif isinstance(c, str): cover = c
    if cover and cover.startswith("http"):
        item["cover"] = cover
        item["coverUrl"] = cover
    return cover


def attach_cover_cache(item):
    if not isinstance(item, dict):
        return item
    cover_url = resolve_cover_for_item(item)
    if not cover_url or not isinstance(cover_url, str) or not cover_url.startswith("http"):
        return item
    h = hashlib.md5(cover_url.encode("utf-8")).hexdigest()
    dest = COVER_CACHE_DIR / f"{h}.jpg"
    if dest.exists() and dest.stat().st_size > 0:
        item["coverPath"] = f"file://{dest}"
    else:
        item["coverPath"] = cover_url
        _cover_pool.submit(_download_cover, cover_url, dest)
    return item


def attach_covers(items):
    for item in items:
        attach_cover_cache(item)
    return items


def issue_source_tokens(items):
    now = time.time()
    for key, record in list(live_sources.items()):
        if record.get("expires", 0) < now: live_sources.pop(key, None)
    output=[]
    for item in items:
        source=dict(item); token=uuid.uuid4().hex; source["source_id"]=token
        subs=source.get("subtitles") or []
        first=subs[0] if subs else {}
        subtitle=first.get("url") or first.get("file") or "" if isinstance(first,dict) else first if isinstance(first,str) else ""
        live_sources[token]={"url":source.get("url") or source.get("resourceLink") or source.get("link"),"subtitle":subtitle,"referer":source.get("referer", ""),"expires":now+3600}
        output.append(source)
    return output


active_players = 0
playback_lock = threading.Lock()


def monitor_playback(process, socket_path, metadata):
    def run():
        global active_players
        with playback_lock:
            active_players += 1
        ipc = None
        deadline = time.time() + 15
        while process.poll() is None and time.time() < deadline:
            try:
                ipc = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                ipc.connect(socket_path)
                ipc.setblocking(False)
                break
            except OSError:
                if ipc:
                    try: ipc.close()
                    except Exception: pass
                ipc = None
                time.sleep(0.2)
        position = 0.0
        duration = 0.0
        eof_reached = False
        req_id = 0
        pending_reqs = {}
        buf = b""
        if ipc:
            try:
                # Observe properties so mpv streams property updates directly
                for prop_id, prop_name in [(1, "time-pos"), (2, "duration"), (3, "eof-reached")]:
                    try:
                        ipc.sendall((json.dumps({"command": ["observe_property", prop_id, prop_name]}) + "\n").encode())
                    except OSError:
                        break

                while process.poll() is None:
                    for prop_name in ["time-pos", "duration", "eof-reached"]:
                        req_id += 1
                        pending_reqs[req_id] = prop_name
                        try:
                            ipc.sendall((json.dumps({"command": ["get_property", prop_name], "request_id": req_id}) + "\n").encode())
                        except OSError:
                            break

                    # Drain socket responses
                    while True:
                        r, _, _ = select.select([ipc], [], [], 0.2)
                        if not r:
                            break
                        try:
                            chunk = ipc.recv(4096)
                            if not chunk:
                                break
                            buf += chunk
                            while b"\n" in buf:
                                line, buf = buf.split(b"\n", 1)
                                if not line.strip():
                                    continue
                                try:
                                    msg = json.loads(line.decode(errors="ignore"))
                                except Exception:
                                    continue

                                ev = msg.get("event")
                                if ev == "end-file":
                                    # ONLY natural eof means completed episode.
                                    # 'stop', 'quit', or manual window close are NOT eof!
                                    if msg.get("reason") == "eof":
                                        eof_reached = True
                                elif ev == "property-change":
                                    pname = msg.get("name")
                                    pdata = msg.get("data")
                                    if pname == "time-pos" and isinstance(pdata, (int, float)):
                                        position = float(pdata)
                                    elif pname == "duration" and isinstance(pdata, (int, float)):
                                        duration = float(pdata)
                                    elif pname == "eof-reached" and isinstance(pdata, bool):
                                        if pdata:
                                            eof_reached = True

                                rid = msg.get("request_id")
                                if rid in pending_reqs:
                                    prop = pending_reqs.pop(rid)
                                    pdata = msg.get("data")
                                    if prop == "time-pos" and isinstance(pdata, (int, float)):
                                        position = float(pdata)
                                    elif prop == "duration" and isinstance(pdata, (int, float)):
                                        duration = float(pdata)
                                    elif prop == "eof-reached" and isinstance(pdata, bool):
                                        if pdata:
                                            eof_reached = True
                        except Exception:
                            break

                    if len(pending_reqs) > 50:
                        pending_reqs.clear()

                    if position > 0 or duration > 0:
                        is_watched = bool(eof_reached or (duration > 0 and position >= duration * 0.90))
                        cache.update_progress(
                            metadata["id"],
                            metadata.get("episode_id") or metadata.get("episode", ""),
                            position,
                            duration,
                            is_watched,
                            title=metadata.get("title"),
                            episode=metadata.get("episode"),
                            cover=metadata.get("cover") or metadata.get("coverUrl") or ""
                        )
                    time.sleep(1.0)
            except OSError:
                pass
            finally:
                try: ipc.close()
                except Exception: pass

        is_watched = bool(eof_reached or (duration > 0 and position >= duration * 0.90))
        final_pos = 0.0 if is_watched else position
        cache.update_progress(
            metadata["id"],
            metadata.get("episode_id") or metadata.get("episode", ""),
            final_pos,
            duration,
            is_watched,
            title=metadata.get("title"),
            episode=metadata.get("episode"),
            cover=metadata.get("cover") or metadata.get("coverUrl") or ""
        )

        try: os.unlink(socket_path)
        except OSError: pass

        with playback_lock:
            active_players = max(0, active_players - 1)

        settings = cache.settings()
        if is_watched and settings.get("auto_next"):
            try:
                play_next_episode(metadata)
            except Exception as e:
                log.warning("Auto-next episode trigger failed for %s: %s", metadata, e)

    threading.Thread(target=run, daemon=True, name="hakucho-mpv-progress").start()


def play_next_episode(metadata):
    aid = str(metadata.get("id", ""))
    cur_ep_num = str(metadata.get("episode", ""))
    if not aid or not cur_ep_num:
        return
    eps = call_cached("episodes:" + aid, 1800, "episodes", aid)
    if not eps:
        return
    next_ep = None
    for i, ep in enumerate(eps):
        if str(ep.get("number")) == cur_ep_num or str(ep.get("id")) == str(metadata.get("episode_id")) or str(ep.get("number")).lstrip("0") == cur_ep_num.lstrip("0"):
            if i + 1 < len(eps):
                next_ep = eps[i + 1]
            break
    if not next_ep:
        log.info("Auto-next: reached end of episode list for %s", aid)
        return

    lang = str(metadata.get("language") or "sub")
    provider_name = str(metadata.get("provider") or "")
    if not provider_name:
        p, _ = provider_for_id(aid)
        provider_name = p.name

    servers = next_ep.get("servers") or []
    matching_same_lang = [s for s in servers if s.get("language") == lang]
    other_lang = [s for s in servers if s.get("language") != lang]
    sorted_servers = matching_same_lang + other_lang

    sources = None
    chosen_server = None
    for s in sorted_servers:
        s_id = str(s.get("episode_id") or next_ep.get("id", ""))
        s_prov = str(s.get("provider", provider_name))
        s_lang = str(s.get("language", lang))
        try:
            res = provider_call("sources", s_prov, s_id, s_lang)
            if res:
                sources = res
                chosen_server = (s_prov, s_id, s_lang)
                break
        except Exception as ex:
            log.debug("Auto-next candidate server %s failed: %s", s_id, ex)

    if not sources:
        candidate_id = str(next_ep.get("id", ""))
        for candidate_lang in (lang, "sub" if lang == "dub" else "dub"):
            try:
                res = provider_call("sources", provider_name, candidate_id, candidate_lang)
                if res:
                    sources = res
                    chosen_server = (provider_name, candidate_id, candidate_lang)
                    break
            except Exception:
                pass

    if not sources or not chosen_server:
        log.warning("Auto-next: no working sources found for %s ep %s", aid, next_ep.get("number"))
        return

    provider_name, next_ep_id, lang = chosen_server
    from providers.hianime import _safe_url

    user_settings = cache.settings()
    pref_quality = user_settings.get("default_quality", "Auto")
    source = next((s for s in sources if str(s.get("quality", "")) == pref_quality), None) if pref_quality != "Auto" else None
    source = source or sources[0]

    url = str(source.get("url") or source.get("resourceLink") or source.get("link") or "")
    if not url or not _safe_url(url):
        return

    args = ["mpv", "--force-window=immediate", "--cache=yes", "--demuxer-max-bytes=50M"]
    if user_settings.get("fullscreen_player"):
        args.append("--fullscreen")
    referer = str(source.get("referer", ""))
    if referer and _safe_url(referer):
        args.append("--referrer=" + referer)
    subtitle = str(source.get("subtitle", ""))
    sub_pref = user_settings.get("preferred_subtitle", "en")
    if subtitle and sub_pref != "none" and _safe_url(subtitle):
        args.append("--sub-file=" + subtitle)

    playback_id = uuid.uuid4().hex
    socket_dir = DATA.parent / "mpv"
    socket_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    socket_path = str(socket_dir / (playback_id + ".sock"))
    args.append("--input-ipc-server=" + socket_path)
    args.append(url)

    next_metadata = {
        "id": aid,
        "title": str(metadata.get("title", "")),
        "episode": str(next_ep.get("number", "")),
        "episode_id": next_ep_id,
        "provider": provider_name,
        "language": lang,
        "canonical_id": metadata.get("canonical_id")
    }

    cache.touch(aid, str(metadata.get("title", ""))[:300], str(next_ep.get("number", "")), next_ep_id,
                provider_name, metadata.get("canonical_id"), 0, 0)

    if shutil.which("notify-send"):
        try:
            subprocess.Popen(["notify-send", "Hakuchō",
                              f"Autoplay next: {next_metadata['title']} — Episode {next_metadata['episode']}"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            pass

    log.info("Auto-next: starting mpv for %s Episode %s", next_metadata["title"], next_metadata["episode"])
    proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    monitor_playback(proc, socket_path, next_metadata)


def provider_for_id(anime_id):
    aid = str(anime_id)
    if aid.startswith("hiyori:"): name, raw = "hiyori", aid.split(":", 1)[1]
    elif aid.startswith("hianime:"): name, raw = "hianime", aid.split(":", 1)[1]
    elif len(providers) == 1: name, raw = providers[0].name, aid
    elif re.fullmatch(r"[1-9][0-9]{0,9}", aid): name, raw = "hiyori", aid
    elif re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*-[0-9]+", aid): name, raw = "hianime", aid
    else: name, raw = "", aid
    for p in providers:
        if p.name == name or (not name and len(providers) == 1):
            return p, raw
    raise ProviderError(f"Provider reference is unavailable: {name or aid}", "provider_unavailable")


def provider_call(method, *args):
    # Source references are provider-owned. Never probe the registry: doing so can
    # accidentally hand an exact Hiyori watch path to an unrelated adapter.
    if method == "sources":
        provider_name, episode_id, language = args
        p = next((item for item in providers if item.name == str(provider_name)), None)
        if not p:
            raise ProviderError(f"Provider reference is unavailable: {provider_name}", "provider_unavailable")
        if DEBUG_SOURCES:
            log.debug("source request provider=%s exact_episode_id=%s language=%s", p.name, episode_id, language)
        return p.sources(str(episode_id), language)
    if method in ("trending", "discover"):
        pref = getattr(cache, "get_setting", lambda *a, **kw: "all")("preferred_provider", "all")
        active = [p for p in providers if p.name == pref] if pref in ("hianime", "hiyori") else sorted(providers, key=lambda p: 0 if p.name == "hiyori" else 1)
        for p in active:
            if hasattr(p, "trending"):
                try:
                    items = p.trending(*args)
                    if items:
                        attach_covers(items)
                        return items
                except Exception as e:
                    log.warning("provider=%s trending failed: %s", p.name, e)
        return []
    if method == "genre":
        genre_name = args[0] if args else ""
        pref = getattr(cache, "get_setting", lambda *a, **kw: "all")("preferred_provider", "all")
        active = [p for p in providers if p.name == pref] if pref in ("hianime", "hiyori") else sorted(providers, key=lambda p: 0 if p.name == "hiyori" else 1)
        for p in active:
            if hasattr(p, "genre"):
                try:
                    items = p.genre(genre_name)
                    if items:
                        attach_covers(items)
                        return items
                except Exception as e:
                    log.warning("provider=%s genre failed: %s", p.name, e)
        return []
    if method == "search":
        found, errors = [], []
        pref = getattr(cache, "get_setting", lambda *a, **kw: "all")("preferred_provider", "all")
        active_providers = [p for p in providers if p.name == pref] if pref in ("hianime", "hiyori") else sorted(providers, key=lambda p: 0 if p.name == "hiyori" else 1)
        pool = ThreadPoolExecutor(max_workers=max(1, len(active_providers)))
        try:
            tasks = {pool.submit(getattr(p, method), *args): p for p in active_providers}
            try:
                for task in as_completed(tasks, timeout=8.0):
                    p = tasks[task]
                    try:
                        res = task.result()
                        if res:
                            found.extend(res)
                            if len(found) >= 10:
                                break
                    except ProviderError as e:
                        errors.append(e); log.warning("provider=%s method=%s kind=%s: %s", p.name, method, e.kind, e)
                    except Exception as e:
                        log.exception("provider=%s search failed", p.name); errors.append(ProviderError(str(e), "provider_unavailable"))
            except (TimeoutError, FutureTimeoutError):
                pass
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        if found:
            attach_covers(found)
            return found
        auto_fallback = getattr(cache, "get_setting", lambda *a, **kw: False)("auto_fallback", False)
        if auto_fallback and pref in ("hianime", "hiyori") and len(active_providers) < len(providers):
            fallback_providers = [p for p in providers if p.name != pref]
            fpool = ThreadPoolExecutor(max_workers=max(1, len(fallback_providers)))
            try:
                tasks = {fpool.submit(getattr(p, method), *args): p for p in fallback_providers}
                try:
                    for task in as_completed(tasks, timeout=8.0):
                        p = tasks[task]
                        try:
                            res = task.result()
                            if res: found.extend(res)
                        except Exception: pass
                except (TimeoutError, FutureTimeoutError):
                    pass
            finally:
                fpool.shutdown(wait=False, cancel_futures=True)
            if found:
                attach_covers(found)
                return found
        if errors: raise errors[0]
        return []
    if method in ("details", "episodes"):
        p, raw = provider_for_id(args[0])
        result = getattr(p, method)(raw, *args[1:])
        if method == "details" and isinstance(result, dict):
            attach_cover_cache(result)
        if method == "episodes" and p.name == "hianime":
            pref = getattr(cache, "get_setting", lambda *a, **kw: "all")("preferred_provider", "all")
            if pref == "all":
                try:
                    detail = cache.get("details:" + str(args[0]))
                    if detail and isinstance(detail, dict) and detail.get("mal_id"):
                        canonical = "mal:" + str(detail["mal_id"])
                        refs = cache.provider_refs(canonical) if hasattr(cache, "provider_refs") else {}
                        ref = refs.get("hiyori")
                        if ref:
                            extra = cache.get("episodes:" + ref)
                            if extra and isinstance(extra, list):
                                by_number = {str(item["number"]): item for item in extra}
                                for episode in result:
                                    match = by_number.get(str(episode["number"]))
                                    if match and match.get("servers"):
                                        episode["servers"] = (episode.get("servers") or []) + match["servers"]
                except Exception as e:
                    log.debug("cross-provider mapping cache lookup: %s", e)
        return result
    errors = []
    for p in providers:
        try:
            return getattr(p, method)(*args)
        except ProviderError as e:
            errors.append(e)
            log.warning("provider=%s method=%s kind=%s: %s", p.name, method, e.kind, e)
    if errors: raise errors[-1]
    raise ProviderError("No providers are configured")


def call_cached(key, ttl, method, *args):
    if method != "sources":
        value = cache.get(key)
        if value is not None: return value
    value = provider_call(method, *args)
    if method != "sources": cache.put(key, value, ttl)
    return value


def handle_get(path, qs):
    if path == "/health": return {"ok": True, "providers": [p.name for p in providers], "download_server": True, "download_manager": 4}
    if path == "/playback_status": return {"ok": True, "active_players": active_players}
    if path == "/search":
        q = qs.get("q", [""])[0].strip()
        if not q or len(q) > 120: raise ProviderError("Query must be 1–120 characters", "invalid_input")
        pref = getattr(cache, "get_setting", lambda *a, **kw: "all")("preferred_provider", "all")
        items = call_cached(f"search:{pref}:{q.casefold()}", 3600, "search", q)
        attach_covers(items)
        return {"ok": True, "items": items, **({"kind": "no_results"} if not items else {})}
    m = re.fullmatch(r"/anime/([^/]+)(/episodes)?", path)
    if m:
        aid = m.group(1)
        if m.group(2):
            items = call_cached("episodes:" + aid, 1800, "episodes", aid)
            if hasattr(cache, "episode_status"):
                detail = cache.get("details:" + aid) or {}
                canonical = ("mal:" + str(detail["mal_id"])) if isinstance(detail, dict) and detail.get("mal_id") else ("anilist:" + str(detail["anilist_id"])) if isinstance(detail, dict) and detail.get("anilist_id") else None
                statuses = cache.episode_status(aid, canonical)
                for ep in items:
                    status = statuses.get(str(ep.get("id"))) or statuses.get(str(ep.get("number")))
                    if not status:
                        status = next((statuses.get(str(option.get("episode_id"))) for option in ep.get("servers", []) if statuses.get(str(option.get("episode_id")))), None)
                    ep["watch_status"] = (status or {}).get("status", "unwatched")
                    if status:
                        ep["watch_position"] = status.get("position", 0)
                        ep["watch_duration"] = status.get("duration", 0)
                    else:
                        ep["watch_position"] = 0
                        ep["watch_duration"] = 0
            return {"ok": True, "items": items}
        val = call_cached("details:" + aid, 86400, "details", aid)
        if isinstance(val, dict): attach_cover_cache(val)
        return {"ok": True, "value": val}
    m = re.fullmatch(r"/episode/([0-9]+)/sources", path)
    if m:
        lang = qs.get("language", ["sub"])[0]
        if lang not in ("sub", "dub"): raise ProviderError("language must be sub or dub", "invalid_input")
        provider_name = qs.get("provider", [""])[0]
        if not provider_name: raise ProviderError("A provider is required for this episode reference", "invalid_input")
        return {"ok": True, "items": provider_call("sources", provider_name, m.group(1), lang)}
    m = re.fullmatch(r"/watch/([A-Za-z0-9_-]+)/([0-9]+)/(sub|dub)/([A-Za-z0-9._~-]+)", path)
    if m:
        return {"ok": True, "items": provider_call("sources", "hiyori", path.lstrip("/"), m.group(3))}
    if path == "/recent":
        items = cache.recent_items()
        attach_covers(items)
        return {"ok": True, "items": items}
    if path == "/history":
        items = cache.history()
        attach_covers(items)
        return {"ok": True, "items": items}
    if path == "/favorites":
        items = cache.favorites()
        for f in items:
            meta = f.get("metadata") or {}
            f["cover"] = meta.get("cover") or meta.get("coverUrl") or ""
        attach_covers(items)
        return {"ok": True, "items": items}
    raise ProviderError("Route not found", "not_found")


def handle_compat(req):
    """JSON bridge used by the existing QML; every operation still passes through providers."""
    cmd = req.get("cmd")
    if cmd == "ping": return {"ok": True, "pong": True}
    if cmd == "playback_status": return {"ok": True, "active_players": active_players}
    if cmd in ("search", "suggest"):
        q = str(req.get("q", "")).strip()
        if len(q) < 2: return {"ok": True, "items": [], "suggestions": []}
        pref = getattr(cache, "get_setting", lambda *a, **kw: "all")("preferred_provider", "all")
        items = call_cached(f"search:{pref}:{q.casefold()}", 3600, "search", q)
        attach_covers(items)
        return {"ok": True, "items": items, "suggestions": [{"id": x["id"], "name": x["title"], "cover": x.get("coverPath") or x.get("cover", "")} for x in items[:8]], **({"kind": "no_results"} if not items else {})}
    if cmd == "details":
        aid = str(req.get("id", "")); val = call_cached("details:" + aid, 86400, "details", aid)
        if isinstance(val, dict): attach_cover_cache(val)
        return {"ok": True, "value": val}
    if cmd == "episodes":
        aid = str(req.get("id", "")); items = call_cached("episodes:" + aid, 1800, "episodes", aid)
        if hasattr(cache, "episode_status"):
            detail = cache.get("details:" + aid) or {}
            canonical = ("mal:" + str(detail["mal_id"])) if isinstance(detail, dict) and detail.get("mal_id") else ("anilist:" + str(detail["anilist_id"])) if isinstance(detail, dict) and detail.get("anilist_id") else None
            statuses = cache.episode_status(aid, canonical)
            for ep in items:
                status = statuses.get(str(ep.get("id"))) or statuses.get(str(ep.get("number")))
                if not status:
                    status = next((statuses.get(str(option.get("episode_id"))) for option in ep.get("servers", []) if statuses.get(str(option.get("episode_id")))), None)
                ep["watch_status"] = (status or {}).get("status", "unwatched")
                if status:
                    ep["watch_position"] = status.get("position", 0)
                    ep["watch_duration"] = status.get("duration", 0)
                else:
                    ep["watch_position"] = 0
                    ep["watch_duration"] = 0
        return {"ok": True, "items": items}
    if cmd == "streams":
        aid, number = str(req.get("id", "")), str(req.get("episode", ""))
        requested_episode_id = str(req.get("episode_id", ""))
        if not requested_episode_id:
            raise ProviderError("The selected episode has no provider episode reference", "episode_unavailable")
        eps = call_cached("episodes:" + aid, 1800, "episodes", aid)
        ep = next((e for e in eps if e["number"] == number), None)
        if not ep: raise ProviderError(f"Episode {number} is not available", "episode_unavailable")
        lang = req.get("mode", "sub")
        if lang not in ("sub", "dub"): raise ProviderError("mode must be sub or dub", "invalid_input")
        server = req.get("server") or {}
        if isinstance(server, dict) and server.get("provider"):
            if server.get("language") != lang:
                raise ProviderError("The selected server does not provide that language", "episode_unavailable")
            # Require the option to belong to this exact episode; this guards stale or
            # mismatched UI state while preserving Hiyori's exact watch reference.
            if server not in ep.get("servers", []):
                raise ProviderError("Selected server is not available for this episode", "episode_unavailable")
            exact_id = str(server["episode_id"])
            if requested_episode_id and requested_episode_id != exact_id:
                raise ProviderError("Selected episode reference does not match the selected server", "episode_unavailable")
            provider_name = str(server["provider"])
            server_name = str(server.get("server", ""))
        else:
            p, _raw = provider_for_id(aid)
            provider_name = p.name
            server_name = "provider default"
            exact_id = str(ep.get("id", ""))
            if requested_episode_id and requested_episode_id != exact_id:
                raise ProviderError("Selected episode reference does not match the requested episode", "episode_unavailable")
        if not exact_id:
            raise ProviderError("Selected episode has no provider episode reference", "episode_unavailable")
        if DEBUG_SOURCES:
            detail = cache.get("details:" + aid) or {}
            canonical_id = ("mal:" + str(detail.get("mal_id"))) if detail.get("mal_id") else ("anilist:" + str(detail.get("anilist_id"))) if detail.get("anilist_id") else "unknown"
            _selected_provider, provider_anime_id = provider_for_id(aid)
            log.debug("received selected episode number=%s canonical_id=%s provider=%s provider_anime_id=%s requested_episode_id=%s provider_episode_id=%s language=%s server=%s", number, canonical_id, provider_name, provider_anime_id, requested_episode_id, exact_id, lang, server_name)
        cache_key = f"sources:{provider_name}:{exact_id}:{lang}"
        sources = cache.get(cache_key)
        if sources is None:
            sources = provider_call("sources", provider_name, exact_id, lang)
            if sources:
                cache.put(cache_key, sources, 900)
        if DEBUG_SOURCES:
            for source in sources:
                log.debug("source response provider=%s episode_id=%s server=%s language=%s quality=%s stream_url=%s", provider_name, exact_id, server_name, lang, source.get("quality"), source.get("url") or source.get("resourceLink"))
        return {"ok": True, "items": issue_source_tokens(sources)}
    if cmd == "homepage":
        if req.get("kind") == "recent":
            recent = cache.recent_items()
            attach_covers(recent)
            return {"ok": True, "items": recent, "kind": "recent"}
        pref = getattr(cache, "get_setting", lambda *a, **kw: "all")("preferred_provider", "all")
        trending = call_cached(f"discover:trending:{pref}", 3600, "trending")
        attach_covers(trending)
        return {"ok": True, "items": trending, "kind": "discover"}
    if cmd in ("trending", "discover"):
        pref = getattr(cache, "get_setting", lambda *a, **kw: "all")("preferred_provider", "all")
        items = call_cached(f"discover:trending:{pref}", 3600, "trending")
        attach_covers(items)
        return {"ok": True, "items": items, "kind": "discover"}
    if cmd == "search_genre":
        pref = getattr(cache, "get_setting", lambda *a, **kw: "all")("preferred_provider", "all")
        genre_name = str(req.get("genre") or req.get("q") or "").strip()
        if not genre_name or genre_name.lower() == "all":
            items = call_cached(f"discover:trending:{pref}", 3600, "trending")
        else:
            items = call_cached(f"genre:{pref}:{genre_name.casefold()}", 3600, "genre", genre_name)
        attach_covers(items)
        return {"ok": True, "items": items, "kind": "genre"}
    if cmd == "favorites":
        items = cache.favorites()
        for f in items:
            meta = f.get("metadata") or {}
            f["cover"] = meta.get("cover") or meta.get("coverUrl") or ""
        attach_covers(items)
        return {"ok": True, "items": items}
    if cmd == "history":
        items = cache.history()
        attach_covers(items)
        return {"ok": True, "items": items}
    if cmd == "clear_history": cache.clear_history(); return {"ok": True}
    if cmd == "remove_history": cache.remove_history(str(req.get("id", "")), str(req.get("episode_id", ""))); return {"ok": True}
    if cmd == "settings": return {"ok": True, "value": cache.settings()}
    if cmd == "status":
        return {"ok": True, "backend": "running", "download": downloads.status(),
                "providers": [p.name for p in providers], "version": VERSION,
                "cache_bytes": cache.cache_size(),
                "log_path": str(Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "hakucho" / "backend.log")}
    if cmd == "clear_cache": cache.clear_cache(); return {"ok": True, "cache_bytes": 0}
    if cmd == "clear_download_history": downloads.clear_history(); return {"ok": True}
    if cmd == "download_control":
        try: return {"ok": True, "job": downloads.control(str(req.get("id", "")), str(req.get("action", "")))}
        except ValueError as e: raise ProviderError(str(e), "invalid_input") from e
    if cmd == "download_delete":
        try: downloads.delete(str(req.get("id", "")))
        except (ValueError, OSError) as e: raise ProviderError(str(e), "invalid_input") from e
        return {"ok": True}
    if cmd == "download_retry":
        job = next((item for item in downloads.list_jobs() if item.get("id") == str(req.get("id", ""))), None)
        if not job or not job.get("provider") or not job.get("episode_id"):
            raise ProviderError("Retry needs an existing provider episode reference", "download_failure")
        try:
            sources = provider_call("sources", job["provider"], job["episode_id"], job.get("language", "sub"))
            preferred = job.get("quality", "Auto")
            source = next((s for s in sources if str(s.get("quality", "")) == preferred), None) if preferred != "Auto" else None
            source = source or next(iter(sources), None)
            if not source:
                raise ProviderError("Provider returned no download sources", "no_stream")
            result = downloads.retry(str(req.get("id", "")), source.get("url") or source.get("resourceLink") or source.get("link", ""), source.get("referer", ""))
        except (ValueError, RuntimeError) as e:
            raise ProviderError(str(e), "download_failure") from e
        return {"ok": True, "job": result}
    if cmd == "downloads": return {"ok": True, "items": downloads.list_jobs(), "destination": str(downloads.DOWNLOAD_DIR), "download_status": downloads.status()}
    if cmd == "download":
        source = live_sources.pop(str(req.get("source_id", "")), None)
        if not source or source["expires"] < time.time():
            raise ProviderError("Stream source expired; reload sources", "stream_url_invalid")
        try:
            metadata = req.get("metadata") if isinstance(req.get("metadata"), dict) else {}
            metadata.update({"cover": req.get("cover", metadata.get("cover", "")),
                             "provider": req.get("provider", metadata.get("provider", "")),
                             "episode_id": req.get("episode_id", metadata.get("episode_id", "")),
                             "language": req.get("language", metadata.get("language", "sub")),
                             "quality": req.get("quality", metadata.get("quality", "Auto")),
                             "anime_id": req.get("anime_id", metadata.get("anime_id", ""))})
            job = downloads.create(source.get("url", ""), str(req.get("title", "Anime")),
                                   str(req.get("episode", "1")), source.get("referer", ""), metadata)
        except (ValueError, RuntimeError) as e:
            raise ProviderError(str(e), "download_failure") from e
        return {"ok": True, "job": job}
    if cmd == "set_setting":
        key = str(req.get("key", ""))
        allowed_keys = (
            "preferred_provider", "auto_fallback", "resume_playback", "default_language", "default_quality",
            "preferred_subtitle", "auto_next", "fullscreen_player", "compact_episodes",
            "show_covers", "notify_playback_errors", "download_location",
            "download_max_simultaneous", "download_default_quality",
            "notify_download_completed", "notify_download_failed"
        )
        if key not in allowed_keys:
            raise ProviderError("Unknown setting", "invalid_input")
        value = req.get("value")
        if key in ("auto_fallback", "resume_playback", "auto_next", "fullscreen_player",
                   "compact_episodes", "show_covers", "notify_playback_errors",
                   "notify_download_completed", "notify_download_failed"):
            if not isinstance(value, bool): raise ProviderError("Setting value must be boolean", "invalid_input")
        elif key == "preferred_provider" and value not in ("all", "hianime", "hiyori"):
            raise ProviderError("Preferred provider must be all, hianime, or hiyori", "invalid_input")
        elif key == "default_language" and value not in ("sub", "dub"):
            raise ProviderError("Preferred language must be sub or dub", "invalid_input")
        elif key in ("default_quality", "download_default_quality") and value not in ("Auto", "1080p", "720p", "480p"):
            raise ProviderError("Unsupported quality", "invalid_input")
        elif key == "preferred_subtitle" and value not in ("en", "none", "auto"):
            raise ProviderError("Unsupported subtitle setting", "invalid_input")
        elif key == "download_max_simultaneous":
            try: value = int(value); downloads.set_limit(value)
            except (TypeError, ValueError) as e: raise ProviderError(str(e), "invalid_input") from e
        elif key == "download_location":
            try: value = downloads.set_download_dir(value)
            except (TypeError, ValueError, OSError) as e: raise ProviderError(str(e), "invalid_input") from e
        cache.set_setting(key, value)
        if key == "preferred_provider" and hasattr(cache, "clear_cache"):
            cache.clear_cache()
        return {"ok": True, "value": value}
    if cmd == "reset_settings":
        cache.clear_settings()
        if hasattr(cache, "clear_cache"):
            cache.clear_cache()
        downloads.set_download_dir(str(Path.home() / "Videos" / "Hakuchō"))
        downloads.set_limit(2)
        return {"ok": True, "value": {"preferred_provider": "all", "resume_playback": True, "default_language": "sub",
                                        "auto_fallback": False, "default_quality": "Auto",
                                        "preferred_subtitle": "en", "auto_next": False,
                                        "fullscreen_player": False, "compact_episodes": False,
                                        "show_covers": True, "notify_playback_errors": True,
                                        "download_location": str(downloads.DOWNLOAD_DIR),
                                        "download_max_simultaneous": 2, "download_default_quality": "Auto",
                                        "notify_download_completed": True, "notify_download_failed": True}}
    if cmd == "favorite":
        aid = str(req.get("id", "")); title = str(req.get("title", ""))[:300]
        if not aid or len(aid) > 200 or not title: raise ProviderError("Favorite needs a valid anime ID and title", "invalid_input")
        return {"ok": True, "favorite": cache.toggle_favorite(aid, title, req.get("canonical_id"), req.get("metadata"))}
    raise ProviderError("Unknown operation", "invalid_input")


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, value):
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.send_header("Access-Control-Allow-Origin", "http://localhost"); self.end_headers(); self.wfile.write(data)

    def do_GET(self):
        try:
            u = urlparse(self.path)
            if u.path == "/cover":
                qs = parse_qs(u.query)
                raw_url = qs.get("url", [""])[0].strip()
                if not raw_url:
                    self._send(400, {"ok": False, "error": "url parameter required"})
                    return
                h = hashlib.md5(raw_url.encode("utf-8")).hexdigest()
                dest = COVER_CACHE_DIR / f"{h}.jpg"
                if not dest.exists() or dest.stat().st_size == 0:
                    _download_cover(raw_url, dest)
                if dest.exists() and dest.stat().st_size > 0:
                    data = dest.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "public, max-age=31536000")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self._send(404, {"ok": False, "error": "Image not found"})
                return
            value = handle_get(u.path, parse_qs(u.query)); self._send(200, value)
        except ProviderError as e:
            code = 404 if e.kind == "not_found" else 400 if e.kind == "invalid_input" else 502
            log.warning("request=%s kind=%s error=%s", self.path, e.kind, e); self._send(code, {"ok": False, "error": str(e), "kind": e.kind})
        except Exception:
            log.exception("Unhandled GET error for %s", self.path); self._send(500, {"ok": False, "error": "Internal backend error", "kind": "internal_error"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 65536: raise ProviderError("Request too large", "invalid_input")
            req = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/ipc": value = handle_compat(req)
            elif self.path == "/play":
                from providers.hianime import _safe_url
                metadata = req.get("metadata") if isinstance(req.get("metadata"), dict) else {}
                source_id = str(req.get("source_id", ""))
                source = live_sources.get(source_id)
                url = str((source or {}).get("url") or "")
                referer = str((source or {}).get("referer", ""))
                subtitle = str((source or {}).get("subtitle", ""))

                # If source token is missing or expired, auto-resolve fresh source from provider using metadata
                if not source or source.get("expires", 0) < time.time() or not _safe_url(url):
                    aid = str(metadata.get("id", ""))
                    ep_id = str(metadata.get("episode_id") or metadata.get("episode", ""))
                    lang = str(metadata.get("language") or "sub")
                    provider_name = str(metadata.get("provider", ""))
                    if not provider_name and aid:
                        p, _ = provider_for_id(aid)
                        provider_name = p.name
                    if aid and ep_id and provider_name:
                        try:
                            sources = provider_call("sources", provider_name, ep_id, lang)
                            if not sources and lang == "dub":
                                sources = provider_call("sources", provider_name, ep_id, "sub")
                                lang = "sub"
                            if sources:
                                user_settings = cache.settings()
                                pref_quality = user_settings.get("default_quality", "Auto")
                                s_match = next((s for s in sources if str(s.get("quality", "")) == pref_quality), None) if pref_quality != "Auto" else None
                                s_match = s_match or sources[0]
                                url = str(s_match.get("url") or s_match.get("resourceLink") or s_match.get("link") or "")
                                referer = str(s_match.get("referer", ""))
                                subs = s_match.get("subtitles") or []
                                first = subs[0] if subs else {}
                                subtitle = first.get("url") or first.get("file") or "" if isinstance(first, dict) else first if isinstance(first, str) else ""
                        except Exception as ex:
                            log.warning("Failed to auto-resolve source on /play: %s", ex)

                if not url or not _safe_url(url):
                    raise ProviderError("Could not retrieve a valid stream source; please re-select the episode", "stream_url_invalid")

                args = ["mpv", "--force-window=immediate", "--cache=yes", "--demuxer-max-bytes=50M"]
                user_settings = cache.settings()
                if user_settings.get("fullscreen_player"):
                    args.append("--fullscreen")
                sub_pref = user_settings.get("preferred_subtitle", "en")
                if referer and _safe_url(referer):
                    args.append("--referrer=" + referer)
                if subtitle and sub_pref != "none" and _safe_url(subtitle):
                    args.append("--sub-file=" + subtitle)
                try: resume=max(0.0,min(float(req.get("resume_position",0)),86400.0))
                except (TypeError,ValueError): raise ProviderError("Invalid resume position", "invalid_input")
                if resume: args.append("--start=" + str(resume))
                playback_id=uuid.uuid4().hex
                socket_dir=DATA.parent/"mpv"; socket_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
                socket_path=str(socket_dir/(playback_id+".sock"))
                args.append("--input-ipc-server="+socket_path)
                args.append(url)
                if DEBUG_SOURCES:
                    log.debug("starting mpv provider=%s anime_id=%s episode=%s episode_id=%s argv=%r", metadata.get("provider"), metadata.get("id"), metadata.get("episode"), metadata.get("episode_id"), args)
                if metadata.get("id") and metadata.get("episode_id"):
                    cov = resolve_cover_for_item({"id": metadata.get("id"), "cover": metadata.get("cover") or metadata.get("coverUrl") or ""})
                    cache.touch(str(metadata["id"]), str(metadata.get("title", ""))[:300], str(metadata.get("episode", "")),
                                str(metadata["episode_id"]), str(metadata.get("provider", "")), metadata.get("canonical_id"),
                                resume, 0, cover=cov)
                try:
                    process=subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                    if metadata.get("id"):
                        monitor_playback(process, socket_path, metadata)
                except OSError as e: raise ProviderError(f"Could not start mpv: {e}", "mpv_failure")
                value = {"ok": True}
            elif self.path == "/watched":
                cache.touch(str(req.get("id", "")), str(req.get("title", "")), str(req.get("episode", ""))); value = {"ok": True}
            else: raise ProviderError("Route not found", "not_found")
            self._send(200, value)
        except (ProviderError, json.JSONDecodeError, ValueError) as e:
            kind = e.kind if isinstance(e, ProviderError) else "invalid_input"; code = 404 if kind == "not_found" else 400 if kind == "invalid_input" else 502
            self._send(code, {"ok": False, "error": str(e), "kind": kind})
        except Exception:
            log.exception("Unhandled POST error for %s", self.path); self._send(500, {"ok": False, "error": "Internal backend error", "kind": "internal_error"})

    def log_message(self, fmt, *args): log.info("%s - %s", self.address_string(), fmt % args)


def _backfill_covers():
    try:
        with cache._lock:
            missing_recent = cache.db.execute("SELECT DISTINCT anime_id, title FROM recent WHERE cover IS NULL OR cover = '' OR cover LIKE '{%'").fetchall()
            missing_hist = cache.db.execute("SELECT DISTINCT anime_id, title FROM watch_history WHERE cover IS NULL OR cover = '' OR cover LIKE '{%'").fetchall()
        all_missing = set(missing_recent + missing_hist)
        for aid, title in all_missing:
            try:
                cov = resolve_cover_for_item({"id": aid, "title": title})
                if cov:
                    cache.update_cover(aid, cov)
                    h = hashlib.md5(cov.encode("utf-8")).hexdigest()
                    dest = COVER_CACHE_DIR / f"{h}.jpg"
                    _download_cover(cov, dest)
            except Exception:
                pass
    except Exception:
        pass


def main():
    threading.Thread(target=_backfill_covers, daemon=True, name="hakucho-backfill-covers").start()
    host = os.environ.get("HAKUCHO_HOST") or os.environ.get("ANIMECHY_HOST", "127.0.0.1")
    port = int(os.environ.get("HAKUCHO_PORT") or os.environ.get("ANIMECHY_PORT", "8765"))
    server = ThreadingHTTPServer((host, port), Handler)
    log.info("backend listening at http://%s:%d; cache=%s", host, port, DATA)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == "__main__": main()
