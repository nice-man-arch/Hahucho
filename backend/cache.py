import json
import sqlite3
import threading
import time
from pathlib import Path


def _clean_cover(val):
    if not val:
        return ""
    if isinstance(val, dict):
        return str(val.get("url") or val.get("extraLarge") or val.get("large") or val.get("medium") or "")
    val = str(val).strip()
    if val.startswith("{") and ("'url':" in val or '"url":' in val):
        try:
            val_json = json.loads(val.replace("'", '"'))
            if isinstance(val_json, dict):
                return str(val_json.get("url") or "")
        except Exception:
            pass
    if val.startswith("http://") or val.startswith("https://") or val.startswith("file://"):
        return val
    return ""


class Cache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.RLock()
        self._mem = {}
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
        self.db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, expires REAL NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS recent (anime_id TEXT PRIMARY KEY, title TEXT NOT NULL, episode TEXT NOT NULL, watched REAL NOT NULL, cover TEXT DEFAULT '')")
        self.db.execute("CREATE TABLE IF NOT EXISTS watch_history (anime_id TEXT NOT NULL, canonical_id TEXT, title TEXT NOT NULL, episode_id TEXT NOT NULL, episode TEXT NOT NULL, provider TEXT NOT NULL DEFAULT '', position REAL NOT NULL DEFAULT 0, duration REAL NOT NULL DEFAULT 0, is_watched INTEGER NOT NULL DEFAULT 0, watched REAL NOT NULL, cover TEXT DEFAULT '', PRIMARY KEY(anime_id, episode_id))")
        try:
            self.db.execute("ALTER TABLE recent ADD COLUMN cover TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            self.db.execute("ALTER TABLE watch_history ADD COLUMN cover TEXT DEFAULT ''")
        except Exception:
            pass
        self.db.execute("CREATE TABLE IF NOT EXISTS favorites (anime_id TEXT PRIMARY KEY, canonical_id TEXT, title TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}', added REAL NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS provider_references (canonical_id TEXT NOT NULL, provider TEXT NOT NULL, provider_id TEXT NOT NULL, added REAL NOT NULL, PRIMARY KEY(canonical_id,provider))")
        self.db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS download_jobs (id TEXT PRIMARY KEY, value TEXT NOT NULL, updated REAL NOT NULL)")
        try:
            self.db.execute("UPDATE watch_history SET position=0.0 WHERE is_watched=1")
            self.db.execute("UPDATE watch_history SET is_watched=1, position=0.0 WHERE (anime_id, episode) IN (SELECT anime_id, episode FROM watch_history WHERE is_watched=1)")
        except Exception:
            pass
        self.db.commit()

    def get(self, key):
        now = time.time()
        mem_item = self._mem.get(key)
        if mem_item:
            val, exp = mem_item
            if exp >= now:
                return val
            self._mem.pop(key, None)
        row = self.db.execute("SELECT value, expires FROM cache WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        if row[1] < now:
            self.db.execute("DELETE FROM cache WHERE key=?", (key,)); self.db.commit()
            return None
        try:
            val = json.loads(row[0])
            self._mem[key] = (val, row[1])
            return val
        except ValueError:
            return None

    def put(self, key, value, ttl):
        exp = time.time() + ttl
        self._mem[key] = (value, exp)
        self.db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)", (key, json.dumps(value), exp))
        self.db.commit()

    def touch(self, anime_id, title, episode, episode_id=None, provider="", canonical_id=None, position=0, duration=0, is_watched=False, cover=""):
        with self._lock:
            cov = _clean_cover(cover)
            if not cov:
                existing = self.db.execute("SELECT cover FROM recent WHERE anime_id=? AND cover != '' LIMIT 1", (str(anime_id),)).fetchone()
                if existing and existing[0]:
                    cov = _clean_cover(existing[0])
                else:
                    existing = self.db.execute("SELECT cover FROM watch_history WHERE anime_id=? AND cover != '' LIMIT 1", (str(anime_id),)).fetchone()
                    if existing and existing[0]:
                        cov = _clean_cover(existing[0])
            self.db.execute("INSERT OR REPLACE INTO recent(anime_id,title,episode,watched,cover) VALUES (?,?,?,?,?)",
                            (str(anime_id), str(title), str(episode), time.time(), cov))
            episode_id = str(episode_id or episode)
            self.db.execute(
                """INSERT INTO watch_history(anime_id,canonical_id,title,episode_id,episode,provider,position,duration,is_watched,watched,cover)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(anime_id,episode_id) DO UPDATE SET
                       canonical_id=COALESCE(excluded.canonical_id, watch_history.canonical_id),
                       title=excluded.title,
                       episode=excluded.episode,
                       provider=CASE WHEN excluded.provider != '' THEN excluded.provider ELSE watch_history.provider END,
                       position=CASE WHEN watch_history.is_watched = 1 THEN 0.0 WHEN excluded.position > 0 THEN excluded.position ELSE watch_history.position END,
                       duration=CASE WHEN excluded.duration > 0 THEN excluded.duration ELSE watch_history.duration END,
                       is_watched=CASE WHEN excluded.is_watched != 0 THEN excluded.is_watched ELSE watch_history.is_watched END,
                       watched=excluded.watched,
                       cover=CASE WHEN excluded.cover != '' THEN excluded.cover ELSE watch_history.cover END""",
                (str(anime_id), canonical_id, str(title), episode_id, str(episode), str(provider),
                 max(0.0, float(position or 0)), max(0.0, float(duration or 0)), int(bool(is_watched)), time.time(), cov)
            )
            self.db.commit()

    def update_progress(self, anime_id, episode_id, position, duration, is_watched=None, title=None, episode=None, cover=""):
        with self._lock:
            cov = _clean_cover(cover)
            pos = max(0.0, float(position or 0))
            dur = max(0.0, float(duration or 0))
            done = bool(is_watched) if is_watched is not None else bool(dur > 0 and pos >= dur * 0.90)
            if done:
                pos = 0.0

            ep_num = str(episode) if episode is not None else str(episode_id)
            eid = str(episode_id)

            if done:
                cur = self.db.execute(
                    "UPDATE watch_history SET position=0.0,duration=CASE WHEN ?>0 THEN ? ELSE duration END,is_watched=1,watched=?,cover=CASE WHEN ? != '' THEN ? ELSE cover END WHERE anime_id=? AND (episode_id=? OR episode=? OR episode=?)",
                    (dur, dur, time.time(), cov, cov, str(anime_id), eid, ep_num, eid)
                )
            else:
                cur = self.db.execute(
                    "UPDATE watch_history SET position=?,duration=CASE WHEN ?>0 THEN ? ELSE duration END,is_watched=0,watched=?,cover=CASE WHEN ? != '' THEN ? ELSE cover END WHERE anime_id=? AND (episode_id=? OR episode=?)",
                    (pos, dur, dur, time.time(), cov, cov, str(anime_id), eid, ep_num)
                )

            if cur.rowcount == 0 and (title or episode):
                self.db.execute(
                    "INSERT OR REPLACE INTO watch_history(anime_id,canonical_id,title,episode_id,episode,provider,position,duration,is_watched,watched,cover) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (str(anime_id), None, str(title or ""), eid, ep_num, "", pos, dur, int(done), time.time(), cov)
                )
            if cov:
                self.db.execute("UPDATE recent SET watched=?, cover=? WHERE anime_id=?", (time.time(), cov, str(anime_id)))
            else:
                self.db.execute("UPDATE recent SET watched=? WHERE anime_id=?", (time.time(), str(anime_id)))
            self.db.commit()

    def update_cover(self, anime_id, cover):
        cov = _clean_cover(cover)
        if not cov:
            return
        with self._lock:
            self.db.execute("UPDATE recent SET cover=? WHERE anime_id=?", (cov, str(anime_id)))
            self.db.execute("UPDATE watch_history SET cover=? WHERE anime_id=?", (cov, str(anime_id)))
            self.db.commit()

    def recent_items(self):
        with self._lock:
            rows = self.db.execute("SELECT anime_id,title,episode,watched,cover FROM recent ORDER BY watched DESC LIMIT 30").fetchall()
            out=[]
            for a,t,e,w,c in rows:
                progress=self.db.execute("SELECT position,duration,provider,episode_id,canonical_id,is_watched,cover FROM watch_history WHERE anime_id=? AND (episode=? OR episode_id=?) ORDER BY watched DESC LIMIT 1",(a,e,e)).fetchone()
                p,d,provider,eid,cid,done,cov=progress or (0,0,"",e,None,0,"")
                cover = _clean_cover(c or cov or "")
                out.append(dict(id=a,title=t,episode=e,watched=w,position=(0.0 if done else p),duration=d,provider=provider,episode_id=eid,canonical_id=cid,is_watched=bool(done),cover=cover))
            return out

    def history(self):
        with self._lock:
            return [dict(id=a,canonical_id=c,title=t,episode_id=eid,episode=e,provider=p,position=(0.0 if done else pos),duration=dur,is_watched=bool(done),watched=w,cover=_clean_cover(cov))
                    for a,c,t,eid,e,p,pos,dur,done,w,cov in self.db.execute("SELECT anime_id,canonical_id,title,episode_id,episode,provider,position,duration,is_watched,watched,cover FROM watch_history ORDER BY watched DESC LIMIT 100")]

    def episode_status(self, anime_id, canonical_id=None):
        with self._lock:
            rows = self.db.execute(
                "SELECT episode_id,episode,provider,position,duration,is_watched,watched FROM watch_history WHERE anime_id=? OR (? IS NOT NULL AND canonical_id=?) ORDER BY watched ASC",
                (str(anime_id), canonical_id, canonical_id)
            ).fetchall()
            res = {}
            ep_by_num = {}
            for eid, ep, provider, pos, dur, done, w in rows:
                entry = {
                    "episode": str(ep),
                    "provider": provider,
                    "position": 0.0 if done else pos,
                    "duration": dur,
                    "status": "watched" if done else "watching" if pos > 0 else "unwatched"
                }
                if eid:
                    res[str(eid)] = entry
                ep_str = str(ep)
                if ep_str:
                    if ep_str not in ep_by_num or done or ep_by_num[ep_str].get("status") != "watched":
                        ep_by_num[ep_str] = entry

            for ep_str, entry in ep_by_num.items():
                res[ep_str] = entry
            return res

    def remove_history(self, anime_id, episode_id):
        self.db.execute("DELETE FROM watch_history WHERE anime_id=? AND episode_id=?", (anime_id, episode_id))
        self.db.commit()

    def clear_history(self):
        self.db.execute("DELETE FROM watch_history")
        self.db.execute("DELETE FROM recent")
        self.db.commit()

    def toggle_favorite(self, anime_id, title, canonical_id=None, metadata=None):
        found=self.db.execute("SELECT anime_id FROM favorites WHERE anime_id=? OR (? IS NOT NULL AND canonical_id=?) LIMIT 1",(anime_id,canonical_id,canonical_id)).fetchone()
        if found:
            self.db.execute("DELETE FROM favorites WHERE anime_id=?",(found[0],)); added=False
        else:
            self.db.execute("INSERT INTO favorites VALUES (?,?,?,?,?)",(anime_id,canonical_id,title,json.dumps(metadata or {}),time.time())); added=True
        self.db.commit(); return added

    def favorites(self):
        return [dict(id=a,canonical_id=c,title=t,metadata=json.loads(m),added=w) for a,c,t,m,w in self.db.execute("SELECT anime_id,canonical_id,title,metadata,added FROM favorites ORDER BY added DESC")]

    def map_provider(self, canonical_id, provider, provider_id):
        if canonical_id and provider and provider_id:
            self.db.execute("INSERT OR REPLACE INTO provider_references VALUES (?,?,?,?)",(str(canonical_id),str(provider),str(provider_id),time.time())); self.db.commit()

    def provider_refs(self, canonical_id):
        return {provider:provider_id for provider,provider_id in self.db.execute("SELECT provider,provider_id FROM provider_references WHERE canonical_id=?",(str(canonical_id),))}

    def settings(self):
        with self._lock:
            return {key: json.loads(value) for key,value in self.db.execute("SELECT key,value FROM settings")}

    def set_setting(self, key, value):
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (str(key), json.dumps(value)))
            self.db.commit()

    def get_setting(self, key, default=None):
        with self._lock:
            row = self.db.execute("SELECT value FROM settings WHERE key=?", (str(key),)).fetchone()
            if row:
                try: return json.loads(row[0])
                except ValueError: pass
            return default

    def save_download(self, job):
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO download_jobs VALUES (?,?,?)",
                            (str(job["id"]), json.dumps(job), time.time()))
            self.db.commit()

    def download_jobs(self):
        with self._lock:
            return [json.loads(value) for (value,) in self.db.execute("SELECT value FROM download_jobs ORDER BY updated DESC")]

    def delete_download(self, job_id):
        with self._lock:
            self.db.execute("DELETE FROM download_jobs WHERE id=?", (str(job_id),))
            self.db.commit()

    def clear_downloads(self):
        with self._lock:
            self.db.execute("DELETE FROM download_jobs")
            self.db.commit()

    def cache_size(self):
        with self._lock:
            return self.db.execute("SELECT COALESCE(SUM(length(value)),0) FROM cache").fetchone()[0]

    def clear_cache(self):
        with self._lock:
            self._mem.clear()
            self.db.execute("DELETE FROM cache")
            self.db.commit()

    def clear_settings(self):
        with self._lock:
            self.db.execute("DELETE FROM settings")
            self.db.commit()
