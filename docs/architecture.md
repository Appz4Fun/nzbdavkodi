# Architecture

This document gives contributors a high-level map of the addon paths that turn a
TMDBHelper play request into a Kodi stream. User-facing setup and usage stay in
the [README](../README.md); detailed proxy internals stay in
[proxy-architecture.md](proxy-architecture.md).

## Search Flow

```mermaid
flowchart TD
    A[TMDBHelper player URL] --> B[addon.py]
    B --> C[router.py]
    C --> D{Search provider settings}
    D -->|NZBHydra2| E[hydra.py]
    D -->|Prowlarr| F[prowlarr.py]
    D -->|Direct Newznab| G[direct_indexers.py]
    E --> H[NZB results]
    F --> H
    G --> H
    H --> I[filter.py]
    I --> J[results_dialog.py]
    J --> K[Selected NZB]
```

The router turns the Kodi plugin invocation into a movie or episode search.
Enabled providers return raw results, `filter.py` parses and ranks them, and the
result picker returns the NZB the user wants to play.

## Resolve Flow

```mermaid
flowchart TD
    A[Selected NZB] --> B[resolver.py]
    B --> C[nzbdav_api.py]
    C -->|Submit NZB| D[nzbdav]
    B -->|Poll status| D
    D -->|Ready folder| E[webdav.py]
    E --> F[Find playable file]
    F --> G[Remote WebDAV stream URL]
    G --> H[Prepare local proxy stream]
    H --> I[xbmcplugin.setResolvedUrl]
    I --> J[Kodi Player]
```

The resolver submits the selected NZB, waits for nzbdav to expose a playable
file over WebDAV, asks the background service to prepare a local proxy URL, and
then resolves the Kodi handle. Failure paths still resolve the handle with a
failed item so Kodi does not hang waiting for playback.

## Stream Proxy Flow

```mermaid
flowchart TD
    A[resolver.py] -->|prepare_stream_via_service| B[service.py]
    B --> C[StreamProxy]
    C --> D{Container and settings}
    D -->|Faststart MP4| E[Pass-through range proxy]
    D -->|Tail-moov MP4| F[Virtual faststart MP4]
    D -->|MKV or other| G[Pass-through range proxy]
    D -->|Force remux| H[ffmpeg remux path]
    E --> I[Local HTTP URL]
    F --> I
    G --> I
    H --> I
    I --> J[Kodi range requests]
    J --> K[nzbdav WebDAV]
```

The background service owns the long-lived proxy. The proxy hides WebDAV quirks
from Kodi, preserves range seeking where possible, can rewrite MP4 layout for
playback, and can use ffmpeg when the configured remux path is needed.

## Key Modules

| Module | Role |
|---|---|
| `router.py` | Dispatches Kodi plugin routes into search and playback actions. |
| `hydra.py`, `prowlarr.py`, `direct_indexers.py` | Search configured NZB providers. |
| `filter.py` | Parses release metadata, filters results, and sorts candidates. |
| `results_dialog.py` | Displays filtered results and returns the selected NZB. |
| `resolver.py` | Submits the NZB, polls nzbdav, prepares playback, and resolves Kodi handles. |
| `webdav.py` | Discovers playable files and builds WebDAV stream URLs. |
| `service.py` | Runs the background proxy service and playback monitoring. |
| `stream_proxy.py` | Serves local playback URLs and handles pass-through, MP4 rewrite, remux, and recovery paths. |
