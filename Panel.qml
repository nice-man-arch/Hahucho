import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
    id: root

    property var anchorItem: null
    property var hostWidget: null
    readonly property var barIdentity: hostWidget || root
    readonly property string backendUrl: "http://127.0.0.1:8765/ipc"
    // ---------------- state ----------------
    property string view: "home"
    // home | grid | details
    property var details: null
    property string currentId: ""
    property string currentTitle: ""
    property var seasons: [] // list of {id, title}
    property int curSeasonIdx: 0
    property var episodes: [] // list of {id, number}
    property string resumeEpisode: ""
    property string resumeEpisodeId: ""
    property string homeKind: "discover"
    property string curEp: "1"
    property string mode: "sub" // sub | dub
    property var streams: []
    property var selectedServer: null
    property var fallbackTried: []
    property bool currentFavorite: false
    property real resumePosition: 0
    property int selStream: -1
    property bool busy: false
    property string busyLabel: ""
    property string statusText: "Search anime — try \"One Piece\" or pick a genre"
    property string query: ""
    property var results: []
    property bool playing: false
    property bool mpvActive: false
    property bool homeLoading: false
    property var playbackSettings: ({
        preferred_provider: "all",
        auto_fallback: false,
        auto_next: false,
        resume_playback: true,
        default_quality: "Auto",
        default_language: "sub",
        preferred_subtitle: "en",
        fullscreen_player: false,
        compact_episodes: false,
        show_covers: true,
        notify_playback_errors: true,
        download_location: "",
        download_max_simultaneous: 2,
        download_default_quality: "Auto",
        notify_download_completed: true,
        notify_download_failed: true
    })
    property string settingsCategory: ""
    property var downloadItems: []
    property var backendStatus: ({backend:"checking", download:{ready:false}, providers:[], version:"—", cache_bytes:0})
    property string downloadDestination: ""
    property string pendingDeleteId: ""
    property string activeModal: ""
    function openModal(name) { activeModal = name; }
    function closeModal() { activeModal = ""; }
    property int suggestGen: 0
    property int searchGen: 0
    property int detailGen: 0
    property int resourceGen: 0
    property int episodeGen: 0
    property var searchHistory: [] // recent searches [ "one piece", "naruto" ]
    readonly property int maxHistory: 10
    property string selectedGenre: ""
    property bool genresExpanded: false
    property int genreGen: 0
    Component.onCompleted: { loadHistory(); loadSettings(); loadAdminStatus(); }
    property var genreCache: ({
    })
    property var genreCacheTime: ({
    })
    readonly property var genres: ["All", "Action", "Adventure", "Comedy", "Drama", "Fantasy", "Romance", "Slice of Life", "Sci-Fi", "Mystery", "Sports", "Supernatural"]
    onOpenedChanged: {
        if (root.opened) {
            root.loadHome(true);
            root.loadSettings();
            if (root.view === "details" && root.currentId) {
                root.loadEpisodes(root.currentId);
            }
        }
    }
    onVisibleChanged: {
        if (visible) {
            root.loadHome(true);
            root.loadSettings();
            if (root.view === "details" && root.currentId) {
                root.loadEpisodes(root.currentId);
            }
        }
    }
    // ---------------- backend IPC (serialized) ----------------
    property var pending: []
    property var cbChain: null
    // local helper for episode count
    property int _episodeCount: 0

    function sanitize(s) {
        if (!s)
            return "";

        return String(s).replace(/<[^>]*>/g, "");
    }

    function sanitizeDetails(d) {
        if (!d)
            return null;

        var out = {
        };
        for (var k in d) {
            var v = d[k];
            if (typeof v === "string")
                out[k] = root.sanitize(v);
            else if (Array.isArray(v))
                out[k] = v.map(function(x) {
                return typeof x === "string" ? root.sanitize(x) : x;
            });
            else
                out[k] = v;
        }
        return out;
    }

    function sanitizeStreams(arr) {
        if (!Array.isArray(arr))
            return [];

        return arr.map(function(s) {
            var out = {
            };
            for (var k in s) {
                var v = s[k];
                if (typeof v === "string")
                    out[k] = root.sanitize(v);
                else
                    out[k] = v;
            }
            return out;
        });
    }

    function request(cmd, params, cb) {
        params = params || {
        };
        if (apiProc.running) {
            root.pending.push({
                "cmd": cmd,
                "params": params,
                "cb": cb
            });
            return ;
        }
        root._start(cmd, params, cb);
    }

    function _apiCmd(json) {
        // User data is passed as one curl argument; no shell interpolation.
        return ["curl", "-sS", "--max-time", "45", "-H", "Content-Type: application/json", "--data-binary", json, root.backendUrl];
    }
    function _start(cmd, params, cb) {
        apiProc.collected = "";
        root.cbChain = cb;
        var req = JSON.parse(JSON.stringify(params));
        req.cmd = cmd;
        apiProc.command = root._apiCmd(JSON.stringify(req));
        apiProc.running = true;
    }

    // ---------------- helpers ----------------
    function _isAllowedCoverUrl(u) {
        if (!u || typeof u !== "string") return false;
        u = u.trim();
        if (u.startsWith("file://") || u.startsWith("/")) return true;
        if (u.startsWith("//")) u = "https:" + u;
        if (u.startsWith("http://127.0.0.1") || u.startsWith("http://localhost")) return true;
        if (u.startsWith("https://")) return true;
        return false;
    }
    function _sanitizeCoverUrl(u) {
        if (!u || typeof u !== "string") return "";
        u = u.trim();
        if (u.startsWith("file://")) return u;
        if (u.startsWith("/")) return "file://" + u;
        if (u.startsWith("//")) return "https:" + u;
        if (u.startsWith("http://127.0.0.1") || u.startsWith("http://localhost") || u.startsWith("https://")) return u;
        return "";
    }
    function coverUrlOf(obj) {
        if (!obj) return "";
        var raw = "";
        var c = obj.cover;
        if (c && typeof c === "object") raw = c.url || c.extraLarge || c.large || "";
        else if (typeof c === "string") raw = c;
        else raw = obj.coverPath || obj.coverUrl || "";
        return _sanitizeCoverUrl(raw);
    }

    // ---------------- actions ----------------
    function doSearch() {
        var q = searchField.text.trim();
        if (!q)
            return ;

        suggestionModel.clear();
        searchField.focus = false;
        root.addHistory(q);
        root.query = q;
        root.selectedGenre = "";
        root.busy = true;
        root.busyLabel = "Searching …";
        root.statusText = "";
        root.searchGen++;
        root.genreGen++;
        var gen = root.searchGen;
        request("search", {
            "q": q,
            "page": 1
        }, function(resp, code) {
            if (gen !== root.searchGen)
                return ;

            root.busy = false;
            if (!resp || !resp.ok) {
                root.statusText = (resp && resp.error) || "Search failed";
                return ;
            }
            root.results = resp.items || [];
            resultModel.clear();
            for (var i = 0; i < root.results.length; i++) {
                var r = root.results[i];
                resultModel.append({
                    "id": root.sanitize(r.id),
                    "title": root.sanitize(r.title),
                    "year": root.sanitize(r.year || ""),
                    "rating": root.sanitize(r.rating ? String(r.rating) : "-"),
                    "cover": root.sanitize(r.cover || ""),
                    "coverPath": root.sanitize(r.cover || ""),
                    "duration": root.sanitize(r.duration || "")
                });
            }
            root.view = "grid";
            suggestionModel.clear();
            searchField.focus = false;
            root.statusText = resultModel.count + " results for “" + q + "”";
            addToHistory(q);
        });
    }

    function debounceSuggest() {
        suggestTimer.restart();
    }

    // ---------- search history (in-memory + file via Process) ----------
    function loadHistory() {
        // use $HOME so fresh installs with different usernames work; not watched by plugin watcher
        historyLoadProc.command = ["bash", "-c", "cat \"$HOME/.local/state/animechy/hists\" 2>/dev/null || cat \"${XDG_STATE_HOME:-$HOME/.local/state}/animechy/hists\" 2>/dev/null || true"];
        historyLoadProc.running = true;
    }

    function saveHistory() {
        var payload = JSON.stringify(root.searchHistory);
        var b64 = Qt.btoa(payload);
        historySaveProc.command = ["bash", "-c", "mkdir -p \"$HOME/.local/state/animechy\" && echo '" + b64 + "' | base64 -d > \"$HOME/.local/state/animechy/hists\""];
        historySaveProc.running = true;
    }

    function addToHistory(q) {
        if (!q) return;
        q = String(q).trim();
        if (!q) return;
        var idx = root.searchHistory.indexOf(q);
        if (idx >= 0)
            root.searchHistory.splice(idx, 1);
        root.searchHistory.unshift(q);
        if (root.searchHistory.length > root.maxHistory)
            root.searchHistory = root.searchHistory.slice(0, root.maxHistory);
        // trigger binding update (assign new array reference)
        root.searchHistory = root.searchHistory.slice();
        saveHistory();
    }

    // ----------------------------------------

    function openDetails(idx, fromHome) {
        var model = fromHome ? homeModel : resultModel;
        if (idx < 0 || idx >= model.count)
            return ;

        var it = model.get(idx);
        root.currentId = root.sanitize(it.id);
        root.currentTitle = root.sanitize(it.title);
        root.currentFavorite = false;
        root.resumePosition = 0;
        root.details = null;
        root.seasons = [];
        root.episodes = [];
        root.streams = [];
        root.selStream = -1;
        root.selectedServer = null;
        root.curEp = "1";
        root.resumeEpisode = fromHome ? String(it.episode || "") : "";
        root.resumeEpisodeId = fromHome ? String(it.episode_id || "") : "";
        root.detailGen++;
        var gen = root.detailGen;
        root.busy = true;
        root.busyLabel = "Loading details & episodes …";
        root.statusText = "Loading “" + it.title + "” …";
        root.view = "details";
        detailPoster.source = _sanitizeCoverUrl(it.cover || "");
        // fetch details and episodes in parallel — episodes start immediately
        root.loadEpisodes(it.id, gen);
        request("favorites", {}, function(fr) {
            if (!fr || !fr.ok) return;
            var canonical=root.details && root.details.mal_id ? "mal:"+root.details.mal_id : root.details && root.details.anilist_id ? "anilist:"+root.details.anilist_id : "";
            for (var fi=0; fi<fr.items.length; fi++) if (fr.items[fi].id === root.currentId || (canonical && fr.items[fi].canonical_id === canonical)) { root.currentFavorite=true; break; }
        });
        request("details", {
            "id": it.id
        }, function(resp, code) {
            if (gen !== root.detailGen) return ;
            if (!resp || !resp.ok) {
                root.busy = false;
                root.statusText = (resp && resp.error) || "Details failed";
                return ;
            }
            root.details = root.sanitizeDetails(resp.value);
            request("favorites", {}, function(fr) {
                if (!fr || !fr.ok || gen !== root.detailGen) return;
                var canonical=root.details && root.details.mal_id ? "mal:"+root.details.mal_id : root.details && root.details.anilist_id ? "anilist:"+root.details.anilist_id : "";
                root.currentFavorite=false;
                for (var fi=0;fi<fr.items.length;fi++) if (fr.items[fi].id===root.currentId || (canonical && fr.items[fi].canonical_id===canonical)) { root.currentFavorite=true; break; }
            });
            var rawSeasons = (resp.value && resp.value.seasons_list) || (resp.value && resp.value.seasons && resp.value.seasons.seasons) || [];
            var normSeasons = [];
            for (var s = 0; s < rawSeasons.length; s++) {
                var ss = rawSeasons[s];
                if (ss.id && ss.title)
                    normSeasons.push({ "id": root.sanitize(ss.id), "title": root.sanitize(ss.title) });
                else if (ss.id)
                    normSeasons.push({ "id": root.sanitize(ss.id), "title": root.sanitize(ss.id) });
            }
            root.seasons = normSeasons;
            root.curSeasonIdx = 0;
            var cover = root.coverUrlOf(root.details);
            if (cover)
                detailPoster.source = cover;
            // if episodes not yet loaded, fetch them; otherwise keep already-loaded episodes
            // (speculative loadEpisodes already started in parallel)
            if (root.episodes.length === 0) {
                root.loadEpisodes(root.currentId, gen);
            }
        });
    }

    function loadEpisodes(aid, gen) {
        root.episodeGen++;
        var eGen = root.episodeGen;
        root.busy = true;
        root.busyLabel = "Loading episodes …";
        request("episodes", {
            "id": aid
        }, function(resp, code) {
            if (eGen !== root.episodeGen)
                return ;

            if (gen !== undefined && gen !== root.detailGen)
                return ;

            if (!resp || !resp.ok) {
                root.busy = false;
                root.statusText = (resp && resp.error) || "No episodes";
                return ;
            }
            var eps = resp.items || [];
            root.episodes = eps;
            // expose to QML for repeater needing count
            root._episodeCount = eps.length;
            // default episode
            if (eps.length > 0) {
                var selected = null;
                if (root.resumeEpisode) {
                    for (var ei = 0; ei < eps.length; ei++) {
                        if (String(eps[ei].number) === root.resumeEpisode) {
                            if (eps[ei].watch_status === "watched" && ei + 1 < eps.length && eps[ei + 1].watch_status !== "watched") {
                                selected = eps[ei + 1];
                            } else {
                                selected = eps[ei];
                            }
                            break;
                        }
                    }
                }
                if (!selected && root.playbackSettings.resume_playback !== false) {
                    for (var wi = 0; wi < eps.length; wi++) {
                        if (eps[wi].watch_status === "watching" && Number(eps[wi].watch_position || 0) > 0) {
                            selected = eps[wi];
                            break;
                        }
                    }
                    if (!selected) {
                        var lastWatchedIdx = -1;
                        for (var vi = 0; vi < eps.length; vi++) {
                            if (eps[vi].watch_status === "watched") lastWatchedIdx = vi;
                        }
                        if (lastWatchedIdx >= 0 && lastWatchedIdx + 1 < eps.length) {
                            selected = eps[lastWatchedIdx + 1];
                        }
                    }
                }
                if (!selected) selected = eps[0];
                root.curEp = selected.number;
                root.resumeEpisode = "";
                root.statusText = eps.length + " episodes — pick one";
                if (root.resumeEpisodeId && Array.isArray(selected.servers)) {
                    var matchServer=selected.servers.filter(function(s){return s.episode_id===root.resumeEpisodeId;});
                    if (matchServer.length) root.selectedServer=matchServer[0];
                }
                root.selectEpisode(selected);
                root.resumeEpisodeId="";
            } else {
                root.busy = false;
                root.statusText = "No episodes found";
            }
        });
    }

    // helper to reload episodes when season changed
    function selectSeason(idx) {
        if (idx < 0 || idx >= root.seasons.length)
            return ;

        root.curSeasonIdx = idx;
        var s = root.seasons[idx];
        root.currentId = s.id;
        root.currentTitle = s.title;
        root.details = null;
        root.episodes = [];
        root.streams = [];
        root.selStream = -1;
        root.busy = true;
        root.busyLabel = "Loading season …";
        root.detailGen++;
        var gen = root.detailGen;
        request("details", {
            "id": s.id
        }, function(resp) {
            if (gen !== root.detailGen)
                return ;

            if (resp && resp.ok) {
                root.details = root.sanitizeDetails(resp.value);
                var cover = root.coverUrlOf(root.details);
                if (cover)
                    detailPoster.source = cover;

            }
            // load episodes for this season id
            root.loadEpisodes(s.id, gen);
        });
    }

    function loadStreams(aid, ep, mode, _fallbackTried, episodeRef) {
        root.busy = true;
        root.busyLabel = "Loading streams …";
        var effMode = mode || root.mode;
        var selectedEpisode = root.currentEpisodeObject();
        var exactEpisodeRef = episodeRef || (root.selectedServer ? root.selectedServer.episode_id : selectedEpisode ? selectedEpisode.id : "");
        var req = { cmd: "streams", id: aid, episode: String(ep), episode_id: exactEpisodeRef, mode: effMode, server: root.selectedServer || {} };
        if (streamsProc.running) {
            // Keep the active curl callback attached to its own request. The latest
            // click replaces a queued request instead of relabeling an old response.
            streamsProc.gen++;
            streamsProc.pendingRequest = {aid:aid, ep:ep, mode:effMode, fallback:_fallbackTried, episodeRef:exactEpisodeRef};
            return;
        }
        streamsProc.gen++;
        var gen = streamsProc.gen;
        streamsProc.collected = "";
        streamsProc.cbChain = function(resp, code) {
            if (gen !== streamsProc.gen) return;
            var items = (resp && resp.ok && resp.items) ? resp.items : [];
            if (items.length === 0 && resp && resp.ok && !_fallbackTried && !root.selectedServer) {
                var other = (effMode === "sub") ? "dub" : "sub";
                root.busyLabel = "No " + effMode + " streams — trying " + other + " …";
                loadStreams(aid, ep, other, true);
                return;
            }
            root.busy = false;
            if (!resp || !resp.ok) {
                if (root.playbackSettings.auto_fallback && root.selectedServer) {
                    var options=root.serversForMode(effMode), next=null;
                    if (root.fallbackTried.indexOf(root.selectedServer.episode_id)<0) root.fallbackTried=root.fallbackTried.concat([root.selectedServer.episode_id]);
                    for (var oi=0;oi<options.length;oi++) if (root.fallbackTried.indexOf(options[oi].episode_id)<0) { next=options[oi]; break; }
                    if (next) { root.statusText=String(root.selectedServer.server).toUpperCase()+" failed; trying "+String(next.server).toUpperCase()+" automatically"; root.selectedServer=next; root.loadStreams(aid,ep,effMode,true,next.episode_id); return; }
                }
                root.streams = [];
                root.selStream = -1;
                root.statusText = (root.selectedServer ? String(root.selectedServer.server).toUpperCase()+" failed to provide a playable stream. " : "") + ((resp && resp.error) || "Backend request failed while finding a stream");
                return;
            }
            root.streams = root.sanitizeStreams(items);
            root.selStream = root.streams.length > 0 ? 0 : -1;
            if (root.streams.length === 0) {
                root.statusText = "No streams for E" + ep + " (" + effMode + ")" + (_fallbackTried ? " — tried sub & dub" : "");
            } else {
                if (_fallbackTried && effMode !== (mode || root.mode)) {
                    root.mode = effMode;
                    root.statusText = root.streams.length + " streams (" + effMode + ") — pick quality & Play (auto-switched)";
                } else {
                    root.statusText = root.streams.length + " streams — pick quality & Play";
                }
                root.curEp = String(ep);
            }
        };
        streamsProc.command = root._apiCmd(JSON.stringify(req));
        streamsProc.running = true;
    }

    function selectStream(i) {
        root.selStream = i;
    }

    function currentEpisodeObject() {
        for (var i=0;i<root.episodes.length;i++) if (String(root.episodes[i].number)===String(root.curEp)) return root.episodes[i];
        return null;
    }

    function serversForMode(mode) {
        var ep=currentEpisodeObject(); if (!ep || !Array.isArray(ep.servers)) return [];
        return ep.servers.filter(function(s){ return s.language===mode; });
    }

    function selectEpisode(epObj) {
        var preferredId=root.selectedServer ? root.selectedServer.episode_id : "";
        root.curEp=String(epObj.number); root.selectedServer=null; root.fallbackTried=[];
        if (Array.isArray(epObj.servers)) {
            var options=epObj.servers;
            var exact=options.filter(function(s){return preferredId && s.episode_id===preferredId && s.language===root.mode;});
            var preferred=options.filter(function(s){return s.language===root.mode;});
            if (!preferred.length) {
                root.mode=options.length ? options[0].language : "sub";
                preferred=options.filter(function(s){return s.language===root.mode;});
            }
            root.selectedServer=exact.length ? exact[0] : preferred.length ? preferred[0] : null;
        }
        if (root.playbackSettings.resume_playback !== false) {
            if (epObj.watch_status === "watched") {
                root.resumePosition = 0;
            } else if (Number(epObj.watch_position || 0) > 0) {
                root.resumePosition = Number(epObj.watch_position);
            } else if (root.resumePosition > 0 && String(root.curEp) === String(epObj.number)) {
                // keep existing resumePosition for this episode
            } else {
                root.resumePosition = 0;
            }
        } else {
            root.resumePosition = 0;
        }
        root.loadStreams(root.currentId, root.curEp, root.mode, false, root.selectedServer ? root.selectedServer.episode_id : epObj.id);
    }

    function selectServer(server) {
        root.fallbackTried=[];
        root.selectedServer=server;
        root.loadStreams(root.currentId, root.curEp, root.mode, false, server.episode_id);
    }

    function playExternal() {
        if (root.selStream < 0 || root.streams.length === 0)
            return ;

        var s = root.streams[root.selStream];
        var link = s.resourceLink || s.link || s.url || "";
        if (!link) {
            root.statusText = "Stream has no URL";
            return ;
        }
        var subtitle = Array.isArray(s.subtitles) && s.subtitles.length > 0 ? s.subtitles[0] : "";
        var canonical = root.details && root.details.mal_id ? "mal:" + root.details.mal_id : root.details && root.details.anilist_id ? "anilist:" + root.details.anilist_id : "";
        var selectedEp = root.currentEpisodeObject();
        var selectedEpId = root.selectedServer ? root.selectedServer.episode_id : (selectedEp ? selectedEp.id : root.curEp);
        var selectedProvider = root.selectedServer ? root.selectedServer.provider : (root.currentId.split(":")[0] || "hianime");
        var resumePos = (root.playbackSettings.resume_playback !== false) ? root.resumePosition : 0;

        var playRequest = {
            source_id: s.source_id || "",
            resume_position: resumePos,
            metadata: {
                id: root.currentId,
                title: root.currentTitle,
                episode: root.curEp,
                episode_id: selectedEpId,
                provider: selectedProvider,
                language: root.mode,
                canonical_id: canonical
            }
        };
        mpvProc.collected = "";
        mpvProc.command = ["curl", "-sS", "--max-time", "8", "-H", "Content-Type: application/json", "--data-binary", JSON.stringify(playRequest), "http://127.0.0.1:8765/play"];
        mpvProc.running = true;
    }

    function downloadSelected() {
        if (root.selStream < 0 || root.selStream >= root.streams.length) return;
        var preferred = root.playbackSettings.download_default_quality || "Auto";
        var s = root.streams[root.selStream];
        if (preferred !== "Auto") {
            for (var i=0; i<root.streams.length; i++) if (String(root.streams[i].quality || "") === preferred) { s=root.streams[i]; break; }
        }
        var selectedProvider = root.selectedServer ? root.selectedServer.provider : root.currentId.split(":")[0];
        var selectedEpisode = root.selectedServer ? root.selectedServer.episode_id : "";
        if (!selectedEpisode) {
            for (var e=0;e<root.episodes.length;e++) if (String(root.episodes[e].number) === String(root.curEp)) { selectedEpisode=root.episodes[e].id; break; }
        }
        request("download", {source_id:s.source_id || "", title:root.currentTitle, episode:root.curEp,
            cover:root.coverUrlOf(root.details), provider:selectedProvider, episode_id:selectedEpisode,
            language:root.mode, quality:String(s.quality || preferred), anime_id:root.currentId}, function(r) {
            if (r && r.ok) { root.statusText = "Download queued: " + r.job.path; root.refreshDownloads(); }
            else root.statusText = (r && r.error) || "Download request failed";
        });
    }

    function showDownloads() {
        root.view = "downloads";
        root.refreshDownloads();
    }
    function showSettings(cat) {
        root.view = "settings";
        root.settingsCategory = cat || "";
        root.loadSettings();
        root.loadAdminStatus();
    }
    function refreshDownloads() {
        request("downloads", {}, function(resp) {
            if (!resp || !resp.ok) { root.statusText=(resp && resp.error)||"Could not load downloads"; return; }
            root.downloadItems=resp.items || []; root.downloadDestination=resp.destination || "";
        });
    }

    function downloadAction(id, action) {
        request("download_control", {id:id, action:action}, function(r) {
            if (!r || !r.ok) root.statusText=(r && r.error)||"Download action failed";
            root.refreshDownloads();
        });
    }
    function retryDownload(id) {
        request("download_retry", {id:id}, function(r) {
            if (!r || !r.ok) root.statusText=(r && r.error)||"Retry failed";
            root.refreshDownloads();
        });
    }
    function requestDownloadDelete(id) { root.pendingDeleteId=id; root.openModal("delete_download"); }
    function deleteDownload() {
        request("download_delete", {id:root.pendingDeleteId}, function(r) {
            if (!r || !r.ok) root.statusText=(r && r.error)||"Could not delete download";
            root.pendingDeleteId=""; root.closeModal(); root.refreshDownloads();
        });
    }
    function openPath(path) { openPathProc.command=["xdg-open", path]; openPathProc.running=true; }
    function openDownloadFolder() { if (root.downloadDestination) root.openPath(root.downloadDestination); }
    function parentPath(path) { var p=String(path || ""), i=p.lastIndexOf("/"); return i > 0 ? p.slice(0,i) : root.downloadDestination; }
    function formatBytes(bytes) {
        var n=Number(bytes||0), units=["B","KB","MB","GB","TB"], u=0;
        while (n>=1024 && u<units.length-1) { n/=1024; u++; }
        return (u ? n.toFixed(1) : Math.round(n))+" "+units[u];
    }
    function formatEta(seconds) {
        if (seconds === null || seconds === undefined || !isFinite(Number(seconds))) return "—";
        seconds=Math.max(0,Math.floor(Number(seconds)));
        return (seconds>=3600 ? Math.floor(seconds/3600)+":"+String(Math.floor(seconds%3600/60)).padStart(2,"0") : String(Math.floor(seconds/60)).padStart(2,"0"))+":"+String(seconds%60).padStart(2,"0");
    }
    function loadAdminStatus() {
        request("status", {}, function(r) { if (r && r.ok) { root.backendStatus=r; root.downloadDestination=r.download.location || root.downloadDestination; } });
    }
    function clearCache() {
        request("clear_cache", {}, function(r) {
            if (r && r.ok) {
                root.genreCache = {};
                root.genreCacheTime = {};
                root.statusText = "Metadata and API cache cleared";
                root.loadAdminStatus();
            } else {
                root.statusText = (r && r.error) || "Could not clear cache";
            }
        });
    }
    function clearSearchHistory() {
        root.searchHistory = [];
        saveHistory();
        root.statusText = "Search history cleared";
    }
    function clearDownloadHistory() {
        request("clear_download_history", {}, function(r) { if (r && r.ok) root.refreshDownloads(); });
    }
    function resetSettings() {
        request("reset_settings", {}, function(r) {
            if (r && r.ok) {
                root.playbackSettings=r.value; root.downloadDestination=r.value.download_location; root.mode="sub";
            }
        });
    }

    function setFavorite() {
        var canonical = root.details && root.details.mal_id ? "mal:" + root.details.mal_id : root.details && root.details.anilist_id ? "anilist:" + root.details.anilist_id : "";
        favoriteProc.collected = "";
        favoriteProc.command = ["curl", "-sS", "--max-time", "5", "-H", "Content-Type: application/json", "--data-binary", JSON.stringify({cmd:"favorite", id:root.currentId, title:root.currentTitle, canonical_id:canonical, metadata:{cover:root.coverUrlOf(root.details), provider:root.currentId.split(":")[0]||"", canonical_id:canonical}}), "http://127.0.0.1:8765/ipc"];
        favoriteProc.running = true;
    }

    function showFavorites() {
        root.homeLoading = true; root.view="home"; root.homeKind="favorites";
        request("favorites", {}, function(resp) {
            root.homeLoading=false; homeModel.clear();
            if (!resp || !resp.ok) { root.statusText=(resp && resp.error)||"Could not load favorites"; return; }
            for (var i=0;i<resp.items.length;i++) {
                var f=resp.items[i], m=f.metadata||{};
                homeModel.append({id:f.id,title:f.title,cover:m.cover||m.coverUrl||"",coverPath:m.cover||m.coverUrl||"",year:"",rating:"-",episode:""});
            }
            root.statusText=homeModel.count+" favorites";
        });
    }

    function showWatchHistory() {
        root.homeLoading=true; root.view="home"; root.homeKind="history";
        request("history", {}, function(resp) {
            root.homeLoading=false; homeModel.clear();
            if (!resp || !resp.ok) { root.statusText=(resp && resp.error)||"Could not load history"; return; }
            for (var i=0;i<resp.items.length;i++) {
                var h=resp.items[i];
                homeModel.append({id:h.id,title:h.title,cover:"",coverPath:"",year:"",rating:"-",episode:h.episode,
                    position:Number(h.position||0),playDuration:Number(h.duration||0),episode_id:h.episode_id,provider:h.provider});
            }
            root.statusText=homeModel.count+" history entries";
        });
    }

    function clearWatchHistory() {
        request("clear_history", {}, function(resp) { if (resp && resp.ok) showWatchHistory(); else root.statusText=(resp && resp.error)||"Could not clear history"; });
    }

    function loadSettings() { request("settings", {}, function(r) { if (r && r.ok) { root.playbackSettings = Object.assign({}, root.playbackSettings, r.value || {}); root.mode=root.playbackSettings.default_language || "sub"; } }); }
    function saveSetting(key, value) {
        var s=Object.assign({}, root.playbackSettings); s[key]=value; root.playbackSettings=s;
        if (key === "default_language") root.mode=value;
        request("set_setting", {key:key,value:value}, function(r) {
            if (!r || !r.ok) { root.statusText=(r && r.error)||"Could not save setting"; root.loadSettings(); }
            else if (key === "download_location") root.downloadDestination=r.value;
        });
    }

    function prefetchDetails(id) {
        if (!id || apiProc.running || root.pending.length > 0 || prefetchProc.running)
            return ;

        var req = JSON.stringify({
            "cmd": "details",
            "id": id
        });
        prefetchProc.collected = "";
        prefetchProc.command = root._apiCmd(req);
        prefetchProc.running = true;
    }

    function loadHome(force) {
        if (root.homeLoading)
            return ;

        if (!force && homeModel.count > 0) {
            root.view = "home";
            return ;
        }
        root.homeLoading = true;
        root.busy = true;
        root.busyLabel = "Loading home …";
        request("homepage", {
            "page": 1,
            "perPage": 24
        }, function(resp) {
            root.homeLoading = false;
            root.busy = false;
            if (!resp || !resp.ok) {
                root.statusText = (resp && resp.error) || "Could not load home";
                root.view = "home";
                return ;
            }
            var items = resp.items || [];
            root.homeKind = resp.kind || "discover";
            // shuffle for variety
            for (var i = items.length - 1; i > 0; i--) {
                var j = Math.floor(Math.random() * (i + 1));
                if (root.homeKind !== "recent") {
                    var tmp = items[i];
                    items[i] = items[j];
                    items[j] = tmp;
                }
            }
            homeModel.clear();
            for (var k = 0; k < items.length && k < 24; k++) {
                var r = items[k];
                homeModel.append({
                    "id": root.sanitize(r.id),
                    "title": root.sanitize(r.title),
                    "year": root.sanitize(r.year || ""),
                    "rating": root.sanitize(r.rating ? String(r.rating) : "-"),
                    "cover": root.sanitize(r.cover || ""),
                    "coverPath": root.sanitize(r.cover || ""),
                    "duration": root.sanitize(r.duration || ""),
                    "episode": root.sanitize(r.episode || ""),
                    "position": Number(r.position || 0), "playDuration": Number(r.duration || 0),
                    "episode_id": r.episode_id || "", "provider": r.provider || ""
                });
            }
            root.view = "home";
            root.statusText = homeModel.count ? (root.homeKind === "recent" ? "Continue watching • " : "Discover • ") + homeModel.count + " titles" : "Search anime above";
        });
    }

    function goHome() {
        root.selectedGenre = "";
        root.loadHome(true);
    }

    function refreshHomeSilently() {
        if (root.homeLoading || root.busy || root.selectedGenre !== "")
            return ;
        request("homepage", { "page": 1, "perPage": 24 }, function(resp) {
            if (!resp || !resp.ok || root.homeLoading) return ;
            var items = resp.items || [];
            if (resp.kind === "recent") {
                root.homeKind = "recent";
                if (homeModel.count !== items.length) {
                    homeModel.clear();
                    for (var k = 0; k < items.length && k < 24; k++) {
                        var r = items[k];
                        homeModel.append({
                            "id": root.sanitize(r.id),
                            "title": root.sanitize(r.title),
                            "year": root.sanitize(r.year || ""),
                            "rating": root.sanitize(r.rating ? String(r.rating) : "-"),
                            "cover": root.sanitize(r.cover || ""),
                            "coverPath": root.sanitize(r.cover || ""),
                            "duration": root.sanitize(r.duration || ""),
                            "episode": root.sanitize(r.episode || ""),
                            "position": Number(r.position || 0), "playDuration": Number(r.duration || 0),
                            "episode_id": r.episode_id || "", "provider": r.provider || ""
                        });
                    }
                } else {
                    for (var i = 0; i < items.length && i < homeModel.count; i++) {
                        var itm = items[i];
                        if (homeModel.get(i).id === itm.id) {
                            homeModel.setProperty(i, "episode", root.sanitize(itm.episode || ""));
                            homeModel.setProperty(i, "position", Number(itm.position || 0));
                            homeModel.setProperty(i, "playDuration", Number(itm.duration || 0));
                            homeModel.setProperty(i, "episode_id", itm.episode_id || "");
                        } else {
                            homeModel.set(i, {
                                "id": root.sanitize(itm.id),
                                "title": root.sanitize(itm.title),
                                "year": root.sanitize(itm.year || ""),
                                "rating": root.sanitize(itm.rating ? String(itm.rating) : "-"),
                                "cover": root.sanitize(itm.cover || ""),
                                "coverPath": root.sanitize(itm.cover || ""),
                                "duration": root.sanitize(itm.duration || ""),
                                "episode": root.sanitize(itm.episode || ""),
                                "position": Number(itm.position || 0), "playDuration": Number(itm.duration || 0),
                                "episode_id": itm.episode_id || "", "provider": itm.provider || ""
                            });
                        }
                    }
                }
            }
        });
    }

    function refreshEpisodesSilently(aid) {
        if (!aid || root.busy) return ;
        var curAid = root.currentId;
        request("episodes", { "id": aid }, function(resp) {
            if (!resp || !resp.ok || root.currentId !== curAid) return ;
            var eps = resp.items || [];
            if (!eps.length) return ;
            root.episodes = eps;
            root._episodeCount = eps.length;
            var curEpObj = root.currentEpisodeObject();
            if (curEpObj && root.playbackSettings.resume_playback !== false) {
                if (curEpObj.watch_status === "watched") {
                    root.resumePosition = 0;
                } else if (Number(curEpObj.watch_position || 0) > 0) {
                    root.resumePosition = Number(curEpObj.watch_position);
                }
            }
        });
    }

    function searchByGenre(genre, force) {
        if (genre === "All" || genre === "") {
            root.selectedGenre = "";
            root.loadHome(true);
            return ;
        }
        root.selectedGenre = genre;
        var now = Date.now();
        var cached = root.genreCache[genre];
        var cachedAt = root.genreCacheTime[genre] || 0;
        var fresh = cached && (now - cachedAt < 600000) && !force;
        if (fresh) {
            root.results = cached;
            resultModel.clear();
            for (var ci = 0; ci < cached.length; ci++) {
                var cr = cached[ci];
                resultModel.append({
                    "id": root.sanitize(cr.id),
                    "title": root.sanitize(cr.title),
                    "year": root.sanitize(cr.year || ""),
                    "rating": root.sanitize(cr.rating ? String(cr.rating) : "-"),
                    "cover": root.sanitize(cr.cover || ""),
                    "coverPath": root.sanitize(cr.cover || ""),
                    "duration": root.sanitize(cr.duration || "")
                });
            }
            root.view = "grid";
            root.statusText = resultModel.count + " " + genre + " titles";
            root.busy = false;
            return ;
        }
        root.busy = true;
        root.busyLabel = "Loading " + genre + " …";
        root.statusText = "";
        root.genreGen++;
        var gen = root.genreGen;
        request("search_genre", {
            "genre": genre,
            "q": genre,
            "page": 1
        }, function(resp, code) {
            if (gen !== root.genreGen)
                return ;

            root.busy = false;
            if (!resp || !resp.ok) {
                root.statusText = (resp && resp.error) || "Search failed";
                return ;
            }
            root.results = resp.items || [];
            var nc = {
            };
            for (var k in root.genreCache) nc[k] = root.genreCache[k]
            nc[genre] = root.results.slice();
            root.genreCache = nc;
            var nt = {
            };
            for (var k2 in root.genreCacheTime) nt[k2] = root.genreCacheTime[k2]
            nt[genre] = Date.now();
            root.genreCacheTime = nt;
            resultModel.clear();
            for (var i = 0; i < root.results.length; i++) {
                var r = root.results[i];
                resultModel.append({
                    "id": root.sanitize(r.id),
                    "title": root.sanitize(r.title),
                    "year": root.sanitize(r.year || ""),
                    "rating": root.sanitize(r.rating ? String(r.rating) : "-"),
                    "cover": root.sanitize(r.cover || ""),
                    "coverPath": root.sanitize(r.cover || ""),
                    "duration": root.sanitize(r.duration || "")
                });
            }
            root.view = "grid";
            root.statusText = resultModel.count + " " + genre + " titles";
        });
    }

    function refreshCurrent() {
        if (root.view === "home") {
            root.loadHome(true);
        } else if (root.view === "grid") {
            if (root.selectedGenre)
                root.searchByGenre(root.selectedGenre, true);
            else
                root.doSearch();
        } else if (root.view === "details" && root.currentId) {
            root.detailGen++;
            var gen = root.detailGen;
            root.busy = true;
            root.busyLabel = "Refreshing …";
            request("details", {
                "id": root.currentId
            }, function(resp) {
                if (gen !== root.detailGen)
                    return ;

                if (resp && resp.ok) {
                    root.details = root.sanitizeDetails(resp.value);
                    var cover = root.coverUrlOf(root.details);
                    if (cover)
                        detailPoster.source = cover;

                }
                root.loadEpisodes(root.currentId, gen);
            });
        } else {
            root.loadHome(true);
        }
    }

    // ---------------- open/close wiring ----------------
    function openFromHotkey() {
        root.loadHome(true);
        if (root.view === "details" && root.currentId) {
            root.loadEpisodes(root.currentId);
        }
        root.controller.show();
        Qt.callLater(function() {
            if (root.opened)
                searchField.forceActiveFocus();
        });
    }

    function close() {
        // Playback is launched in a separate process and the panel closes after
        // launch. Clear this one-shot UI lock so Play is enabled when reopened.
        root.playing = false;
        root.controller.hide();
    }

    function toggle() {
        if (root.opened)
            root.close();
        else
            root.openFromHotkey();
    }

    function closeForPopoutSwitch() {
        root.close();
    }

    function switchPanel(direction) {
        if (root.bar && typeof root.bar.switchPanelFrom === "function")
            return root.bar.switchPanelFrom(root.barIdentity, direction);

        return false;
    }

    moduleName: "tenzin.animechy"
    implicitWidth: 820
    implicitHeight: 580

    Process {
        id: apiProc

        property string collected: ""

        onExited: function(code, status) {
            var cb = root.cbChain;
            root.cbChain = null;
            var resp = null;
            try {
                resp = JSON.parse(apiProc.collected);
            } catch (e) {
            }
            if (cb)
                cb(resp, code);

            if (root.pending.length > 0) {
                var next = root.pending.shift();
                root._start(next.cmd, next.params, next.cb);
            }
        }

        stdout: SplitParser {
            onRead: function(data) {
                apiProc.collected += data;
            }
        }

    }

    Process {
        id: mpvProc
        property string collected: ""
        stdout: SplitParser { onRead: function(data) { mpvProc.collected += data } }
        onExited: function(code) {
            var resp = null;
            try { resp = JSON.parse(mpvProc.collected); } catch (e) {}
            mpvProc.collected = "";
            if (resp && resp.ok) {
                root.playing = true;
                root.mpvActive = true;
                root.statusText = "Playing in mpv";
                Qt.callLater(function() { root.close(); });
            } else {
                root.playing = false;
                root.mpvActive = false;
                root.statusText = (resp && resp.error) || (code !== 0 ? "Backend could not start mpv" : "Playback request failed");
            }
        }
    }

    Process {
        id: prefetchProc

        property string collected: ""

        onExited: function(code) {
            try {
                JSON.parse(prefetchProc.collected);
            } catch (e) {
            }
        }

        stdout: SplitParser {
            onRead: function(data) {
                prefetchProc.collected += data;
            }
        }

    }

    Process {
        id: streamsProc
        property string collected: ""
        property var cbChain: null
        property var pendingRequest: null
        property int gen: 0
        onExited: function(code, status) {
            var cb = streamsProc.cbChain;
            streamsProc.cbChain = null;
            var resp = null;
            try { resp = JSON.parse(streamsProc.collected); } catch(e){}
            streamsProc.collected = "";
            if (cb) cb(resp, code);
            var pending=streamsProc.pendingRequest;
            streamsProc.pendingRequest=null;
            if (pending) root.loadStreams(pending.aid,pending.ep,pending.mode,pending.fallback,pending.episodeRef);
        }
        stdout: SplitParser { onRead: function(data){ streamsProc.collected += data } }
    }

    Process {
        id: historyLoadProc
        property string collected: ""
        onExited: function(code) {
            var raw = historyLoadProc.collected.trim();
            historyLoadProc.collected = "";
            if (!raw) return;
            try {
                var arr = JSON.parse(raw);
                if (Array.isArray(arr)) {
                    if (arr.length > root.maxHistory) arr = arr.slice(0, root.maxHistory);
                    root.searchHistory = arr;
                }
            } catch (e) {}
        }
        stdout: SplitParser { onRead: function(data){ historyLoadProc.collected += data } }
    }
    Process { id: historySaveProc }
    Process { id: watchedProc }
    Process {
        id: favoriteProc
        property string collected: ""
        stdout: SplitParser { onRead: function(data) { favoriteProc.collected += data } }
        onExited: function(code) {
            var result=null; try { result=JSON.parse(favoriteProc.collected); } catch(e) {}
            favoriteProc.collected="";
            if (result && result.ok) root.currentFavorite=!!result.favorite;
            else root.statusText=(result && result.error)||"Could not update favorite";
        }
    }

    Timer {
        interval: 1000
        repeat: true
        running: root.view === "downloads"
        onTriggered: root.refreshDownloads()
    }

    Timer {
        id: playbackWatcher
        interval: 1000
        repeat: true
        running: root.mpvActive
        onTriggered: {
            request("playback_status", {}, function(resp) {
                if (resp && resp.ok && resp.active_players === 0) {
                    root.mpvActive = false;
                    root.loadHome(true);
                    if (root.view === "details" && root.currentId) {
                        root.loadEpisodes(root.currentId);
                    }
                }
            });
        }
    }

    Timer {
        interval: 1500
        repeat: false
        running: true
        onTriggered: {
            if (root.backendStatus.backend !== "running") { root.loadSettings(); root.loadAdminStatus(); }
        }
    }

    Process { id: openPathProc }

    // ---------------- UI ----------------
    ListModel {
        id: resultModel
    }

    ListModel {
        id: homeModel
    }

    Timer {
        id: suggestTimer

        interval: 380
        repeat: false
        onTriggered: {
            var q = searchField.text.trim();
            if (q.length < 2) {
                suggestionModel.clear();
                return ;
            }
            root.suggestGen++;
            var gen = root.suggestGen;
            request("suggest", {
                "q": q
            }, function(resp) {
                if (gen !== root.suggestGen)
                    return ;

                suggestionModel.clear();
                var list = (resp && resp.ok && resp.suggestions) ? resp.suggestions : [];
                for (var i = 0; i < list.length && i < 8; i++) suggestionModel.append({
                    "name": root.sanitize(list[i].name)
                })
            });
        }
    }

    ListModel {
        id: suggestionModel
    }

    KeyboardPanel {
        id: panel

        readonly property bool isSmallScreen: panel.screenW > 0 && panel.screenW < 1366
        readonly property bool isLargeScreen: panel.screenW >= 1920
        readonly property real uiScale: Math.min(1.4, Math.max(0.9, panel.screenW / 1920))

        anchorItem: root.anchorItem
        owner: root.barIdentity
        bar: root.bar
        open: root.opened
        centerOnBar: true
        margin: Style.gapsOut
        gap: Style.gapsOut
        contentWidth: isSmallScreen ? panel.fittedContentWidth(panel.screenW * 0.74) : isLargeScreen ? panel.fittedContentWidth(panel.screenW * 0.67) : panel.fittedContentWidth(panel.screenW * 0.71)
        contentHeight: isSmallScreen ? panel.fittedContentHeight(panel.screenH * 0.86, panel.screenH * 0.94) : isLargeScreen ? panel.fittedContentHeight(panel.screenH * 0.84, panel.screenH * 0.9) : panel.fittedContentHeight(panel.screenH * 0.82, panel.screenH * 0.9)

        ColumnLayout {
            id: mainColumn

            anchors.fill: parent
            anchors.margins: 14
            spacing: 10

            // header
            RowLayout {
                Layout.fillWidth: true
                spacing: Style.spacing.md

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 2

                    RowLayout {
                        spacing: 8

                        Text {
                            text: "ア"
                            font.family: Style.font.family
                            font.pixelSize: Style.font.title
                            color: Color.accent
                        }

                        Text {
                            text: "Animechy"
                            font.family: Style.font.family
                            font.pixelSize: Style.font.title
                            font.bold: true
                            color: Color.foreground
                        }

                        Rectangle {
                            width: 1
                            height: 18
                            color: Color.foreground
                            opacity: 0.12
                            Layout.leftMargin: 4
                            Layout.rightMargin: 4
                        }

                        Text {
                            text: root.view === "details" ? "Details" : root.view === "grid" ? "Results" : root.view === "downloads" ? "Downloads" : root.view === "settings" ? "Settings" : "Discover"
                            font.family: Style.font.family
                            font.pixelSize: Style.font.bodySmall
                            color: Qt.darker(Color.foreground, 1.25)
                            font.capitalization: Font.AllUppercase
                        }

                        Item {
                            Layout.fillWidth: true
                        }

                        RowLayout {
                            spacing: 6
                            visible: root.busy || root.playing

                            Rectangle {
                                width: 8
                                height: 8
                                radius: 4
                                color: Color.accent
                                opacity: 0.9
                                visible: root.busy

                                SequentialAnimation on opacity {
                                    running: root.busy
                                    loops: Animation.Infinite

                                    NumberAnimation {
                                        from: 0.4
                                        to: 1
                                        duration: 700
                                    }

                                    NumberAnimation {
                                        from: 1
                                        to: 0.4
                                        duration: 700
                                    }

                                }

                            }

                            Text {
                                textFormat: Text.PlainText
                                text: root.busy ? root.busyLabel : "Playing"
                                font.family: Style.font.family
                                font.pixelSize: Style.font.caption
                                color: Color.accent
                            }

                        }

                    }

                    RowLayout {
                        spacing: 4
                        Button { text: "Home"; fontSize: Style.font.caption; selected: root.view === "home"; onClicked: root.goHome() }
                        Button { text: "Favorites"; fontSize: Style.font.caption; selected: root.view === "grid" && root.homeKind === "favorites"; onClicked: root.showFavorites() }
                        Button { text: "History"; fontSize: Style.font.caption; selected: root.view === "grid" && root.homeKind === "history"; onClicked: root.showWatchHistory() }
                        Button { text: "Downloads"; fontSize: Style.font.caption; selected: root.view === "downloads"; onClicked: root.showDownloads() }
                        Button { text: "Settings"; fontSize: Style.font.caption; selected: root.view === "settings"; onClicked: root.showSettings() }
                    }

                    Text {
                        textFormat: Text.PlainText
                        text: root.statusText
                        font.family: Style.font.family
                        font.pixelSize: Style.font.caption - 1
                        color: Qt.darker(Color.foreground, 1.35)
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                        maximumLineCount: 1
                    }

                }

                Button {
                    text: "↻"
                    tooltipText: "Refresh"
                    fontSize: Style.font.body
                    horizontalPadding: 10
                    verticalPadding: 5
                    onClicked: root.refreshCurrent()
                }

                Button {
                    text: "✕"
                    tooltipText: "Close"
                    fontSize: Style.font.body
                    horizontalPadding: 12
                    verticalPadding: 6
                    onClicked: root.close()
                }

            }

            PanelSeparator {
                Layout.fillWidth: true
                opacity: 0.5
                visible: root.view === "home" || root.view === "grid" || root.view === "details"
            }

            // search
            RowLayout {
                Layout.fillWidth: true
                spacing: 8
                visible: root.view === "home" || root.view === "grid" || root.view === "details"

                TextField {
                    id: searchField

                    Layout.fillWidth: true
                    placeholderText: "Search anime..."
                    onAccepted: root.doSearch()
                    onTextChanged: if (text.length >= 2) root.debounceSuggest()
                    Keys.onEscapePressed: {
                        if (searchField.hasFocus) {
                            clear();
                            suggestionModel.clear();
                        } else {
                            root.close();
                        }
                    }
                }

                Button {
                    text: "Search"
                    iconText: "\uf002"
                    selected: true
                    onClicked: root.doSearch()
                }

            }

            // search history chips (only when search input is empty & focused)
            Item {
                id: historyContainer
                Layout.fillWidth: true
                Layout.preferredHeight: 32
                visible: (root.view === "home" || root.view === "grid" || root.view === "details") && root.searchHistory.length > 0 && searchField.activeFocus && searchField.text.trim().length < 2
                clip: true

                ListView {
                    anchors.fill: parent
                    orientation: ListView.Horizontal
                    spacing: 6
                    clip: true
                    boundsBehavior: Flickable.StopAtBounds
                    model: root.searchHistory
                    delegate: Button {
                        text: modelData
                        fontSize: Style.font.caption - 1
                        selected: root.query === modelData
                        onClicked: {
                            searchField.text = modelData;
                            root.doSearch();
                        }
                    }
                }
            }

            // live autocomplete suggestions (horizontal scrollable bar)
            Item {
                id: suggestionsContainer
                Layout.fillWidth: true
                Layout.preferredHeight: 32
                visible: (root.view === "home" || root.view === "grid" || root.view === "details") && suggestionModel.count > 0 && searchField.activeFocus && searchField.text.trim().length >= 2
                clip: true

                ListView {
                    anchors.fill: parent
                    orientation: ListView.Horizontal
                    spacing: 6
                    clip: true
                    boundsBehavior: Flickable.StopAtBounds
                    model: suggestionModel
                    delegate: Button {
                        text: model.name
                        fontSize: Style.font.caption
                        horizontalPadding: 10
                        verticalPadding: 4
                        onClicked: {
                            searchField.text = model.name;
                            root.doSearch();
                        }
                    }
                }
            }

            // genre selector — visible for home + grid
            RowLayout {
                Layout.fillWidth: true
                visible: root.view === "home" || root.view === "grid"
                spacing: 6
                Button { text: "Genres  ▾"; fontSize: Style.font.caption; selected: root.selectedGenre !== ""; onClicked: root.genresExpanded=!root.genresExpanded }
                Text { text: root.selectedGenre || "Filter discovery"; font.pixelSize: Style.font.caption; color: Qt.darker(Color.foreground, 1.3) }
            }

            Flow {
                Layout.fillWidth: true
                visible: root.genresExpanded && (root.view === "home" || root.view === "grid")
                spacing: 5
                Repeater {
                    model: root.genres
                    Button {
                        text: modelData; fontSize: Style.font.caption - 1; horizontalPadding: 8; verticalPadding: 3
                        enabled: !root.busy && !root.homeLoading
                        selected: root.selectedGenre === modelData || (modelData === "All" && root.selectedGenre === "")
                        onClicked: { root.searchByGenre(modelData); root.genresExpanded=false }
                    }
                }
            }

            // body
            Item {
                id: body

                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true

                // ---- home (discover) ----
                Item {
                    anchors.fill: parent
                    visible: root.view === "home"

                    ColumnLayout {
                        anchors.fill: parent
                        spacing: 8

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 8

                            Text {
                                Layout.fillWidth: true
                                text: root.homeLoading ? "Loading …" : (homeModel.count ? (root.homeKind === "recent" ? "Continue Watching" : root.homeKind === "history" ? "Watch History" : root.homeKind === "favorites" ? "Favorites • stored on this device" : root.homeKind === "downloads" ? "Downloads" : "Discover • tap any title") : root.homeKind === "history" ? "Watch History" : root.homeKind === "favorites" ? "Favorites" : root.homeKind === "downloads" ? "Downloads" : "Discover")
                                font.family: Style.font.family
                                font.pixelSize: Style.font.body
                                font.bold: true
                                color: Color.accent
                            }

                            Button {
                                text: "Continue Watching"
                                fontSize: Style.font.caption
                                selected: root.homeKind === "recent"
                                onClicked: root.loadHome(true)
                            }
                            Button {
                                text: "Favorites"
                                fontSize: Style.font.caption
                                selected: root.homeKind === "favorites"
                                onClicked: root.showFavorites()
                            }
                            Button { text: "History"; fontSize: Style.font.caption; selected: root.homeKind === "history"; onClicked: root.showWatchHistory() }
                            Button { text: "Clear history"; fontSize: Style.font.caption; visible: root.homeKind === "history"; onClicked: root.clearWatchHistory() }

                        }

                        GridView {
                            id: homeGrid

                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            clip: true
                            cacheBuffer: 400
                            flickableDirection: Flickable.VerticalFlick
                            boundsBehavior: Flickable.StopAtBounds
                            maximumFlickVelocity: 4000
                            reuseItems: true
                            visible: !root.homeLoading
                            model: homeModel
                            cellWidth: Math.round(168 * panel.uiScale)
                            cellHeight: Math.round(236 * panel.uiScale)

                            delegate: Item {
                                id: homeDelegate

                                property bool hovered: homeMouse.containsMouse

                                width: homeGrid.cellWidth
                                height: homeGrid.cellHeight

                                Column {
                                    anchors.fill: parent
                                    anchors.margins: 6
                                    spacing: 5

                                    Rectangle {
                                        width: parent.width
                                        height: parent.height * 0.74
                                        radius: Style.cornerRadius
                                        color: Color.surface ?? Qt.darker(Color.foreground, 2.15)
                                        border.width: homeDelegate.hovered ? 1 : 0
                                        border.color: homeDelegate.hovered ? Color.accent : "transparent"
                                        clip: true

                                        Image {
                                            anchors.fill: parent
                                            source: root._sanitizeCoverUrl(model.coverPath || model.cover || "")
                                            fillMode: Image.PreserveAspectCrop
                                            sourceSize.width: Math.round(homeGrid.cellWidth * 1.5)
                                            sourceSize.height: Math.round(homeGrid.cellHeight * 1.5)
                                            visible: source !== ""
                                            asynchronous: true
                                            cache: true
                                        }

                                        Rectangle {
                                            anchors.left: parent.left
                                            anchors.right: parent.right
                                            anchors.bottom: parent.bottom
                                            height: 22
                                            color: "#66000000"
                                            visible: model.rating && model.rating !== "-"

                                            Text {
                                                anchors.centerIn: parent
                                                textFormat: Text.PlainText
                                                text: "★ " + model.rating
                                                font.family: Style.font.family
                                                font.pixelSize: 10
                                                color: "white"
                                                font.bold: true
                                            }

                                        }

                                        Text {
                                            anchors.centerIn: parent
                                            visible: !model.cover && !model.coverPath
                                            text: "ア"
                                            font.family: Style.font.family
                                            font.pixelSize: 30
                                            color: Qt.darker(Color.foreground, 1.3)
                                        }

                                    }

                                    Text {
                                        width: parent.width
                                        textFormat: Text.PlainText
                                        text: model.title
                                        elide: Text.ElideRight
                                        font.family: Style.font.family
                                        font.pixelSize: Style.font.caption
                                        color: Color.foreground
                                        maximumLineCount: 1
                                    }

                                    Text {
                                        textFormat: Text.PlainText
                                        text: model.episode ? ("Continue • episode " + model.episode + (model.position > 0 ? " • " + Math.floor(model.position/60) + ":" + ("0"+Math.floor(model.position%60)).slice(-2) : "")) : ((model.year ? model.year : "—") + "  ★ " + model.rating)
                                        elide: Text.ElideRight
                                        font.family: Style.font.family
                                        font.pixelSize: Style.font.caption - 2
                                        color: Qt.darker(Color.foreground, 1.5)
                                    }

                                }

                                MouseArea {
                                    id: homeMouse

                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    hoverEnabled: true
                                    enabled: !root.busy && !root.homeLoading && root.homeKind !== "downloads"
                                    onEntered: homeHoverTimer.restart()
                                    onExited: homeHoverTimer.stop()
                                    onClicked: {
                                        if (!root.busy && !root.homeLoading)
                                            root.openDetails(index, true);

                                    }

                                    Timer {
                                        id: homeHoverTimer

                                        interval: 380
                                        repeat: false
                                        onTriggered: root.prefetchDetails(model.id)
                                    }

                                }

                            }

                        }

                        Text {
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            visible: root.homeLoading
                            text: "Loading highlights …"
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                            font.family: Style.font.family
                            font.pixelSize: Style.font.body
                            color: Qt.darker(Color.foreground, 1.5)
                        }

                        Text {
                            Layout.fillWidth: true
                            visible: !root.homeLoading && homeModel.count === 0
                            text: root.homeKind === "recent" ? "No recent episodes yet — search to start watching." : root.homeKind === "favorites" ? "No favorites yet — add one from its details." : "No titles yet — try Search above."
                            horizontalAlignment: Text.AlignHCenter
                            font.family: Style.font.family
                            font.pixelSize: Style.font.caption
                            color: Qt.darker(Color.foreground, 1.4)
                        }

                    }

                }

                // ---- results grid ----
                GridView {
                    id: grid

                    anchors.fill: parent
                    visible: root.view === "grid"
                    model: resultModel
                    clip: true
                    cacheBuffer: 600
                    flickableDirection: Flickable.VerticalFlick
                    boundsBehavior: Flickable.StopAtBounds
                    maximumFlickVelocity: 4000
                    reuseItems: true
                    cellWidth: Math.round(168 * panel.uiScale)
                    cellHeight: Math.round(236 * panel.uiScale)

                    delegate: Item {
                        id: gridDelegate

                        property bool hovered: gridMouse.containsMouse

                        width: grid.cellWidth
                        height: grid.cellHeight

                        Column {
                            anchors.fill: parent
                            anchors.margins: 6
                            spacing: 5

                            Rectangle {
                                width: parent.width
                                height: parent.height * 0.74
                                radius: Style.cornerRadius
                                color: Color.surface ?? Qt.darker(Color.foreground, 2.15)
                                border.width: gridDelegate.hovered ? 1 : 0
                                border.color: gridDelegate.hovered ? Color.accent : "transparent"
                                clip: true

                                Image {
                                    anchors.fill: parent
                                    source: root._sanitizeCoverUrl(model.coverPath || model.cover || "")
                                    fillMode: Image.PreserveAspectCrop
                                    sourceSize.width: Math.round(grid.cellWidth * 1.5)
                                    sourceSize.height: Math.round(grid.cellHeight * 1.5)
                                    visible: source !== ""
                                    asynchronous: true
                                    cache: true
                                }

                                Rectangle {
                                    anchors.left: parent.left
                                    anchors.right: parent.right
                                    anchors.bottom: parent.bottom
                                    height: 22
                                    radius: 0
                                    visible: model.rating && model.rating !== "-"
                                    color: "#66000000"

                                    Text {
                                        anchors.centerIn: parent
                                        textFormat: Text.PlainText
                                        text: "★ " + model.rating
                                        font.family: Style.font.family
                                        font.pixelSize: 10
                                        color: "white"
                                        font.bold: true
                                    }

                                }

                                Text {
                                    anchors.centerIn: parent
                                    visible: !model.cover && !model.coverPath
                                    text: "ア"
                                    font.family: Style.font.family
                                    font.pixelSize: 30
                                    color: Qt.darker(Color.foreground, 1.3)
                                }

                                Behavior on border.width {
                                    NumberAnimation {
                                        duration: 100
                                    }

                                }

                            }

                            Text {
                                width: parent.width
                                textFormat: Text.PlainText
                                text: model.title
                                elide: Text.ElideRight
                                font.family: Style.font.family
                                font.pixelSize: Style.font.caption
                                color: Color.foreground
                                maximumLineCount: 1
                            }

                            Text {
                                textFormat: Text.PlainText
                                text: (model.year ? model.year : "—") + "  ★ " + model.rating
                                elide: Text.ElideRight
                                font.family: Style.font.family
                                font.pixelSize: Style.font.caption - 2
                                color: Qt.darker(Color.foreground, 1.5)
                            }

                        }

                        MouseArea {
                            id: gridMouse

                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            hoverEnabled: true
                            enabled: !root.busy
                            onEntered: {
                                if (!root.busy) {
                                    hoverTimer.restart();
                                }
                            }
                            onExited: hoverTimer.stop()
                            onClicked: {
                                if (!root.busy)
                                    root.openDetails(index, false);

                            }

                            Timer {
                                id: hoverTimer

                                interval: 380
                                repeat: false
                                onTriggered: root.prefetchDetails(model.id)
                            }

                        }

                    }

                }

                // ---- details ----
                Item {
                    anchors.fill: parent
                    visible: root.view === "details"

                    RowLayout {
                        anchors.fill: parent
                        spacing: 14

                        // poster
                        Rectangle {
                            Layout.preferredWidth: Math.round(170 * panel.uiScale)
                            Layout.fillHeight: true
                            radius: Style.cornerRadius
                            color: Qt.darker(Color.foreground, 2.2)
                            clip: true

                            Image {
                                id: detailPoster

                                anchors.fill: parent
                                fillMode: Image.PreserveAspectCrop
                                sourceSize.width: 480
                                sourceSize.height: 720
                                asynchronous: true
                                cache: true
                                source: ""
                            }

                            Text {
                                anchors.centerIn: parent
                                visible: detailPoster.source === ""
                                text: "ア"
                                font.family: Style.font.family
                                font.pixelSize: 40
                                color: Qt.darker(Color.foreground, 1.3)
                            }

                        }

                        // info column
                        ColumnLayout {
                            id: detailsContent

                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            spacing: 8

                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 8

                                Text {
                                    Layout.fillWidth: true
                                    textFormat: Text.PlainText
                                    text: root.currentTitle
                                    elide: Text.ElideRight
                                    font.family: Style.font.family
                                    font.pixelSize: Style.font.title
                                    font.bold: true
                                    color: Color.foreground
                                }

                                Button {
                                    text: "← Back"
                                    fontSize: Style.font.caption
                                    onClicked: root.goHome()
                                }

                                Button {
                                    text: root.currentFavorite ? "♥ Favorite" : "♡ Favorite"
                                    fontSize: Style.font.caption
                                    onClicked: root.setFavorite()
                                }

                            }

                            Text {
                                textFormat: Text.PlainText
                                Layout.fillWidth: true
                                text: {
                                    if (!root.details)
                                        return "";

                                    var parts = [];
                                    var year = root.details.year ? String(root.details.year) : "";
                                    if (year)
                                        parts.push(year);

                                    if (root.details.genre)
                                        parts.push(root.details.genre);

                                    if (root.details.duration)
                                        parts.push(root.details.duration);

                                    if (root.details.imdbRatingValue)
                                        parts.push("★ " + root.details.imdbRatingValue);

                                    return parts.join("  •  ");
                                }
                                wrapMode: Text.WordWrap
                                font.family: Style.font.family
                                font.pixelSize: Style.font.caption
                                color: Qt.darker(Color.foreground, 1.4)
                            }

                            Text {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 48
                                textFormat: Text.PlainText
                                text: (root.details && (root.details.intro || root.details.description || "")) || ""
                                wrapMode: Text.WordWrap
                                elide: Text.ElideRight
                                font.family: Style.font.family
                                font.pixelSize: Style.font.caption
                                color: Qt.darker(Color.foreground, 1.4)
                            }

                            // sub/dub toggle
                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 6

                                Text {
                                    text: "Audio:"
                                    font.family: Style.font.family
                                    font.pixelSize: Style.font.caption
                                    color: Qt.darker(Color.foreground, 1.2)
                                    font.bold: true
                                }

                                Button {
                                    text: "Sub"
                                    visible: root.currentId.indexOf("hiyori:") !== 0 || root.serversForMode("sub").length > 0
                                    fontSize: Style.font.caption
                                    selected: root.mode === "sub"
                                    onClicked: {
                                        if (root.mode !== "sub") {
                                            root.mode = "sub";
                                            if (root.serversForMode("sub").length > 0 || root.currentId.indexOf("hiyori:") === 0) root.selectEpisode(root.currentEpisodeObject());
                                            else if (root.curEp)
                                                root.loadStreams(root.currentId, root.curEp, "sub");

                                        }
                                    }
                                }

                                Button {
                                    text: "Dub"
                                    visible: root.currentId.indexOf("hiyori:") !== 0 || root.serversForMode("dub").length > 0
                                    fontSize: Style.font.caption
                                    selected: root.mode === "dub"
                                    onClicked: {
                                        if (root.mode !== "dub") {
                                            root.mode = "dub";
                                            if (root.serversForMode("dub").length > 0 || root.currentId.indexOf("hiyori:") === 0) root.selectEpisode(root.currentEpisodeObject());
                                            else if (root.curEp)
                                                root.loadStreams(root.currentId, root.curEp, "dub");

                                        }
                                    }
                                }

                                Item {
                                    Layout.fillWidth: true
                                }

                                Text {
                                    text: root.episodes.length ? root.episodes.length + " episodes" : ""
                                    font.family: Style.font.family
                                    font.pixelSize: Style.font.caption - 1
                                    color: Qt.darker(Color.foreground, 1.4)
                                }

                            }

                            Flow {
                                Layout.fillWidth: true
                                visible: root.serversForMode(root.mode).length > 0
                                spacing: 6
                                Text { text: "Server:"; font.family: Style.font.family; font.pixelSize: Style.font.caption; color: Color.foreground }
                                Repeater {
                                    model: root.serversForMode(root.mode)
                                    Button {
                                        text: String(modelData.server).toUpperCase()
                                        tooltipText: modelData.title || modelData.episode_id
                                        fontSize: Style.font.caption
                                        selected: root.selectedServer && root.selectedServer.episode_id === modelData.episode_id
                                        onClicked: root.selectServer(modelData)
                                    }
                                }
                            }

                            // seasons
                            Flow {
                                Layout.fillWidth: true
                                visible: root.seasons.length > 1
                                spacing: 6

                                Repeater {
                                    model: root.seasons

                                    Button {
                                        text: "S" + (index + 1)
                                        tooltipText: modelData.title
                                        fontSize: Style.font.caption
                                        selected: index === root.curSeasonIdx
                                        onClicked: root.selectSeason(index)
                                    }

                                }

                            }

                            // episodes — light grey fill + outline enclosing all episodes
                            Rectangle {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 110
                                visible: root.episodes.length > 0
                                radius: Style.cornerRadius
                                color: Util.alpha(Color.foreground, 0.07)
                                border.width: 1
                                border.color: Util.alpha(Color.foreground, 0.18)
                                clip: true

                                Flickable {
                                    id: epFlick

                                    anchors.fill: parent
                                    anchors.margins: 6
                                    clip: true
                                    contentWidth: width
                                    contentHeight: episodeFlow.implicitHeight
                                    boundsBehavior: Flickable.StopAtBounds
                                    flickableDirection: Flickable.VerticalFlick

                                    Flow {
                                        id: episodeFlow

                                        width: epFlick.width
                                        spacing: 6

                                        Repeater {
                                            id: episodeRepeater

                                            model: root._episodeCount

                                            Button {
                                                property var epObj: (index < root.episodes.length) ? root.episodes[index] : null
                                                property string epNum: epObj ? epObj.number : String(index + 1)
                                                property bool isFiller: epObj ? epObj.filler : false

                                                text: "E" + epNum + (epObj && epObj.watch_status === "watched" ? " ✓" : epObj && epObj.watch_status === "watching" ? " ▶" : " ○")
                                                fontSize: Style.font.caption
                                                selected: epNum === root.curEp
                                                opacity: isFiller ? 0.6 : 1
                                                onClicked: {
                                                    root.curEp = epNum;
                                                    if (epObj) root.selectEpisode(epObj);
                                                    else root.loadStreams(root.currentId, epNum, root.mode);
                                                }
                                            }

                                        }

                                    }

                                    ScrollBar.vertical: ScrollBar {
                                        policy: ScrollBar.AsNeeded
                                    }

                                }

                            }

                            PanelSeparator {
                                Layout.fillWidth: true
                            }

                            // streams
                            Text {
                                Layout.fillWidth: true
                                text: "Streams" + (root.curEp ? " — Episode " + root.curEp + " (" + root.mode.toUpperCase() + ")" : "") +
                                      ((root.resumePosition > 0 && root.playbackSettings.resume_playback !== false) ?
                                      (" • Resumes at " + Math.floor(root.resumePosition/60) + ":" + ("0"+Math.floor(root.resumePosition%60)).slice(-2)) : "")
                                font.family: Style.font.family
                                font.pixelSize: Style.font.caption
                                font.bold: true
                                color: Color.accent
                            }

                            ListView {
                                Layout.fillWidth: true
                                Layout.fillHeight: true
                                clip: true
                                spacing: 4
                                cacheBuffer: 200
                                boundsBehavior: Flickable.StopAtBounds
                                maximumFlickVelocity: 3500
                                reuseItems: true
                                model: root.streams

                                Text {
                                    anchors.centerIn: parent
                                    visible: root.streams.length === 0 && !root.busy
                                    text: {
                                        if (!root.episodes.length) return "No streams";
                                        if (root.curEp) return "No streams for Episode " + root.curEp + " — tap episode again to retry";
                                        return "Select an episode to load streams";
                                    }
                                    font.family: Style.font.family
                                    font.pixelSize: Style.font.caption
                                    color: Qt.darker(Color.foreground, 1.5)
                                }
                                Text {
                                    anchors.centerIn: parent
                                    visible: root.streams.length === 0 && root.busy && root.curEp
                                    text: "Loading streams for Episode " + root.curEp + " …"
                                    font.family: Style.font.family
                                    font.pixelSize: Style.font.caption
                                    color: Color.accent
                                }

                                delegate: Button {
                                    width: parent ? parent.width : 0
                                    text: {
                                        var q = modelData.quality || (modelData.resolution ? modelData.resolution + "p" : "Stream");
                                        var bw = modelData.bandwidth ? Math.round(modelData.bandwidth / 1000) + " kbps" : "";
                                        return q + (bw ? "  •  " + bw : "") + (modelData.codecName ? "  •  " + String(modelData.codecName).toUpperCase() : "");
                                    }
                                    leftAlign: true
                                    selected: index === root.selStream
                                    fontSize: Style.font.caption
                                    onClicked: root.selectStream(index)
                                }

                            }

                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 8
                                Layout.bottomMargin: 6

                                Text {
                                    Layout.fillWidth: true
                                    textFormat: Text.PlainText
                                    text: root.statusText
                                    elide: Text.ElideRight
                                    font.family: Style.font.family
                                    font.pixelSize: Style.font.caption - 2
                                    color: Qt.darker(Color.foreground, 1.4)
                                }

                                Button {
                                    text: "↺ Start Over"
                                    visible: root.resumePosition > 0 && root.playbackSettings.resume_playback !== false
                                    enabled: root.selStream >= 0 && !root.playing
                                    fontSize: Style.font.caption
                                    onClicked: {
                                        root.resumePosition = 0;
                                        root.playExternal();
                                    }
                                }

                                Button {
                                    text: (root.resumePosition > 0 && root.playbackSettings.resume_playback !== false) ?
                                          ("▶ Resume Ep " + root.curEp + " (" + Math.floor(root.resumePosition/60) + ":" + ("0"+Math.floor(root.resumePosition%60)).slice(-2) + ")") :
                                          ("▶ Play Ep " + (root.curEp || "1"))
                                    selected: true
                                    enabled: root.selStream >= 0 && !root.playing
                                    onClicked: {
                                        root.playExternal();
                                    }
                                }

                                Button {
                                    text: "↓ Download"
                                    enabled: root.selStream >= 0 && !root.playing
                                    onClicked: root.downloadSelected()
                                }

                            }

                        }

                    }

                }

                // ---- downloads ----
                Item {
                    anchors.fill: parent
                    visible: root.view === "downloads"

                    ColumnLayout {
                        anchors.fill: parent
                        spacing: 8

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 8

                            Text {
                                text: "Downloads"
                                font.family: Style.font.family
                                font.pixelSize: Style.font.body
                                font.bold: true
                                color: Color.accent
                            }

                            Text {
                                Layout.fillWidth: true
                                text: root.downloadDestination
                                color: Qt.darker(Color.foreground, 1.4)
                                elide: Text.ElideMiddle
                                font.family: Style.font.family
                                font.pixelSize: Style.font.caption
                            }

                            Button {
                                text: "Open folder"
                                fontSize: Style.font.caption
                                onClicked: root.openDownloadFolder()
                            }

                            Button {
                                text: "Refresh"
                                fontSize: Style.font.caption
                                onClicked: root.refreshDownloads()
                            }
                        }

                        ListView {
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            clip: true
                            spacing: 8
                            model: root.downloadItems
                            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

                            delegate: Rectangle {
                                required property var modelData
                                width: ListView.view.width
                                height: downloadRow.implicitHeight + 16
                                color: Util.alpha(Color.foreground, 0.05)
                                radius: Style.cornerRadius
                                border.color: Util.alpha(Color.foreground, 0.12)
                                border.width: 1

                                RowLayout {
                                    id: downloadRow
                                    anchors.fill: parent
                                    anchors.margins: 8
                                    spacing: 12

                                    Rectangle {
                                        Layout.preferredWidth: 50
                                        Layout.preferredHeight: 70
                                        radius: Math.max(2, Style.cornerRadius - 2)
                                        color: Util.alpha(Color.foreground, 0.08)
                                        clip: true

                                        Image {
                                            anchors.fill: parent
                                            source: root._sanitizeCoverUrl(modelData.cover || "")
                                            fillMode: Image.PreserveAspectCrop
                                            sourceSize.width: 100
                                            sourceSize.height: 140
                                            asynchronous: true
                                            cache: true
                                            visible: source !== ""
                                        }

                                        Text {
                                            anchors.centerIn: parent
                                            visible: !modelData.cover
                                            text: "ア"
                                            font.family: Style.font.family
                                            font.pixelSize: 20
                                            color: Qt.darker(Color.foreground, 1.4)
                                        }
                                    }

                                    ColumnLayout {
                                        Layout.fillWidth: true
                                        spacing: 4

                                        RowLayout {
                                            Layout.fillWidth: true
                                            spacing: 6
                                            Text {
                                                Layout.fillWidth: true
                                                text: modelData.title || "Anime"
                                                color: Color.foreground
                                                font.bold: true
                                                font.family: Style.font.family
                                                font.pixelSize: Style.font.bodySmall
                                                elide: Text.ElideRight
                                            }
                                            Rectangle {
                                                radius: 4
                                                color: modelData.status === "completed" ? Util.alpha(Color.accent, 0.15) :
                                                       modelData.status === "downloading" ? Util.alpha(Color.accent, 0.25) :
                                                       modelData.status === "paused" ? Util.alpha(Color.foreground, 0.1) :
                                                       Util.alpha("#ff6b6b", 0.18)
                                                border.width: 1
                                                border.color: modelData.status === "failed" ? "#ff6b6b" : Color.accent
                                                height: 18
                                                width: statusLabel.implicitWidth + 10
                                                Text {
                                                    id: statusLabel
                                                    anchors.centerIn: parent
                                                    text: modelData.status.toUpperCase()
                                                    color: modelData.status === "failed" ? "#ff6b6b" : Color.accent
                                                    font.family: Style.font.family
                                                    font.pixelSize: Style.font.caption - 2
                                                    font.bold: true
                                                }
                                            }
                                        }

                                        Text {
                                            Layout.fillWidth: true
                                            text: (modelData.episode ? "Episode " + modelData.episode : modelData.path) + (modelData.quality ? " · " + modelData.quality : "") + (modelData.language ? " · " + modelData.language.toUpperCase() : "")
                                            color: Qt.darker(Color.foreground, 1.35)
                                            font.family: Style.font.family
                                            font.pixelSize: Style.font.caption
                                            elide: Text.ElideMiddle
                                        }

                                        Rectangle {
                                            Layout.fillWidth: true
                                            height: 6
                                            radius: 3
                                            color: Util.alpha(Color.foreground, 0.12)
                                            clip: true
                                            visible: modelData.percent !== null && modelData.percent !== undefined
                                            Rectangle {
                                                height: parent.height
                                                width: parent.width * Math.min(1, Math.max(0, (Number(modelData.percent || 0) / 100)))
                                                radius: 3
                                                color: modelData.status === "failed" ? "#ff6b6b" : Color.accent
                                            }
                                        }

                                        Text {
                                            Layout.fillWidth: true
                                            text: (modelData.percent === null || modelData.percent === undefined ? "Progress unavailable" : Number(modelData.percent).toFixed(1) + "%") +
                                                  " · " + root.formatBytes(modelData.downloaded || 0) + " / " + (modelData.total_size ? root.formatBytes(modelData.total_size) : "size unknown") +
                                                  " · " + (modelData.speed ? root.formatBytes(modelData.speed) + "/s" : "speed —") +
                                                  (modelData.progress_basis ? " · " + modelData.progress_basis : "") +
                                                  " · ETA " + root.formatEta(modelData.eta)
                                            color: Qt.darker(Color.foreground, 1.4)
                                            font.family: Style.font.family
                                            font.pixelSize: Style.font.caption - 1
                                            elide: Text.ElideRight
                                        }

                                        Text {
                                            Layout.fillWidth: true
                                            visible: !!modelData.error
                                            text: modelData.error
                                            color: "#ff6b6b"
                                            font.family: Style.font.family
                                            font.pixelSize: Style.font.caption - 1
                                            wrapMode: Text.Wrap
                                            maximumLineCount: 2
                                        }

                                        RowLayout {
                                            Layout.fillWidth: true
                                            spacing: 6
                                            Button { text: "Pause"; fontSize: Style.font.caption - 1; visible: modelData.status === "queued" || modelData.status === "downloading"; onClicked: root.downloadAction(modelData.id, "pause") }
                                            Button { text: "Resume"; fontSize: Style.font.caption - 1; visible: modelData.status === "paused"; onClicked: root.downloadAction(modelData.id, "resume") }
                                            Button { text: "Cancel"; fontSize: Style.font.caption - 1; visible: ["queued", "downloading", "paused"].indexOf(modelData.status) >= 0; onClicked: root.downloadAction(modelData.id, "cancel") }
                                            Button { text: "Retry"; fontSize: Style.font.caption - 1; visible: modelData.status === "failed" || modelData.status === "cancelled"; onClicked: root.retryDownload(modelData.id) }
                                            Button { text: "Delete"; fontSize: Style.font.caption - 1; visible: ["failed", "cancelled", "completed"].indexOf(modelData.status) >= 0; onClicked: root.requestDownloadDelete(modelData.id) }
                                            Button { text: "Open file"; fontSize: Style.font.caption - 1; visible: modelData.status === "completed"; onClicked: root.openPath(modelData.path) }
                                            Button { text: "Open folder"; fontSize: Style.font.caption - 1; visible: modelData.status === "completed"; onClicked: root.openPath(root.parentPath(modelData.path)) }
                                        }
                                    }
                                }
                            }

                            Text {
                                anchors.centerIn: parent
                                visible: root.downloadItems.length === 0
                                text: "No downloads yet"
                                font.family: Style.font.family
                                font.pixelSize: Style.font.body
                                color: Qt.darker(Color.foreground, 1.4)
                            }
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 8
                            Text {
                                Layout.fillWidth: true
                                text: "Failed and cancelled items keep partial files until deleted."
                                color: Qt.darker(Color.foreground, 1.4)
                                font.family: Style.font.family
                                font.pixelSize: Style.font.caption
                            }
                            Button {
                                text: "Clear failed history"
                                fontSize: Style.font.caption
                                onClicked: root.openModal("clear_failed")
                            }
                        }
                    }
                }

                // ---- settings ----
                Item {
                    anchors.fill: parent
                    visible: root.view === "settings"

                    ScrollView {
                        id: settingsScroll
                        anchors.fill: parent
                        contentWidth: availableWidth
                        clip: true
                        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

                        ColumnLayout {
                            width: settingsScroll.availableWidth
                            spacing: 12

                            // 1. PROVIDERS & STREAMING
                            PanelSectionHeader { text: "PROVIDERS & SOURCES" }

                            Rectangle {
                                Layout.fillWidth: true
                                height: provCol.implicitHeight + 20
                                color: Util.alpha(Color.foreground, 0.05)
                                radius: Style.cornerRadius
                                border.width: 1
                                border.color: Util.alpha(Color.foreground, 0.12)

                                ColumnLayout {
                                    id: provCol
                                    anchors.fill: parent
                                    anchors.margins: 12
                                    spacing: 8

                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: 8

                                        ColumnLayout {
                                            Layout.fillWidth: true
                                            spacing: 2

                                            Text {
                                                text: "Anime Provider"
                                                color: Color.foreground
                                                font.family: Style.font.family
                                                font.pixelSize: Style.font.bodySmall
                                                font.bold: true
                                            }
                                            Text {
                                                text: "Choose which provider to use for anime search and episode streams."
                                                color: Qt.darker(Color.foreground, 1.35)
                                                font.family: Style.font.family
                                                font.pixelSize: Style.font.caption
                                            }
                                        }

                                        ButtonGroup {
                                            options: [
                                                { label: "Auto / Both", value: "all" },
                                                { label: "HiAnime", value: "hianime" },
                                                { label: "Hiyori", value: "hiyori" }
                                            ]
                                            value: String(root.playbackSettings.preferred_provider || "all")
                                            onChanged: function(val) { root.saveSetting("preferred_provider", val); }
                                        }
                                    }

                                    PanelSeparator { Layout.fillWidth: true }

                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: 8

                                        ColumnLayout {
                                            Layout.fillWidth: true
                                            spacing: 2

                                            Text {
                                                text: "Default Stream Quality"
                                                color: Color.foreground
                                                font.family: Style.font.family
                                                font.pixelSize: Style.font.bodySmall
                                                font.bold: true
                                            }
                                            Text {
                                                text: "Preferred stream resolution requested when loading episodes."
                                                color: Qt.darker(Color.foreground, 1.35)
                                                font.family: Style.font.family
                                                font.pixelSize: Style.font.caption
                                            }
                                        }

                                        ButtonGroup {
                                            options: [
                                                { label: "Auto", value: "Auto" },
                                                { label: "1080p", value: "1080p" },
                                                { label: "720p", value: "720p" },
                                                { label: "480p", value: "480p" }
                                            ]
                                            value: String(root.playbackSettings.default_quality || "Auto")
                                            onChanged: function(val) { root.saveSetting("default_quality", val); }
                                        }
                                    }

                                    PanelSeparator { Layout.fillWidth: true }

                                    Toggle {
                                        Layout.fillWidth: true
                                        label: "Try alternative server automatically if stream fails"
                                        checked: root.playbackSettings.auto_fallback === true
                                        onClicked: root.saveSetting("auto_fallback", !root.playbackSettings.auto_fallback)
                                    }
                                }
                            }

                            // 2. PLAYBACK & AUDIO
                            PanelSectionHeader { text: "PLAYBACK & AUDIO" }

                            Rectangle {
                                Layout.fillWidth: true
                                height: pbCol.implicitHeight + 20
                                color: Util.alpha(Color.foreground, 0.05)
                                radius: Style.cornerRadius
                                border.width: 1
                                border.color: Util.alpha(Color.foreground, 0.12)

                                ColumnLayout {
                                    id: pbCol
                                    anchors.fill: parent
                                    anchors.margins: 12
                                    spacing: 8

                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: 8

                                        ColumnLayout {
                                            Layout.fillWidth: true
                                            spacing: 2

                                            Text {
                                                text: "Default Audio Language"
                                                color: Color.foreground
                                                font.family: Style.font.family
                                                font.pixelSize: Style.font.bodySmall
                                                font.bold: true
                                            }
                                            Text {
                                                text: "Preferred audio language track (Sub / Dub)."
                                                color: Qt.darker(Color.foreground, 1.35)
                                                font.family: Style.font.family
                                                font.pixelSize: Style.font.caption
                                            }
                                        }

                                        ButtonGroup {
                                            options: [
                                                { label: "Sub (Japanese)", value: "sub" },
                                                { label: "Dub (English)", value: "dub" }
                                            ]
                                            value: String(root.playbackSettings.default_language || "sub")
                                            onChanged: function(val) { root.saveSetting("default_language", val); }
                                        }
                                    }

                                    PanelSeparator { Layout.fillWidth: true }

                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: 8

                                        ColumnLayout {
                                            Layout.fillWidth: true
                                            spacing: 2

                                            Text {
                                                text: "Subtitles"
                                                color: Color.foreground
                                                font.family: Style.font.family
                                                font.pixelSize: Style.font.bodySmall
                                                font.bold: true
                                            }
                                            Text {
                                                text: "Subtitle language passed to player."
                                                color: Qt.darker(Color.foreground, 1.35)
                                                font.family: Style.font.family
                                                font.pixelSize: Style.font.caption
                                            }
                                        }

                                        ButtonGroup {
                                            options: [
                                                { label: "English", value: "en" },
                                                { label: "Off", value: "none" }
                                            ]
                                            value: String(root.playbackSettings.preferred_subtitle || "en")
                                            onChanged: function(val) { root.saveSetting("preferred_subtitle", val); }
                                        }
                                    }

                                    PanelSeparator { Layout.fillWidth: true }

                                    Toggle {
                                        Layout.fillWidth: true
                                        label: "Remember playback position and resume where you left off"
                                        checked: root.playbackSettings.resume_playback !== false
                                        onClicked: root.saveSetting("resume_playback", root.playbackSettings.resume_playback === false)
                                    }

                                    Toggle {
                                        Layout.fillWidth: true
                                        label: "Autoplay next episode"
                                        checked: root.playbackSettings.auto_next === true
                                        onClicked: root.saveSetting("auto_next", !root.playbackSettings.auto_next)
                                    }

                                    Toggle {
                                        Layout.fillWidth: true
                                        label: "Launch mpv in fullscreen"
                                        checked: root.playbackSettings.fullscreen_player === true
                                        onClicked: root.saveSetting("fullscreen_player", !root.playbackSettings.fullscreen_player)
                                    }
                                }
                            }

                            // 3. DOWNLOADS
                            PanelSectionHeader { text: "DOWNLOADS" }

                            Rectangle {
                                Layout.fillWidth: true
                                height: dlCol.implicitHeight + 20
                                color: Util.alpha(Color.foreground, 0.05)
                                radius: Style.cornerRadius
                                border.width: 1
                                border.color: Util.alpha(Color.foreground, 0.12)

                                ColumnLayout {
                                    id: dlCol
                                    anchors.fill: parent
                                    anchors.margins: 12
                                    spacing: 8

                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: 8

                                        ColumnLayout {
                                            Layout.fillWidth: true
                                            spacing: 2

                                            Text {
                                                text: "Download Location"
                                                color: Color.foreground
                                                font.family: Style.font.family
                                                font.pixelSize: Style.font.bodySmall
                                                font.bold: true
                                            }
                                            Text {
                                                text: root.downloadDestination || "~/Videos/Animechy"
                                                color: Qt.darker(Color.foreground, 1.35)
                                                font.family: Style.font.family
                                                font.pixelSize: Style.font.caption
                                                elide: Text.ElideMiddle
                                                Layout.fillWidth: true
                                            }
                                        }

                                        Button {
                                            text: "Open Folder"
                                            fontSize: Style.font.caption
                                            onClicked: root.openDownloadFolder()
                                        }
                                    }

                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: 8

                                        TextField {
                                            id: dlPathField
                                            Layout.fillWidth: true
                                            placeholderText: root.downloadDestination || "Enter folder path..."
                                            text: root.downloadDestination || ""
                                        }

                                        Button {
                                            text: "Save Path"
                                            fontSize: Style.font.caption
                                            onClicked: {
                                                if (dlPathField.text)
                                                    root.saveSetting("download_location", dlPathField.text);
                                            }
                                        }
                                    }

                                    PanelSeparator { Layout.fillWidth: true }

                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: 8

                                        ColumnLayout {
                                            Layout.fillWidth: true
                                            spacing: 2

                                            Text {
                                                text: "Default Download Quality"
                                                color: Color.foreground
                                                font.family: Style.font.family
                                                font.pixelSize: Style.font.bodySmall
                                                font.bold: true
                                            }
                                            Text {
                                                text: "Preferred quality for saved video files."
                                                color: Qt.darker(Color.foreground, 1.35)
                                                font.family: Style.font.family
                                                font.pixelSize: Style.font.caption
                                            }
                                        }

                                        ButtonGroup {
                                            options: [
                                                { label: "Auto", value: "Auto" },
                                                { label: "1080p", value: "1080p" },
                                                { label: "720p", value: "720p" },
                                                { label: "480p", value: "480p" }
                                            ]
                                            value: String(root.playbackSettings.download_default_quality || "Auto")
                                            onChanged: function(val) { root.saveSetting("download_default_quality", val); }
                                        }
                                    }

                                    PanelSeparator { Layout.fillWidth: true }

                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: 8

                                        ColumnLayout {
                                            Layout.fillWidth: true
                                            spacing: 2

                                            Text {
                                                text: "Max Simultaneous Downloads"
                                                color: Color.foreground
                                                font.family: Style.font.family
                                                font.pixelSize: Style.font.bodySmall
                                                font.bold: true
                                            }
                                            Text {
                                                text: "Number of concurrent ffmpeg download workers."
                                                color: Qt.darker(Color.foreground, 1.35)
                                                font.family: Style.font.family
                                                font.pixelSize: Style.font.caption
                                            }
                                        }

                                        ButtonGroup {
                                            options: [
                                                { label: "1", value: "1" },
                                                { label: "2", value: "2" },
                                                { label: "3", value: "3" },
                                                { label: "4", value: "4" }
                                            ]
                                            value: String(root.playbackSettings.download_max_simultaneous || 2)
                                            onChanged: function(val) { root.saveSetting("download_max_simultaneous", Number(val)); }
                                        }
                                    }
                                }
                            }

                            // 4. STORAGE & CACHE
                            PanelSectionHeader { text: "STORAGE & CACHE" }

                            Rectangle {
                                Layout.fillWidth: true
                                height: storageCardCol.implicitHeight + 20
                                color: Util.alpha(Color.foreground, 0.05)
                                radius: Style.cornerRadius
                                border.width: 1
                                border.color: Util.alpha(Color.foreground, 0.12)

                                ColumnLayout {
                                    id: storageCardCol
                                    anchors.fill: parent
                                    anchors.margins: 12
                                    spacing: 8

                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: 8

                                        ColumnLayout {
                                            Layout.fillWidth: true
                                            spacing: 2

                                            Text {
                                                text: "Metadata & Stream Cache"
                                                color: Color.foreground
                                                font.family: Style.font.family
                                                font.pixelSize: Style.font.bodySmall
                                                font.bold: true
                                            }
                                            Text {
                                                text: "Cached anime details, episodes, search indexes, and genres (" + root.formatBytes(root.backendStatus.cache_bytes || 0) + ")."
                                                color: Qt.darker(Color.foreground, 1.35)
                                                font.family: Style.font.family
                                                font.pixelSize: Style.font.caption
                                            }
                                        }

                                        Button {
                                            text: "Clear Cache"
                                            fontSize: Style.font.caption
                                            onClicked: root.clearCache()
                                        }
                                    }

                                    PanelSeparator { Layout.fillWidth: true }

                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: 8

                                        ColumnLayout {
                                            Layout.fillWidth: true
                                            spacing: 2

                                            Text {
                                                text: "Search History"
                                                color: Color.foreground
                                                font.family: Style.font.family
                                                font.pixelSize: Style.font.bodySmall
                                                font.bold: true
                                            }
                                            Text {
                                                text: "Recent search keywords saved for suggestions (" + root.searchHistory.length + " saved)."
                                                color: Qt.darker(Color.foreground, 1.35)
                                                font.family: Style.font.family
                                                font.pixelSize: Style.font.caption
                                            }
                                        }

                                        Button {
                                            text: "Clear Searches"
                                            fontSize: Style.font.caption
                                            onClicked: root.clearSearchHistory()
                                        }
                                    }

                                    PanelSeparator { Layout.fillWidth: true }

                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: 8

                                        ColumnLayout {
                                            Layout.fillWidth: true
                                            spacing: 2

                                            Text {
                                                text: "Watch History & Progress"
                                                color: Color.foreground
                                                font.family: Style.font.family
                                                font.pixelSize: Style.font.bodySmall
                                                font.bold: true
                                            }
                                            Text {
                                                text: "Episode resume positions and watched markers."
                                                color: Qt.darker(Color.foreground, 1.35)
                                                font.family: Style.font.family
                                                font.pixelSize: Style.font.caption
                                            }
                                        }

                                        Button {
                                            text: "Clear History"
                                            fontSize: Style.font.caption
                                            onClicked: root.clearWatchHistory()
                                        }
                                    }
                                }
                            }

                            // 5. SYSTEM STATUS & RESET
                            PanelSectionHeader { text: "SYSTEM STATUS & RESET" }

                            Rectangle {
                                Layout.fillWidth: true
                                height: statusCardCol.implicitHeight + 20
                                color: Util.alpha(Color.foreground, 0.05)
                                radius: Style.cornerRadius
                                border.width: 1
                                border.color: Util.alpha(Color.foreground, 0.12)

                                ColumnLayout {
                                    id: statusCardCol
                                    anchors.fill: parent
                                    anchors.margins: 12
                                    spacing: 10

                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: 8

                                        Rectangle {
                                            Layout.fillWidth: true
                                            height: 36
                                            color: Util.alpha(Color.foreground, 0.06)
                                            radius: Style.cornerRadius
                                            border.width: 1
                                            border.color: Util.alpha(Color.foreground, 0.1)

                                            RowLayout {
                                                anchors.fill: parent
                                                anchors.margins: 8
                                                spacing: 6
                                                Rectangle { width: 8; height: 8; radius: 4; color: root.backendStatus.backend === "running" ? "#a6e3a1" : "#f38ba8" }
                                                Text { text: "Backend"; font.family: Style.font.family; font.pixelSize: Style.font.caption; font.bold: true; color: Color.foreground }
                                                Item { Layout.fillWidth: true }
                                                Text { text: root.backendStatus.backend === "running" ? "127.0.0.1:8765" : "Offline"; font.family: Style.font.family; font.pixelSize: Style.font.caption - 1; color: Qt.darker(Color.foreground, 1.35) }
                                            }
                                        }

                                        Rectangle {
                                            Layout.fillWidth: true
                                            height: 36
                                            color: Util.alpha(Color.foreground, 0.06)
                                            radius: Style.cornerRadius
                                            border.width: 1
                                            border.color: Util.alpha(Color.foreground, 0.1)

                                            RowLayout {
                                                anchors.fill: parent
                                                anchors.margins: 8
                                                spacing: 6
                                                Rectangle { width: 8; height: 8; radius: 4; color: (root.backendStatus.download && root.backendStatus.download.ready) ? "#a6e3a1" : "#f9e2af" }
                                                Text { text: "ffmpeg"; font.family: Style.font.family; font.pixelSize: Style.font.caption; font.bold: true; color: Color.foreground }
                                                Item { Layout.fillWidth: true }
                                                Text { text: (root.backendStatus.download && root.backendStatus.download.ready) ? "Ready" : "Checking"; font.family: Style.font.family; font.pixelSize: Style.font.caption - 1; color: Qt.darker(Color.foreground, 1.35) }
                                            }
                                        }

                                        Rectangle {
                                            Layout.fillWidth: true
                                            height: 36
                                            color: Util.alpha(Color.foreground, 0.06)
                                            radius: Style.cornerRadius
                                            border.width: 1
                                            border.color: Util.alpha(Color.foreground, 0.1)

                                            RowLayout {
                                                anchors.fill: parent
                                                anchors.margins: 8
                                                spacing: 6
                                                Rectangle { width: 8; height: 8; radius: 4; color: "#a6e3a1" }
                                                Text { text: "HiAnime"; font.family: Style.font.family; font.pixelSize: Style.font.caption; font.bold: true; color: Color.foreground }
                                                Item { Layout.fillWidth: true }
                                                Text { text: "Available"; font.family: Style.font.family; font.pixelSize: Style.font.caption - 1; color: Qt.darker(Color.foreground, 1.35) }
                                            }
                                        }

                                        Rectangle {
                                            Layout.fillWidth: true
                                            height: 36
                                            color: Util.alpha(Color.foreground, 0.06)
                                            radius: Style.cornerRadius
                                            border.width: 1
                                            border.color: Util.alpha(Color.foreground, 0.1)

                                            RowLayout {
                                                anchors.fill: parent
                                                anchors.margins: 8
                                                spacing: 6
                                                Rectangle { width: 8; height: 8; radius: 4; color: "#a6e3a1" }
                                                Text { text: "Hiyori"; font.family: Style.font.family; font.pixelSize: Style.font.caption; font.bold: true; color: Color.foreground }
                                                Item { Layout.fillWidth: true }
                                                Text { text: "Available"; font.family: Style.font.family; font.pixelSize: Style.font.caption - 1; color: Qt.darker(Color.foreground, 1.35) }
                                            }
                                        }
                                    }

                                    PanelSeparator { Layout.fillWidth: true }

                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: 8

                                        ColumnLayout {
                                            Layout.fillWidth: true
                                            spacing: 2

                                            Text {
                                                text: "Reset Animechy Settings"
                                                color: Color.foreground
                                                font.family: Style.font.family
                                                font.pixelSize: Style.font.bodySmall
                                                font.bold: true
                                            }
                                            Text {
                                                text: "Restore all watching, provider, and download preferences to default."
                                                color: Qt.darker(Color.foreground, 1.35)
                                                font.family: Style.font.family
                                                font.pixelSize: Style.font.caption
                                            }
                                        }

                                        Button {
                                            text: "Reset All Settings"
                                            fontSize: Style.font.caption
                                            onClicked: root.openModal("reset_settings")
                                        }
                                    }
                                }
                            }

                            Item { height: 10 }
                        }
                    }
                }

            }

        }

        // In-panel modal overlay
        Rectangle {
            anchors.fill: parent
            z: 999
            color: Qt.rgba(0, 0, 0, 0.7)
            visible: root.activeModal !== ""

            // Absorb clicks
            MouseArea {
                anchors.fill: parent
                onClicked: {}
            }

            Rectangle {
                anchors.centerIn: parent
                width: Math.min(parent.width - 40, 480)
                height: modalColumn.implicitHeight + 32
                color: Color.background ?? "#1e1e2e"
                radius: Style.cornerRadius ?? 8
                border.color: Util.alpha(Color.foreground, 0.2)
                border.width: 1

                ColumnLayout {
                    id: modalColumn
                    anchors.fill: parent
                    anchors.margins: 16
                    spacing: 12

                    Text {
                        Layout.fillWidth: true
                        text: root.activeModal === "delete_download" ? "Delete download?" :
                              root.activeModal === "clear_failed" ? "Delete failed and cancelled downloads?" :
                              root.activeModal === "reset_settings" ? "Reset Animechy settings?" : ""
                        color: Color.foreground
                        font.family: Style.font.family
                        font.pixelSize: Style.font.title
                        font.bold: true
                    }

                    Text {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        color: Qt.darker(Color.foreground, 1.2)
                        font.family: Style.font.family
                        font.pixelSize: Style.font.body
                        text: root.activeModal === "delete_download" ? "This permanently removes the downloaded video and partial files from disk." :
                              root.activeModal === "clear_failed" ? "This removes failed/cancelled entries and their partial files. Completed downloads are kept." :
                              root.activeModal === "reset_settings" ? "Saved playback and download preferences will return to defaults." : ""
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        Layout.topMargin: 8
                        spacing: 8

                        Item { Layout.fillWidth: true }

                        // Delete single download buttons
                        Button {
                            text: "Keep"
                            fontSize: Style.font.caption
                            visible: root.activeModal === "delete_download"
                            onClicked: root.closeModal()
                        }
                        Button {
                            text: "Delete"
                            fontSize: Style.font.caption
                            selected: true
                            visible: root.activeModal === "delete_download"
                            onClicked: root.deleteDownload()
                        }

                        // Clear all failed buttons
                        Button {
                            text: "Keep"
                            fontSize: Style.font.caption
                            visible: root.activeModal === "clear_failed"
                            onClicked: root.closeModal()
                        }
                        Button {
                            text: "Delete failed items"
                            fontSize: Style.font.caption
                            selected: true
                            visible: root.activeModal === "clear_failed"
                            onClicked: { root.closeModal(); root.clearDownloadHistory(); }
                        }

                        // Reset settings buttons
                        Button {
                            text: "Cancel"
                            fontSize: Style.font.caption
                            visible: root.activeModal === "reset_settings"
                            onClicked: root.closeModal()
                        }
                        Button {
                            text: "Reset"
                            fontSize: Style.font.caption
                            selected: true
                            visible: root.activeModal === "reset_settings"
                            onClicked: { root.closeModal(); root.resetSettings(); }
                        }
                    }
                }
            }
        }

    }

}
