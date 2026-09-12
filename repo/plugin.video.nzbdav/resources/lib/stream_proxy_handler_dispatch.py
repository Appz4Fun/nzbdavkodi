# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""HTTP verb dispatch (GET/POST/HEAD), prepare-stream, and HLS routing.

Stage-2 mixin split of ``stream_proxy._StreamHandler``. These methods were
moved verbatim; every reference to a ``stream_proxy`` module-level name is
reached at call time via ``_sp.<name>`` so test monkeypatches on
``resources.lib.stream_proxy`` keep resolving. MRO composes them back onto
``_StreamHandler``; they keep using ``self`` for handler state and methods.
"""

import resources.lib.stream_proxy as _sp  # noqa: E402


class _DispatchMixin:  # pylint: disable=too-few-public-methods
    """HTTP verb dispatch (GET/POST/HEAD), prepare-stream, and HLS routing."""

    def _prepare_token_ok(self):
        """Constant-time check of the loopback /prepare auth token.

        A byte-by-byte != short-circuits on the first differing byte and leaks
        timing info about the secret that authorizes outbound HTTP from the
        loopback service. hmac.compare_digest rejects type mismatches, so coerce
        both sides to str("") if None.
        """
        expected_token = getattr(self.server, "prepare_token", "")
        if not expected_token:
            return False

        supplied_token = self.headers.get(_sp._PREPARE_TOKEN_HEADER)
        if not _sp.hmac.compare_digest(supplied_token or "", expected_token):
            return False
        return True

    def _read_json_post_body(self):
        """Read + JSON-parse a POST body, returning a dict or None.

        On any framing/parse/type error this sends the matching status code
        (400/413) and returns None; callers must just ``return`` in that case.
        """
        import json

        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            self.send_error(400)
            return None
        if length < 0:
            self.send_error(400)
            return None
        if length > _sp._PREPARE_REQUEST_MAX_BYTES:
            self.send_error(413)
            return None
        body = self.rfile.read(length) if length else b""
        try:
            data = json.loads(body)
        except (ValueError, KeyError):
            self.send_error(400)
            return None
        if not isinstance(data, dict):
            self.send_error(400)
            return None
        return data

    def _send_prepare_success(self, proxy, proxy_url, stream_info):
        """Write the 200 JSON /prepare response; clean up on client disconnect."""
        import json

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        result = {"proxy_url": proxy_url}
        result.update(stream_info)
        resp = json.dumps(result).encode()
        self.send_header("Content-Length", str(len(resp)))
        try:
            self.end_headers()
            self.wfile.write(resp)
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            # Plugin client gave up before we finished — most likely the
            # 60 s urlopen timeout in ``prepare_stream_via_service``
            # firing while a slow ``_prepare_tempfile_faststart`` remux
            # was still running server-side. The session is now an
            # orphan: there is no client URL holder and the tempfile
            # would otherwise survive until the next play (`clear_sessions`)
            # or the 6-hour TTL prune. Tear it down immediately so disk
            # use stays bounded by what's actually being watched.
            # Closes TODO.md §H.2-H12.
            session_id = _sp._extract_session_id_from_proxy_url(proxy_url)
            if session_id is not None:
                proxy.cleanup_session_by_id(session_id)
            _sp.xbmc.log(
                "NZB-DAV: /prepare client disconnected before response "
                "write; cleaned up session={} (reason={})".format(
                    session_id, e.__class__.__name__
                ),
                _sp.xbmc.LOGINFO,
            )

    def _parse_prepare_body(self, data):
        """Extract + validate /prepare params, returning kwargs for prepare_stream.

        Returns (remote_url, auth_header, prepare_kwargs) or None after sending
        the matching 400; callers must just ``return`` in that case.
        """
        remote_url = data.get("remote_url", "")
        auth_header = data.get("auth_header")
        fallback_sources = data.get("fallback_sources", [])
        settings_snapshot = _sp.normalize_settings_snapshot(
            data.get("settings_snapshot")
        )
        # Type-validate remote_url BEFORE any clear_sessions / prepare_stream
        # work. A non-string (list/dict/int) made it into _validate_url and
        # raised AttributeError on .startswith, but only after prepare_stream
        # had already invoked clear_sessions(), clobbering an in-flight stream.
        if not isinstance(remote_url, str) or not remote_url:
            self.send_error(400)
            return None
        if not isinstance(fallback_sources, list):
            self.send_error(400)
            return None
        try:
            auth_header = _sp._validate_auth_header(auth_header)
        except ValueError:
            self.send_error(400)
            return None
        content_length_hint = _sp._normalize_content_length_hint(
            data.get("content_length_hint")
        )
        prepare_kwargs = {"fallback_sources": fallback_sources}
        if content_length_hint > 0:
            prepare_kwargs["content_length_hint"] = content_length_hint
        if settings_snapshot:
            prepare_kwargs["settings_snapshot"] = settings_snapshot
        return remote_url, auth_header, prepare_kwargs

    def _run_prepare_stream(self, proxy, remote_url, auth_header, prepare_kwargs):
        """Call proxy.prepare_stream, returning (proxy_url, stream_info) or None.

        On failure this sends the matching status (400/500) and returns None;
        callers must just ``return`` in that case.
        """
        try:
            return proxy.prepare_stream(remote_url, auth_header, **prepare_kwargs)
        except ValueError:
            self.send_error(400)
            return None
        except Exception as e:  # noqa: BLE001 — keep loopback handler alive
            _sp.xbmc.log(
                "NZB-DAV: /prepare failed: {} (reason=prepare_exception)".format(
                    _sp._redact_text(e)
                ),
                _sp.xbmc.LOGERROR,
            )
            try:
                proxy.clear_sessions()
            except Exception:  # noqa: BLE001 — best-effort partial cleanup
                pass
            self.send_error(500)
            return None

    def do_POST(self):
        """Handle POST /prepare and /stream/<id>/fallbacks (plugin → service)."""
        raw_path = self.path.split("?", 1)[0]
        fallback_match = _sp._FALLBACK_UPDATE_PATH_RE.match(raw_path)
        if fallback_match:
            self._handle_fallback_update(fallback_match.group(1))
            return
        if raw_path != "/prepare":
            self.send_error(404)
            return
        if not self._prepare_token_ok():
            self.send_error(403)
            return
        data = self._read_json_post_body()
        if data is None:
            return
        parsed = self._parse_prepare_body(data)
        if parsed is None:
            return
        remote_url, auth_header, prepare_kwargs = parsed

        proxy = self.server.owner_proxy
        prepared = self._run_prepare_stream(
            proxy, remote_url, auth_header, prepare_kwargs
        )
        if prepared is None:
            return
        proxy_url, stream_info = prepared
        self._send_prepare_success(proxy, proxy_url, stream_info)

    def _handle_fallback_update(self, session_id):
        """Merge late-adopted fallback sources into a live session (auth'd).

        The fallback submit worker keeps adopting alternate copies for ~tens of
        seconds after playback starts — after the one-shot /prepare snapshot.
        This sibling endpoint lets the resolver push those late arrivals into
        the live session so the cutover has something to switch to. Mirrors the
        /prepare auth + body validation exactly.
        """
        import json

        if not self._prepare_token_ok():
            self.send_error(403)
            return
        data = self._read_json_post_body()
        if data is None:
            return
        fallback_sources = data.get("fallback_sources", [])
        if not isinstance(fallback_sources, list):
            self.send_error(400)
            return
        proxy = self.server.owner_proxy
        try:
            added = proxy.merge_session_fallbacks(session_id, fallback_sources)
        except ValueError:
            # _normalize_fallback_sources rejected a malformed stream_url.
            self.send_error(400)
            return
        if added is None:
            # Unknown / already-torn-down session.
            self.send_error(404)
            return
        self.send_response(200)
        resp = json.dumps({"added": added}).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        try:
            self.wfile.write(resp)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _hls_head_content_type(self, resource, seg_fmt):
        """Resolve the Content-Type for an HLS HEAD request.

        Returns the content-type string, or None when the resource is
        invalid for this session's segment format (caller sends 404).
        Mirrors the strict extension/mode validation in _handle_hls.
        """
        if resource == "playlist":
            return "application/vnd.apple.mpegurl"
        if resource == "init":
            if seg_fmt != "fmp4":
                return None
            return "video/mp4"
        if not _sp._is_segment_resource(resource):
            return None
        _, _seg_n, ext = resource
        expected_ext = "m4s" if seg_fmt == "fmp4" else "ts"
        if ext != expected_ext:
            return None
        return "video/mp4" if seg_fmt == "fmp4" else "video/mp2t"

    def _do_head_hls(self, raw_path):
        """Handle HEAD for an /hls/ path. Returns True if it handled the
        request (sent a response or error), False if not an HLS request.
        """
        parsed = self._parse_hls_resource(raw_path)
        if parsed is None:
            self.send_error(404)
            return True
        ctx = self._get_stream_context()
        if ctx is None or ctx.get("mode") != "hls":
            self.send_error(404)
            return True
        _session_id, resource = parsed
        seg_fmt = ctx.get("hls_segment_format", "mpegts")
        content_type = self._hls_head_content_type(resource, seg_fmt)
        if content_type is None:
            self.send_error(404)
            return True
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Connection", "close")
        self.end_headers()
        return True

    def _do_head_stream(self, ctx):
        """Send HEAD headers for a non-HLS stream context."""
        if ctx.get("faststart"):
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(ctx["virtual_size"]))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
        elif ctx.get("temp_faststart"):
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(ctx["content_length"]))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
        elif ctx.get("remux"):
            self.send_response(200)
            self.send_header("Content-Type", "video/x-matroska")
            self.send_header("Accept-Ranges", "none")
            self.send_header("Connection", "close")
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header("Content-Type", ctx["content_type"])
            self.send_header("Content-Length", str(ctx["content_length"]))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

    def do_HEAD(self):
        """Respond to HEAD with content metadata (type, length, ranges)."""
        raw_path = getattr(self, "path", "/stream").split("?", 1)[0]
        if raw_path.startswith("/hls/"):
            self._do_head_hls(raw_path)
            return

        ctx = self._get_stream_context()
        if ctx is None:
            self.send_error(404)
            return
        self._do_head_stream(ctx)

    def do_GET(self):
        """Route requests to the appropriate handler."""
        raw_path = getattr(self, "path", "/stream").split("?", 1)[0]
        if raw_path.startswith("/hls/"):
            self._handle_hls(raw_path)
            return

        ctx = self._get_stream_context(acquire=True)
        if ctx is None:
            self.send_error(404)
            return

        try:
            if ctx.get("faststart"):
                self._serve_mp4_faststart(ctx)
            elif ctx.get("temp_faststart"):
                self._serve_temp_faststart(ctx)
            elif ctx.get("remux"):
                self._serve_remux(ctx)
            else:
                self._serve_proxy(ctx)
        finally:
            self._release_stream_context(ctx)

    def _handle_hls(self, path):
        """Dispatch an /hls/<session>/... GET to playlist, init, or
        segment. Enforces strict extension↔ctx-mode validation so a
        request with the wrong extension for the session's segment
        format returns 404 rather than being silently served.
        """
        parsed = self._parse_hls_resource(path)
        if parsed is None:
            self.send_error(404)
            return
        ctx = self._get_stream_context(acquire=True)
        if ctx is None or ctx.get("mode") != "hls":
            self.send_error(404)
            return
        try:
            _session_id, resource = parsed
            seg_fmt = ctx.get("hls_segment_format", "mpegts")
            self._dispatch_hls_resource(ctx, resource, seg_fmt)
        finally:
            self._release_stream_context(ctx)

    def _dispatch_hls_resource(self, ctx, resource, seg_fmt):
        """Serve a parsed HLS resource (playlist/init/segment) for ctx.

        Enforces the strict extension/mode validation: an init request
        on a non-fmp4 session, or a segment whose extension doesn't
        match the session format, gets a 404.
        """
        if resource == "playlist":
            self._serve_hls_playlist(ctx)
            return
        if resource == "init":
            if seg_fmt != "fmp4":
                self.send_error(404)
                return
            self._serve_hls_init(ctx)
            return
        if _sp._is_segment_resource(resource):
            _, seg_n, ext = resource
            expected_ext = "m4s" if seg_fmt == "fmp4" else "ts"
            if ext != expected_ext:
                self.send_error(404)
                return
            self._serve_hls_segment(ctx, seg_n)
            return
        self.send_error(404)
