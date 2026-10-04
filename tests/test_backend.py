import sys
import unittest
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
_test_cache = tempfile.TemporaryDirectory(prefix="hakucho-tests-")
os.environ["XDG_CACHE_HOME"] = _test_cache.name
sys.path.insert(0, str(ROOT / "backend"))
import server  # noqa: E402
from providers.base import ProviderError  # noqa: E402
from providers.hiyori import HiyoriProvider  # noqa: E402


class FixtureProvider:
    name = "fixture"
    def search(self, q): return [{"id": "sample-1", "title": q.title(), "cover": "https://img.example/poster.jpg"}]
    def details(self, aid): return {"id": aid, "title": "Sample"}
    def episodes(self, aid): return [{"id": "42", "number": "1", "title": "Episode 1", "filler": False}]
    def sources(self, eid, language="sub"):
        return [{"quality": "720p", "url": "https://media.example/a.m3u8", "resourceLink": "https://media.example/a.m3u8", "language": language, "subtitles": []}]


class FailedProvider:
    name = "offline"
    def __getattr__(self, _): raise ProviderError("fixture network failure", "network_failure")


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.providers = patch.object(server, "providers", [FixtureProvider()]); self.providers.start()
        self._settings_store = {}
        self.cache = patch.object(server, "cache", type("C", (), {
            "get": lambda *a: None,
            "put": lambda *a: None,
            "recent_items": lambda *a: [],
            "get_setting": lambda s, k, d=None: self._settings_store.get(k, d),
            "set_setting": lambda s, k, v: self._settings_store.update({k: v}),
            "clear_settings": lambda s: self._settings_store.clear(),
            "clear_cache": lambda s: None
        })()); self.cache.start()

    def tearDown(self): self.cache.stop(); self.providers.stop()

    def test_search(self):
        self.assertEqual(server.handle_get("/search", {"q": ["naruto"]})["items"][0]["title"], "Naruto")

    def test_anime_details(self):
        self.assertEqual(server.handle_get("/anime/sample-1", {})["value"]["title"], "Sample")

    def test_episode_retrieval(self):
        self.assertEqual(server.handle_get("/anime/sample-1/episodes", {})["items"][0]["id"], "42")

    def test_legacy_unqualified_hianime_episode_id_routes_to_hianime(self):
        class HiAnimeFixture(FixtureProvider):
            name = "hianime"
            def episodes(self, aid):
                self.asserted_id = aid
                return [{"id": "44", "number": "1", "title": "Episode 1"}]
        class HiyoriFixture(FixtureProvider):
            name = "hiyori"
        hianime, hiyori = HiAnimeFixture(), HiyoriFixture()
        server.providers[:] = [hianime, hiyori]
        result = server.handle_compat({"cmd": "episodes", "id": "black-clover-949"})
        self.assertEqual(hianime.asserted_id, "black-clover-949")
        self.assertEqual(result["items"][0]["number"], "1")

    def test_legacy_unqualified_numeric_episode_id_routes_to_hiyori(self):
        class HiAnimeFixture(FixtureProvider):
            name = "hianime"
        class HiyoriFixture(FixtureProvider):
            name = "hiyori"
        server.providers[:] = [HiAnimeFixture(), HiyoriFixture()]
        self.assertIs(server.provider_for_id("97940")[0], server.providers[1])

    def test_source_retrieval(self):
        result = server.handle_compat({"cmd": "streams", "id": "sample-1", "episode": "1", "episode_id":"42", "mode": "sub"})
        self.assertEqual(result["items"][0]["quality"], "720p")

    def test_download_request_consumes_backend_source_token(self):
        token = "download-test-token"
        server.live_sources[token] = {"url": "https://media.example/a.m3u8", "referer": "", "expires": 9999999999}
        expected = {"id": "job-1", "status": "queued", "path": "/tmp/Anime - Episode 1.mkv"}
        with patch.object(server.downloads, "create", return_value=expected) as create:
            result = server.handle_compat({"cmd": "download", "source_id": token, "title": "Anime", "episode": "1"})
        self.assertEqual(result["job"], expected)
        create.assert_called_once_with("https://media.example/a.m3u8", "Anime", "1", "", {
            "cover":"", "provider":"", "episode_id":"", "language":"sub", "quality":"Auto", "anime_id":""})
        self.assertNotIn(token, server.live_sources)

    def test_download_rejects_expired_or_unknown_source_token(self):
        with self.assertRaisesRegex(ProviderError, "expired"):
            server.handle_compat({"cmd": "download", "source_id": "not-issued"})

    def test_sources_route_only_to_named_provider(self):
        calls=[]
        class OtherProvider(FixtureProvider):
            name="other"
            def sources(self, eid, language="sub"):
                calls.append((self.name,eid))
                return [{"quality":"480p"}]
        class FirstProvider(FixtureProvider):
            name="first"
            def sources(self, eid, language="sub"):
                calls.append((self.name,eid))
                raise ProviderError("first source failed", "no_stream")
        server.providers[:]=[FirstProvider(),OtherProvider()]
        result=server.provider_call("sources","other","watch/kiwi/21/sub/exact-id","sub")
        self.assertEqual(result[0]["quality"],"480p")
        self.assertEqual(calls,[("other","watch/kiwi/21/sub/exact-id")])

    def test_selected_hiyori_episode_reference_is_forwarded_exactly(self):
        requested=[]
        class HiyoriFixture:
            name="hiyori"
            def episodes(self, aid):
                return [{"id":f"hiyori-episode:123:{n}","number":str(n),"servers":[
                    {"provider":"hiyori","server":srv,"language":"sub","episode_id":f"watch/{srv}/123/sub/black-clover-{n}"}
                    for srv in ("kiwi","arc")]} for n in (1,2,5,50,170)]
            def sources(self, eid, language="sub"):
                requested.append(eid)
                return [{"url":f"https://media.example/{eid.split('/')[-1]}.m3u8","quality":"720p"}]
        server.providers[:]=[HiyoriFixture()]
        for number, srv in ((1,"kiwi"),(2,"kiwi"),(2,"arc"),(5,"kiwi"),(50,"arc"),(170,"kiwi")):
            eid=f"watch/{srv}/123/sub/black-clover-{number}"
            result=server.handle_compat({"cmd":"streams","id":"hiyori:123","episode":str(number),"episode_id":eid,"mode":"sub",
                "server":{"provider":"hiyori","server":srv,"language":"sub","episode_id":eid}})
            self.assertTrue(result["ok"])
            self.assertTrue(result["items"][0]["url"].endswith(f"black-clover-{number}.m3u8"))
        self.assertEqual(requested,["watch/kiwi/123/sub/black-clover-1","watch/kiwi/123/sub/black-clover-2",
            "watch/arc/123/sub/black-clover-2","watch/kiwi/123/sub/black-clover-5",
            "watch/arc/123/sub/black-clover-50","watch/kiwi/123/sub/black-clover-170"])

    def test_invalid_query_and_id(self):
        with self.assertRaises(ProviderError): server.handle_get("/search", {"q": [""]})
        with self.assertRaises(ProviderError): server.handle_get("/anime/https://example.org", {})

    def test_provider_failure_is_preserved(self):
        server.providers[:] = [FailedProvider()]
        with self.assertRaisesRegex(ProviderError, "fixture network failure") as err:
            server.provider_call("search", "x")
        self.assertEqual(err.exception.kind, "network_failure")

    def test_hiyori_search_normalizes_anilist_identity(self):
        provider=HiyoriProvider()
        with patch.object(provider,"_json",return_value={"results":[{"id":21,"title":{"english":"One Piece"},"idMal": "21","coverImage":{"large":"https://img.example/one.jpg"}}]}):
            item=provider.search("One Piece")[0]
        self.assertEqual(item["id"],"hiyori:21")
        self.assertEqual(item["anilist_id"],21)
        self.assertEqual(item["mal_id"],"21")

    def test_hiyori_episodes_preserve_exact_server_ids_and_language(self):
        provider=HiyoriProvider()
        route="watch/kiwi/21/sub/pahe-episode-1"
        body={"mappings":{"malId":21},"providers":{"kiwi":{"episodes":{"sub":[{"id":route,"number":1,"title":"Romance Dawn"}],"dub":[]}},"arc":{"episodes":{"sub":[{"id":"watch/arc/21/sub/episode-1","number":1,"title":"Episode 1"}]}}}}
        with patch.object(provider,"_json",return_value=body):
            eps=provider.episodes("hiyori:21")
        self.assertEqual(len(eps),1)
        self.assertEqual([s["server"] for s in eps[0]["servers"]],["kiwi","arc"])
        self.assertEqual(eps[0]["servers"][0]["episode_id"],route)
        self.assertEqual([s["language"] for s in eps[0]["servers"]],["sub","sub"])

    def test_hiyori_sources_keep_quality_subtitles_and_skip_metadata(self):
        provider=HiyoriProvider()
        payload={"streams":[{"url":"https://media.example/master.m3u8","type":"hls","quality":"1080p"}],"subtitles":[{"file":"https://media.example/en.vtt","label":"English"}],"intro":{"start":2,"end":90}}
        with patch.object(provider,"_json",return_value=payload):
            source=provider.sources("watch/arc/21/sub/episode-1")[0]
        self.assertEqual(source["quality"],"1080p")
        self.assertEqual(source["subtitles"][0]["url"],"https://media.example/en.vtt")
        self.assertEqual(source["intro"]["end"],90)

    def test_hiyori_rejects_invalid_ids_and_watch_references(self):
        provider=HiyoriProvider()
        with self.assertRaises(ProviderError): provider.details("hiyori:../21")
        with self.assertRaises(ProviderError): provider.sources("https://evil.example")

    def test_favorites_and_progress_persist_in_sqlite(self):
        from cache import Cache
        with tempfile.TemporaryDirectory() as directory:
            db=Cache(Path(directory)/"state.sqlite3")
            db.touch("hiyori:21","One Piece","1","watch/kiwi/21/sub/episode-1","hiyori","21")
            db.update_progress("hiyori:21","watch/kiwi/21/sub/episode-1",872,1440)
            self.assertEqual(db.history()[0]["position"],872)
            # Re-touching on playback start with position 0 should NOT wipe the saved progress
            db.touch("hiyori:21","One Piece","1","watch/kiwi/21/sub/episode-1","hiyori","21",position=0,duration=0)
            self.assertEqual(db.history()[0]["position"],872)
            # episode_status should find by both episode_id and episode number
            statuses = db.episode_status("hiyori:21")
            self.assertEqual(statuses["watch/kiwi/21/sub/episode-1"]["position"], 872)
            self.assertEqual(statuses["1"]["position"], 872)
            self.assertEqual(statuses["1"]["status"], "watching")
            self.assertTrue(db.toggle_favorite("hiyori:21","One Piece","21"))
            self.assertEqual(db.favorites()[0]["canonical_id"],"21")

    def test_settings_survive_cache_reopen(self):
        from cache import Cache
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"settings.sqlite3"
            first=Cache(path); first.set_setting("download_location", directory); first.set_setting("resume_playback", False)
            second=Cache(path)
            self.assertEqual(second.settings()["download_location"], directory)
            self.assertFalse(second.settings()["resume_playback"])

    def test_download_delete_removes_files_from_disk(self):
        import downloads
        with tempfile.TemporaryDirectory() as directory:
            final=Path(directory)/"Anime - Episode 1.mkv"
            partial=final.with_suffix(".part.mkv"); partial.write_bytes(b"incomplete")
            log=final.with_suffix(".ffmpeg.log"); log.write_text("failure")
            failed={"id":"failed-fixture","path":final,"status":"failed"}
            completed_path=Path(directory)/"done.mkv"; completed_path.write_bytes(b"complete")
            completed={"id":"completed-fixture","path":completed_path,"status":"completed"}
            with patch.object(downloads,"_jobs",{failed["id"]:failed,completed["id"]:completed}), patch.object(downloads,"_store",None):
                self.assertTrue(downloads.delete("failed-fixture"))
                self.assertFalse(partial.exists()); self.assertFalse(log.exists())
                self.assertTrue(downloads.delete("completed-fixture"))
                self.assertFalse(completed_path.exists())

    def test_partial_mkv_is_not_reported_as_a_completed_download(self):
        import downloads
        with tempfile.TemporaryDirectory() as directory:
            partial=Path(directory)/"Anime - Episode 1.part.mkv"; partial.write_bytes(b"partial")
            with patch.object(downloads,"DOWNLOAD_DIR",Path(directory)), patch.object(downloads,"_jobs",{}):
                self.assertEqual(downloads.list_jobs(),[])

    def test_download_progress_uses_ffmpeg_out_time_and_file_bytes(self):
        import io
        import downloads
        class FakeProcess:
            pid=999999
            stdout=io.StringIO("out_time_us=50000000\nspeed=2.0x\nprogress=continue\nout_time_us=100000000\nspeed=2.0x\nprogress=end\n")
            def poll(self): return None
            def wait(self, timeout=None): return 0
        class Store:
            def __init__(self): self.rows=[]
            def save_download(self, job): self.rows.append(dict(job))
            def settings(self): return {"notify_download_completed":False}
        with tempfile.TemporaryDirectory() as directory:
            final=Path(directory)/"Progress - Episode 3.mkv"
            store=Store()
            job={"id":"progress-fixture","title":"Progress","episode":"3","status":"queued",
                 "path":final,"created":1,"downloaded":0,"total_size":None,"percent":None,
                 "speed":None,"eta":None,"error":"","cancel_event":__import__("threading").Event()}
            def fake_popen(args, **kwargs):
                Path(args[-1]).write_bytes(b"x"*50000)
                return FakeProcess()
            with patch.object(downloads,"_jobs",{job["id"]:job}), patch.object(downloads,"_store",store), \
                 patch.object(downloads,"_probe",return_value=(100.0,100000)), patch.object(downloads.subprocess,"Popen",side_effect=fake_popen):
                downloads._run(job["id"],"https://media.example/video.m3u8","")
            self.assertTrue(any(row.get("percent") == 50.0 and row.get("downloaded") == 50000 for row in store.rows))
            self.assertEqual(job["status"],"completed")
            self.assertEqual(job["percent"],100.0)

    def test_failed_download_retry_requeues_and_removes_old_partial(self):
        import threading
        import downloads
        with tempfile.TemporaryDirectory() as directory:
            final=Path(directory)/"Retry - Episode 4.mkv"
            partial=final.with_suffix(".part.mkv"); partial.write_bytes(b"old partial")
            job={"id":"retry-fixture","title":"Retry","episode":"4","path":final,"status":"failed",
                 "cancel_event":threading.Event(),"provider":"hianime","episode_id":"episode-4"}
            with patch.object(downloads,"_jobs",{job["id"]:job}), patch.object(downloads,"_store",None), \
                 patch.object(downloads,"_pool") as pool, patch.object(downloads.shutil,"which",return_value="/usr/bin/ffmpeg"):
                result=downloads.retry(job["id"],"https://media.example/fresh.m3u8")
            self.assertEqual(result["status"],"queued")
            self.assertFalse(partial.exists())
            pool.submit.assert_called_once()


    def test_preferred_provider_setting(self):
        result = server.handle_compat({"cmd": "set_setting", "key": "preferred_provider", "value": "hianime"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["value"], "hianime")
        with self.assertRaises(ProviderError):
            server.handle_compat({"cmd": "set_setting", "key": "preferred_provider", "value": "invalid_provider"})


if __name__ == "__main__": unittest.main()
