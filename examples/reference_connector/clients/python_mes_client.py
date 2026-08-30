#!/usr/bin/env python3
"""Minimal MES client for the Reference Connector HTTP boundary."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import uuid
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


MAX_BODY_BYTES = 1024 * 1024
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024


def _is_loopback_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    if hostname.casefold() == "localhost":
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False


class _RejectRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _read_order(path: str) -> dict[str, Any]:
    source = Path(path)
    current = source.lstat()
    if (
        not stat.S_ISREG(current.st_mode)
        or source.is_symlink()
        or current.st_size > MAX_BODY_BYTES
    ):
        raise ValueError("order 必须是小于 1 MiB 的普通 JSON 文件")
    value = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("order JSON 顶层必须是 object")
    return value


class ConnectorClient:
    def __init__(self, base_url: str, token: str) -> None:
        parsed = urlsplit(base_url.strip())
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("REFERENCE_CONNECTOR_URL 必须是无凭据的 HTTP(S) URL")
        if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
            raise ValueError("非 loopback 的 REFERENCE_CONNECTOR_URL 必须使用 HTTPS")
        if not token:
            raise ValueError("REFERENCE_CONNECTOR_INBOUND_TOKEN 不能为空")
        self.base_url = base_url.rstrip("/") + "/"
        self.origin = (parsed.scheme, parsed.netloc)
        self.token = token
        handlers = [_RejectRedirectHandler()]
        if _is_loopback_host(parsed.hostname):
            handlers.insert(0, ProxyHandler({}))
        self.opener = build_opener(*handlers)

    def _url(self, path: str) -> str:
        url = urljoin(self.base_url, path)
        target = urlsplit(url)
        if (target.scheme, target.netloc) != self.origin:
            raise ValueError("拒绝访问 Connector origin 之外的 URL")
        return url

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self._url(path)
        body = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
            "X-Request-ID": str(uuid.uuid4()),
        }
        if payload is not None:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, method=method, headers=headers)
        try:
            with self.opener.open(
                request,
                timeout=20,
            ) as response:
                raw = response.read(MAX_BODY_BYTES + 1)
                status = int(response.status)
        except HTTPError as error:
            raw = error.read(MAX_BODY_BYTES + 1)
            status = int(error.code)
        except (OSError, TimeoutError, URLError) as error:
            raise RuntimeError(f"Connector 请求失败：{error}") from error
        if len(raw) > MAX_BODY_BYTES:
            raise RuntimeError("Connector 响应超过 1 MiB")
        if not 200 <= status < 300:
            preview = raw.decode("utf-8", errors="replace")[:512]
            raise RuntimeError(f"Connector HTTP {status}: {preview}")
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise RuntimeError("Connector 返回了无效 JSON")
        return value

    def download(self, external_run_id: str, output_key: str, destination: str) -> dict[str, Any]:
        url = self._url(
            "v1/orders/"
            f"{quote(external_run_id, safe='')}/outputs/{quote(output_key, safe='')}"
        )
        request = Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "X-Request-ID": str(uuid.uuid4()),
            },
        )
        try:
            response = self.opener.open(request, timeout=20)
        except HTTPError as error:
            preview = error.read(512).decode("utf-8", errors="replace")
            raise RuntimeError(f"Connector HTTP {error.code}: {preview}") from error
        except (OSError, TimeoutError, URLError) as error:
            raise RuntimeError(f"Connector 请求失败：{error}") from error
        with response:
            if not 200 <= int(response.status) < 300:
                raise RuntimeError(f"Connector HTTP {response.status}")
            try:
                expected_size = int(response.headers["Content-Length"])
            except (KeyError, TypeError, ValueError) as error:
                raise RuntimeError("Connector 下载响应缺少有效 Content-Length") from error
            expected_digest = str(response.headers.get("X-Checksum-SHA256") or "")
            if (
                expected_size < 0
                or expected_size > MAX_DOWNLOAD_BYTES
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_digest)
            ):
                raise RuntimeError("Connector 下载响应的大小或摘要无效")
            target = Path(destination)
            if target.exists() or target.is_symlink() or not target.parent.is_dir():
                raise ValueError("下载目标必须是尚不存在且父目录已存在的普通路径")
            temporary_name = ""
            try:
                with tempfile.NamedTemporaryFile(
                    dir=target.parent,
                    prefix=f".{target.name}.",
                    delete=False,
                ) as handle:
                    temporary_name = handle.name
                    os.fchmod(handle.fileno(), 0o600)
                    digest = hashlib.sha256()
                    size = 0
                    while chunk := response.read(1024 * 1024):
                        size += len(chunk)
                        if size > expected_size or size > MAX_DOWNLOAD_BYTES:
                            raise RuntimeError("Connector 下载响应超过声明大小")
                        digest.update(chunk)
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                actual_digest = "sha256:" + digest.hexdigest()
                if size != expected_size or actual_digest != expected_digest:
                    raise RuntimeError("Connector 下载结果大小或 SHA-256 校验失败")
                os.link(temporary_name, target)
                Path(temporary_name).unlink()
                temporary_name = ""
            finally:
                if temporary_name:
                    Path(temporary_name).unlink(missing_ok=True)
        return {"path": str(target), "size": expected_size, "sha256": expected_digest}


def main() -> int:
    parser = argparse.ArgumentParser(description="Reference Connector MES client")
    commands = parser.add_subparsers(dest="command", required=True)
    submit = commands.add_parser("submit")
    submit.add_argument("order")
    for name in ("status", "reconcile", "results"):
        command = commands.add_parser(name)
        command.add_argument("external_run_id")
    download = commands.add_parser("download")
    download.add_argument("external_run_id")
    download.add_argument("output_key")
    download.add_argument("destination")
    args = parser.parse_args()
    try:
        client = ConnectorClient(
            os.environ.get("REFERENCE_CONNECTOR_URL", "http://127.0.0.1:8090"),
            os.environ.get("REFERENCE_CONNECTOR_INBOUND_TOKEN", ""),
        )
        if args.command == "submit":
            result = client.request("POST", "v1/orders", _read_order(args.order))
        elif args.command == "download":
            result = client.download(
                args.external_run_id,
                args.output_key,
                args.destination,
            )
        else:
            external_run_id = quote(args.external_run_id, safe="")
            suffix = "" if args.command == "status" else f"/{args.command}"
            method = "GET" if args.command == "status" else "POST"
            payload = None if method == "GET" else {}
            result = client.request(
                method,
                f"v1/orders/{external_run_id}{suffix}",
                payload,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
