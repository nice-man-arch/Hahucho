"""Progress and controls for the Hakuchō ffmpeg download worker."""
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from providers.hianime import _safe_url

_DEFAULT_DL_DIR = Path.home() / "Videos" / "Hakuchō"
DOWNLOAD_DIR = Path(os.environ.get("HAKUCHO_DOWNLOAD_DIR") or os.environ.get("ANIMECHY_DOWNLOAD_DIR") or _DEFAULT_DL_DIR).expanduser()
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="hakucho-download")
_lock = threading.RLock()
_condition = threading.Condition(_lock)
_jobs = {}
_store = None
_limit = 2
_active = 0


def configure(store, download_dir=None, max_simultaneous=2):
    global _store, DOWNLOAD_DIR, _limit
    with _lock:
        _store = store
        if download_dir:
            set_download_dir(download_dir)
        _limit = max(1, min(int(max_simultaneous), 4))
        for saved in store.download_jobs():
            if saved.get("status") in ("queued", "downloading", "paused"):
                saved.update(status="failed", error="Backend restarted before this download completed", finished=time.time())
                store.save_download(saved)
            saved["path"] = Path(saved["path"])
            _jobs[saved["id"]] = saved
        _condition.notify_all()


def set_download_dir(value):
    global DOWNLOAD_DIR
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        raise ValueError("Download location must be an absolute path")
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError("Download location is not a directory")
    DOWNLOAD_DIR = path
    return str(path)


def set_limit(value):
    global _limit
    value = int(value)
    if value not in (1, 2, 3, 4):
        raise ValueError("Maximum simultaneous downloads must be between 1 and 4")
    with _condition:
        _limit = value
        _condition.notify_all()


def _filename_part(value):
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", " ", str(value or ""))
    return re.sub(r"\s+", " ", value).strip(" .")[:100] or "Anime"


def _snapshot(job):
    return {k: v for k, v in job.items() if k not in ("path", "process", "cancel_event", "log_path", "retry_url", "referer", "worker_submitted")} | {"path": str(job["path"])}


def _save(job):
    store = _store
    if store:
        try:
            store.save_download(_snapshot(job))
        except Exception:
            pass


def _notify(job):
    if not _store:
        return
    key = "notify_download_completed" if job["status"] == "completed" else "notify_download_failed" if job["status"] == "failed" else ""
    if key and _store.settings().get(key, True) and shutil.which("notify-send"):
        try:
            subprocess.Popen(["notify-send", "Hakuchō download " + job["status"],
                              f'{job["title"]} — Episode {job["episode"]}'],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            pass


def _public(job):
    return _snapshot(job)


def _wait_slot(job):
    global _active
    with _condition:
        while True:
            if job["cancel_event"].is_set():
                return False
            while job["status"] == "paused" and not job["cancel_event"].is_set():
                _condition.wait(0.5)
            if job["cancel_event"].is_set():
                return False
            if _active < _limit:
                _active += 1
                job["status"] = "downloading"
                _save(job)
                return True
            _condition.wait(0.5)


def _is_hls(url):
    u = str(url or "").lower()
    return ".m3u8" in u or (u.startswith("http") and not any(u.endswith(ext) for ext in (".mp4", ".mkv", ".webm", ".avi", ".ts")))


def _probe(url, referer):
    if not shutil.which("ffprobe"):
        return None, None
    for try_hls in ([True, False] if _is_hls(url) else [False]):
        args = ["ffprobe", "-v", "error"]
        if try_hls:
            args += ["-allowed_segment_extensions", "ALL", "-allowed_extensions", "ALL", "-extension_picky", "0"]
        if referer:
            args += ["-headers", "Referer: " + referer + "\r\n"]
        args += ["-show_entries", "format=duration,size", "-of", "json", url]
        try:
            result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True, timeout=20)
            if result.returncode == 0:
                fmt = json.loads(result.stdout).get("format", {})
                duration = float(fmt["duration"]) if fmt.get("duration") not in (None, "N/A") else None
                total = int(fmt["size"]) if fmt.get("size") not in (None, "N/A") else None
                return (duration if duration and duration > 0 else None,
                        total if total and total > 0 else None)
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            pass
    return None, None


def _run(job_id, url, referer):
    global _active
    with _lock:
        job = _jobs[job_id]
    if not _wait_slot(job):
        return
    process = None
    stderr_path = job["path"].with_suffix(".ffmpeg.log")
    tmp = job["path"].with_suffix(".part.mkv")
    try:
        duration, total_size = _probe(url, referer)
        job.update(duration=duration, total_size=total_size, downloaded=tmp.stat().st_size if tmp.exists() else 0)
        with _condition:
            while job["status"] == "paused" and not job["cancel_event"].is_set():
                _condition.wait(0.25)
            if job["cancel_event"].is_set():
                job.update(status="cancelled", finished=time.time(), error="Cancelled")
                return
        args = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats",
                "-progress", "pipe:1", "-stats_period", "0.5", "-y"]
        if _is_hls(url):
            args += ["-allowed_segment_extensions", "ALL", "-allowed_extensions", "ALL", "-extension_picky", "0"]
        if referer:
            if not _safe_url(referer):
                raise ValueError("Invalid stream Referer")
            args += ["-referer", referer]
        args += ["-i", url, "-map", "0:v?", "-map", "0:a?", "-map", "0:s?", "-c", "copy", "-y", str(tmp)]
        with open(stderr_path, "w", encoding="utf-8") as errlog:
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=errlog, text=True, bufsize=1, start_new_session=True)
            with _lock:
                job["process"] = process
                if job["cancel_event"].is_set():
                    process.terminate()
                elif job["status"] == "paused":
                    os.killpg(process.pid, signal.SIGSTOP)
                elif job["status"] == "queued":
                    job["status"] = "downloading"
                _save(job)
            last_bytes = 0
            last_time = time.monotonic()
            speed_bps = None
            out_time = 0.0
            for line in process.stdout:
                if job["cancel_event"].is_set():
                    if process.poll() is None:
                        process.terminate()
                    continue
                key, sep, value = line.strip().partition("=")
                if not sep:
                    continue
                if key in ("out_time_us", "out_time_ms"):
                    try:
                        out_time = int(value) / 1_000_000
                    except ValueError:
                        pass
                elif key == "speed":
                    try:
                        factor = float(value.rstrip("x"))
                        if factor > 0 and duration is not None:
                            job["eta"] = max(0, int((duration - out_time) / factor))
                    except ValueError:
                        pass
                elif key == "progress":
                    now = time.monotonic()
                    try:
                        current = tmp.stat().st_size
                    except OSError:
                        current = 0
                    elapsed = now - last_time
                    if elapsed > 0:
                        speed_bps = max(0, int((current - last_bytes) / elapsed))
                    percent = min(99.9, max(0, out_time * 100 / duration)) if duration else (
                        min(99.9, max(0, current * 100 / total_size)) if total_size else None)
                    if job.get("eta") is None and total_size and speed_bps:
                        job["eta"] = max(0, int(max(0, total_size-current) / speed_bps))
                    job.update(downloaded=current, speed=speed_bps, percent=percent,
                               progress_basis="media time" if duration else "bytes" if total_size else None)
                    _save(job)
                    last_bytes, last_time = current, now
            rc = process.wait(timeout=20)
        if job["cancel_event"].is_set():
            with _lock:
                job.update(status="cancelled", finished=time.time(), error="Cancelled")
        elif rc != 0:
            try:
                error = stderr_path.read_text(errors="replace")[-1200:].strip()
            except OSError:
                error = "ffmpeg download failed"
            raise RuntimeError(error or "ffmpeg download failed")
        elif not tmp.is_file() or tmp.stat().st_size == 0:
            raise RuntimeError("Download produced an empty file")
        else:
            tmp.replace(job["path"])
            size = job["path"].stat().st_size
            job.update(status="completed", finished=time.time(), downloaded=size, total_size=size,
                       percent=100.0, speed=0, eta=0, error="")
    except Exception as exc:
        if job["cancel_event"].is_set():
            job.update(status="cancelled", error="Cancelled", finished=time.time())
        else:
            job.update(status="failed", error=str(exc)[:1200], finished=time.time())
    finally:
        with _condition:
            job.pop("process", None)
            _active = max(0, _active - 1)
            _condition.notify_all()
        _save(job)
        _notify(job)


def create(url, title, episode, referer="", metadata=None):
    if not _safe_url(url):
        raise ValueError("Invalid download URL")
    if referer and not _safe_url(referer):
        raise ValueError("Invalid stream Referer")
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required for downloads (install with: sudo pacman -S ffmpeg)")
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    metadata = metadata if isinstance(metadata, dict) else {}
    name = f"{_filename_part(title)} - Episode {_filename_part(episode)}.mkv"
    path = DOWNLOAD_DIR / name
    if path.exists():
        stem, suffix, n = path.stem, path.suffix, 2
        while (DOWNLOAD_DIR / f"{stem} ({n}){suffix}").exists():
            n += 1
        path = DOWNLOAD_DIR / f"{stem} ({n}){suffix}"
    job_id = uuid.uuid4().hex
    job = {"id": job_id, "title": str(title)[:300], "episode": str(episode)[:40],
           "status": "queued", "path": path, "created": time.time(), "downloaded": 0,
           "total_size": None, "percent": None, "speed": None, "eta": None, "error": "",
           "cover": str(metadata.get("cover", ""))[:1000], "provider": str(metadata.get("provider", "")),
           "episode_id": str(metadata.get("episode_id", "")), "language": str(metadata.get("language", "sub")),
           "quality": str(metadata.get("quality", "Auto")), "anime_id": str(metadata.get("anime_id", "")),
           "cancel_event": threading.Event(), "retry_url": url, "referer": referer, "worker_submitted": True}
    with _lock:
        _jobs[job_id] = job
    _save(job)
    _pool.submit(_run, job_id, url, referer)
    return _public(job)


def _find(job_id):
    with _lock:
        return _jobs.get(str(job_id))


def control(job_id, action):
    job = _find(job_id)
    if not job:
        raise ValueError("Download was not found")
    with _condition:
        process = job.get("process")
        if action == "pause" and job["status"] in ("queued", "downloading"):
            if process and process.poll() is None:
                os.killpg(process.pid, signal.SIGSTOP)
            job["status"] = "paused"
        elif action == "resume" and job["status"] == "paused":
            if process and process.poll() is None:
                os.killpg(process.pid, signal.SIGCONT)
                job["status"] = "downloading"
            else:
                job["status"] = "queued"
                if not job.get("worker_submitted"):
                    job["worker_submitted"] = True
                    _pool.submit(_run, job["id"], job.get("retry_url", ""), job.get("referer", ""))
        elif action == "cancel" and job["status"] in ("queued", "downloading", "paused"):
            job["cancel_event"].set()
            job.update(status="cancelled", error="Cancelled", finished=time.time())
            if process and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGCONT)
                    process.terminate()
                except OSError:
                    pass
        else:
            raise ValueError("That action is not available for this download")
        _save(job)
        _condition.notify_all()
    return _public(job)


def retry(job_id, url, referer=""):
    if not _safe_url(url):
        raise ValueError("Invalid download URL")
    if referer and not _safe_url(referer):
        raise ValueError("Invalid stream Referer")
    job = _find(job_id)
    if not job or job["status"] not in ("failed", "cancelled"):
        raise ValueError("Only failed or cancelled downloads can be retried")
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required for downloads")
    tmp = job["path"].with_suffix(".part.mkv")
    tmp.unlink(missing_ok=True)
    job.update(status="queued", error="", downloaded=0, percent=0.0, speed=None, eta=None,
               created=time.time(), finished=None, cancel_event=threading.Event(), referer=referer, retry_url=url,
               worker_submitted=True)
    _save(job)
    _pool.submit(_run, job["id"], url, referer)
    return _public(job)


def delete(job_id):
    job_id_str = str(job_id)
    job = _find(job_id_str)
    if not job:
        if job_id_str.startswith("file-"):
            try:
                ino = int(job_id_str[5:])
                for path in DOWNLOAD_DIR.iterdir():
                    if path.is_file() and path.stat().st_ino == ino:
                        path.unlink(missing_ok=True)
                        return True
            except (ValueError, OSError):
                pass
        raise ValueError("Download was not found")
    with _lock:
        if job.get("process") and job["process"].poll() is None:
            raise ValueError("Active downloads cannot be deleted; cancel first")
        job_path = Path(job["path"])
        for path in (job_path.with_suffix(".part.mkv"), job_path.with_suffix(".ffmpeg.log"), job_path):
            try:
                if path.exists():
                    path.unlink(missing_ok=True)
            except OSError:
                pass
        _jobs.pop(job["id"], None)
        if _store:
            _store.delete_download(job["id"])
    return True


def clear_history():
    with _lock:
        removable = [j["id"] for j in _jobs.values() if j["status"] in ("failed", "cancelled")]
    for job_id in removable:
        delete(job_id)


def list_jobs():
    with _lock:
        jobs = [_public(j) for j in sorted(_jobs.values(), key=lambda x: x["created"], reverse=True)]
    known = {j["path"] for j in jobs}
    try:
        for path in DOWNLOAD_DIR.iterdir():
            if path.is_file() and path.suffix.lower() == ".mkv" and not path.name.lower().endswith(".part.mkv") and str(path) not in known:
                jobs.append({"id": "file-" + str(path.stat().st_ino), "title": path.stem,
                             "episode": "", "status": "completed", "path": str(path),
                             "size": path.stat().st_size, "downloaded": path.stat().st_size,
                             "total_size": path.stat().st_size, "percent": 100, "created": path.stat().st_mtime})
    except OSError:
        pass
    return sorted(jobs, key=lambda x: x.get("created", 0), reverse=True)


def status():
    return {"ready": bool(shutil.which("ffmpeg")), "ffmpeg": shutil.which("ffmpeg"),
            "location": str(DOWNLOAD_DIR), "max_simultaneous": _limit}
