"""HTTP and mDNS transport used between main and support AutoCheckers.

The main computer advertises one ``_autochecker._tcp.local.`` service and
owns the authoritative order/statistics files.  Support computers keep local
mirrors and send domain operations instead of modifying Windows SMB files.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import socket
import threading
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import portalocker
import requests
from zeroconf import IPVersion, ServiceBrowser, ServiceInfo, ServiceListener, Zeroconf


API_VERSION = 1
SERVICE_TYPE = "_autochecker._tcp.local."
DEFAULT_PORT = 8765
DISCOVERY_TIMEOUT = 5.0
REQUEST_TIMEOUT = 15.0
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
MAX_JSON_BODY = 2 * 1024 * 1024
MAX_PHOTO_BODY = 64 * 1024 * 1024
MAX_SYNC_FILE_BODY = 256 * 1024 * 1024


class NetworkUnavailableError(ConnectionError):
    """The main AutoChecker could not be discovered or contacted."""


class NetworkAPIError(RuntimeError):
    """An authenticated AutoChecker API request was rejected."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = int(status)
        self.message = str(message)


@dataclass(frozen=True)
class MainEndpoint:
    host: str
    port: int
    server_id: str = ""

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/api/v{API_VERSION}"


class _MainServiceListener(ServiceListener):
    def __init__(self, zeroconf: Zeroconf, preferred_server_id: str = ""):
        self.zeroconf = zeroconf
        self.preferred_server_id = preferred_server_id
        self.endpoint: MainEndpoint | None = None
        self.fallback_endpoint: MainEndpoint | None = None
        self.ready = threading.Event()

    def _inspect(self, service_type: str, name: str) -> None:
        info = self.zeroconf.get_service_info(service_type, name, timeout=1500)
        if info is None:
            return
        properties = {
            key.decode("utf-8", errors="ignore"): value.decode(
                "utf-8", errors="ignore"
            )
            for key, value in info.properties.items()
        }
        if properties.get("role") != "main":
            return
        if properties.get("api") != str(API_VERSION):
            return
        addresses = info.parsed_addresses(IPVersion.V4Only)
        if not addresses:
            return
        endpoint = MainEndpoint(
            addresses[0],
            int(info.port),
            properties.get("server_id", ""),
        )
        if self.fallback_endpoint is None:
            self.fallback_endpoint = endpoint
        if (
            not self.preferred_server_id
            or endpoint.server_id == self.preferred_server_id
        ):
            self.endpoint = endpoint
            self.ready.set()

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self._inspect(type_, name)

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self._inspect(type_, name)

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        return None


def discover_main_autochecker(
    timeout: float = DISCOVERY_TIMEOUT,
    preferred_server_id: str = "",
) -> MainEndpoint:
    """Discover the main AutoChecker without relying on Windows .local DNS."""
    zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
    listener = _MainServiceListener(zeroconf, preferred_server_id)
    browser = ServiceBrowser(zeroconf, SERVICE_TYPE, listener)
    try:
        listener.ready.wait(timeout=max(0.1, float(timeout)))
        endpoint = listener.endpoint
        if endpoint is None and not preferred_server_id:
            endpoint = listener.fallback_endpoint
        if endpoint is None:
            if preferred_server_id and listener.fallback_endpoint is not None:
                raise NetworkUnavailableError(
                    "Found a different main AutoChecker; NETWORK.server_id "
                    "does not match"
                )
            raise NetworkUnavailableError(
                "Main AutoChecker was not found via mDNS"
            )
        return endpoint
    finally:
        browser.cancel()
        zeroconf.close()


def _lan_ipv4_addresses() -> list[bytes]:
    addresses: list[str] = []
    try:
        for item in socket.getaddrinfo(
            socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM
        ):
            address = item[4][0]
            if not address.startswith("127.") and address not in addresses:
                addresses.append(address)
    except OSError:
        pass
    if not addresses:
        # UDP connect selects the interface without sending a packet.
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            address = probe.getsockname()[0]
            if not address.startswith("127."):
                addresses.append(address)
        except OSError:
            pass
        finally:
            probe.close()
    if not addresses:
        addresses.append("127.0.0.1")
    return [socket.inet_aton(address) for address in addresses]


def _safe_mdns_hostname() -> str:
    hostname = re.sub(r"[^A-Za-z0-9-]", "-", socket.gethostname()).strip("-")
    return (hostname or "autochecker") + ".local."


class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class AutoCheckerNetworkServer:
    """Expose a callback backend through a small authenticated HTTP API."""

    def __init__(self, backend, token: str, port: int, server_id: str):
        if not token:
            raise ValueError("NETWORK.token is empty")
        self.backend = backend
        self.token = token
        self.port = int(port)
        self.server_id = server_id
        self.httpd: _ReusableThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.zeroconf: Zeroconf | None = None
        self.service_info: ServiceInfo | None = None

    def start(self) -> "AutoCheckerNetworkServer":
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "AutoCheckerHTTP/1"

            def log_message(self, fmt: str, *args) -> None:
                return None

            def _json_response(self, status: int, payload: dict) -> None:
                body = json.dumps(
                    payload, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _error(self, status: int, message: str) -> None:
                self._json_response(status, {"ok": False, "error": str(message)})

            def _authorized(self) -> bool:
                value = self.headers.get("Authorization", "")
                expected = f"Bearer {owner.token}"
                if not hmac.compare_digest(value, expected):
                    self._error(401, "Invalid AutoChecker network token")
                    return False
                return True

            def _read_json(self) -> dict:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError as error:
                    raise NetworkAPIError(400, "Invalid Content-Length") from error
                if length <= 0 or length > MAX_JSON_BODY:
                    raise NetworkAPIError(400, "Invalid JSON request size")
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise NetworkAPIError(400, "Invalid JSON request") from error
                if not isinstance(payload, dict):
                    raise NetworkAPIError(400, "JSON request must be an object")
                return payload

            def _dispatch(self, callback_name: str, payload: dict) -> dict:
                callback = getattr(owner.backend, callback_name)
                result = callback(payload)
                if not isinstance(result, dict):
                    raise RuntimeError(
                        f"Network backend {callback_name} returned invalid data"
                    )
                return {"ok": True, **result}

            def do_GET(self) -> None:
                if not self._authorized():
                    return
                path = urlparse(self.path).path
                try:
                    if path == f"/api/v{API_VERSION}/health":
                        self._json_response(200, {
                            "ok": True,
                            "api": API_VERSION,
                            "server_id": owner.server_id,
                        })
                        return
                    if path == f"/api/v{API_VERSION}/catalogs":
                        self._json_response(200, {
                            "ok": True,
                            "catalogs": owner.backend.get_catalogs(),
                        })
                        return
                    if path == f"/api/v{API_VERSION}/files/manifest":
                        self._json_response(200, {
                            "ok": True,
                            **owner.backend.get_file_manifest(),
                        })
                        return
                    if path.startswith(f"/api/v{API_VERSION}/files/"):
                        file_id = unquote(path.rsplit("/", 1)[-1])
                        file_path = Path(owner.backend.resolve_file(file_id))
                        if not file_path.is_file():
                            raise NetworkAPIError(404, "File is no longer available")
                        size = file_path.stat().st_size
                        self.send_response(200)
                        self.send_header("Content-Type", "application/octet-stream")
                        self.send_header("Content-Length", str(size))
                        self.send_header(
                            "Content-Disposition",
                            f'attachment; filename="{file_path.name}"',
                        )
                        self.end_headers()
                        with file_path.open("rb") as source:
                            while True:
                                chunk = source.read(DOWNLOAD_CHUNK_SIZE)
                                if not chunk:
                                    break
                                self.wfile.write(chunk)
                        return
                    if path == f"/api/v{API_VERSION}/statistics/today":
                        self._json_response(200, {
                            "ok": True,
                            **owner.backend.get_statistics_snapshot(),
                        })
                        return
                    raise NetworkAPIError(404, "Unknown AutoChecker API endpoint")
                except NetworkAPIError as error:
                    self._error(error.status, error.message)
                except (BrokenPipeError, ConnectionResetError):
                    return
                except Exception as error:
                    self._error(500, str(error))

            def do_POST(self) -> None:
                if not self._authorized():
                    return
                path = urlparse(self.path).path
                try:
                    routes = {
                        f"/api/v{API_VERSION}/sessions/open": "open_session",
                        f"/api/v{API_VERSION}/sessions/update": "update_session",
                        f"/api/v{API_VERSION}/orders/next": "next_order",
                        f"/api/v{API_VERSION}/orders/claim": "claim_order",
                        f"/api/v{API_VERSION}/orders/release": "release_order",
                        f"/api/v{API_VERSION}/orders/cancel": "cancel_order",
                        f"/api/v{API_VERSION}/orders/pack": "pack_order",
                    }
                    if path in routes:
                        payload = self._read_json()
                        self._json_response(
                            200,
                            self._dispatch(routes[path], payload),
                        )
                        return
                    if path == f"/api/v{API_VERSION}/photos":
                        try:
                            length = int(self.headers.get("Content-Length", "0"))
                        except ValueError as error:
                            raise NetworkAPIError(400, "Invalid photo size") from error
                        if length <= 0 or length > MAX_PHOTO_BODY:
                            raise NetworkAPIError(400, "Invalid photo size")
                        encoded_meta = self.headers.get("X-AutoChecker-Meta", "")
                        try:
                            padding = "=" * (-len(encoded_meta) % 4)
                            metadata = json.loads(
                                base64.urlsafe_b64decode(
                                    encoded_meta + padding
                                ).decode("utf-8")
                            )
                        except Exception as error:
                            raise NetworkAPIError(400, "Invalid photo metadata") from error
                        if not isinstance(metadata, dict):
                            raise NetworkAPIError(400, "Invalid photo metadata")
                        photo = self.rfile.read(length)
                        result = owner.backend.save_photo(metadata, photo)
                        self._json_response(200, {"ok": True, **result})
                        return
                    if path == f"/api/v{API_VERSION}/files/upload":
                        try:
                            length = int(self.headers.get("Content-Length", "0"))
                        except ValueError as error:
                            raise NetworkAPIError(400, "Invalid file size") from error
                        if length <= 0 or length > MAX_SYNC_FILE_BODY:
                            raise NetworkAPIError(400, "Invalid file size")
                        encoded_meta = self.headers.get("X-AutoChecker-Meta", "")
                        try:
                            padding = "=" * (-len(encoded_meta) % 4)
                            metadata = json.loads(
                                base64.urlsafe_b64decode(
                                    encoded_meta + padding
                                ).decode("utf-8")
                            )
                        except Exception as error:
                            raise NetworkAPIError(400, "Invalid file metadata") from error
                        if not isinstance(metadata, dict):
                            raise NetworkAPIError(400, "Invalid file metadata")
                        data = self.rfile.read(length)
                        result = owner.backend.save_uploaded_file(metadata, data)
                        self._json_response(200, {"ok": True, **result})
                        return
                    raise NetworkAPIError(404, "Unknown AutoChecker API endpoint")
                except NetworkAPIError as error:
                    self._error(error.status, error.message)
                except Exception as error:
                    self._error(500, str(error))

        self.httpd = _ReusableThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        self.port = int(self.httpd.server_port)
        self.thread = threading.Thread(
            target=self.httpd.serve_forever,
            name="AutoChecker HTTP server",
            daemon=True,
        )
        self.thread.start()

        self.zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
        properties = {
            "role": "main",
            "api": str(API_VERSION),
            "server_id": self.server_id,
        }
        self.service_info = ServiceInfo(
            SERVICE_TYPE,
            f"AutoChecker.{SERVICE_TYPE}",
            addresses=_lan_ipv4_addresses(),
            port=self.port,
            properties=properties,
            server=_safe_mdns_hostname(),
        )
        self.zeroconf.register_service(self.service_info, allow_name_change=True)
        return self

    def stop(self) -> None:
        if self.zeroconf is not None and self.service_info is not None:
            try:
                self.zeroconf.unregister_service(self.service_info)
            except Exception:
                pass
            self.zeroconf.close()
            self.zeroconf = None
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        if self.thread is not None:
            self.thread.join(timeout=2.0)
            self.thread = None


class AutoCheckerNetworkClient:
    """Discover the main server and maintain local file/statistics mirrors."""

    def __init__(
        self,
        token: str,
        client_id: str,
        *,
        preferred_server_id: str = "",
        endpoint: MainEndpoint | None = None,
        timeout: float = REQUEST_TIMEOUT,
        outbox_dir: Path | None = None,
    ) -> None:
        if not token:
            raise ValueError("NETWORK.token is empty on support AutoChecker")
        self.token = token
        self.client_id = client_id
        self.preferred_server_id = preferred_server_id
        self.endpoint = endpoint
        self.timeout = float(timeout)
        self.session = requests.Session()
        self.session.trust_env = False
        self.session_id = ""
        self.outbox_dir = Path(outbox_dir) if outbox_dir is not None else None

    def discover(self, force: bool = False) -> MainEndpoint:
        if self.endpoint is None or force:
            self.endpoint = discover_main_autochecker(
                preferred_server_id=self.preferred_server_id
            )
        return self.endpoint

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        last_error: Exception | None = None
        request_headers = dict(kwargs.pop("headers", {}))
        request_timeout = kwargs.pop("timeout", self.timeout)
        for attempt in range(2):
            try:
                endpoint = self.discover(force=attempt > 0)
                headers = dict(request_headers)
                headers["Authorization"] = f"Bearer {self.token}"
                response = self.session.request(
                    method,
                    endpoint.base_url + path,
                    headers=headers,
                    timeout=request_timeout,
                    **kwargs,
                )
                if response.status_code >= 400:
                    try:
                        message = response.json().get("error", response.text)
                    except ValueError:
                        message = response.text
                    raise NetworkAPIError(response.status_code, message)
                return response
            except NetworkAPIError:
                raise
            except (requests.RequestException, NetworkUnavailableError) as error:
                last_error = error
                self.endpoint = None
        raise NetworkUnavailableError(
            f"Main AutoChecker is unavailable: {last_error}"
        ) from last_error

    def _json(self, method: str, path: str, payload: dict | None = None) -> dict:
        kwargs = {"json": payload} if payload is not None else {}
        response = self._request(method, path, **kwargs)
        try:
            result = response.json()
        except ValueError as error:
            raise NetworkAPIError(502, "Main AutoChecker returned invalid JSON") from error
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise NetworkAPIError(502, "Main AutoChecker returned invalid data")
        return result

    def health(self) -> dict:
        return self._json("GET", "/health")

    def get_catalogs(self) -> dict:
        catalogs = self._json("GET", "/catalogs").get("catalogs")
        if not isinstance(catalogs, dict):
            raise NetworkAPIError(502, "Invalid catalogs response")
        return catalogs

    def get_manifest(self) -> dict:
        result = self._json("GET", "/files/manifest")
        if not isinstance(result.get("files"), list):
            raise NetworkAPIError(502, "Invalid file manifest")
        return result

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as file:
            while True:
                chunk = file.read(DOWNLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _show_progress(name: str, current: int, total: int, last: int) -> int:
        percent = 100 if total <= 0 else min(100, int(current * 100 / total))
        if percent == last and current < total:
            return last
        width = 24
        filled = round(width * percent / 100)
        # ASCII remains readable in the legacy Windows cp1251 console.
        bar = "#" * filled + "-" * (width - filled)
        print(
            f"\r* Downloading {name}: [{bar}] {percent:3d}% "
            f"({current / 1048576:.1f}/{total / 1048576:.1f} MB)",
            end="",
            flush=True,
        )
        if current >= total:
            print()
        return percent

    def sync_files(self, downloads_dir: Path) -> tuple[bool, str]:
        downloads_dir = Path(downloads_dir)
        downloads_dir.mkdir(parents=True, exist_ok=True)
        manifest = self.get_manifest()
        changed = False
        for entry in manifest["files"]:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", ""))
            if not name or Path(name).name != name:
                raise NetworkAPIError(502, f"Unsafe manifest filename: {name!r}")
            expected_hash = str(entry.get("sha256", ""))
            expected_size = int(entry.get("size", -1))
            expected_mtime_ns = int(entry.get("mtime_ns", 0))
            target = downloads_dir / name
            if target.is_file() and target.stat().st_size == expected_size:
                if target.stat().st_mtime_ns == expected_mtime_ns:
                    continue
                if self._sha256(target) == expected_hash:
                    if expected_mtime_ns > 0:
                        os.utime(
                            target,
                            ns=(expected_mtime_ns, expected_mtime_ns),
                        )
                    continue

            temporary = downloads_dir / f".{name}.{uuid.uuid4().hex}.part"
            digest = hashlib.sha256()
            downloaded = 0
            last_percent = -1
            try:
                with self._request(
                    "GET",
                    f"/files/{entry['id']}",
                    stream=True,
                    timeout=max(self.timeout, 180),
                ) as response:
                    total = int(response.headers.get("Content-Length", expected_size))
                    with temporary.open("wb") as destination:
                        for chunk in response.iter_content(DOWNLOAD_CHUNK_SIZE):
                            if not chunk:
                                continue
                            destination.write(chunk)
                            digest.update(chunk)
                            downloaded += len(chunk)
                            last_percent = self._show_progress(
                                name, downloaded, total, last_percent
                            )
                if downloaded != expected_size or digest.hexdigest() != expected_hash:
                    raise NetworkAPIError(
                        502, f"Downloaded file verification failed: {name}"
                    )
                os.replace(temporary, target)
                if expected_mtime_ns > 0:
                    os.utime(
                        target,
                        ns=(expected_mtime_ns, expected_mtime_ns),
                    )
                changed = True
            finally:
                temporary.unlink(missing_ok=True)
        return changed, str(manifest.get("revision", ""))

    def get_statistics_snapshot(self) -> dict:
        result = self._json("GET", "/statistics/today")
        if not isinstance(result.get("content"), str):
            raise NetworkAPIError(502, "Invalid statistics snapshot")
        return result

    def sync_statistics(self, statistics_dir: Path) -> tuple[bool, str]:
        snapshot = self.get_statistics_snapshot()
        filename = str(snapshot.get("filename", ""))
        if not filename or Path(filename).name != filename:
            raise NetworkAPIError(502, "Unsafe statistics filename")
        data = snapshot["content"].encode("utf-8")
        target = Path(statistics_dir) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        with portalocker.Lock(
            str(target),
            mode="a+b",
            timeout=max(self.timeout, 30),
        ) as file:
            file.seek(0)
            changed = file.read() != data
            if changed:
                file.seek(0)
                file.truncate()
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
        return changed, str(snapshot.get("revision", ""))

    def open_session(
        self,
        name: str,
        department: str,
        display_mac: str = "",
        sort_mode: str = "Standard",
    ) -> str:
        result = self._json("POST", "/sessions/open", {
            "request_id": uuid.uuid4().hex,
            "client_id": self.client_id,
            "name": name,
            "department": department,
            "display_mac": display_mac,
            "sort": sort_mode,
        })
        self.session_id = str(result.get("session_id", ""))
        if not self.session_id:
            raise NetworkAPIError(502, "Main AutoChecker did not create a session")
        return self.session_id

    def update_session_name(self, name: str) -> None:
        if not self.session_id:
            return
        self._json("POST", "/sessions/update", {
            "request_id": uuid.uuid4().hex,
            "session_id": self.session_id,
            "client_id": self.client_id,
            "name": name,
        })

    def _order_request(self, path: str, **payload) -> dict:
        payload.setdefault("request_id", uuid.uuid4().hex)
        payload.setdefault("client_id", self.client_id)
        payload.setdefault("session_id", self.session_id)
        return self._json("POST", path, payload)

    def next_order(self, name: str, sort_mode: str) -> dict:
        return self._order_request(
            "/orders/next", name=name, department="RETAIL", sort=sort_mode
        )

    def claim_order(self, order_id: str, name: str) -> dict:
        return self._order_request(
            "/orders/claim",
            order_id=order_id,
            name=name,
            department="RETAIL",
        )

    def release_order(self, order_id: str, name: str) -> dict:
        return self._order_request(
            "/orders/release", order_id=order_id, name=name
        )

    def cancel_order(self, order_id: str, name: str) -> dict:
        return self._order_request(
            "/orders/cancel", order_id=order_id, name=name
        )

    def pack_order(
        self,
        order_id: str,
        name: str,
        parenthesized: bool = False,
    ) -> dict:
        return self._order_request(
            "/orders/pack",
            order_id=order_id,
            name=name,
            parenthesized=bool(parenthesized),
            department="RETAIL_UP",
        )

    @staticmethod
    def _photo_meta_header(metadata: dict) -> str:
        raw = json.dumps(metadata, ensure_ascii=False).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    def _upload_photo_once(self, path: Path, metadata: dict) -> dict:
        headers = {
            "X-AutoChecker-Meta": self._photo_meta_header(metadata),
            "Content-Type": "image/jpeg",
        }
        response = self._request(
            "POST",
            "/photos",
            data=path.read_bytes(),
            headers=headers,
            timeout=max(self.timeout, 180),
        )
        result = response.json()
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise NetworkAPIError(502, "Invalid photo upload response")
        return result

    def upload_photo(
        self,
        path: Path,
        order_id: str,
        name: str,
        manually: bool,
        *,
        queue_on_failure: bool = True,
    ) -> dict:
        metadata = {
            "request_id": uuid.uuid4().hex,
            "client_id": self.client_id,
            "session_id": self.session_id,
            "order_id": order_id,
            "name": name,
            "manually": bool(manually),
            "filename": Path(path).name,
        }
        try:
            return self._upload_photo_once(Path(path), metadata)
        except (NetworkUnavailableError, requests.RequestException):
            if not queue_on_failure or self.outbox_dir is None:
                raise
            self.outbox_dir.mkdir(parents=True, exist_ok=True)
            record = {
                "path": str(Path(path).resolve()),
                "metadata": metadata,
            }
            record_path = self.outbox_dir / f"{metadata['request_id']}.json"
            record_path.write_text(
                json.dumps(record, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return {"ok": True, "queued": True, "filename": Path(path).name}

    def upload_file(self, path: Path, name: str) -> dict:
        """Upload one explicit support file to the main Downloads folder."""
        path = Path(path)
        metadata = {
            "request_id": uuid.uuid4().hex,
            "client_id": self.client_id,
            "session_id": self.session_id,
            "name": name,
            "filename": path.name,
            "sha256": self._sha256(path),
        }
        headers = {
            "X-AutoChecker-Meta": self._photo_meta_header(metadata),
            "Content-Type": "application/octet-stream",
        }
        response = self._request(
            "POST",
            "/files/upload",
            data=path.read_bytes(),
            headers=headers,
            timeout=max(self.timeout, 300),
        )
        result = response.json()
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise NetworkAPIError(502, "Invalid file upload response")
        return result

    def flush_photo_outbox(self) -> int:
        if self.outbox_dir is None or not self.outbox_dir.is_dir():
            return 0
        uploaded = 0
        for record_path in sorted(self.outbox_dir.glob("*.json")):
            try:
                record = json.loads(record_path.read_text(encoding="utf-8"))
                photo_path = Path(record["path"])
                metadata = record["metadata"]
                if not photo_path.is_file():
                    record_path.unlink(missing_ok=True)
                    continue
                self._upload_photo_once(photo_path, metadata)
                record_path.unlink(missing_ok=True)
                uploaded += 1
            except (NetworkUnavailableError, requests.RequestException):
                break
            except Exception as error:
                print(f"XXX Cannot upload queued photo {record_path.name}: {error}")
        return uploaded


def copy_catalog_values(settings: dict, catalogs: dict, excluded: set[str]) -> bool:
    """Merge server-owned settings while preserving local hardware/network keys."""
    changed = False
    for key in list(settings):
        if key not in excluded and key not in catalogs:
            del settings[key]
            changed = True
    for key, value in catalogs.items():
        if key in excluded:
            continue
        if settings.get(key) != value:
            settings[key] = value
            changed = True
    return changed
