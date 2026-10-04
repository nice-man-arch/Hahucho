# Animechy

Animechy is a Quickshell/Qt Quick anime client for Omarchy. The existing Animechy UI and HiAnime source adapter remain in place; a separate Python backend now coordinates HiAnime and Hiyori/Miruro provider data. mpv handles video playback.

## Install and launch

Requirements: Omarchy with Quickshell, Python 3.12+, `curl`, `mpv`, and `ffmpeg`. Python uses only its standard library. The backend stores SQLite state in `${XDG_CACHE_HOME:-~/.cache}/animechy/animechy.sqlite3`.

From this checkout:

```sh
./install.sh
```

The installer copies the plugin into `~/.config/omarchy/plugins/tenzin.animechy`, asks Omarchy to rescan and enable it, and refuses to overwrite an existing plugin directory. Click **ア** in the top bar. Plugin startup launches the loopback backend and logs to `${XDG_STATE_HOME:-~/.local/state}/animechy/backend.log`. Downloads are saved under `~/Videos/Animechy`; set `ANIMECHY_DOWNLOAD_DIR` before starting the backend to change the destination.

To launch the backend manually while developing:

```sh
python backend/server.py
curl 'http://127.0.0.1:8765/search?q=one%20piece'
```

For provider routing diagnostics, start the backend with `ANIMECHY_DEBUG_SOURCES=1`. Debug logs include the selected episode number and exact provider episode reference, provider/server/language, resolved stream URL and the mpv argv. Stream/source responses are never stored in the metadata cache. This flag prints temporary third-party stream URLs to the backend log, so enable it only while diagnosing playback.
