from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .logging_config import get_trace_id
from .mcp_client import MCPTool


TRACE_HEADER = "X-Pulse-Trace-Id"


class _MCPHTTPTransportError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class HttpMCPTransport:
    """HTTP transport supporting Streamable HTTP, legacy HTTP+SSE, and custom fallback."""

    DEFAULT_PROTOCOL_VERSION = "2025-03-26"
    _HTTP_MODES = {"auto", "http", "streamable_http", "http_sse", "sse", "legacy_sse", "custom_http"}

    def __init__(
        self,
        *,
        base_url: str,
        timeout_sec: float = 8.0,
        auth_token: str = "",
        transport_mode: str = "auto",
        protocol_version: str = DEFAULT_PROTOCOL_VERSION,
    ) -> None:
        safe_base = str(base_url or "").strip().rstrip("/")
        if not safe_base:
            raise ValueError("base_url is required")
        safe_mode = str(transport_mode or "auto").strip().lower() or "auto"
        if safe_mode not in self._HTTP_MODES:
            raise ValueError(f"unsupported HTTP MCP transport_mode: {transport_mode}")
        self._base_url = safe_base
        # ADR-005 §7.4 post-mortem: a 30s silent upper bound here made
        # ``BossMcpSettings.timeout_sec`` (default=90, le=180) **completely
        # ineffective** — any tool handler that runs longer than 30s on the
        # gateway side (pull_conversations, scan_jobs — anything that drives
        # a playwright page) reliably hit `urlopen` timeout, the connector
        # retried, and two or more handlers raced on the same browser page.
        # That is how the user's auto-reply patrol spent 65s per turn yet
        # returned rows=0 every time (2026-04-22 real run). Trust the caller
        # — pydantic settings already bound the value — but fail-loud on
        # obviously broken inputs instead of silently truncating.
        safe_timeout = float(timeout_sec)
        if not (1.0 <= safe_timeout <= 600.0):
            raise ValueError(
                f"timeout_sec out of allowed range [1.0, 600.0]: got {timeout_sec!r}. "
                "Bind at caller side via schema (see BossMcpSettings.timeout_sec)."
            )
        self._timeout_sec = safe_timeout
        self._auth_token = str(auth_token or "").strip()
        self._transport_mode = "auto" if safe_mode == "http" else safe_mode
        self._protocol_version = str(protocol_version or self.DEFAULT_PROTOCOL_VERSION).strip() or self.DEFAULT_PROTOCOL_VERSION
        self._active_mode: str | None = None
        self._session_id = ""
        self._initialized = False
        self._request_id = 0
        self._prefetched_custom_tools: list[dict[str, Any]] | None = None

    def list_tools(self) -> list[MCPTool]:
        mode = self._ensure_mode()
        if mode == "custom_http":
            raw_tools = list(self._prefetched_custom_tools or self._custom_list_tools())
        else:
            raw_tools = self._mcp_request("tools/list", params={}).get("tools")
        if not isinstance(raw_tools, list):
            return []
        tools: list[MCPTool] = []
        for item in raw_tools:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            tools.append(
                MCPTool(
                    server=str(item.get("server") or "external").strip() or "external",
                    name=name,
                    description=str(item.get("description") or "").strip() or name,
                    schema=dict(item.get("inputSchema") or item.get("schema") or {}),
                )
            )
        return tools

    def call_tool(self, server: str, name: str, arguments: dict[str, Any]) -> Any:
        mode = self._ensure_mode()
        if mode == "custom_http":
            body = self._custom_call_tool(server=server, name=name, arguments=arguments)
        else:
            body = self._mcp_request(
                "tools/call",
                params={
                    "name": str(name or "").strip(),
                    "arguments": dict(arguments or {}),
                },
            )
        if isinstance(body, dict) and "result" in body:
            return body["result"]
        content = body.get("content") if isinstance(body, dict) else None
        if isinstance(content, list):
            texts = [
                str(item.get("text", ""))
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            if texts:
                text = "\n".join(texts).strip()
                try:
                    return json.loads(text)
                except Exception:
                    return text
        return body

    def _ensure_mode(self) -> str:
        if self._active_mode is not None:
            return self._active_mode
        candidates = self._candidate_modes()
        errors: list[str] = []
        for mode in candidates:
            try:
                if mode == "streamable_http":
                    self._initialize_streamable_http()
                elif mode == "legacy_sse":
                    self._probe_legacy_sse()
                elif mode == "custom_http":
                    self._probe_custom_http()
                self._active_mode = mode
                return mode
            except Exception as exc:
                errors.append(f"{mode}: {exc}")
                self._initialized = False
                self._session_id = ""
        raise RuntimeError("No HTTP MCP transport mode succeeded: " + " | ".join(errors))

    def _candidate_modes(self) -> list[str]:
        if self._transport_mode in {"streamable_http"}:
            return ["streamable_http"]
        if self._transport_mode in {"http_sse", "sse", "legacy_sse"}:
            return ["legacy_sse"]
        if self._transport_mode == "custom_http":
            return ["custom_http"]
        return ["streamable_http", "legacy_sse", "custom_http"]

    def _initialize_streamable_http(self) -> None:
        if self._initialized:
            return
        request_id = self._next_request_id()
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": self._protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "pulse", "version": "1.0"},
            },
        }
        response = self._open_response(
            method="POST",
            url=self._base_url,
            payload=payload,
            accept="application/json, text/event-stream",
            include_session=False,
        )
        try:
            message = self._read_jsonrpc_response(response, request_id)
            result = dict(message.get("result") or {})
            session_id = self._header_value(response, "Mcp-Session-Id")
            self._session_id = str(session_id or "").strip()
            negotiated = str(result.get("protocolVersion") or "").strip()
            if negotiated:
                self._protocol_version = negotiated
        finally:
            self._close_response(response)
        self._send_streamable_notification("notifications/initialized")
        self._initialized = True

    def _send_streamable_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        response = self._open_response(
            method="POST",
            url=self._base_url,
            payload=payload,
            accept="application/json, text/event-stream",
        )
        try:
            response.read()
        finally:
            self._close_response(response)

    def _mcp_request(self, method: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        mode = self._active_mode or self._ensure_mode()
        if mode == "streamable_http":
            if not self._initialized:
                self._initialize_streamable_http()
            request_id = self._next_request_id()
            payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
            if params is not None:
                payload["params"] = params
            try:
                response = self._open_response(
                    method="POST",
                    url=self._base_url,
                    payload=payload,
                    accept="application/json, text/event-stream",
                )
            except _MCPHTTPTransportError as exc:
                if exc.status_code == 404 and self._session_id:
                    self._initialized = False
                    self._session_id = ""
                raise
            try:
                message = self._read_jsonrpc_response(response, request_id)
            finally:
                self._close_response(response)
            return dict(message.get("result") or {})
        if mode == "legacy_sse":
            return self._legacy_sse_request(method, params=params)
        raise RuntimeError(f"unsupported MCP request mode: {mode}")

    def _legacy_sse_request(self, method: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response, endpoint_url = self._open_legacy_sse_stream()
        try:
            init_id = self._next_request_id()
            self._legacy_sse_post(
                endpoint_url,
                {
                    "jsonrpc": "2.0",
                    "id": init_id,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": self._protocol_version,
                        "capabilities": {},
                        "clientInfo": {"name": "pulse", "version": "1.0"},
                    },
                },
            )
            init_message = self._await_sse_jsonrpc_response(response, init_id)
            init_result = dict(init_message.get("result") or {})
            negotiated = str(init_result.get("protocolVersion") or "").strip()
            if negotiated:
                self._protocol_version = negotiated
            self._legacy_sse_post(
                endpoint_url,
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            request_id = self._next_request_id()
            payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
            if params is not None:
                payload["params"] = params
            self._legacy_sse_post(endpoint_url, payload)
            message = self._await_sse_jsonrpc_response(response, request_id)
            return dict(message.get("result") or {})
        finally:
            self._close_response(response)

    def _probe_legacy_sse(self) -> None:
        response, _endpoint_url = self._open_legacy_sse_stream()
        self._close_response(response)

    def _open_legacy_sse_stream(self) -> tuple[Any, str]:
        response = self._open_response(
            method="GET",
            url=self._base_url,
            payload=None,
            accept="text/event-stream",
            include_session=False,
            include_protocol=False,
            content_type="",
        )
        content_type = self._content_type(response)
        if "text/event-stream" not in content_type:
            self._close_response(response)
            raise RuntimeError(f"legacy SSE endpoint did not return text/event-stream: {content_type}")
        while True:
            event = self._read_sse_event(response)
            if event is None:
                self._close_response(response)
                raise RuntimeError("legacy SSE endpoint closed before endpoint event")
            if event["event"] == "endpoint":
                endpoint_raw = str(event["data"] or "").strip()
                if not endpoint_raw:
                    self._close_response(response)
                    raise RuntimeError("legacy SSE endpoint event missing URL")
                endpoint_url = urllib.parse.urljoin(f"{self._base_url}/", endpoint_raw)
                return response, endpoint_url

    def _legacy_sse_post(self, endpoint_url: str, payload: dict[str, Any]) -> None:
        response = self._open_response(
            method="POST",
            url=endpoint_url,
            payload=payload,
            accept="application/json, text/event-stream",
            include_session=False,
            include_protocol=False,
        )
        try:
            response.read()
        finally:
            self._close_response(response)

    def _await_sse_jsonrpc_response(self, response: Any, request_id: int) -> dict[str, Any]:
        while True:
            event = self._read_sse_event(response)
            if event is None:
                raise RuntimeError(f"SSE stream closed before response id={request_id}")
            data = str(event["data"] or "").strip()
            if not data:
                continue
            try:
                payload = json.loads(data)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("id") == request_id:
                if "error" in payload:
                    error = payload.get("error")
                    if isinstance(error, dict):
                        raise RuntimeError(f"MCP JSON-RPC error {error.get('code', -1)}: {error.get('message', '')}")
                    raise RuntimeError(f"MCP JSON-RPC error: {error}")
                return payload

    def _probe_custom_http(self) -> None:
        self._prefetched_custom_tools = self._custom_list_tools()

    def _custom_list_tools(self) -> list[dict[str, Any]]:
        body = self._request_json("GET", "/tools")
        raw_tools = body.get("tools") if isinstance(body, dict) else body
        return raw_tools if isinstance(raw_tools, list) else []

    def _custom_call_tool(self, *, server: str, name: str, arguments: dict[str, Any]) -> Any:
        payload = {
            "server": str(server or "").strip(),
            "name": str(name or "").strip(),
            "arguments": dict(arguments or {}),
        }
        body = self._request_json("POST", "/call", payload=payload)
        if isinstance(body, dict) and "result" in body:
            return body["result"]
        return body

    def _next_request_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _request_json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        safe_path = path if path.startswith("/") else f"/{path}"
        response = self._open_response(
            method=method,
            url=f"{self._base_url}{safe_path}",
            payload=payload,
            accept="application/json",
            include_session=False,
            include_protocol=False,
        )
        try:
            text = response.read().decode("utf-8", errors="ignore")
        finally:
            self._close_response(response)
        if not text.strip():
            return {}
        return json.loads(text)

    def _open_response(
        self,
        *,
        method: str,
        url: str,
        payload: dict[str, Any] | None,
        accept: str,
        include_session: bool = True,
        include_protocol: bool = True,
        content_type: str = "application/json; charset=utf-8",
    ) -> Any:
        data: bytes | None = None
        headers: dict[str, str] = {"Accept": accept}
        if content_type:
            headers["Content-Type"] = content_type
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        if include_protocol and self._protocol_version:
            headers["MCP-Protocol-Version"] = self._protocol_version
        if include_session and self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        # ADR-005 §2: forward the caller's trace_id so the remote MCP
        # process can tag its logs + per-trace bucket accordingly.
        # "-" is the sentinel "no trace bound"; don't leak it as a header.
        current_trace = get_trace_id()
        if current_trace and current_trace != "-":
            headers[TRACE_HEADER] = current_trace
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method=method.upper(),
        )
        try:
            return urllib.request.urlopen(request, timeout=self._timeout_sec)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            raise _MCPHTTPTransportError(
                f"http transport error: {exc.code} {body[:300]}",
                status_code=int(exc.code),
            ) from exc
        except urllib.error.URLError as exc:
            raise _MCPHTTPTransportError(f"http transport url error: {exc.reason}") from exc

    def _read_jsonrpc_response(self, response: Any, request_id: int) -> dict[str, Any]:
        content_type = self._content_type(response)
        if "text/event-stream" in content_type:
            return self._await_sse_jsonrpc_response(response, request_id)
        text = response.read().decode("utf-8", errors="ignore")
        if not text.strip():
            raise RuntimeError("empty JSON-RPC response body")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON from MCP server: {text[:300]}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"Unexpected JSON-RPC payload: {payload!r}")
        if "error" in payload:
            error = payload.get("error")
            if isinstance(error, dict):
                raise RuntimeError(f"MCP JSON-RPC error {error.get('code', -1)}: {error.get('message', '')}")
            raise RuntimeError(f"MCP JSON-RPC error: {error}")
        payload_id = payload.get("id")
        if payload_id not in {request_id, str(request_id), None}:
            raise RuntimeError(f"Unexpected JSON-RPC response id: {payload_id!r}")
        return payload

    def _read_sse_event(self, response: Any) -> dict[str, str] | None:
        event_name = "message"
        event_id = ""
        data_lines: list[str] = []
        while True:
            raw_line = response.readline()
            if not raw_line:
                if not data_lines and not event_id:
                    return None
                break
            line = raw_line.decode("utf-8", errors="ignore").rstrip("\r\n")
            if not line:
                if data_lines or event_id or event_name != "message":
                    break
                continue
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if field == "event":
                event_name = value or "message"
            elif field == "data":
                data_lines.append(value)
            elif field == "id":
                event_id = value
        return {"event": event_name, "id": event_id, "data": "\n".join(data_lines)}

    @staticmethod
    def _content_type(response: Any) -> str:
        headers = getattr(response, "headers", None)
        if headers is None:
            return ""
        getter = getattr(headers, "get", None)
        if callable(getter):
            return str(getter("Content-Type", "") or "").lower()
        return str(headers).lower()

    @staticmethod
    def _header_value(response: Any, key: str) -> str:
        headers = getattr(response, "headers", None)
        if headers is None:
            return ""
        getter = getattr(headers, "get", None)
        if not callable(getter):
            return ""
        return str(getter(key, "") or getter(key.lower(), "") or "")

    @staticmethod
    def _close_response(response: Any) -> None:
        close = getattr(response, "close", None)
        if callable(close):
            close()
