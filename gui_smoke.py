#!/usr/bin/env python3
"""HTTP smoke test for the local HOMAI gallery workbench.

Starts gui_server.py on 127.0.0.1:8799 with HOMAI_SMOKE=1 and an isolated
HOMAI_REMODEL_DIR.  Checks the gallery read APIs, prompt-overlay round trip,
media/thumbnail routes, path traversal protection, and the old-house remodel
upload/analyze/generate flow, including the no-image-input guard and aspect
mapping.  The paid image model is stubbed; no real generation is performed.
"""

from __future__ import annotations

import base64
import html as html_lib
import http.client
import io
import json
import os
import re
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

try:
    from PIL import Image
except ImportError:  # The remodel endpoints require Pillow; fail clearly below.
    Image = None

import gui_server

ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 8799
BASE = f"http://{HOST}:{PORT}"
TIMEOUT = 30

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    suffix = f"  {detail}" if detail else ""
    print(f"{'PASS' if ok else 'FAIL'}  {name}{suffix}")


def fetch(path: str, method: str = "GET", payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, response.headers.get_content_type(), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get_content_type(), exc.read()


def fetch_headers(path: str, method: str = "GET", headers: dict | None = None):
    """Return (status, headers, body) including non-2xx responses such as 304."""
    request = urllib.request.Request(BASE + path, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def fetch_json(path: str, method: str = "GET", payload=None):
    status, _content_type, body = fetch(path, method, payload)
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = None
    return status, parsed


def fetch_multipart(path: str, field: str, filename: str, content: bytes, content_type: str):
    boundary = "----homai-smoke-" + uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")
    request = urllib.request.Request(
        BASE + path,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, response.headers.get_content_type(), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get_content_type(), exc.read()


def png_bytes(width: int, height: int, color=(186, 168, 138)) -> bytes:
    buffer = io.BytesIO()
    with Image.new("RGB", (width, height), color) as image:
        image.save(buffer, format="PNG")
    return buffer.getvalue()


def multipart_json(path: str, filename: str, content: bytes, content_type: str):
    status, _ctype, body = fetch_multipart(path, "file", filename, content, content_type)
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = None
    return status, parsed


def multipart_form(
    path: str,
    fields: dict[str, str],
    field: str,
    filename: str,
    content: bytes,
    content_type: str,
):
    boundary = "----homai-smoke-" + uuid.uuid4().hex
    chunks = []
    for name, value in fields.items():
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    chunks.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
        + content
        + f"\r\n--{boundary}--\r\n".encode("utf-8")
    )
    request = urllib.request.Request(
        BASE + path,
        data=b"".join(chunks),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, response.headers.get_content_type(), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get_content_type(), exc.read()


def wait_item_analysis(item_id: str, timeout: float = 20.0):
    deadline = time.time() + timeout
    status = None
    item = None
    while time.time() < deadline:
        status, item = fetch_json(
            "/api/remodel/items/" + urllib.parse.quote(item_id, safe="")
        )
        if isinstance(item, dict):
            structure = item.get("structure")
            if item.get("analysis_error"):
                return status, item
            if isinstance(structure, dict) and str(structure.get("structure_en") or "").strip():
                return status, item
        time.sleep(0.15)
    return status, item


def wait_remodel_job(job_id: str, timeout: float = 20.0):
    deadline = time.time() + timeout
    snapshot = None
    status = None
    while time.time() < deadline:
        status, snapshot = fetch_json("/api/remodel/jobs/" + urllib.parse.quote(job_id, safe=""))
        if status == 200 and isinstance(snapshot, dict) and snapshot.get("status") in {"done", "error"}:
            return status, snapshot
        time.sleep(0.2)
    return status, snapshot


def overlay_ids() -> set[str]:
    path = ROOT / "style" / "prompt_overrides.json"
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return set(data) if isinstance(data, dict) else set()
    except Exception:
        return set()


def demo_metadata_contract() -> tuple[bool, str]:
    """demo/metadata.json must describe exactly 10 entries whose images exist."""
    path = ROOT / "demo" / "metadata.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"无法读取 demo/metadata.json：{exc}"
    if not isinstance(data, list):
        return False, "demo/metadata.json 不是数组"
    missing = []
    for item in data:
        if not isinstance(item, dict):
            missing.append("<non-object>")
            continue
        local_path = str(item.get("local_path") or "")
        if not local_path or not (ROOT / "demo" / local_path).is_file():
            missing.append(local_path or "<empty>")
    ok = len(data) == 10 and not missing
    return ok, f"entries={len(data)}, missing={len(missing)}"


def runtime_gallery_count() -> int:
    """Number of gallery entries in the runtime metadata.json."""
    try:
        data = json.loads((ROOT / "metadata.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    return len(data) if isinstance(data, list) else 0


def runtime_remodel_count() -> int:
    """Number of remodel project directories under remodel/items."""
    items_dir = ROOT / "remodel" / "items"
    if not items_dir.is_dir():
        return 0
    return sum(1 for path in items_dir.iterdir() if path.is_dir())


def runtime_seed_snapshot() -> dict:
    images_dir = ROOT / "images" / "ai"
    style_items_dir = ROOT / "style" / "items"
    style_palette_dir = ROOT / "style" / "palette"
    metadata_path = ROOT / "metadata.json"
    return {
        "webp": len(list(images_dir.rglob("*.webp"))) if images_dir.is_dir() else 0,
        "style_items": (
            len(list(style_items_dir.glob("*.json")))
            if style_items_dir.is_dir()
            else 0
        ),
        "palette": (
            len(list(style_palette_dir.glob("*.json")))
            if style_palette_dir.is_dir()
            else 0
        ),
        "remodel": runtime_remodel_count(),
        "metadata_mtime": (
            metadata_path.stat().st_mtime_ns if metadata_path.is_file() else 0
        ),
    }


def demo_seed_noop_contract() -> tuple[bool, str]:
    """seed_demo_data() imports missing demo data once, then is a no-op."""
    try:
        gui_server.seed_demo_data()
    except Exception as exc:
        return False, f"seed_demo_data 首次调用失败：{exc}"
    if not (ROOT / "metadata.json").is_file():
        return False, "seed_demo_data 未生成 metadata.json"
    before = runtime_seed_snapshot()
    try:
        gui_server.seed_demo_data()
    except Exception as exc:
        return False, f"seed_demo_data 重复调用失败：{exc}"
    after = runtime_seed_snapshot()
    ok = before == after
    detail = (
        f"gallery={runtime_gallery_count()}, remodel={after['remodel']}, "
        f"style_items={after['style_items']}, palette={after['palette']}, "
        f"webp={after['webp']}, mtime {before['metadata_mtime']}->{after['metadata_mtime']}"
    )
    return ok, detail


def wait_ready(process: subprocess.Popen) -> bool:
    deadline = time.time() + 20
    while time.time() < deadline:
        if process.poll() is not None:
            return False
        try:
            status, _content_type, _body = fetch("/api/bootstrap")
            if status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def chrome_dom(url: str, budget_ms: int = 6000) -> str | None:
    """Return the rendered DOM from headless Chrome, or None if unavailable."""
    chrome = next(
        (
            path
            for path in (
                shutil.which("google-chrome"),
                shutil.which("google-chrome-stable"),
                shutil.which("chromium"),
                shutil.which("chromium-browser"),
            )
            if path
        ),
        None,
    )
    if not chrome:
        return None
    with tempfile.TemporaryDirectory(prefix="homai-chrome-") as profile:
        try:
            completed = subprocess.run(
                [
                    chrome,
                    "--headless=new",
                    "--no-sandbox",
                    "--disable-gpu",
                    "--disable-dev-shm-usage",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-background-networking",
                    f"--user-data-dir={profile}",
                    f"--virtual-time-budget={int(budget_ms)}",
                    "--dump-dom",
                    url,
                ],
                capture_output=True,
                text=True,
                timeout=40,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0 or not completed.stdout.strip():
            return None
        return completed.stdout


def html_text(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def chrome_binary() -> str | None:
    return next(
        (
            path
            for path in (
                shutil.which("google-chrome"),
                shutil.which("google-chrome-stable"),
                shutil.which("chromium"),
                shutil.which("chromium-browser"),
            )
            if path
        ),
        None,
    )


class _CDPSocket:
    """Minimal Chrome DevTools Protocol client over a stdlib WebSocket."""

    def __init__(self, url: str, timeout: float = 20.0):
        parsed = urllib.parse.urlparse(url)
        self.sock = socket.create_connection((parsed.hostname, parsed.port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        path = parsed.path + (("?" + parsed.query) if parsed.query else "")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Origin: http://127.0.0.1\r\n\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("DevTools WebSocket 握手被关闭")
            response += chunk
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise RuntimeError("DevTools WebSocket 握手失败：" + response[:200].decode("utf-8", "replace"))
        self.buffer = b""
        self._id = 0

    def _read_exact(self, size: int) -> bytes:
        while len(self.buffer) < size:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("DevTools WebSocket 已关闭")
            self.buffer += chunk
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def _read_frame(self) -> tuple[bool, int, bytes]:
        header = self._read_exact(2)
        fin = bool(header[0] & 0x80)
        opcode = header[0] & 0x0F
        masked = bool(header[1] & 0x80)
        length = header[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        mask = self._read_exact(4) if masked else None
        payload = self._read_exact(length)
        if mask:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return fin, opcode, payload

    def _send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        mask = os.urandom(4)
        frame = bytearray([0x81])
        length = len(payload)
        if length < 126:
            frame.append(0x80 | length)
        elif length < 65536:
            frame.append(0x80 | 126)
            frame += struct.pack("!H", length)
        else:
            frame.append(0x80 | 127)
            frame += struct.pack("!Q", length)
        frame += mask
        frame += bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(frame))

    def _read_message(self) -> str:
        chunks = []
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == 0x9:  # ping
                mask = os.urandom(4)
                self.sock.sendall(
                    bytes([0x8A, 0x80 | len(payload)])
                    + mask
                    + bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
                )
                continue
            if opcode == 0x8:
                raise RuntimeError("DevTools WebSocket 收到关闭帧")
            if opcode in (0x0, 0x1):
                chunks.append(payload)
            if fin:
                return b"".join(chunks).decode("utf-8")

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        self._send_text(
            json.dumps({"id": self._id, "method": method, "params": params or {}})
        )
        while True:
            message = json.loads(self._read_message())
            if message.get("id") != self._id:
                continue
            if "error" in message:
                raise RuntimeError(f"{method}: {message['error']}")
            return message.get("result", {})


class ChromePage:
    """Headless Chrome page for interaction smoke tests (screenshots included)."""

    def __init__(self, url: str, timeout: float = 20.0, preload: str | None = None):
        chrome = chrome_binary()
        if not chrome:
            raise RuntimeError("未找到 Chrome")
        self._profile = tempfile.mkdtemp(prefix="homai-chrome-cdp-")
        self._process = subprocess.Popen(
            [
                chrome,
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-background-networking",
                "--remote-debugging-port=0",
                "--remote-allow-origins=*",
                f"--user-data-dir={self._profile}",
                "about:blank",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            port = self._wait_for_port(timeout)
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/list", timeout=5
            ) as response:
                targets = json.load(response)
            pages = [target for target in targets if target.get("type") == "page"]
            if not pages:
                raise RuntimeError("Chrome 没有 page target")
            self._cdp = _CDPSocket(pages[0]["webSocketDebuggerUrl"])
            self._cdp.call("Page.enable")
            self._cdp.call("Runtime.enable")
            if preload:
                self._cdp.call(
                    "Page.addScriptToEvaluateOnNewDocument", {"source": preload}
                )
            self._cdp.call("Page.navigate", {"url": url})
            self._wait_for_app(timeout)
        except Exception:
            self.close()
            raise

    def _wait_for_port(self, timeout: float) -> int:
        port_file = Path(self._profile) / "DevToolsActivePort"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise RuntimeError("Chrome 提前退出")
            try:
                lines = port_file.read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
            if len(lines) >= 2 and lines[0].strip().isdigit():
                return int(lines[0])
            time.sleep(0.1)
        raise RuntimeError("等待 Chrome DevTools 端口超时")

    def _wait_for_app(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                ready = self.evaluate(
                    "document.readyState === 'complete' && "
                    "!!document.getElementById('rm-new-item')"
                )
            except Exception:
                ready = False
            if ready:
                return
            time.sleep(0.1)
        raise RuntimeError("等待页面完成加载超时")

    def evaluate(self, expression: str):
        result = self._cdp.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
                "userGesture": True,
            },
        )
        if "exceptionDetails" in result:
            raise RuntimeError(str(result["exceptionDetails"]))
        return result.get("result", {}).get("value")

    def set_file_input(self, selector: str, path: Path) -> None:
        document = self._cdp.call("DOM.getDocument", {"depth": -1})
        found = self._cdp.call(
            "DOM.querySelector",
            {"nodeId": document["root"]["nodeId"], "selector": selector},
        )
        node_id = found.get("nodeId")
        if not node_id:
            raise RuntimeError(f"找不到 {selector}")
        self._cdp.call(
            "DOM.setFileInputFiles", {"nodeId": node_id, "files": [str(path)]}
        )

    def screenshot(self, path: Path) -> None:
        shot = self._cdp.call("Page.captureScreenshot", {"format": "png"})
        path.write_bytes(base64.b64decode(shot["data"]))

    def close(self) -> None:
        try:
            self._cdp.sock.close()
        except Exception:
            pass
        try:
            self._process.terminate()
            self._process.wait(timeout=5)
        except Exception:
            try:
                self._process.kill()
            except Exception:
                pass
        shutil.rmtree(self._profile, ignore_errors=True)


PREVIEW_SMOKE_JS = r"""
(async () => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  for (let attempt = 0; attempt < 120; attempt += 1) {
    if (document.readyState === "complete" && document.getElementById("rm-create-drop")) break;
    await sleep(100);
  }
  const $ = (id) => document.getElementById(id);
  const drop = $("rm-create-drop");
  const input = $("rm-create-file");
  const preview = $("rm-create-preview");
  const placeholder = $("rm-create-placeholder");
  const actions = $("rm-create-file-actions");
  const remove = $("rm-create-remove");
  const error = $("rm-create-error");
  const submit = $("rm-create-submit");
  const nameLine = $("rm-create-file-name");
  const openButton = $("rm-new-item");
  if (!drop || !input || !preview || !placeholder || !actions || !remove || !error || !submit || !nameLine || !openButton) {
    return { missing: true };
  }
  const visible = (node) => !node.hidden && getComputedStyle(node).display !== "none";
  openButton.click();
  await sleep(80);
  const dialog = $("rm-dialog-create");
  const out = { open: !!dialog && dialog.open };

  out.initial = {
    hidden: preview.hidden,
    displayNone: getComputedStyle(preview).display === "none",
    placeholder: visible(placeholder),
    submitDisabled: submit.disabled,
    errorRole: error.getAttribute("role"),
    dropTabIndex: drop.getAttribute("tabindex"),
    inputTabIndex: input.getAttribute("tabindex")
  };

  const created = [];
  const revoked = [];
  const realCreate = URL.createObjectURL.bind(URL);
  const realRevoke = URL.revokeObjectURL.bind(URL);
  URL.createObjectURL = (blob) => {
    const url = realCreate(blob);
    created.push(url);
    return url;
  };
  URL.revokeObjectURL = (url) => {
    revoked.push(url);
    return realRevoke(url);
  };

  const canvas = document.createElement("canvas");
  canvas.width = 4;
  canvas.height = 3;
  const context = canvas.getContext("2d");
  context.fillStyle = "#c8a060";
  context.fillRect(0, 0, 4, 3);
  const pngBlob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
  const makeFile = (name, type, blob) => new File([blob], name, { type });
  const select = (file) => {
    const transfer = new DataTransfer();
    transfer.items.add(file);
    input.files = transfer.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  };

  select(makeFile("room.png", "image/png", pngBlob));
  await sleep(60);
  const style = getComputedStyle(preview);
  out.valid = {
    hidden: preview.hidden,
    visible: visible(preview),
    srcBlob: String(preview.src).startsWith("blob:"),
    alt: preview.alt,
    actionsVisible: visible(actions),
    submitEnabled: !submit.disabled,
    nameLine: nameLine.textContent,
    created: created.length,
    decodes: await (async () => {
      try {
        await preview.decode();
        return preview.naturalWidth > 0 && preview.naturalHeight > 0;
      } catch (err) {
        return false;
      }
    })(),
    fillsBox: Math.abs(preview.getBoundingClientRect().width - drop.clientWidth) <= 2,
    maxHeight: style.maxHeight,
    objectFit: style.objectFit,
    borderRadius: style.borderRadius
  };

  select(makeFile("room-retry.png", "image/png", pngBlob));
  await sleep(40);
  out.reselect = {
    created: created.length,
    oldRevoked: revoked.includes(created[0]),
    alt: preview.alt
  };

  remove.click();
  await sleep(40);
  out.removed = {
    hidden: preview.hidden,
    srcEmpty: !preview.getAttribute("src"),
    placeholder: visible(placeholder),
    submitDisabled: submit.disabled,
    nameLine: nameLine.textContent,
    revokedLatest: revoked.includes(created[created.length - 1])
  };

  select(makeFile("room-close.png", "image/png", pngBlob));
  await sleep(40);
  const closeUrl = created[created.length - 1];
  dialog.close();
  await sleep(40);
  out.closed = { revokedLatest: revoked.includes(closeUrl) };

  openButton.click();
  await sleep(60);
  const realClick = input.click.bind(input);
  let clicks = 0;
  input.click = () => {
    clicks += 1;
  };
  drop.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  drop.dispatchEvent(new KeyboardEvent("keydown", { key: " ", bubbles: true }));
  input.click = realClick;
  out.keyboard = { clicks };

  const transfer = new DataTransfer();
  transfer.items.add(makeFile("drag.png", "image/png", pngBlob));
  drop.dispatchEvent(new DragEvent("dragover", { bubbles: true, cancelable: true, dataTransfer: transfer }));
  out.dragOver = drop.classList.contains("is-dragover");
  drop.dispatchEvent(new DragEvent("drop", { bubbles: true, cancelable: true, dataTransfer: transfer }));
  await sleep(60);
  out.drop = {
    hidden: preview.hidden,
    srcBlob: String(preview.src).startsWith("blob:"),
    alt: preview.alt,
    highlightCleared: !drop.classList.contains("is-dragover")
  };
  drop.dispatchEvent(new DragEvent("dragleave", { bubbles: true, dataTransfer: new DataTransfer() }));
  out.dragLeave = !drop.classList.contains("is-dragover");
  remove.click();

  select(makeFile("notes.txt", "text/plain", new Blob(["hello"])));
  await sleep(40);
  out.badType = {
    hidden: preview.hidden,
    placeholder: visible(placeholder),
    submitDisabled: submit.disabled,
    error: error.textContent,
    errorVisible: !error.hidden
  };

  select(makeFile("huge.png", "image/png", new Blob([new Uint8Array(12 * 1024 * 1024 + 1)])));
  await sleep(40);
  out.tooBig = {
    hidden: preview.hidden,
    submitDisabled: submit.disabled,
    error: error.textContent,
    errorVisible: !error.hidden
  };
  return out;
})()
"""


def preview_source_contract() -> tuple[bool, str]:
    index_html = (ROOT / "gui" / "index.html").read_text(encoding="utf-8")
    remodel_js = (ROOT / "gui" / "remodel.js").read_text(encoding="utf-8")
    html_tokens = [
        'id="rm-create-drop"',
        'id="rm-create-preview"',
        'id="rm-create-placeholder"',
        'id="rm-create-file-actions"',
        'id="rm-create-remove"',
        'id="rm-create-reselect"',
        'role="alert"',
    ]
    js_tokens = [
        "URL.createObjectURL",
        "URL.revokeObjectURL",
        '"dragover"',
        '"dragleave"',
        '"drop"',
    ]
    missing = [token for token in html_tokens if token not in index_html]
    missing += [token for token in js_tokens if token not in remodel_js]
    return not missing, "源码缺失：" + ", ".join(missing) if missing else "已按源码契约检查"


def preview_contract_ok(out) -> tuple[bool, str]:
    if not isinstance(out, dict) or out.get("missing"):
        return False, "页面缺少新建对话框的预览/操作元素"
    initial = out.get("initial") or {}
    valid = out.get("valid") or {}
    reselect = out.get("reselect") or {}
    removed = out.get("removed") or {}
    closed = out.get("closed") or {}
    keyboard = out.get("keyboard") or {}
    drag_over = out.get("dragOver")
    dropped = out.get("drop") or {}
    drag_leave = out.get("dragLeave")
    bad_type = out.get("badType") or {}
    too_big = out.get("tooBig") or {}
    checks = {
        "dialog_open": out.get("open") is True,
        "initial_hidden": initial.get("hidden") is True and initial.get("displayNone") is True,
        "initial_placeholder": initial.get("placeholder") is True,
        "initial_submit_disabled": initial.get("submitDisabled") is True,
        "error_role_alert": initial.get("errorRole") == "alert",
        "single_tab_stop": initial.get("dropTabIndex") == "0" and initial.get("inputTabIndex") == "-1",
        "valid_shown": valid.get("hidden") is False and valid.get("visible") is True,
        "valid_blob_url": valid.get("srcBlob") is True,
        "valid_alt": valid.get("alt") == "room.png",
        "valid_actions": valid.get("actionsVisible") is True,
        "valid_submit_enabled": valid.get("submitEnabled") is True,
        "valid_filename_line": "room.png" in str(valid.get("nameLine") or ""),
        "valid_decodes": valid.get("decodes") is True,
        "valid_object_url": int(valid.get("created") or 0) >= 1,
        "preview_fills_box": valid.get("fillsBox") is True,
        "preview_css": valid.get("maxHeight") == "260px" and valid.get("objectFit") == "cover",
        "reselect_new_url": int(reselect.get("created") or 0) >= 2,
        "reselect_revokes_old": reselect.get("oldRevoked") is True,
        "reselect_updates_alt": reselect.get("alt") == "room-retry.png",
        "remove_hides": removed.get("hidden") is True and removed.get("srcEmpty") is True,
        "remove_restores_placeholder": removed.get("placeholder") is True,
        "remove_disables_submit": removed.get("submitDisabled") is True,
        "remove_clears_line": removed.get("nameLine") == "",
        "remove_revokes_url": removed.get("revokedLatest") is True,
        "close_revokes_url": closed.get("revokedLatest") is True,
        "keyboard_opens_picker": int(keyboard.get("clicks") or 0) == 2,
        "dragover_highlight": drag_over is True,
        "drop_preview": dropped.get("hidden") is False and dropped.get("srcBlob") is True,
        "drop_alt": dropped.get("alt") == "drag.png",
        "drop_clears_highlight": dropped.get("highlightCleared") is True,
        "dragleave_clears_highlight": drag_leave is True,
        "bad_type_inline_error": bad_type.get("errorVisible") is True
        and bad_type.get("hidden") is True
        and bad_type.get("placeholder") is True
        and bad_type.get("submitDisabled") is True
        and "JPEG" in str(bad_type.get("error") or ""),
        "too_big_inline_error": too_big.get("errorVisible") is True
        and too_big.get("hidden") is True
        and too_big.get("submitDisabled") is True
        and "12 MB" in str(too_big.get("error") or ""),
    }
    failed = [name for name, ok in checks.items() if not ok]
    if not failed:
        return True, (
            "preview + blob 生命周期 + 移除/校验/拖放/键盘 全部通过"
        )
    return False, "失败项：" + ", ".join(failed) + f"（原始结果={json.dumps(out, ensure_ascii=False)[:1200]}）"


def preview_interaction_contract() -> tuple[bool, str]:
    page = ChromePage(f"{BASE}/#remodel")
    try:
        out = page.evaluate(PREVIEW_SMOKE_JS)
    finally:
        page.close()
    return preview_contract_ok(out)


# ---------- TASK19 detail-image lightbox ----------

LIGHTBOX_WAIT_JS = r"""
(async () => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    const source = document.getElementById("rm-source-img");
    const latest = document.getElementById("rm-current-img");
    const state = document.getElementById("rm-analysis-state");
    const box = document.querySelector(".rm-lightbox");
    if (
      source && source.getAttribute("src") &&
      latest && latest.getAttribute("src") &&
      document.querySelector(".rm-gen-thumb") &&
      state && state.textContent.trim() &&
      box
    ) {
      return true;
    }
    await sleep(100);
  }
  return false;
})()
"""

LIGHTBOX_DETAIL_JS = r"""
(() => {
  const state = document.getElementById("rm-analysis-state");
  const source = document.getElementById("rm-source-img");
  const latest = document.getElementById("rm-current-img");
  const thumb = document.querySelector(".rm-gen-thumb");
  const textMode = document.querySelector('[data-item-mode="text"]');
  const imageMode = document.querySelector('[data-item-mode="img2img"]');
  const readState = () => (state ? state.textContent.trim() : "");
  const initial = readState();
  if (textMode) textMode.click();
  const textState = readState();
  if (imageMode) imageMode.click();
  const backState = readState();
  return {
    analysisState: initial,
    textState: textState,
    backState: backState,
    reanalyzeLabel: (() => {
      const button = document.getElementById("rm-reanalyze-item");
      return button ? button.textContent.trim() : "";
    })(),
    sourceRole: source ? source.getAttribute("role") : "",
    sourceTabIndex: source ? source.getAttribute("tabindex") : "",
    sourceLabel: source ? source.getAttribute("aria-label") : "",
    latestRole: latest ? latest.getAttribute("role") : "",
    latestTabIndex: latest ? latest.getAttribute("tabindex") : "",
    latestLabel: latest ? latest.getAttribute("aria-label") : "",
    thumbTag: thumb ? thumb.tagName.toLowerCase() : "",
    thumbLabel: thumb ? thumb.getAttribute("aria-label") : ""
  };
})()
"""

LIGHTBOX_OPEN_JS = r"""
(async () => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const opener = document.getElementById("rm-current-img");
  opener.focus();
  opener.dispatchEvent(new MouseEvent("click", {bubbles: true, cancelable: true}));
  const deadline = Date.now() + 5000;
  let box = null;
  let image = null;
  while (Date.now() < deadline) {
    box = document.querySelector(".rm-lightbox");
    image = box && box.querySelector(".rm-lightbox-image");
    if (box && !box.hidden && image && image.complete && image.naturalWidth) break;
    await sleep(50);
  }
  const dialog = box && box.querySelector(".rm-lightbox-dialog");
  const caption = box && box.querySelector(".rm-lightbox-caption");
  const download = box && box.querySelector(".rm-lightbox-download");
  const rect = image ? image.getBoundingClientRect() : null;
  return {
    opened: !!(box && !box.hidden),
    role: dialog ? dialog.getAttribute("role") : "",
    ariaModal: dialog ? dialog.getAttribute("aria-modal") : "",
    caption: caption ? caption.textContent.trim() : "",
    naturalWidth: image ? image.naturalWidth : 0,
    naturalHeight: image ? image.naturalHeight : 0,
    renderedWidth: rect ? Math.round(rect.width) : 0,
    renderedHeight: rect ? Math.round(rect.height) : 0,
    mode: box ? box.dataset.mode : "",
    index: box ? box.dataset.index : "",
    focusInside: !!(box && document.activeElement && box.contains(document.activeElement)),
    downloadHref: download ? download.getAttribute("href") : ""
  };
})()
"""

LIGHTBOX_TOGGLE_JS = r"""
(async () => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const box = document.querySelector(".rm-lightbox");
  const image = box && box.querySelector(".rm-lightbox-image");
  if (!box || !image) return {mode: "", renderedWidth: 0, renderedHeight: 0};
  image.dispatchEvent(new MouseEvent("click", {bubbles: true, cancelable: true}));
  await sleep(150);
  const rect = image.getBoundingClientRect();
  return {
    mode: box.dataset.mode,
    naturalWidth: image.naturalWidth,
    naturalHeight: image.naturalHeight,
    renderedWidth: Math.round(rect.width),
    renderedHeight: Math.round(rect.height),
    cursor: getComputedStyle(image).cursor
  };
})()
"""

LIGHTBOX_PAN_JS = r"""
(async () => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const box = document.querySelector(".rm-lightbox");
  const image = box && box.querySelector(".rm-lightbox-image");
  const stage = box && box.querySelector(".rm-lightbox-stage");
  if (!image || !stage) return {scrollBefore: [0, 0], scrollAfter: [0, 0]};
  const before = [stage.scrollLeft, stage.scrollTop];
  image.dispatchEvent(new PointerEvent("pointerdown", {
    bubbles: true, cancelable: true, pointerId: 1, pointerType: "mouse",
    isPrimary: true, button: 0, buttons: 1, clientX: 480, clientY: 360
  }));
  const cursorDown = getComputedStyle(image).cursor;
  document.dispatchEvent(new PointerEvent("pointermove", {
    bubbles: true, cancelable: true, pointerId: 1, pointerType: "mouse",
    isPrimary: true, button: 0, buttons: 1, clientX: 360, clientY: 260
  }));
  await sleep(80);
  document.dispatchEvent(new PointerEvent("pointerup", {
    bubbles: true, cancelable: true, pointerId: 1, pointerType: "mouse",
    isPrimary: true, button: 0, buttons: 0, clientX: 360, clientY: 260
  }));
  await sleep(80);
  return {
    cursorDown,
    scrollBefore: before,
    scrollAfter: [stage.scrollLeft, stage.scrollTop]
  };
})()
"""

LIGHTBOX_NAV_CLOSE_JS = r"""
(async () => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const box = document.querySelector(".rm-lightbox");
  const opener = document.getElementById("rm-current-img");
  document.dispatchEvent(new KeyboardEvent("keydown", {
    key: "ArrowRight", bubbles: true, cancelable: true
  }));
  await sleep(180);
  const caption = box && box.querySelector(".rm-lightbox-caption");
  const navState = {
    caption2: caption ? caption.textContent.trim() : "",
    index2: box ? box.dataset.index : "",
    modeAfterNav: box ? box.dataset.mode : ""
  };
  document.dispatchEvent(new KeyboardEvent("keydown", {
    key: "Escape", bubbles: true, cancelable: true
  }));
  await sleep(180);
  return Object.assign(navState, {
    closed: !!(box && box.hidden),
    focusRestored: document.activeElement === opener
  });
})()
"""

LIGHTBOX_SOURCE_CLOSE_JS = r"""
(async () => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const box = document.querySelector(".rm-lightbox");
  const source = document.getElementById("rm-source-img");
  source.focus();
  source.dispatchEvent(new MouseEvent("click", {bubbles: true, cancelable: true}));
  await sleep(180);
  const caption = box && box.querySelector(".rm-lightbox-caption");
  const opened = !!(box && !box.hidden);
  const captionText = caption ? caption.textContent.trim() : "";
  document.dispatchEvent(new KeyboardEvent("keydown", {
    key: "Escape", bubbles: true, cancelable: true
  }));
  await sleep(120);
  return {opened: opened, caption: captionText, closed: !!(box && box.hidden)};
})()
"""

LIGHTBOX_KEYBOARD_JS = r"""
(async () => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const source = document.getElementById("rm-source-img");
  const box = document.querySelector(".rm-lightbox");
  source.focus();
  source.dispatchEvent(new KeyboardEvent("keydown", {
    key: "Enter", bubbles: true, cancelable: true
  }));
  await sleep(180);
  const opened = !!(box && !box.hidden);
  document.dispatchEvent(new KeyboardEvent("keydown", {
    key: "Escape", bubbles: true, cancelable: true
  }));
  await sleep(120);
  return {opened: opened, closed: !!(box && box.hidden)};
})()
"""

LIGHTBOX_BACKDROP_JS = r"""
(async () => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const opener = document.getElementById("rm-current-img");
  const box = document.querySelector(".rm-lightbox");
  opener.focus();
  opener.dispatchEvent(new MouseEvent("click", {bubbles: true, cancelable: true}));
  await sleep(180);
  const opened = !!(box && !box.hidden);
  const scrim = box && box.querySelector(".rm-lightbox-scrim");
  if (scrim) scrim.dispatchEvent(new MouseEvent("click", {bubbles: true, cancelable: true}));
  await sleep(120);
  return {
    opened: opened,
    closed: !!(box && box.hidden),
    focusRestored: document.activeElement === opener
  };
})()
"""

LIGHTBOX_TRAP_JS = r"""
(async () => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const opener = document.getElementById("rm-current-img");
  const box = document.querySelector(".rm-lightbox");
  opener.focus();
  opener.dispatchEvent(new MouseEvent("click", {bubbles: true, cancelable: true}));
  await sleep(180);
  const close = box && box.querySelector(".rm-lightbox-close");
  const download = box && box.querySelector(".rm-lightbox-download");
  if (download) download.focus();
  document.dispatchEvent(new KeyboardEvent("keydown", {
    key: "Tab", bubbles: true, cancelable: true
  }));
  const forwardWrap = document.activeElement === close;
  if (close) close.focus();
  document.dispatchEvent(new KeyboardEvent("keydown", {
    key: "Tab", shiftKey: true, bubbles: true, cancelable: true
  }));
  const backwardWrap = document.activeElement === download;
  document.dispatchEvent(new KeyboardEvent("keydown", {
    key: "Escape", bubbles: true, cancelable: true
  }));
  await sleep(120);
  return {
    forwardWrap: forwardWrap,
    backwardWrap: backwardWrap,
    closed: !!(box && box.hidden)
  };
})()
"""

LIGHTBOX_BUTTONS_JS = r"""
(async () => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const opener = document.getElementById("rm-current-img");
  const box = document.querySelector(".rm-lightbox");
  opener.focus();
  opener.dispatchEvent(new MouseEvent("click", {bubbles: true, cancelable: true}));
  await sleep(180);
  const next = box && box.querySelector(".rm-lightbox-nav.next");
  const prev = box && box.querySelector(".rm-lightbox-nav.prev");
  if (next) next.click();
  await sleep(120);
  const nextIndex = box ? box.dataset.index : "";
  if (prev) prev.click();
  await sleep(120);
  const prevIndex = box ? box.dataset.index : "";
  document.dispatchEvent(new KeyboardEvent("keydown", {
    key: "Escape", bubbles: true, cancelable: true
  }));
  await sleep(120);
  return {
    nextIndex: nextIndex,
    prevIndex: prevIndex,
    closed: !!(box && box.hidden)
  };
})()
"""

LIGHTBOX_THUMB_JS = r"""
(async () => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const thumbs = document.querySelectorAll(".rm-gen-thumb");
  const thumb = thumbs[1] || thumbs[0];
  if (!thumb) return {found: false, opened: false, compareOpen: false, caption: ""};
  thumb.click();
  await sleep(220);
  const box = document.querySelector(".rm-lightbox");
  const caption = box && box.querySelector(".rm-lightbox-caption");
  const compare = document.getElementById("rm-dialog-compare");
  const out = {
    found: true,
    opened: !!(box && !box.hidden),
    compareOpen: !!(compare && compare.open),
    caption: caption ? caption.textContent.trim() : ""
  };
  document.dispatchEvent(new KeyboardEvent("keydown", {
    key: "Escape", bubbles: true, cancelable: true
  }));
  await sleep(120);
  return out;
})()
"""

LIST_NAV_JS = r"""
(async () => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const deadline = Date.now() + 10000;
  let cover = null;
  while (Date.now() < deadline) {
    cover = document.querySelector(".rm-item-cover");
    if (cover) break;
    await sleep(100);
  }
  if (!cover) return {found: false, hash: location.hash, expected: "", lightboxVisible: false};
  const expected = cover.getAttribute("href");
  cover.click();
  const hashDeadline = Date.now() + 5000;
  while (Date.now() < hashDeadline && location.hash !== expected) {
    await sleep(100);
  }
  const box = document.querySelector(".rm-lightbox");
  return {
    found: true,
    expected: expected,
    hash: location.hash,
    lightboxVisible: !!(box && !box.hidden)
  };
})()
"""


def lightbox_interaction_contract(item_id: str) -> tuple[bool, str, dict]:
    shots = ROOT / "logs" / "task19"
    shots.mkdir(parents=True, exist_ok=True)
    page = ChromePage(
        f"{BASE}/#remodel/{urllib.parse.quote(item_id, safe='')}"
    )
    try:
        page._cdp.call(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": 1280,
                "height": 900,
                "deviceScaleFactor": 1,
                "mobile": False,
            },
        )
        if not page.evaluate(LIGHTBOX_WAIT_JS):
            raise RuntimeError("详情页或灯箱未在超时内就绪")
        detail = page.evaluate(LIGHTBOX_DETAIL_JS)
        page.evaluate(
            "document.getElementById('rm-analysis-state')"
            ".scrollIntoView({block: 'center'})"
        )
        page.screenshot(shots / "detail-no-analysis.png")
        opened = page.evaluate(LIGHTBOX_OPEN_JS)
        page.screenshot(shots / "lightbox-fit.png")
        actual = page.evaluate(LIGHTBOX_TOGGLE_JS)
        page.screenshot(shots / "lightbox-1x1.png")
        pan = page.evaluate(LIGHTBOX_PAN_JS)
        nav = page.evaluate(LIGHTBOX_NAV_CLOSE_JS)
        source = page.evaluate(LIGHTBOX_SOURCE_CLOSE_JS)
        keyboard = page.evaluate(LIGHTBOX_KEYBOARD_JS)
        backdrop = page.evaluate(LIGHTBOX_BACKDROP_JS)
        trap = page.evaluate(LIGHTBOX_TRAP_JS)
        buttons = page.evaluate(LIGHTBOX_BUTTONS_JS)
        thumb = page.evaluate(LIGHTBOX_THUMB_JS)
    finally:
        page.close()
    out = {
        "detail": detail,
        "opened": opened,
        "actual": actual,
        "pan": pan,
        "nav": nav,
        "source": source,
        "keyboard": keyboard,
        "backdrop": backdrop,
        "trap": trap,
        "buttons": buttons,
        "thumb": thumb,
    }
    return lightbox_contract_ok(out), json.dumps(out, ensure_ascii=False), out


def list_navigation_contract() -> tuple[bool, str]:
    page = ChromePage(f"{BASE}/#remodel")
    try:
        out = page.evaluate(LIST_NAV_JS)
    finally:
        page.close()
    ok = (
        isinstance(out, dict)
        and out.get("found") is True
        and out.get("hash") == out.get("expected")
        and str(out.get("hash") or "").startswith("#remodel/")
        and out.get("lightboxVisible") is False
    )
    return ok, json.dumps(out, ensure_ascii=False)


GALLERY_DETAIL_PRELOAD_JS = r"""
(() => {
  const samples = [
    {
      url: "/samples/bedroom-20261105-001.png",
      model: "mock-old",
      size: "1080*1440",
      seconds: 3,
      created_at: "2026-11-17T10:00:00Z"
    },
    {
      url: "/samples/bedroom-20261105-003.png",
      model: "mock-mid",
      size: "1080*1440",
      seconds: 4,
      created_at: "2026-11-17T11:00:00Z"
    },
    {
      url: "/samples/bedroom-20261105-009.png",
      model: "mock-new",
      size: "1080*1440",
      seconds: 5,
      created_at: "2026-11-17T12:00:00Z"
    }
  ];
  const mock = {
    nextStatus: "done",
    jobs: new Map(),
    jobSeq: 0,
    detail: {
      id: "mock-item",
      name: "模拟卧室",
      style_primary_zh: "测试风格",
      category_zh: "卧室",
      local_path: "samples/bedroom-20261105-001.png",
      actual_width: 1080,
      actual_height: 1440,
      prompt_en: "mock prompt",
      prompt_zh: "模拟提示词",
      prompt_short_en: "",
      negative_en: "",
      palette: [],
      samples: samples.slice()
    }
  };
  window.__homaiMock = mock;

  const originalFetch = window.fetch.bind(window);
  const respond = (payload, status) =>
    new Response(JSON.stringify(payload), {
      status: status || 200,
      headers: { "Content-Type": "application/json" }
    });

  window.fetch = async (input, init) => {
    const raw = typeof input === "string" ? input : (input && input.url) || "";
    const url = new URL(raw, location.href);
    const path = url.pathname;
    if (path === "/api/bootstrap") {
      return respond({
        total: 1,
        generated_count: 1,
        edited_count: 0,
        categories: [{ name_zh: "卧室", count: 1 }],
        styles: [{ name_zh: "测试风格", count: 1 }],
        models: ["qwen-image-3.0-pro"],
        sizes: ["1080*1440"]
      });
    }
    if (path === "/api/items") {
      return respond({
        total: 1,
        items: [
          {
            id: "mock-item",
            name: "模拟卧室",
            style_primary_zh: "测试风格",
            thumb_url: "/samples/bedroom-20261105-001.png",
            palette: [],
            generated: true
          }
        ]
      });
    }
    if (path === "/api/items/mock-item") {
      return respond(mock.detail);
    }
    if (path === "/api/items/mock-item/generate") {
      const id = "mock-job-" + (++mock.jobSeq);
      mock.jobs.set(id, { status: mock.nextStatus, polls: 0, finished: false });
      return respond({ job_id: id }, 202);
    }
    if (path.startsWith("/api/jobs/")) {
      const id = path.split("/").pop();
      const job = mock.jobs.get(id);
      if (!job) return respond({ error: "任务不存在" }, 404);
      job.polls += 1;
      if (job.status === "error") {
        return respond({ status: "error", seconds: 0.4, error: "模拟失败" });
      }
      if (job.polls >= 2) {
        if (!job.finished) {
          job.finished = true;
          mock.detail = Object.assign({}, mock.detail, {
            samples: mock.detail.samples.concat([
              {
                url: "/samples/bedroom-20261105-016.png",
                model: "mock-latest",
                size: "1080*1440",
                seconds: 2,
                created_at: "2026-11-17T13:00:00Z"
              }
            ])
          });
        }
        return respond({
          status: "done",
          seconds: 75,
          image_url: "/samples/bedroom-20261105-016.png"
        });
      }
      return respond({ status: "running", seconds: 2 });
    }
    return originalFetch(input, init);
  };
})()
"""


def gallery_detail_completion_contract() -> list[tuple[str, bool, str]]:
    """TASK20: samples first, newest-first cards, completion/failure signals."""
    outcomes: list[tuple[str, bool, str]] = []
    page = ChromePage(f"{BASE}/", preload=GALLERY_DETAIL_PRELOAD_JS)
    try:
        page._cdp.call(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": 1280,
                "height": 900,
                "deviceScaleFactor": 1,
                "mobile": False,
            },
        )
        ready = page.evaluate(
            r"""
            (async () => {
              for (let attempt = 0; attempt < 160; attempt += 1) {
                if (document.querySelector(".sample-grid .sample-card img")) return true;
                await new Promise((resolve) => setTimeout(resolve, 50));
              }
              return false;
            })()
            """
        )
        layout = page.evaluate(
            r"""
            (() => {
              const panel = document.getElementById("detail-panel");
              if (!panel) return { missing: true };
              const children = [...panel.children];
              const indexOf = (node) => (node ? children.indexOf(node) : -1);
              const head = panel.querySelector(".detail-head");
              const sampleSection = [...panel.querySelectorAll(".detail-section")]
                .find((node) => node.querySelector(".sample-grid"));
              const promptSection = [...panel.querySelectorAll(".detail-section")]
                .find((node) => node.querySelector('textarea[data-field="prompt_en"]'));
              const images = [...panel.querySelectorAll(".sample-grid .sample-card img")]
                .map((img) => new URL(img.getAttribute("src"), location.href).pathname);
              const title = sampleSection
                ? sampleSection.querySelector(".section-title")
                : null;
              return {
                ready: !!sampleSection,
                headIndex: indexOf(head),
                sampleIndex: indexOf(sampleSection),
                promptIndex: indexOf(promptSection),
                sources: images,
                title: title ? title.textContent : ""
              };
            })()
            """
        )
        sources = (layout or {}).get("sources") or []
        expected_sources = [
            "/samples/bedroom-20261105-009.png",
            "/samples/bedroom-20261105-003.png",
            "/samples/bedroom-20261105-001.png",
        ]
        outcomes.append(
            (
                "详情页顺序：生成结果紧跟详情头部并先于提示词",
                bool(ready)
                and isinstance(layout, dict)
                and layout.get("sampleIndex") == layout.get("headIndex") + 1
                and layout.get("sampleIndex") < layout.get("promptIndex"),
                json.dumps(
                    {
                        "head": (layout or {}).get("headIndex"),
                        "samples": (layout or {}).get("sampleIndex"),
                        "prompt": (layout or {}).get("promptIndex"),
                    },
                    ensure_ascii=False,
                ),
            )
        )
        outcomes.append(
            (
                "生成结果按最新在前排序（3 张，含标题与最新标记）",
                sources == expected_sources
                and "生成结果 · 3 张" in str((layout or {}).get("title") or ""),
                json.dumps(
                    {
                        "sources": sources,
                        "title": (layout or {}).get("title"),
                    },
                    ensure_ascii=False,
                ),
            )
        )

        completion = page.evaluate(
            r"""
            (async () => {
              const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
              const stack = document.getElementById("toasts");
              stack.replaceChildren();
              const button = document.querySelector("[data-generate-button]");
              if (!button) return { missing: true };
              button.click();
              let card = null;
              for (let attempt = 0; attempt < 160; attempt += 1) {
                card = document.querySelector(".sample-card.is-new");
                if (card) break;
                await sleep(50);
              }
              const successToasts = [...stack.querySelectorAll(".toast.is-success")]
                .map((node) => node.textContent);
              const live = document.querySelector("[data-job-status]");
              const firstImage = document.querySelector(".sample-grid .sample-card img");
              const latestBadge = document.querySelector(".sample-card.is-new .sample-new-badge");
              const out = {
                foundNew: !!card,
                successToasts,
                live: live ? live.textContent : "",
                firstSrc: firstImage
                  ? new URL(firstImage.getAttribute("src"), location.href).pathname
                  : "",
                count: document.querySelectorAll(".sample-card").length,
                latestBadge: !!latestBadge
              };
              await sleep(2200);
              out.stillNew = !!document.querySelector(".sample-card.is-new");
              return out;
            })()
            """
        )
        completion = completion if isinstance(completion, dict) else {}
        success_text = " ".join(completion.get("successToasts") or [])
        outcomes.append(
            (
                "生成完成：成功 toast、live 播报、最新卡片 is-new 2 秒",
                completion.get("foundNew") is True
                and "生成完成" in success_text
                and "1 分 15 秒" in success_text
                and completion.get("live") == "生成完成，共 4 张"
                and completion.get("firstSrc") == "/samples/bedroom-20261105-016.png"
                and completion.get("latestBadge") is True
                and completion.get("stillNew") is False,
                json.dumps(completion, ensure_ascii=False),
            )
        )

        failure = page.evaluate(
            r"""
            (async () => {
              const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
              window.__homaiMock.nextStatus = "error";
              const stack = document.getElementById("toasts");
              stack.replaceChildren();
              const button = document.querySelector("[data-generate-button]");
              if (!button) return { missing: true };
              button.click();
              let toast = null;
              for (let attempt = 0; attempt < 120; attempt += 1) {
                toast = stack.querySelector(".toast.is-error");
                if (toast) break;
                await sleep(50);
              }
              await sleep(350);
              const live = document.querySelector("[data-job-status]");
              return {
                errorToast: toast ? toast.textContent : "",
                isNew: !!document.querySelector(".sample-card.is-new"),
                live: live ? live.textContent : ""
              };
            })()
            """
        )
        failure = failure if isinstance(failure, dict) else {}
        outcomes.append(
            (
                "生成失败：错误 toast 且不添加 is-new",
                "生成失败" in str(failure.get("errorToast") or "")
                and failure.get("isNew") is False,
                json.dumps(failure, ensure_ascii=False),
            )
        )
    finally:
        page.close()
    return outcomes


REMODEL_DETAIL_PRELOAD_JS = r"""
(() => {
  try {
    Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => "visible"
    });
  } catch (error) {
    // Visibility overrides are best-effort only.
  }
  const itemId = "abcdef0123456789";
  const resultUrl = "/samples/bedroom-20261105-016.png";
  const mock = {
    item: {
      item_id: itemId,
      name: "模拟改造项目",
      default_mode: "img2img",
      default_style: "中古现代",
      gen_count: 0,
      updated_at: "2026-11-17T13:00:00Z",
      created_at: "2026-11-17T12:00:00Z",
      source_url: "/samples/bedroom-20261105-001.png",
      source: {
        current: {
          url: "/samples/bedroom-20261105-001.png",
          width: 1080,
          height: 1440
        }
      },
      gens: [],
      failed_jobs: [],
      analysis_state: "none",
      structure: null,
      analysis: null
    },
    jobs: new Map(),
    jobSeq: 0
  };
  window.__homaiRemodelMock = mock;

  const originalFetch = window.fetch.bind(window);
  const respond = (payload, status) =>
    new Response(JSON.stringify(payload), {
      status: status || 200,
      headers: { "Content-Type": "application/json" }
    });

  window.fetch = async (input, init) => {
    const raw = typeof input === "string" ? input : (input && input.url) || "";
    const url = new URL(raw, location.href);
    const path = url.pathname;
    if (path === `/api/remodel/items/${itemId}`) {
      return respond(mock.item);
    }
    if (path === `/api/remodel/items/${itemId}/generate`) {
      const id = "mock-remodel-job-" + (++mock.jobSeq);
      mock.jobs.set(id, { polls: 0, finished: false });
      return respond({ job_id: id, item_id: itemId, gen_id: "f".repeat(32) }, 202);
    }
    if (path.startsWith("/api/remodel/jobs/")) {
      const id = path.split("/").pop();
      const job = mock.jobs.get(id);
      if (!job) return respond({ error: "任务不存在" }, 404);
      job.polls += 1;
      if (job.polls >= 2) {
        if (!job.finished) {
          job.finished = true;
          mock.item = Object.assign({}, mock.item, {
            gen_count: 1,
            gens: [
              {
                gid: "f".repeat(32),
                mode: "img2img",
                style: "中古现代",
                url: resultUrl,
                thumb_url: resultUrl,
                width: 1080,
                height: 1440,
                created_at: "2026-11-17T13:00:00Z",
                score: 0.9,
                attempts: 1
              }
            ]
          });
        }
        return respond({ status: "done", seconds: 93, mode: "img2img", stage: "done" });
      }
      return respond({
        status: "running",
        seconds: 3,
        mode: "img2img",
        stage: "requesting"
      });
    }
    return originalFetch(input, init);
  };
})()
"""


REMODEL_CHECK_PRELOAD_TEMPLATE = r"""
(() => {
  try {
    Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => "visible"
    });
  } catch (error) {
    // Visibility overrides are best-effort only.
  }
  const itemId = "abcdef0123456789";
  const resultUrl = "/samples/bedroom-20261105-016.png";
  const item = {
    item_id: itemId,
    name: "检查轮次项目",
    default_mode: "img2img",
    default_style: "中古现代",
    gen_count: 1,
    cover_gen: "a".repeat(32),
    updated_at: "2026-11-17T13:00:00Z",
    created_at: "2026-11-17T12:00:00Z",
    source_url: "/samples/bedroom-20261105-001.png",
    source: {
      current: {
        url: "/samples/bedroom-20261105-001.png",
        width: 1080,
        height: 1440
      }
    },
    gens: [{
      gid: "a".repeat(32),
      mode: "img2img",
      style: "中古现代",
      url: resultUrl,
      thumb_url: resultUrl,
      width: 1080,
      height: 1440,
      created_at: "2026-11-17T13:00:00Z",
      score: __SCORE__,
      attempts: __ATTEMPTS__,
      checks: __CHECKS__,
      prompt: "",
      negative: ""
    }],
    failed_jobs: [],
    analysis_state: "none",
    structure: null,
    analysis: null
  };
  const originalFetch = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const raw = typeof input === "string" ? input : (input && input.url) || "";
    const url = new URL(raw, location.href);
    if (url.pathname === "/api/remodel/items/" + itemId) {
      return new Response(JSON.stringify(item), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      });
    }
    return originalFetch(input, init);
  };
})()
"""


REMODEL_CHECK_READ_JS = r"""
(async () => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const img = document.getElementById("rm-current-img");
  for (let attempt = 0; attempt < 160; attempt += 1) {
    const src = img ? String(img.getAttribute("src") || "") : "";
    if (img && !img.hidden && src.includes("bedroom-20261105-016.png")) break;
    await sleep(50);
  }
  const card = document.getElementById("rm-current-card");
  const meta = document.getElementById("rm-current-meta");
  const chip = meta ? meta.querySelector(".rm-check-chip") : null;
  const detail = card ? card.querySelector(".rm-check-detail") : null;
  return {
    loaded: !!img && !img.hidden,
    hasChip: !!chip,
    chipText: chip ? chip.textContent : "",
    detailText: detail ? detail.textContent : "",
    metaText: meta ? meta.textContent : ""
  };
})()
"""


CURRENT_PROMPT_PRELOAD_TEMPLATE = r"""
(() => {
  try {
    Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => "visible"
    });
  } catch (error) {
    // Visibility overrides are best-effort only.
  }
  const itemId = "abcdef0123456789";
  const resultUrl = "/samples/bedroom-20261105-016.png";
  const item = {
    item_id: itemId,
    name: "提示词展示项目",
    default_mode: "img2img",
    default_style: "中古现代",
    gen_count: 1,
    cover_gen: "a".repeat(32),
    updated_at: "2026-11-17T13:00:00Z",
    created_at: "2026-11-17T12:00:00Z",
    source_url: "/samples/bedroom-20261105-001.png",
    source: {
      current: {
        url: "/samples/bedroom-20261105-001.png",
        width: 1080,
        height: 1440
      }
    },
    gens: [{
      gid: "a".repeat(32),
      mode: "img2img",
      style: "中古现代",
      model: "qwen-image-3.0-pro",
      size_requested: "1440*1080",
      url: resultUrl,
      thumb_url: resultUrl,
      width: 1080,
      height: 1440,
      created_at: "2026-11-17T13:00:00Z",
      score: 0.9,
      attempts: 1,
      checks: [],
      prompt: __PROMPT__,
      negative: "",
      extra_zh: __EXTRA_ZH__,
      extra_en: __EXTRA_EN__,
      extra_verbatim: false,
      detail_zh: "",
      refine_of: ""
    }],
    failed_jobs: [],
    analysis_state: "none",
    structure: null,
    analysis: null
  };
  const originalFetch = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const raw = typeof input === "string" ? input : (input && input.url) || "";
    const url = new URL(raw, location.href);
    if (url.pathname === "/api/remodel/items/" + itemId) {
      return new Response(JSON.stringify(item), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      });
    }
    return originalFetch(input, init);
  };
})()
"""


CURRENT_PROMPT_READ_JS = r"""
(async () => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const details = document.getElementById("rm-current-prompt");
  const brief = document.getElementById("rm-prompt-brief");
  for (let attempt = 0; attempt < 160; attempt += 1) {
    if (details && !details.hidden && brief && brief.textContent.trim()) break;
    await sleep(50);
  }
  await sleep(120);
  const summary = details ? details.querySelector(".rm-prompt-summary") : null;
  const toggle = summary ? summary.querySelector(".rm-prompt-toggle") : null;
  const toggleTextNode = document.getElementById("rm-prompt-toggle-text");
  const extra = document.getElementById("rm-prompt-extra");
  const extraLabel = extra ? extra.querySelector(".rm-prompt-extra-label") : null;
  const body = document.getElementById("rm-prompt-text");
  const copy = document.getElementById("rm-prompt-copy");
  const style = (node, prop) => (node ? getComputedStyle(node)[prop] : "");
  const rect = (node) => {
    if (!node) return null;
    const box = node.getBoundingClientRect();
    return { width: box.width, height: box.height, left: box.left, right: box.right };
  };
  const collapsed = {
    visible: !!details && !details.hidden,
    open: details ? details.open : null,
    summaryText: summary ? summary.textContent.replace(/\s+/g, " ").trim() : "",
    summaryVisibleText: summary ? summary.innerText.replace(/\s+/g, " ").trim() : "",
    briefText: brief ? brief.textContent : "",
    extraText: extra ? extra.textContent : "",
    extraHidden: extra ? extra.hidden : null,
    extraDisplay: extra ? getComputedStyle(extra).display : "",
    extraLabelText: extraLabel ? extraLabel.textContent.trim() : "",
    extraLabelFontSize: extraLabel ? parseFloat(getComputedStyle(extraLabel).fontSize) : 0,
    toggleText: toggleTextNode ? toggleTextNode.textContent.trim() : "",
    toggleBackground: style(toggle, "backgroundColor"),
    toggleColor: style(toggle, "color"),
    detailsBackground: style(details, "backgroundColor"),
    detailsBorderLeftWidth: style(details, "borderLeftWidth"),
    detailsBorderLeftColor: style(details, "borderLeftColor"),
    summaryRect: rect(summary),
    detailsRect: rect(details),
    extraRect: rect(extra),
    viewportWidth: window.innerWidth,
    documentScrollWidth: document.documentElement.scrollWidth,
    summaryScrollWidth: summary ? summary.scrollWidth : 0,
    summaryClientWidth: summary ? summary.clientWidth : 0,
    detailsRight: details ? details.getBoundingClientRect().right : 0,
    bodyTextLength: body ? body.textContent.length : 0,
    hasCopy: !!copy
  };
  if (details) {
    details.open = true;
    await sleep(80);
  }
  const expanded = {
    open: details ? details.open : null,
    toggleText: toggleTextNode ? toggleTextNode.textContent.trim() : "",
    bodyScrollHeight: body ? body.scrollHeight : 0,
    bodyClientHeight: body ? body.clientHeight : 0,
    bodyScrollable: body ? body.scrollHeight > body.clientHeight + 1 : false,
    copyVisible: !!copy && copy.getClientRects().length > 0
  };
  if (details) {
    details.open = false;
    await sleep(80);
  }
  return { ...collapsed, expanded };
})()
"""


def _rendered_color(value) -> bool:
    text = str(value or "").strip().lower()
    return bool(text) and text not in {
        "transparent",
        "rgba(0, 0, 0, 0)",
        "hsla(0, 0, 0, 0)",
    }


def _read_current_prompt(extra_zh: str, extra_en: str) -> dict:
    preload = (
        CURRENT_PROMPT_PRELOAD_TEMPLATE.replace(
            "__PROMPT__", json.dumps("p" * 5507)
        )
        .replace("__EXTRA_ZH__", json.dumps(extra_zh, ensure_ascii=False))
        .replace("__EXTRA_EN__", json.dumps(extra_en, ensure_ascii=False))
    )
    page = ChromePage(
        f"{BASE}/#remodel/abcdef0123456789",
        preload=preload,
    )
    try:
        page._cdp.call(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": 390,
                "height": 844,
                "deviceScaleFactor": 1,
                "mobile": False,
            },
        )
        result = page.evaluate(CURRENT_PROMPT_READ_JS)
    finally:
        page.close()
    return result if isinstance(result, dict) else {}


def current_prompt_visibility_contract() -> list[tuple[str, bool, str]]:
    """The result-card prompt must be a prominent, complete two-line control."""
    extra = _read_current_prompt("餐厅", "dining room")
    no_extra = _read_current_prompt("", "")
    outcomes: list[tuple[str, bool, str]] = []

    summary_text = str(extra.get("summaryText") or "")
    expected_extra = "补充要求 · 餐厅 → dining room"
    expected_brief = "qwen-image-3.0-pro · 1440*1080 · 中古现代"
    outcomes.append(
        (
            "当前结果提示词：折叠态可见「查看提示词」且补充要求完整不省略",
            extra.get("visible") is True
            and extra.get("open") is False
            and extra.get("toggleText") == "查看提示词"
            and "生图提示词" in summary_text
            and "查看提示词" in summary_text
            and "…" not in summary_text
            and expected_extra in summary_text
            and expected_brief in summary_text,
            json.dumps(
                {
                    key: extra.get(key)
                    for key in (
                        "visible",
                        "open",
                        "toggleText",
                        "summaryText",
                        "briefText",
                        "extraText",
                    )
                },
                ensure_ascii=False,
            ),
        )
    )

    outcomes.append(
        (
            "当前结果提示词：折叠条与查看按钮为实底并带 accent 左条",
            _rendered_color(extra.get("detailsBackground"))
            and _rendered_color(extra.get("toggleBackground"))
            and extra.get("toggleBackground") != extra.get("detailsBackground")
            and extra.get("detailsBorderLeftWidth") == "3px"
            and "210, 179, 134" in str(extra.get("detailsBorderLeftColor") or ""),
            f"details={extra.get('detailsBackground')}, toggle={extra.get('toggleBackground')}, "
            f"toggleColor={extra.get('toggleColor')}, left={extra.get('detailsBorderLeftWidth')} "
            f"{extra.get('detailsBorderLeftColor')}",
        )
    )

    outcomes.append(
        (
            "当前结果提示词：补充要求标签独立且字号 ≥10px",
            extra.get("extraLabelText") == "补充要求"
            and float(extra.get("extraLabelFontSize") or 0) >= 10,
            f"label={extra.get('extraLabelText')!r}, fontSize={extra.get('extraLabelFontSize')}",
        )
    )

    summary_height = float((extra.get("summaryRect") or {}).get("height") or 0)
    details_height = float((extra.get("detailsRect") or {}).get("height") or 0)
    outcomes.append(
        (
            "当前结果提示词：折叠态高度 44–88px",
            44 <= summary_height <= 88 and 44 <= details_height <= 88,
            f"summary={summary_height}, details={details_height}",
        )
    )

    expanded = extra.get("expanded") or {}
    outcomes.append(
        (
            "当前结果提示词：展开后按钮改为「收起提示词」且正文可滚动可复制",
            expanded.get("open") is True
            and expanded.get("toggleText") == "收起提示词"
            and expanded.get("bodyScrollable") is True
            and expanded.get("copyVisible") is True,
            json.dumps(expanded, ensure_ascii=False),
        )
    )

    no_extra_text = str(
        no_extra.get("summaryVisibleText") or no_extra.get("summaryText") or ""
    )
    outcomes.append(
        (
            "当前结果提示词：无补充要求版本不渲染补充行",
            no_extra.get("visible") is True
            and no_extra.get("toggleText") == "查看提示词"
            and no_extra.get("extraHidden") is True
            and no_extra.get("extraDisplay") == "none"
            and float((no_extra.get("extraRect") or {}).get("height") or 0) == 0
            and no_extra.get("briefText") == expected_brief
            and "补充要求" not in no_extra_text
            and "…" not in no_extra_text,
            json.dumps(
                {
                    key: no_extra.get(key)
                    for key in (
                        "visible",
                        "toggleText",
                        "summaryText",
                        "summaryVisibleText",
                        "briefText",
                        "extraHidden",
                        "extraDisplay",
                        "extraRect",
                    )
                },
                ensure_ascii=False,
            ),
        )
    )

    viewport = float(extra.get("viewportWidth") or 0)
    outcomes.append(
        (
            "当前结果提示词：390px 宽无横向溢出",
            0 < viewport <= 390
            and float(extra.get("documentScrollWidth") or 0) <= viewport + 1
            and float(extra.get("detailsRight") or 0) <= viewport + 1
            and float(extra.get("summaryScrollWidth") or 0)
            <= float(extra.get("summaryClientWidth") or 0) + 1,
            f"viewport={viewport}, doc={extra.get('documentScrollWidth')}, "
            f"detailsRight={extra.get('detailsRight')}, "
            f"summary={extra.get('summaryScrollWidth')}/{extra.get('summaryClientWidth')}",
        )
    )
    return outcomes


def _check_warning_preload(checks: list[dict], score: float, attempts: int) -> str:
    return (
        REMODEL_CHECK_PRELOAD_TEMPLATE.replace(
            "__CHECKS__", json.dumps(checks, ensure_ascii=False)
        )
        .replace("__SCORE__", json.dumps(score))
        .replace("__ATTEMPTS__", json.dumps(attempts))
    )


def _check_warning_read(checks: list[dict], score: float, attempts: int) -> dict:
    page = ChromePage(
        f"{BASE}/#remodel/abcdef0123456789",
        preload=_check_warning_preload(checks, score, attempts),
    )
    try:
        result = page.evaluate(REMODEL_CHECK_READ_JS)
    finally:
        page.close()
    return result if isinstance(result, dict) else {}


def check_warning_contract() -> list[tuple[str, bool, str]]:
    """结构存疑 chip must read the final check round and speak Chinese."""
    outcomes: list[tuple[str, bool, str]] = []

    # Round 1 failed, round 2 (the saved result) is all OK: no chip at all.
    all_ok_checks = [
        {
            "attempt": 1,
            "score": 0.789,
            "items": [
                {"key": "floor", "ok": False, "note": "第一轮地面偏移"},
                {"key": "lighting", "ok": False, "note": "第一轮灯光过曝"},
            ],
            "repair_en": "repair",
        },
        {
            "attempt": 2,
            "score": 1.0,
            "items": [
                {"key": "floor", "ok": True, "note": ""},
                {"key": "lighting", "ok": True, "note": ""},
            ],
            "repair_en": "",
        },
    ]
    result = _check_warning_read(all_ok_checks, 1.0, 2)
    outcomes.append(
        (
            "结构存疑：两轮 checks 且最终轮全 OK 时不显示 chip",
            result.get("loaded") is True and result.get("hasChip") is False,
            json.dumps(result, ensure_ascii=False),
        )
    )

    # Round 1 failed floor/openings, round 2 only fails lighting: the chip must
    # come from round 2, in Chinese, without round 1's keys.
    final_fail_checks = [
        {
            "attempt": 1,
            "score": 0.7,
            "items": [
                {"key": "floor", "ok": False, "note": "第一轮地面偏移"},
                {"key": "openings", "ok": False, "note": "第一轮少一扇窗"},
            ],
            "repair_en": "repair",
        },
        {
            "attempt": 2,
            "score": 0.9,
            "items": [
                {"key": "floor", "ok": True, "note": ""},
                {"key": "lighting", "ok": False, "note": "灯光过暗"},
            ],
            "repair_en": "",
        },
    ]
    result = _check_warning_read(final_fail_checks, 0.9, 2)
    chip_text = str(result.get("chipText") or "")
    detail_text = str(result.get("detailText") or "")
    outcomes.append(
        (
            "结构存疑：chip 取最终轮中文名且不含第一轮独有项",
            result.get("loaded") is True
            and result.get("hasChip") is True
            and "灯光" in chip_text
            and "地面" not in chip_text
            and "门窗洞口" not in chip_text
            and "openings" not in detail_text
            and "floor" not in detail_text,
            json.dumps(result, ensure_ascii=False),
        )
    )
    return outcomes


def check_label_coverage_contract() -> tuple[bool, str]:
    """Every backend check key must have a Chinese label in the front end."""
    source = (ROOT / "gui" / "remodel.js").read_text(encoding="utf-8")
    match = re.search(
        r"const\s+CHECK_FAIL_LABELS\s*=\s*\{(.*?)\n\s*\};", source, re.S
    )
    if not match:
        return False, "未找到 CHECK_FAIL_LABELS"
    labels = set(
        re.findall(r"(?:^|[,{\s])([A-Za-z_][A-Za-z0-9_]*)\s*:", match.group(1))
    )
    expected = set(gui_server.REMODEL_CHECK_KEYS)
    missing = sorted(expected - labels)
    return not missing, f"missing={missing}, expected={sorted(expected)}"


def remodel_completion_contract() -> list[tuple[str, bool, str]]:
    """TASK20 requirement 4: toast + is-new without yanking a far-away scroll."""
    outcomes: list[tuple[str, bool, str]] = []
    page = ChromePage(
        f"{BASE}/#remodel/abcdef0123456789", preload=REMODEL_DETAIL_PRELOAD_JS
    )
    try:
        page._cdp.call(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": 1280,
                "height": 900,
                "deviceScaleFactor": 1,
                "mobile": False,
            },
        )
        result = page.evaluate(
            r"""
            (async () => {
              const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
              const view = document.getElementById("remodel-view");
              const button = document.getElementById("rm-generate-item");
              for (let attempt = 0; attempt < 160; attempt += 1) {
                if (button && !button.disabled && document.getElementById("rm-detail-name")
                    && document.getElementById("rm-detail-name").textContent === "模拟改造项目") {
                  break;
                }
                await sleep(50);
              }
              if (!button) return { missing: true };
              const spacer = document.createElement("div");
              spacer.style.height = "1600px";
              view.append(spacer);
              view.scrollTop = 600;
              await sleep(120);
              const scrollBefore = view.scrollTop;
              const stack = document.getElementById("toasts");
              stack.replaceChildren();
              button.click();
              let card = null;
              for (let attempt = 0; attempt < 180; attempt += 1) {
                card = document.querySelector(".rm-current-card.is-new");
                if (card) break;
                await sleep(50);
              }
              const successToasts = [...stack.querySelectorAll(".toast.is-success")]
                .map((node) => node.textContent);
              const out = {
                foundNew: !!card,
                successToasts,
                scrollBefore,
                scrollAfter: view.scrollTop,
                latestHidden: document.getElementById("rm-current-card").hidden
              };
              await sleep(2200);
              out.stillNew = !!document.querySelector(".rm-current-card.is-new");
              return out;
            })()
            """
        )
        result = result if isinstance(result, dict) else {}
        success_text = " ".join(result.get("successToasts") or [])
        outcomes.append(
            (
                "改造完成：成功 toast、最新结果 is-new 2 秒且不打断远距滚动",
                result.get("foundNew") is True
                and "生成完成" in success_text
                and "1 分 33 秒" in success_text
                and int(result.get("scrollBefore") or 0) > 160
                and abs(
                    int(result.get("scrollAfter") or 0)
                    - int(result.get("scrollBefore") or 0)
                ) <= 2
                and result.get("stillNew") is False,
                json.dumps(result, ensure_ascii=False),
            )
        )
    finally:
        page.close()
    return outcomes


def credentials_contract(
    credentials_path: Path, service_log_path: Path
) -> list[tuple[str, bool, str]]:
    """TASK21: API shapes, 0600 file and the real settings modal flow."""
    outcomes: list[tuple[str, bool, str]] = []
    test_key = "sk-smoke-credential-0123456789abcdef"
    browser_key = "sk-browser-credential-0123456789abcdef"
    test_base = "https://local.example.test/compatible-mode/v1"

    # GET before any local file: no source is configured.
    status, initial = fetch_json("/api/credentials")
    initial = initial if isinstance(initial, dict) else {}
    expected_keys = {
        "source",
        "key_masked",
        "base_url",
        "has_local",
        "env_present",
    }
    outcomes.append(
        (
            "GET /api/credentials 返回完整安全状态（未配置）",
            status == 200
            and set(initial) == expected_keys
            and initial.get("source") == "none"
            and initial.get("has_local") is False
            and initial.get("env_present") is False
            and initial.get("key_masked") == ""
            and test_key not in json.dumps(initial, ensure_ascii=False),
            f"HTTP {status}, state={json.dumps(initial, ensure_ascii=False)}",
        )
    )

    # Empty POST is rejected and must not create the local file.
    status_empty, body_empty = fetch_json(
        "/api/credentials", method="POST", payload={"api_key": "", "base_url": test_base}
    )
    outcomes.append(
        (
            "POST /api/credentials 拒绝空 Key（400，不落盘）",
            status_empty == 400
            and not credentials_path.exists()
            and isinstance(body_empty, dict)
            and isinstance(body_empty.get("error"), str),
            f"HTTP {status_empty}, body={json.dumps(body_empty, ensure_ascii=False)}",
        )
    )

    # Valid POST creates the local file and must never echo the raw key.
    status_save, saved = fetch_json(
        "/api/credentials",
        method="POST",
        payload={"api_key": test_key, "base_url": test_base},
    )
    saved = saved if isinstance(saved, dict) else {}
    saved_text = json.dumps(saved, ensure_ascii=False)
    file_mode = stat.S_IMODE(credentials_path.stat().st_mode) if credentials_path.exists() else -1
    outcomes.append(
        (
            "POST /api/credentials 保存本地文件（0600）且不回显 Key",
            status_save == 200
            and set(saved) == expected_keys
            and saved.get("source") == "local"
            and saved.get("has_local") is True
            and saved.get("base_url") == test_base
            and str(saved.get("key_masked") or "").endswith(test_key[-4:])
            and test_key not in saved_text
            and file_mode == 0o600,
            f"HTTP {status_save}, mode={oct(file_mode)}, state={saved_text}",
        )
    )

    # The local file is picked up for the next call without a restart.
    status_get, after_save = fetch_json("/api/credentials")
    outcomes.append(
        (
            "GET /api/credentials 立即读取新的本地凭据",
            status_get == 200
            and isinstance(after_save, dict)
            and after_save.get("source") == "local"
            and after_save.get("has_local") is True
            and after_save.get("key_masked") == saved.get("key_masked"),
            f"HTTP {status_get}, state={json.dumps(after_save, ensure_ascii=False)}",
        )
    )

    # DELETE removes the file and returns to the unconfigured state immediately.
    status_delete, after_delete = fetch_json("/api/credentials", method="DELETE")
    after_delete = after_delete if isinstance(after_delete, dict) else {}
    outcomes.append(
        (
            "DELETE /api/credentials 删除本地文件并回到未配置",
            status_delete == 200
            and set(after_delete) == expected_keys
            and after_delete.get("source") == "none"
            and after_delete.get("has_local") is False
            and after_delete.get("env_present") is False
            and not credentials_path.exists(),
            f"HTTP {status_delete}, state={json.dumps(after_delete, ensure_ascii=False)}",
        )
    )

    # Real browser: banner → modal → empty-key guard → save → clear.
    page = ChromePage(f"{BASE}/")
    try:
        page._cdp.call(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": 1280,
                "height": 900,
                "deviceScaleFactor": 1,
                "mobile": False,
            },
        )
        result = page.evaluate(
            r"""
            (async () => {
              const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
              const $ = (id) => document.getElementById(id);
              const waitFor = async (test, attempts = 220) => {
                for (let i = 0; i < attempts; i += 1) {
                  try {
                    if (test()) return true;
                  } catch (error) {
                    void error;
                  }
                  await sleep(50);
                }
                return false;
              };
              await waitFor(() => document.readyState === "complete" && $("settings-open"));
              const banner = $("credentials-banner");
              await waitFor(() => banner && !banner.hidden);
              const dialog = $("credentials-dialog");
              const initial = {
                bannerVisible: !!banner && !banner.hidden,
                bannerText: banner ? banner.textContent.trim() : "",
                settingsButton: !!$("settings-open"),
              };
              $("settings-open").click();
              await waitFor(() => dialog && dialog.open);
              const badge = $("settings-source-badge");
              const masked = $("settings-key-masked");
              const opened = {
                open: !!dialog && dialog.open,
                badge: badge ? badge.textContent.trim() : "",
                masked: masked ? masked.textContent.trim() : "",
                base: $("settings-base-url") ? $("settings-base-url").value : "",
              };

              $("settings-api-key").value = "";
              $("settings-save").click();
              await waitFor(() => $("settings-error") && !$("settings-error").hidden);
              const emptyError = $("settings-error") ? $("settings-error").textContent.trim() : "";
              const afterEmpty = await fetch("/api/credentials", {cache: "no-store"})
                .then((response) => response.json());

              $("settings-api-key").value = "sk-browser-credential-0123456789abcdef";
              $("settings-base-url").value = "https://browser.example.test/compatible-mode/v1";
              $("settings-save").click();
              const saved = await waitFor(
                () => badge && badge.textContent.trim() === "本地 Key"
              );
              const saveToast = [...document.querySelectorAll(".toast")]
                .map((node) => node.textContent).join(" | ");
              const afterSave = {
                saved,
                badge: badge ? badge.textContent.trim() : "",
                masked: masked ? masked.textContent.trim() : "",
                bannerHidden: !!banner && banner.hidden,
                toast: saveToast,
              };

              $("settings-clear").click();
              const cleared = await waitFor(
                () => badge && badge.textContent.trim() === "未配置"
              );
              await waitFor(() => {
                const text = [...document.querySelectorAll(".toast")]
                  .map((node) => node.textContent).join(" | ");
                return text.includes("本地凭据已清除");
              });
              const afterClear = {
                cleared,
                badge: badge ? badge.textContent.trim() : "",
                bannerVisible: !!banner && !banner.hidden,
                toast: [...document.querySelectorAll(".toast")]
                  .map((node) => node.textContent).join(" | "),
              };

              $("settings-close").click();
              await waitFor(() => dialog && !dialog.open);
              window.location.hash = "#remodel";
              await waitFor(() => document.body.classList.contains("view-remodel"));
              $("settings-open").click();
              const remodelOpen = await waitFor(() => dialog && dialog.open);
              const result = {
                initial,
                opened,
                emptyError,
                afterEmptySource: afterEmpty.source,
                afterSave,
                afterClear,
                remodelOpen,
              };
              if (dialog && dialog.open) dialog.close();
              return result;
            })()
            """
        )
        result = result if isinstance(result, dict) else {}
        initial_ui = result.get("initial") or {}
        opened = result.get("opened") or {}
        after_save_ui = result.get("afterSave") or {}
        after_clear_ui = result.get("afterClear") or {}
        outcomes.append(
            (
                "设置弹窗在 #gallery 打开并显示未配置状态",
                initial_ui.get("settingsButton") is True
                and initial_ui.get("bannerVisible") is True
                and "还没配置模型 Key" in str(initial_ui.get("bannerText") or "")
                and opened.get("open") is True
                and opened.get("badge") == "未配置"
                and opened.get("masked") == "未配置"
                and test_key not in json.dumps(opened, ensure_ascii=False),
                json.dumps({"initial": initial_ui, "opened": opened}, ensure_ascii=False),
            )
        )
        outcomes.append(
            (
                "空 Key 保存被前端拦截且不改变来源",
                "API Key" in str(result.get("emptyError") or "")
                and result.get("afterEmptySource") == "none",
                json.dumps(
                    {
                        "error": result.get("emptyError"),
                        "source": result.get("afterEmptySource"),
                    },
                    ensure_ascii=False,
                ),
            )
        )
        outcomes.append(
            (
                "弹窗保存后 toast + 徽章切换为本地 Key",
                after_save_ui.get("saved") is True
                and after_save_ui.get("badge") == "本地 Key"
                and after_save_ui.get("bannerHidden") is True
                and "凭据已保存（本地文件）" in str(after_save_ui.get("toast") or "")
                and "sk-browser-credential-0123456789abcdef"
                not in json.dumps(after_save_ui, ensure_ascii=False),
                json.dumps(after_save_ui, ensure_ascii=False),
            )
        )
        outcomes.append(
            (
                "弹窗清除后 toast + 徽章切回未配置",
                after_clear_ui.get("cleared") is True
                and after_clear_ui.get("badge") == "未配置"
                and "本地凭据已清除" in str(after_clear_ui.get("toast") or "")
                and after_clear_ui.get("bannerVisible") is True,
                json.dumps(after_clear_ui, ensure_ascii=False),
            )
        )
        outcomes.append(
            (
                "设置入口在 #remodel 路由同样可用",
                result.get("remodelOpen") is True,
                json.dumps({"remodelOpen": result.get("remodelOpen")}, ensure_ascii=False),
            )
        )
    finally:
        page.close()

    try:
        log_text = service_log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        log_text = ""
    outcomes.append(
        (
            "TASK21 服务日志不包含原始 Key",
            test_key not in log_text and browser_key not in log_text,
            f"log_bytes={len(log_text.encode('utf-8'))}",
        )
    )
    return outcomes


def lightbox_contract_ok(out: dict) -> tuple[bool, str]:
    detail = out.get("detail") or {}
    opened = out.get("opened") or {}
    actual = out.get("actual") or {}
    pan = out.get("pan") or {}
    nav = out.get("nav") or {}
    source = out.get("source") or {}
    keyboard = out.get("keyboard") or {}
    backdrop = out.get("backdrop") or {}
    trap = out.get("trap") or {}
    buttons = out.get("buttons") or {}
    thumb = out.get("thumb") or {}
    caption = str(opened.get("caption") or "")
    checks = {
        "analysis_state": "图生图不需要结构分析" in str(detail.get("analysisState") or ""),
        "analysis_text_state": str(detail.get("textState") or "").strip()
        == "纯文本模式需要结构分析，生成时会先分析",
        "analysis_back_state": str(detail.get("backState") or "").strip()
        == "图生图不需要结构分析",
        "analyze_button_label": str(detail.get("reanalyzeLabel") or "").strip()
        == "分析结构",
        "source_button": detail.get("sourceRole") == "button"
        and detail.get("sourceTabIndex") == "0"
        and bool(detail.get("sourceLabel")),
        "latest_button": detail.get("latestRole") == "button"
        and detail.get("latestTabIndex") == "0"
        and bool(detail.get("latestLabel")),
        "thumb_button": detail.get("thumbTag") == "button"
        and bool(detail.get("thumbLabel")),
        "lightbox_open": opened.get("opened") is True,
        "dialog_semantics": opened.get("role") == "dialog"
        and opened.get("ariaModal") == "true",
        "caption_latest": "最新版本" in caption
        and "图生图" in caption
        and "×" in caption,
        "focus_inside": opened.get("focusInside") is True,
        "download_link": str(opened.get("downloadHref") or "").endswith(".png"),
        "fit_mode": opened.get("mode") == "fit",
        "fit_scaled_down": 0 < int(opened.get("renderedWidth") or 0)
        <= int(opened.get("naturalWidth") or 0)
        and 0 < int(opened.get("renderedHeight") or 0)
        <= int(opened.get("naturalHeight") or 0),
        "actual_mode": actual.get("mode") == "actual",
        "actual_1to1": int(actual.get("renderedWidth") or 0)
        == int(actual.get("naturalWidth") or 0)
        and int(actual.get("renderedHeight") or 0)
        == int(actual.get("naturalHeight") or 0),
        "cursor_grab": "grab" in str(actual.get("cursor") or ""),
        "cursor_grabbing": "grabbing" in str(pan.get("cursorDown") or ""),
        "pan_moves": pan.get("scrollAfter") != pan.get("scrollBefore"),
        "arrow_advances": "历史版本" in str(nav.get("caption2") or "")
        and str(nav.get("index2")) == "1",
        "nav_resets_fit": nav.get("modeAfterNav") == "fit",
        "esc_closes": nav.get("closed") is True,
        "focus_restored": nav.get("focusRestored") is True,
        "source_opens": source.get("opened") is True
        and "原图" in str(source.get("caption") or "")
        and "×" in str(source.get("caption") or ""),
        "source_closes": source.get("closed") is True,
        "keyboard_opens": keyboard.get("opened") is True
        and keyboard.get("closed") is True,
        "backdrop_closes": backdrop.get("opened") is True
        and backdrop.get("closed") is True
        and backdrop.get("focusRestored") is True,
        "focus_trap_wraps": trap.get("forwardWrap") is True
        and trap.get("backwardWrap") is True
        and trap.get("closed") is True,
        "prev_next_buttons": str(buttons.get("nextIndex")) == "1"
        and str(buttons.get("prevIndex")) == "0"
        and buttons.get("closed") is True,
        "thumb_opens_lightbox": thumb.get("opened") is True
        and thumb.get("compareOpen") is False
        and "历史版本" in str(thumb.get("caption") or ""),
    }
    failed = [name for name, ok in checks.items() if not ok]
    detail_text = (
        f"natural={opened.get('naturalWidth')}x{opened.get('naturalHeight')}, "
        f"fit={opened.get('renderedWidth')}x{opened.get('renderedHeight')}, "
        f"actual={actual.get('renderedWidth')}x{actual.get('renderedHeight')}, "
        f"shots=logs/task19"
    )
    if failed:
        detail_text = "失败项：" + ", ".join(failed) + "；" + detail_text
    return not failed, detail_text


def rendered_history_contract(
    dom: str, expected_versions: int, expected_failures: int
) -> tuple[bool, str]:
    """Assert the TASK16 history-card contract against rendered Chrome DOM."""
    meta_match = re.search(
        r"<p[^>]*\bid=\"rm-detail-meta\"[^>]*>(.*?)</p>", dom, re.S
    )
    header = html_text(meta_match.group(1)) if meta_match else ""
    expected_header = f"{expected_versions} 个版本"
    if expected_failures:
        expected_header += f" · {expected_failures} 次失败"
    header_ok = header.startswith(expected_header)

    cards = [
        (match.group(1), match.group(0), match.group(2))
        for match in re.finditer(
            r"<article class=\"([^\"]*\brm-gen-card\b[^\"]*)\"[^>]*>(.*?)</article>",
            dom,
            re.S,
        )
    ]
    success_cards = [
        (attrs, block, body)
        for attrs, block, body in cards
        if "is-error" not in attrs
    ]
    failed_cards = [
        (attrs, block, body)
        for attrs, block, body in cards
        if "is-error" in attrs
    ]

    size_misses = []
    menu_misses = []
    labels = ["下载", "对比原图", "设为封面", "备注", "删除本版"]
    for _attrs, _block, body in success_cards:
        text = html_text(body)
        sizes = re.findall(r"输出\s*\d+\s*×\s*\d+", text)
        if len(sizes) != 1:
            size_misses.append(f"{len(sizes)}×size")
        triggers = len(re.findall(r"class=\"rm-icon-button\"", body))
        menu_items = len(re.findall(r"role=\"menuitem\"", body))
        if triggers != 1 or menu_items != 5 or any(
            label not in text for label in labels
        ):
            menu_misses.append(f"{triggers} trigger/{menu_items} items")
        if "rm-gen-action" in body:
            menu_misses.append("inline action leaked")

    failure_ok = len(failed_cards) == expected_failures
    failure_timeout_outside = False
    if failed_cards:
        failure_text = html_text(failed_cards[0][2])
        failure_ok = (
            failure_ok
            and "生成失败" in failure_text
            and bool(re.search(r"排队超时\s*·\s*\d+\s*秒", failure_text))
            and "改用纯文本重试" in failure_text
            and "<img" not in failed_cards[0][2]
            and "生成接口排队超时" not in failure_text
            and "生成接口排队超时" in html_lib.unescape(failed_cards[0][1])
        )
        without_failed_card = dom.replace(failed_cards[0][1], "", 1)
        failure_timeout_outside = "生成接口排队超时" in without_failed_card

    ok = (
        bool(meta_match)
        and header_ok
        and len(cards) == expected_versions + expected_failures
        and len(success_cards) == expected_versions
        and not size_misses
        and not menu_misses
        and failure_ok
        and not failure_timeout_outside
    )
    detail = (
        f"header={header!r}, cards={len(cards)}, success={len(success_cards)}, "
        f"failed={len(failed_cards)}, size_misses={size_misses}, "
        f"menu_misses={menu_misses}, failure_ok={failure_ok}, "
        f"timeout_outside={failure_timeout_outside}"
    )
    return ok, detail


def source_history_contract() -> tuple[bool, str]:
    """Source-level fallback when no Chrome binary is available."""
    remodel_js = (ROOT / "gui" / "remodel.js").read_text(encoding="utf-8")

    def between(start: str, end: str) -> str:
        begin = remodel_js.find(start)
        finish = remodel_js.find(end, begin + len(start))
        return remodel_js[begin:finish] if begin >= 0 and finish >= 0 else ""

    gen_card = between("function renderGenCard", "function renderGenMenu")
    menu = between("function renderGenMenu", "function renderFailedJobCard")
    failed = between("function renderFailedJobCard", "function compactFailureReason")
    detail = between("function renderDetail", "function renderSource")
    labels = ["下载", "对比原图", "设为封面", "备注", "删除本版"]
    ok = (
        gen_card.count("rm-gen-size") == 1
        and gen_card.count("outputSizeLabel(gen)") == 1
        and "aspect_label" not in gen_card
        and menu.count('"rm-icon-button"') == 1
        and all(f'"{label}"' in menu for label in labels)
        and "job.thumb_url" not in failed
        and 'el("p", "rm-gen-error", compactFailureReason(fullError))' in failed
        and "card.title = fullError" in failed
        and "aria-label" in failed
        and "failed_jobs" in detail
        and "次失败" in detail
    )
    return ok, "Chrome 不可用，已按源码契约检查"


def main() -> int:
    if Image is None:
        print("FAIL  需要 Pillow 来构造测试图片；请使用 python3 / .venv/bin/python 运行")
        return 1

    ok, detail = demo_metadata_contract()
    record("示例数据：demo/metadata.json 恰好 10 条且每张图都在 demo/ 下", ok, detail)
    ok, detail = demo_seed_noop_contract()
    record("示例数据：seed_demo_data() 首次导入后重复调用零副作用", ok, detail)
    expected_gallery = runtime_gallery_count()
    expected_remodel = runtime_remodel_count()
    print(
        f"运行时数据：图库 {expected_gallery} 条 / 改造项目 {expected_remodel} 个",
        flush=True,
    )

    print(f"启动测试服务：{BASE}")
    remodel_dir = tempfile.mkdtemp(prefix="homai-remodel-smoke-")
    credentials_path = ROOT / "credentials.json"
    credentials_backup = None
    credentials_mode = None
    if credentials_path.exists():
        credentials_backup = credentials_path.read_bytes()
        credentials_mode = stat.S_IMODE(credentials_path.stat().st_mode)
        credentials_path.unlink()
    env = os.environ.copy()
    env["HOMAI_SMOKE"] = "1"
    env["HOMAI_REMODEL_DIR"] = remodel_dir
    env["HOMAI_SERVICE_LOG"] = os.path.join(remodel_dir, "gui-service.log")
    env.pop("HOMAI_DASHSCOPE_KEY", None)
    env.pop("HOMAI_DASHSCOPE_BASE_URL", None)
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "gui_server.py"), "--host", HOST, "--port", str(PORT)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        if not wait_ready(process):
            error_output = ""
            if process.poll() is not None:
                try:
                    _stdout, stderr = process.communicate(timeout=2)
                    error_output = stderr[-1500:]
                except Exception:
                    pass
            record("服务在 8799 端口启动", False, error_output.strip() or "启动超时")
            return 1
        record("服务在 8799 端口启动", True)

        # 1. bootstrap
        status, bootstrap = fetch_json("/api/bootstrap")
        total = bootstrap.get("total") if isinstance(bootstrap, dict) else None
        style_sum = (
            sum(int(entry.get("count", 0)) for entry in bootstrap.get("styles", []))
            if isinstance(bootstrap, dict)
            else None
        )
        ok = (
            status == 200
            and expected_gallery > 0
            and total == expected_gallery
            and style_sum == expected_gallery
        )
        record(
            f"/api/bootstrap 总数与风格数量（{expected_gallery} 条）",
            ok,
            f"HTTP {status}, total={total}, style_sum={style_sum}",
        )

        # 2. all items
        status, listing = fetch_json("/api/items")
        items = listing.get("items", []) if isinstance(listing, dict) else []
        ok = status == 200 and expected_gallery > 0 and len(items) == expected_gallery
        record(
            f"/api/items 返回全部 {expected_gallery} 条",
            ok,
            f"HTTP {status}, items={len(items)}",
        )

        # 3. style filter
        style_query = urllib.parse.urlencode({"style": "奶油风"})
        status, cream = fetch_json("/api/items?" + style_query)
        cream_items = cream.get("items", []) if isinstance(cream, dict) else []
        ok = (
            status == 200
            and len(cream_items) > 0
            and all(item.get("style_primary_zh") == "奶油风" for item in cream_items)
        )
        record("按风格筛选（奶油风）", ok, f"HTTP {status}, count={len(cream_items)}")

        # 4. text search
        query = urllib.parse.urlencode({"q": "卧室"})
        status, search = fetch_json("/api/items?" + query)
        search_items = search.get("items", []) if isinstance(search, dict) else []
        ok = status == 200 and len(search_items) > 0
        record("关键词搜索（卧室）", ok, f"HTTP {status}, count={len(search_items)}")

        # 5. detail of first item
        first_id = items[0]["id"] if items else ""
        status, detail = fetch_json("/api/items/" + urllib.parse.quote(first_id, safe=""))
        palette = detail.get("palette", []) if isinstance(detail, dict) else []
        prompt_en = detail.get("prompt_en", "") if isinstance(detail, dict) else ""
        ok = status == 200 and len(palette) == 6 and bool(str(prompt_en).strip())
        record(
            "条目详情包含 6 色色板与非空 prompt_en",
            ok,
            f"HTTP {status}, palette={len(palette)}, prompt_chars={len(str(prompt_en))}",
        )

        # 6. prompt override round trip on an item with no existing overlay
        candidates = [item["id"] for item in items if item["id"] not in overlay_ids()]
        if not candidates:
            record("提示词覆盖与恢复", False, "没有未覆盖的条目可安全测试")
        else:
            sid = candidates[0]
            status, original = fetch_json("/api/items/" + urllib.parse.quote(sid, safe=""))
            original_prompt = original.get("prompt_en", "") if isinstance(original, dict) else ""
            marker = "smoke-test-" + uuid.uuid4().hex
            restored = False
            try:
                status, saved = fetch_json(
                    "/api/items/" + urllib.parse.quote(sid, safe="") + "/prompt",
                    method="PUT",
                    payload={"prompt_en": marker},
                )
                status_get, after_save = fetch_json(
                    "/api/items/" + urllib.parse.quote(sid, safe="")
                )
                status_restore, after_restore = fetch_json(
                    "/api/items/" + urllib.parse.quote(sid, safe="") + "/restore",
                    method="POST",
                )
                restored = status_restore == 200
                status_final, final = fetch_json("/api/items/" + urllib.parse.quote(sid, safe=""))
                ok = (
                    status == 200
                    and status_get == 200
                    and status_restore == 200
                    and status_final == 200
                    and after_save.get("prompt_en") == marker
                    and after_save.get("overridden") is True
                    and after_restore.get("overridden") is False
                    and final.get("prompt_en") == original_prompt
                )
                record(
                    "提示词覆盖保存、回读与恢复原文",
                    ok,
                    f"sid={sid}, save={status}, read={status_get}, restore={status_restore}",
                )
            finally:
                if not restored:
                    try:
                        fetch(
                            "/api/items/" + urllib.parse.quote(sid, safe="") + "/restore",
                            method="POST",
                        )
                    except Exception:
                        pass

        # 7. media route
        local_path = items[0].get("thumb_url") if items else None
        status, detail_for_media = fetch_json("/api/items/" + urllib.parse.quote(first_id, safe=""))
        local_path = detail_for_media.get("local_path", "") if isinstance(detail_for_media, dict) else ""
        media_path = "/media/" + urllib.parse.quote(local_path, safe="/")
        status, content_type, body = fetch(media_path)
        ok = status == 200 and content_type.startswith("image/") and len(body) > 0
        record(
            "原图 /media 路由",
            ok,
            f"HTTP {status}, type={content_type}, bytes={len(body)}",
        )

        # 8. thumbnail route
        thumb_path = "/thumbs/" + urllib.parse.quote(first_id, safe="") + ".jpg"
        status, content_type, body = fetch(thumb_path)
        ok = status == 200 and content_type in {"image/jpeg", "image/jpg"} and len(body) > 5000
        record(
            "缩略图 /thumbs 按需生成",
            ok,
            f"HTTP {status}, type={content_type}, bytes={len(body)}",
        )

        # 8b. cache policy: code must revalidate, immutable media stays cached
        code_headers = {}
        code_detail = []
        code_ok = True
        for code_path in ("/", "/remodel.js", "/app.js", "/style.css"):
            head_status, head_headers, _head_body = fetch_headers(
                code_path, method="HEAD"
            )
            cache = head_headers.get("Cache-Control", "")
            etag = head_headers.get("ETag", "")
            last_modified = head_headers.get("Last-Modified", "")
            code_headers[code_path] = head_headers
            path_ok = (
                head_status == 200
                and cache == "no-cache, must-revalidate"
                and bool(etag)
                and bool(last_modified)
            )
            code_ok = code_ok and path_ok
            code_detail.append(f"{code_path}:{head_status}/{cache or '-'}")
        record(
            "静态代码 HEAD 200 + no-cache + ETag/Last-Modified",
            code_ok,
            ", ".join(code_detail),
        )

        media_status, media_headers, _media_body = fetch_headers(media_path)
        thumb_status, thumb_headers, _thumb_body = fetch_headers(thumb_path)
        media_cache = media_headers.get("Cache-Control", "")
        thumb_cache = thumb_headers.get("Cache-Control", "")
        ok = (
            media_status == 200
            and thumb_status == 200
            and "immutable" in media_cache
            and "max-age=31536000" in media_cache
            and "no-cache" not in media_cache
            and "immutable" in thumb_cache
            and "max-age=31536000" in thumb_cache
            and "no-cache" not in thumb_cache
        )
        record(
            "媒体缓存长期 immutable",
            ok,
            f"media={media_cache!r}, thumb={thumb_cache!r}",
        )

        # 8c. validators revalidate to 304 instead of a stale 200
        html_headers = code_headers["/"]
        not_modified_status, _nm_headers, nm_body = fetch_headers(
            "/", headers={"If-None-Match": html_headers.get("ETag", "")}
        )
        ims_status, _ims_headers, _ims_body = fetch_headers(
            "/", headers={"If-Modified-Since": html_headers.get("Last-Modified", "")}
        )
        ok = (
            not_modified_status == 304
            and nm_body == b""
            and ims_status == 304
        )
        record(
            "静态代码条件请求返回 304",
            ok,
            f"etag={not_modified_status}, ims={ims_status}",
        )

        # 9. path traversal, both raw and percent-encoded
        raw_status = None
        try:
            connection = http.client.HTTPConnection(HOST, PORT, timeout=TIMEOUT)
            connection.request("GET", "/media/../metadata.json")
            response = connection.getresponse()
            raw_status = response.status
            response.read()
            connection.close()
        except Exception:
            raw_status = None
        encoded_status, _content_type, _body = fetch("/media/%2e%2e/metadata.json")
        ok = raw_status in {403, 404} and encoded_status in {403, 404}
        record(
            "路径穿越被拒绝",
            ok,
            f"raw={raw_status}, encoded={encoded_status}",
        )

        # 9b. TASK20 gallery detail: section order, newest-first samples and
        # completion/failure signals. API responses are mocked in the page.
        try:
            for name, passed, detail in gallery_detail_completion_contract():
                record(name, passed, detail)
        except Exception as exc:
            record("TASK20 详情页生成结果与完成提示", False, f"异常：{exc}")
        try:
            for name, passed, detail in remodel_completion_contract():
                record(name, passed, detail)
        except Exception as exc:
            record("TASK20 改造完成提示与 is-new", False, f"异常：{exc}")
        try:
            for name, passed, detail in check_warning_contract():
                record(name, passed, detail)
        except Exception as exc:
            record("结构存疑 chip 轮次与文案", False, f"异常：{exc}")
        try:
            passed, detail = check_label_coverage_contract()
            record("结构存疑 label 覆盖全部后端检查项", passed, detail)
        except Exception as exc:
            record("结构存疑 label 覆盖全部后端检查项", False, f"异常：{exc}")
        try:
            for name, passed, detail in current_prompt_visibility_contract():
                record(name, passed, detail)
        except Exception as exc:
            record("当前结果提示词折叠态可见性", False, f"异常：{exc}")

        # ---------- 老房改造 ----------

        # 10. style packs: only styles with at least 3 samples
        status, packs = fetch_json("/api/remodel/packs")
        packs = packs if isinstance(packs, list) else []
        ok = (
            status == 200
            and len(packs) >= 8
            and all(isinstance(entry, dict) and int(entry.get("sample_count") or 0) >= 3 for entry in packs)
            and not any(entry.get("style_zh") in {"美式复古", "法式"} for entry in packs)
        )
        record(
            "改造 packs 仅包含 >=3 张的风格",
            ok,
            f"HTTP {status}, packs={len(packs)}",
        )
        style_zh = str(packs[0].get("style_zh") or "") if packs else ""

        # 11. upload rejects a text file
        status, _payload = multipart_json(
            "/api/remodel/upload", "note.txt", b"this is not an image", "text/plain"
        )
        record("上传拒绝文本文件（400）", status == 400, f"HTTP {status}")

        # 12. upload accepts a real 1x1 PNG
        status, upload = multipart_json(
            "/api/remodel/upload", "one.png", png_bytes(1, 1), "image/png"
        )
        upload = upload if isinstance(upload, dict) else {}
        upload_id = str(upload.get("upload_id") or "")
        ok = (
            status == 200
            and bool(upload_id)
            and upload.get("width") == 1
            and upload.get("height") == 1
            and "1:1" in str(upload.get("aspect_label") or "")
            and str(upload.get("preview_url") or "").startswith("/remodel/uploads/")
        )
        record(
            "上传接受 1x1 PNG 并返回尺寸与比例",
            ok,
            f"HTTP {status}, id={upload_id[:8]}, size={upload.get('width')}x{upload.get('height')}",
        )

        # 13. analyze returns the v2 structure JSON with numeric geometry/camera locks
        status, analysis = fetch_json(
            "/api/remodel/analyze", method="POST", payload={"upload_id": upload_id}
        )
        analysis = analysis if isinstance(analysis, dict) else {}
        structure_en = str(analysis.get("structure_en") or "")
        first_vision_calls = analysis.get("vision_calls")
        geometry = analysis.get("geometry") if isinstance(analysis.get("geometry"), dict) else {}
        camera = analysis.get("camera") if isinstance(analysis.get("camera"), dict) else {}
        openings = analysis.get("openings") if isinstance(analysis.get("openings"), list) else []
        framing = analysis.get("framing") if isinstance(analysis.get("framing"), dict) else {}
        fixed_features = (
            analysis.get("fixed_features")
            if isinstance(analysis.get("fixed_features"), list)
            else []
        )
        width_m = geometry.get("width_m")
        horizon_fraction = camera.get("horizon_fraction")
        count_sentence = (
            f"The number of openings is exactly {len(openings)}; "
            "do not add or remove any opening."
        )
        front_span = (
            framing.get("front_wall_and_opening_span")
            if isinstance(framing.get("front_wall_and_opening_span"), dict)
            else {}
        )
        ok = (
            status == 200
            and analysis.get("schema_version") == 4
            and bool(structure_en.strip())
            and isinstance(width_m, (int, float))
            and 0 < float(width_m) <= 30
            and isinstance(horizon_fraction, (int, float))
            and 0 < float(horizon_fraction) < 1
            and analysis.get("openings_total") == len(openings)
            and count_sentence in structure_en
            and "Fixed features that stay:" in structure_en
            and "the opening is nearly wall-to-wall" in structure_en
            and analysis.get("framing_valid") is True
            and isinstance(front_span.get("from_x"), (int, float))
            and isinstance(front_span.get("to_x"), (int, float))
            and isinstance(framing.get("ceiling_bottom_y"), (int, float))
            and isinstance(framing.get("floor_top_y"), (int, float))
            and isinstance(framing.get("opening_width_over_wall_width"), (int, float))
            and len(fixed_features) > 0
            and isinstance(analysis.get("immovable_zh"), list)
            and isinstance(analysis.get("movable_zh"), list)
            and analysis.get("cached") is False
            and first_vision_calls == 1
        )
        record(
            "视觉分析返回 v4 几何/相机/开口/墙面跨度/固定特征 JSON",
            ok,
            (
                f"HTTP {status}, width_m={width_m}, horizon={horizon_fraction}, "
                f"openings={len(openings)}, total={analysis.get('openings_total')}, "
                f"framing_valid={analysis.get('framing_valid')}, "
                f"fixed_features={len(fixed_features)}, structure_chars={len(structure_en)}"
            ),
        )

        # 14. analyze is cached on the second call (no second vision call)
        status_cached, analysis_cached = fetch_json(
            "/api/remodel/analyze", method="POST", payload={"upload_id": upload_id}
        )
        analysis_cached = analysis_cached if isinstance(analysis_cached, dict) else {}
        ok = (
            status_cached == 200
            and analysis_cached.get("cached") is True
            and analysis_cached.get("vision_calls") == first_vision_calls
            and analysis_cached.get("structure_en") == structure_en
        )
        record(
            "二次分析命中缓存（不重复调用视觉模型）",
            ok,
            f"HTTP {status_cached}, cached={analysis_cached.get('cached')}, vision_calls={analysis_cached.get('vision_calls')}",
        )

        # 14b. a legacy v1 sidecar (no schema_version, no geometry) still loads
        status, upload_v1 = multipart_json(
            "/api/remodel/upload", "legacy.png", png_bytes(2, 2), "image/png"
        )
        upload_v1 = upload_v1 if isinstance(upload_v1, dict) else {}
        upload_v1_id = str(upload_v1.get("upload_id") or "")
        legacy_structure = (
            "Living room with a rectangular plan, a flat 2.8 m ceiling, painted walls "
            "and a level floor. A doorway on the left wall and a sliding glass door "
            "front-right. Camera at 1.5 m height with a 24 mm lens."
        )
        legacy_sidecar = {
            "room_type_en": "living room",
            "shape_en": "rectangular room wider than deep",
            "ceiling_en": "flat ceiling at 2.8 m height",
            "walls_en": "flat painted walls",
            "floor_en": "continuous floor plane",
            "openings": [
                {"side": "left", "type": "doorway", "detail": "open doorway"},
                {
                    "side": "front-right",
                    "type": "sliding glass door",
                    "detail": "four panes with transom",
                },
            ],
            "camera_en": "camera at 1.5 m height, 24 mm lens",
            "light_en": "natural daylight",
            "structure_en": legacy_structure,
            "immovable_zh": ["左墙门洞"],
            "movable_zh": ["沙发"],
            "vision_calls": 1,
        }
        legacy_path = Path(remodel_dir) / "uploads" / f"{upload_v1_id}.json"
        legacy_path.write_text(json.dumps(legacy_sidecar, ensure_ascii=False), encoding="utf-8")
        status_v1, analysis_v1 = fetch_json(
            "/api/remodel/analyze", method="POST", payload={"upload_id": upload_v1_id}
        )
        analysis_v1 = analysis_v1 if isinstance(analysis_v1, dict) else {}
        ok = (
            status_v1 == 200
            and bool(upload_v1_id)
            and analysis_v1.get("cached") is True
            and analysis_v1.get("schema_version") == 1
            and analysis_v1.get("structure_en") == legacy_structure
        )
        record(
            "v1 旧缓存侧车仍可加载（无崩溃）",
            ok,
            (
                f"HTTP {status_v1}, cached={analysis_v1.get('cached')}, "
                f"schema={analysis_v1.get('schema_version')}"
            ),
        )

        # 15. generate must reject any image field (img2img guard)
        status, _payload = fetch_json(
            "/api/remodel/generate",
            method="POST",
            payload={
                "upload_id": upload_id,
                "style": style_zh,
                "structure_en": structure_en,
                "image": "data:image/png;base64,AAAA",
            },
        )
        record("生成拒绝客户端注入 image 字段（原图只能由服务端读取）", status == 400, f"HTTP {status}")

        # 16. unknown style is rejected
        status, _payload = fetch_json(
            "/api/remodel/generate",
            method="POST",
            payload={
                "upload_id": upload_id,
                "style": "不存在的风格",
                "structure_en": structure_en,
            },
        )
        record("未知风格返回 400", status == 400, f"HTTP {status}")

        # 17. history is valid JSON when empty (before any generate call)
        status, history = fetch_json("/api/remodel/history")
        ok = status == 200 and history == []
        record(
            "历史为空时返回合法 JSON",
            ok,
            f"HTTP {status}, body={json.dumps(history, ensure_ascii=False)[:80] if history is not None else 'invalid'}",
        )

        # 18. aspect mapping 0.75 -> 1080*1440, fake image generation in smoke mode
        status, upload_34 = multipart_json(
            "/api/remodel/upload", "three-four.png", png_bytes(300, 400), "image/png"
        )
        upload_34 = upload_34 if isinstance(upload_34, dict) else {}
        fetch_json(
            "/api/remodel/analyze",
            method="POST",
            payload={"upload_id": upload_34.get("upload_id", "")},
        )
        status, job = fetch_json(
            "/api/remodel/generate",
            method="POST",
            payload={
                "upload_id": upload_34.get("upload_id", ""),
                "style": style_zh,
                "structure_en": structure_en,
                "extra_zh": "加一个书架",
                "variants": 1,
                "mode": "text",
            },
        )
        job = job if isinstance(job, dict) else {}
        job_id = str(job.get("job_id") or "")
        ok = status in {200, 202} and job.get("size") == "1080*1440" and bool(job_id)
        record(
            "比例 0.75 映射到 1080*1440",
            ok,
            f"HTTP {status}, size={job.get('size')}",
        )

        status_job, snapshot = wait_remodel_job(job_id)
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        prompt = str(snapshot.get("prompt") or "")
        negative = str(snapshot.get("negative") or "")
        has_cjk = bool(re.search(r"[㐀-鿿豈-﫿]", prompt))
        ok = (
            status_job == 200
            and snapshot.get("status") == "done"
            and bool(snapshot.get("result_url"))
            and bool(prompt.strip())
            and not has_cjk
            and bool(negative.strip())
        )
        record(
            "smoke 模式生成完成，提示词为纯英文且含负面词",
            ok,
            f"status={snapshot.get('status')}, prompt_chars={len(prompt)}, cjk={has_cjk}",
        )

        # 18b. geometry-first prompt order: shell -> composition lock -> openings lock -> realism
        arch_marker = "Architectural shell, fixed and unchangeable:"
        composition_marker = "Composition lock:"
        framing_marker = "Wall proportions measured on the source photo"
        openings_marker = "Openings count lock:"
        realism_marker = (
            "Shot as an editorial interior photograph for a design magazine"
        )
        prompt_marks = [
            prompt.find(arch_marker),
            prompt.find(composition_marker),
            prompt.find(framing_marker),
            prompt.find(openings_marker),
            prompt.find(realism_marker),
        ]
        expected_count = f"The number of openings is exactly {len(openings)}; do not add or remove any opening."
        count_pos = prompt.find(expected_count)
        count_lock = f"Openings count lock: exactly {len(openings)} openings"
        horizon_lock = "horizon line at 55% of frame height"
        perspective_lock = "one-point perspective, straight at the far wall"
        framing_text = (
            "the wall carrying the main opening runs from x=0.10 to x=0.90 and the "
            "opening fills about 85% of that wall; the right wall plane runs from "
            "x=0.78 to x=1.00; the left wall plane from x=0.00 to x=0.12; the "
            "ceiling line sits at y=0.22 and the far-wall floor line at y=0.68"
        )
        opening_sentence = (
            "the opening is nearly wall-to-wall, leaving only narrow piers at each end"
        )
        ordered = (
            all(pos >= 0 for pos in prompt_marks)
            and prompt_marks[0]
            < prompt_marks[1]
            < prompt_marks[2]
            < prompt_marks[3]
            < prompt_marks[4]
            and prompt_marks[0] < count_pos < prompt_marks[3]
            and count_lock in prompt
            and horizon_lock in prompt
            and perspective_lock in prompt
            and framing_text in prompt
            and "85%" in prompt
            and prompt.count(opening_sentence) >= 2
            and "Fixed features that stay:" in prompt
            and "one-point perspective toward the described far wall" not in prompt
        )
        record(
            "提示词顺序：结构壳 → 构图锁 → 墙面跨度 → 开口数锁 → 真实感",
            ordered,
            f"positions={prompt_marks}, count_sentence={count_pos}",
        )
        negative_lower = negative.lower()
        negative_terms = [
            "showroom",
            "cgi",
            "octane render",
            "extra wall panels",
            "missing openings",
            "room widened",
            "raised ceiling",
        ]
        missing_terms = [term for term in negative_terms if term not in negative_lower]
        record(
            "负面提示词覆盖新增强约束",
            not missing_terms,
            f"missing={missing_terms}",
        )

        status_media, content_type, body = fetch(str(snapshot.get("result_url") or "/missing"))
        ok = status_media == 200 and content_type.startswith("image/") and len(body) > 0
        record(
            "改造结果通过 media 路由提供",
            ok,
            f"HTTP {status_media}, type={content_type}, bytes={len(body)}",
        )

        # 18c. the job records the real saved pixel size and an honest aspect_exact flag
        size_requested = str(snapshot.get("size_requested") or "")
        size_final = str(snapshot.get("size_final") or "")
        method_ok = size_final == "1080*1440"
        if method_ok:
            final_w, final_h = (int(part) for part in size_final.split("*", 1))
            method_ok = abs(final_w / final_h - 0.75) <= 0.004
        ok = (
            size_requested == "1080*1440"
            and method_ok
            and snapshot.get("width_final") == 1080
            and snapshot.get("height_final") == 1440
            and snapshot.get("aspect_exact") is True
        )
        record(
            "任务记录真实保存尺寸且 aspect_exact 与文件比例一致",
            ok,
            f"requested={size_requested}, final={size_final}, exact={snapshot.get('aspect_exact')}",
        )

        status_preview, preview_type, preview_body = fetch(str(upload_34.get("preview_url") or "/missing"))
        ok = (
            status_preview == 200
            and preview_type.startswith("image/")
            and len(preview_body) > 0
        )
        record(
            "上传预览通过 media 路由提供",
            ok,
            f"HTTP {status_preview}, type={preview_type}, bytes={len(preview_body)}",
        )

        # 19. aspect mapping 1.333 -> 1440*1080
        status, upload_43 = multipart_json(
            "/api/remodel/upload", "four-three.png", png_bytes(400, 300), "image/png"
        )
        upload_43 = upload_43 if isinstance(upload_43, dict) else {}
        status, job_43 = fetch_json(
            "/api/remodel/generate",
            method="POST",
            payload={
                "upload_id": upload_43.get("upload_id", ""),
                "style": style_zh,
                "structure_en": structure_en,
            },
        )
        job_43 = job_43 if isinstance(job_43, dict) else {}
        job_43_id = str(job_43.get("job_id") or "")
        ok = (
            status in {200, 202}
            and job_43.get("size") == "1440*1080"
            and bool(job_43_id)
            and job_43.get("mode") == "img2img"
            and job_43.get("strict") is None
        )
        record(
            "比例 1.333 映射到 1440*1080（省略 mode 默认 img2img）",
            ok,
            f"HTTP {status}, size={job_43.get('size')}, mode={job_43.get('mode')}",
        )
        wait_remodel_job(job_43_id)

        # 20. history now returns the completed jobs, newest first
        status, history = fetch_json("/api/remodel/history?limit=30")
        history = history if isinstance(history, list) else []
        ids = [entry.get("job_id") for entry in history if isinstance(entry, dict)]
        ok = (
            status == 200
            and len(history) >= 2
            and job_id in ids
            and job_43_id in ids
            and ids[0] == job_43_id
            and all(
                isinstance(entry.get("thumb_url"), str)
                and isinstance(entry.get("result_url"), str)
                and isinstance(entry.get("style"), str)
                and isinstance(entry.get("created_at"), str)
                for entry in history
            )
        )
        record(
            "改造历史返回有效 JSON 且最新在前",
            ok,
            f"HTTP {status}, count={len(history)}, first={ids[0] if ids else ''}",
        )

        # 20b. strict=off omits the composition lock, openings lock and count sentence
        status, job_loose = fetch_json(
            "/api/remodel/generate",
            method="POST",
            payload={
                "upload_id": upload_43.get("upload_id", ""),
                "style": style_zh,
                "structure_en": structure_en,
                "strict": False,
                "mode": "text",
            },
        )
        job_loose = job_loose if isinstance(job_loose, dict) else {}
        job_loose_id = str(job_loose.get("job_id") or "")
        status_loose, snapshot_loose = wait_remodel_job(job_loose_id)
        snapshot_loose = snapshot_loose if isinstance(snapshot_loose, dict) else {}
        loose_prompt = str(snapshot_loose.get("prompt") or "")
        ok = (
            status in {200, 202}
            and status_loose == 200
            and snapshot_loose.get("status") == "done"
            and snapshot_loose.get("strict") is False
            and "Architectural shell, fixed and unchangeable:" in loose_prompt
            and "Composition lock:" not in loose_prompt
            and "Openings count lock:" not in loose_prompt
            and "The number of openings is exactly" not in loose_prompt
            and "Shot as an editorial interior photograph" in loose_prompt
        )
        record(
            "严格结构 OFF 时省略构图锁、开口数锁与计数句",
            ok,
            (
                f"HTTP {status}/{status_loose}, strict={snapshot_loose.get('strict')}, "
                f"prompt_chars={len(loose_prompt)}"
            ),
        )

        # 20c. a non-standard upload ratio is centre-cropped end to end
        status, upload_07 = multipart_json(
            "/api/remodel/upload", "ratio-07.png", png_bytes(700, 1000), "image/png"
        )
        upload_07 = upload_07 if isinstance(upload_07, dict) else {}
        status, job_07 = fetch_json(
            "/api/remodel/generate",
            method="POST",
            payload={
                "upload_id": upload_07.get("upload_id", ""),
                "style": style_zh,
                "structure_en": structure_en,
            },
        )
        job_07 = job_07 if isinstance(job_07, dict) else {}
        job_07_id = str(job_07.get("job_id") or "")
        status_07, snapshot_07 = wait_remodel_job(job_07_id)
        snapshot_07 = snapshot_07 if isinstance(snapshot_07, dict) else {}
        cropped_bytes = None
        if snapshot_07.get("result_url"):
            status_crop_media, _ctype, cropped_bytes = fetch(str(snapshot_07["result_url"]))
        cropped_size = None
        if cropped_bytes:
            try:
                with Image.open(io.BytesIO(cropped_bytes)) as cropped_image:
                    cropped_size = cropped_image.size
            except Exception:
                cropped_size = None
        cropped_ratio = (
            cropped_size[0] / cropped_size[1] if cropped_size else None
        )
        ok = (
            status in {200, 202}
            and status_07 == 200
            and snapshot_07.get("status") == "done"
            and snapshot_07.get("size_requested") == "1080*1440"
            and snapshot_07.get("size_final") == "1008*1440"
            and snapshot_07.get("width_final") == 1008
            and snapshot_07.get("height_final") == 1440
            and snapshot_07.get("aspect_exact") is True
            and cropped_size == (1008, 1440)
            and cropped_ratio is not None
            and abs(cropped_ratio - 0.7) <= 0.004
        )
        record(
            "非标准比例端到端中心裁切并记录 size_final",
            ok,
            (
                f"status={snapshot_07.get('status')}, requested="
                f"{snapshot_07.get('size_requested')}, final={snapshot_07.get('size_final')}, "
                f"file={cropped_size}, ratio={cropped_ratio}"
            ),
        )

        # 20d. a v2 sidecar is upgraded to v3 in place, without a vision call
        status, upload_v2 = multipart_json(
            "/api/remodel/upload", "legacy-v2.png", png_bytes(320, 240), "image/png"
        )
        upload_v2 = upload_v2 if isinstance(upload_v2, dict) else {}
        upload_v2_id = str(upload_v2.get("upload_id") or "")
        v2_sidecar = {
            "schema_version": 2,
            "room_type_en": "living room",
            "shape_en": "rectangular, wider than deep",
            "ceiling_en": "flat ceiling at 2.8 m height",
            "walls_en": "flat painted walls",
            "floor_en": "continuous floor plane",
            "geometry": {
                "room_type_en": "living room",
                "shape_en": "rectangular, wider than deep",
                "width_m": 4.2,
                "depth_m": 3.1,
                "ceiling_m": 2.8,
                "floor_level_changes": False,
            },
            "openings": [
                {
                    "side": "left",
                    "type": "open doorway",
                    "width_m": 0.95,
                    "height_m": 2.05,
                    "sill_m": 0.0,
                    "center_offset_fraction": 0.42,
                    "detail": "open doorway to corridor",
                }
            ],
            "openings_total": 1,
            "camera": {
                "height_m": 1.5,
                "distance_to_far_wall_m": 3.6,
                "lens_mm": 24,
                "tilt_deg": -3,
                "horizon_fraction": 0.55,
                "perspective": "two-point, angled toward the front-left corner",
            },
            "camera_en": "camera at 1.5 m height, two-point perspective",
            "light_en": "natural daylight",
            "structure_en": (
                "Living room 4.2 m x 3.1 m with a flat 2.8 m ceiling and a left "
                "doorway. Camera at 1.5 m height with one-point perspective."
            ),
            "immovable_zh": ["左墙门洞"],
            "movable_zh": ["沙发"],
            "vision_calls": 1,
        }
        v2_path = Path(remodel_dir) / "uploads" / f"{upload_v2_id}.json"
        v2_path.write_text(json.dumps(v2_sidecar, ensure_ascii=False), encoding="utf-8")
        status_v2, analysis_v2 = fetch_json(
            "/api/remodel/analyze", method="POST", payload={"upload_id": upload_v2_id}
        )
        analysis_v2 = analysis_v2 if isinstance(analysis_v2, dict) else {}
        v2_saved = {}
        try:
            v2_saved = json.loads(v2_path.read_text(encoding="utf-8"))
        except Exception:
            v2_saved = {}
        framing_v2 = (
            analysis_v2.get("framing")
            if isinstance(analysis_v2.get("framing"), dict)
            else {}
        )
        structure_v2 = str(analysis_v2.get("structure_en") or "")
        ok = (
            status_v2 == 200
            and analysis_v2.get("cached") is True
            and analysis_v2.get("schema_version") == 4
            and analysis_v2.get("vision_calls") == 1
            and v2_saved.get("schema_version") == 4
            and analysis_v2.get("framing_valid") is False
            and "帧占比" in str(analysis_v2.get("framing_note") or "")
            and "two-point" in structure_v2
            and "one-point" not in structure_v2.casefold()
        )
        record(
            "v2 侧车原地升级为 v4（旧帧占比标为不可用，不重复调用视觉模型）",
            ok,
            (
                f"HTTP {status_v2}, cached={analysis_v2.get('cached')}, "
                f"schema={analysis_v2.get('schema_version')}, "
                f"file_schema={v2_saved.get('schema_version')}, "
                f"framing_valid={analysis_v2.get('framing_valid')}, "
                f"vision_calls={analysis_v2.get('vision_calls')}"
            ),
        )

        # 20e. the prompt follows the analysed perspective instead of hardcoding one-point
        contradictory = (
            structure_v2
            + " One-point perspective toward the described far wall is also acceptable."
        )
        status, job_v2 = fetch_json(
            "/api/remodel/generate",
            method="POST",
            payload={
                "upload_id": upload_v2_id,
                "style": style_zh,
                "structure_en": contradictory,
                "mode": "text",
            },
        )
        job_v2 = job_v2 if isinstance(job_v2, dict) else {}
        job_v2_id = str(job_v2.get("job_id") or "")
        status_v2_job, snapshot_v2 = wait_remodel_job(job_v2_id)
        snapshot_v2 = snapshot_v2 if isinstance(snapshot_v2, dict) else {}
        prompt_v2 = str(snapshot_v2.get("prompt") or "")
        ok = (
            status in {200, 202}
            and status_v2_job == 200
            and snapshot_v2.get("status") == "done"
            and "two-point perspective, angled toward the front-left corner" in prompt_v2
            and "one-point" not in prompt_v2.casefold()
        )
        record(
            "提示词采用分析出的两点透视且不再出现 one-point",
            ok,
            (
                f"status={snapshot_v2.get('status')}, "
                f"two_point={'two-point' in prompt_v2.casefold()}, "
                f"one_point={'one-point' in prompt_v2.casefold()}"
            ),
        )

        # 20f. a stubbed low score triggers exactly one repair retry; the newer,
        # better-scoring attempt is kept.
        status, upload_retry = multipart_json(
            "/api/remodel/upload", "retry.png", png_bytes(360, 270), "image/png"
        )
        upload_retry = upload_retry if isinstance(upload_retry, dict) else {}
        status, job_retry = fetch_json(
            "/api/remodel/generate",
            method="POST",
            payload={
                "upload_id": upload_retry.get("upload_id", ""),
                "style": style_zh,
                "structure_en": structure_en,
                "smoke_check": "low",
            },
        )
        job_retry = job_retry if isinstance(job_retry, dict) else {}
        job_retry_id = str(job_retry.get("job_id") or "")
        status_retry, snapshot_retry = wait_remodel_job(job_retry_id)
        snapshot_retry = snapshot_retry if isinstance(snapshot_retry, dict) else {}
        checks_retry = (
            snapshot_retry.get("checks")
            if isinstance(snapshot_retry.get("checks"), list)
            else []
        )
        retry_prompt = str(snapshot_retry.get("prompt") or "")
        ok = (
            status in {200, 202}
            and status_retry == 200
            and snapshot_retry.get("status") == "done"
            and snapshot_retry.get("attempts") == 2
            and snapshot_retry.get("final_attempt") == 2
            and len(checks_retry) == 2
            and checks_retry[0].get("attempt") == 1
            and isinstance(checks_retry[0].get("score"), (int, float))
            and float(checks_retry[0]["score"]) < 0.85
            and float(checks_retry[1]["score"]) > float(checks_retry[0]["score"])
            and snapshot_retry.get("final_score") == checks_retry[1]["score"]
            and "Hard corrections, these override other instructions:" in retry_prompt
            and "restore the plaster cornice" in retry_prompt
        )
        record(
            "低分自检触发一次重试并保留更高分的第 2 版",
            ok,
            (
                f"status={snapshot_retry.get('status')}, attempts={snapshot_retry.get('attempts')}, "
                f"final_attempt={snapshot_retry.get('final_attempt')}, "
                f"scores={[entry.get('score') for entry in checks_retry]}"
            ),
        )

        # 20g. a high score does not retry
        status, upload_high = multipart_json(
            "/api/remodel/upload", "high.png", png_bytes(361, 271), "image/png"
        )
        upload_high = upload_high if isinstance(upload_high, dict) else {}
        status, job_high = fetch_json(
            "/api/remodel/generate",
            method="POST",
            payload={
                "upload_id": upload_high.get("upload_id", ""),
                "style": style_zh,
                "structure_en": structure_en,
                "smoke_check": "high",
            },
        )
        job_high = job_high if isinstance(job_high, dict) else {}
        job_high_id = str(job_high.get("job_id") or "")
        status_high, snapshot_high = wait_remodel_job(job_high_id)
        snapshot_high = snapshot_high if isinstance(snapshot_high, dict) else {}
        ok = (
            status in {200, 202}
            and status_high == 200
            and snapshot_high.get("status") == "done"
            and snapshot_high.get("attempts") == 1
            and snapshot_high.get("final_attempt") == 1
            and isinstance(snapshot_high.get("final_score"), (int, float))
            and float(snapshot_high["final_score"]) >= 0.85
        )
        record(
            "高分自检不重试（attempts == 1）",
            ok,
            (
                f"status={snapshot_high.get('status')}, attempts={snapshot_high.get('attempts')}, "
                f"final_score={snapshot_high.get('final_score')}"
            ),
        )

        # 20h. a self-check error is not a gate: the job still finishes done
        status, upload_check_error = multipart_json(
            "/api/remodel/upload", "check-error.png", png_bytes(362, 272), "image/png"
        )
        upload_check_error = upload_check_error if isinstance(upload_check_error, dict) else {}
        status, job_check_error = fetch_json(
            "/api/remodel/generate",
            method="POST",
            payload={
                "upload_id": upload_check_error.get("upload_id", ""),
                "style": style_zh,
                "structure_en": structure_en,
                "smoke_check": "error",
            },
        )
        job_check_error = job_check_error if isinstance(job_check_error, dict) else {}
        job_check_error_id = str(job_check_error.get("job_id") or "")
        status_check_error, snapshot_check_error = wait_remodel_job(job_check_error_id)
        snapshot_check_error = (
            snapshot_check_error if isinstance(snapshot_check_error, dict) else {}
        )
        checks_check_error = (
            snapshot_check_error.get("checks")
            if isinstance(snapshot_check_error.get("checks"), list)
            else []
        )
        error_entries = [
            entry
            for entry in checks_check_error
            if isinstance(entry, dict) and entry.get("attempt") == 0
        ]
        ok = (
            status in {200, 202}
            and status_check_error == 200
            and snapshot_check_error.get("status") == "done"
            and bool(snapshot_check_error.get("result_url"))
            and snapshot_check_error.get("attempts") == 1
            and bool(error_entries)
            and bool(snapshot_check_error.get("check_error"))
            and snapshot_check_error.get("final_score") is None
        )
        record(
            "自检异常仍返回 done，失败调用留在 checks 且 final_score 为 null",
            ok,
            (
                f"status={snapshot_check_error.get('status')}, "
                f"checks={len(checks_check_error)}, errors={len(error_entries)}, "
                f"final_score={snapshot_check_error.get('final_score')}"
            ),
        )

        # 20i. a truncated first check falls back to the short-form prompt and
        # still allows the repair retry (attempts == 2).
        status, upload_trunc = multipart_json(
            "/api/remodel/upload", "truncated.png", png_bytes(363, 273), "image/png"
        )
        upload_trunc = upload_trunc if isinstance(upload_trunc, dict) else {}
        status, job_trunc = fetch_json(
            "/api/remodel/generate",
            method="POST",
            payload={
                "upload_id": upload_trunc.get("upload_id", ""),
                "style": style_zh,
                "structure_en": structure_en,
                "smoke_check": "truncated",
            },
        )
        job_trunc = job_trunc if isinstance(job_trunc, dict) else {}
        job_trunc_id = str(job_trunc.get("job_id") or "")
        status_trunc, snapshot_trunc = wait_remodel_job(job_trunc_id)
        snapshot_trunc = snapshot_trunc if isinstance(snapshot_trunc, dict) else {}
        checks_trunc = (
            snapshot_trunc.get("checks")
            if isinstance(snapshot_trunc.get("checks"), list)
            else []
        )
        truncated_entries = [
            entry
            for entry in checks_trunc
            if isinstance(entry, dict) and entry.get("attempt") == 0
        ]
        short_form_entries = [
            entry
            for entry in checks_trunc
            if isinstance(entry, dict) and entry.get("short_form") is True
        ]
        ok = (
            status in {200, 202}
            and status_trunc == 200
            and snapshot_trunc.get("status") == "done"
            and snapshot_trunc.get("attempts") == 2
            and snapshot_trunc.get("final_attempt") == 2
            and bool(truncated_entries)
            and any(
                entry.get("finish_reason") == "length" and entry.get("usage")
                for entry in truncated_entries
            )
            and bool(short_form_entries)
            and float(snapshot_trunc.get("final_score") or 0) > 0.85
        )
        record(
            "自检截断走短格式重试，并触发一次修复重试（attempts == 2）",
            ok,
            (
                f"status={snapshot_trunc.get('status')}, "
                f"attempts={snapshot_trunc.get('attempts')}, "
                f"short_form={len(short_form_entries)}, "
                f"diagnostics={len(truncated_entries)}, "
                f"final_score={snapshot_trunc.get('final_score')}"
            ),
        )

        # 20j. invalid wall spans are dropped instead of entering the prompt.
        status, upload_bad_framing = multipart_json(
            "/api/remodel/upload", "bad-framing.png", png_bytes(364, 274), "image/png"
        )
        upload_bad_framing = (
            upload_bad_framing if isinstance(upload_bad_framing, dict) else {}
        )
        bad_framing = {
            "front_wall_and_opening_span": {"from_x": 0.9, "to_x": 0.1},
            "right_wall_span": {"from_x": 0.78, "to_x": 1.0},
            "left_wall_span": {"from_x": 0.0, "to_x": 0.12},
            "ceiling_bottom_y": 0.22,
            "floor_top_y": 0.68,
            "opening_width_over_wall_width": 0.85,
        }
        bad_analysis = gui_server.GalleryStore._normalize_analysis(
            {**gui_server.GalleryStore._stub_analysis(), "framing": bad_framing}
        )
        bad_path = (
            Path(remodel_dir)
            / "uploads"
            / f"{upload_bad_framing.get('upload_id', '')}.json"
        )
        bad_path.write_text(
            json.dumps(bad_analysis, ensure_ascii=False), encoding="utf-8"
        )
        status, job_bad_framing = fetch_json(
            "/api/remodel/generate",
            method="POST",
            payload={
                "upload_id": upload_bad_framing.get("upload_id", ""),
                "style": style_zh,
                "mode": "text",
            },
        )
        job_bad_framing = job_bad_framing if isinstance(job_bad_framing, dict) else {}
        _status_bad, snapshot_bad_framing = wait_remodel_job(
            str(job_bad_framing.get("job_id") or "")
        )
        snapshot_bad_framing = (
            snapshot_bad_framing if isinstance(snapshot_bad_framing, dict) else {}
        )
        bad_prompt = str(snapshot_bad_framing.get("prompt") or "")
        ok = (
            status in {200, 202}
            and snapshot_bad_framing.get("status") == "done"
            and snapshot_bad_framing.get("framing_valid") is False
            and "to_x 必须大于 from_x" in str(
                snapshot_bad_framing.get("framing_note") or ""
            )
            and "Wall proportions measured on the source photo" not in bad_prompt
            and "the opening is nearly wall-to-wall" not in bad_prompt
        )
        record(
            "不合理的墙面跨度被丢弃，提示词不含帧占比块",
            ok,
            (
                f"status={snapshot_bad_framing.get('status')}, "
                f"framing_valid={snapshot_bad_framing.get('framing_valid')}, "
                f"note={str(snapshot_bad_framing.get('framing_note') or '')[:60]}"
            ),
        )

        # 20k. a plausible measured set reaches the prompt as percentages, and
        # opening_width_over_wall_width selects the matching span sentence.
        span_cases = [
            (0.9, "the opening is nearly wall-to-wall, leaving only narrow piers at each end"),
            (0.6, "the opening covers about half the wall with solid wall on both sides"),
            (0.3, "the opening is a modest window set in a mostly solid wall"),
        ]
        span_failures = []
        for index, (ratio, sentence) in enumerate(span_cases):
            status, upload_span = multipart_json(
                "/api/remodel/upload",
                f"span-{index}.png",
                png_bytes(365 + index, 275),
                "image/png",
            )
            upload_span = upload_span if isinstance(upload_span, dict) else {}
            framing_span = {
                "front_wall_and_opening_span": {"from_x": 0.10, "to_x": 0.90},
                "right_wall_span": {"from_x": 0.78, "to_x": 1.00},
                "left_wall_span": {"from_x": 0.00, "to_x": 0.12},
                "ceiling_bottom_y": 0.22,
                "floor_top_y": 0.68,
                "opening_width_over_wall_width": ratio,
            }
            span_analysis = gui_server.GalleryStore._normalize_analysis(
                {**gui_server.GalleryStore._stub_analysis(), "framing": framing_span}
            )
            span_path = (
                Path(remodel_dir)
                / "uploads"
                / f"{upload_span.get('upload_id', '')}.json"
            )
            span_path.write_text(
                json.dumps(span_analysis, ensure_ascii=False), encoding="utf-8"
            )
            status, job_span = fetch_json(
                "/api/remodel/generate",
                method="POST",
                payload={
                    "upload_id": upload_span.get("upload_id", ""),
                    "style": style_zh,
                    "mode": "text",
                },
            )
            job_span = job_span if isinstance(job_span, dict) else {}
            _status_span, snapshot_span = wait_remodel_job(
                str(job_span.get("job_id") or "")
            )
            snapshot_span = (
                snapshot_span if isinstance(snapshot_span, dict) else {}
            )
            span_prompt = str(snapshot_span.get("prompt") or "")
            pct = int(round(ratio * 100))
            case_ok = (
                status in {200, 202}
                and snapshot_span.get("status") == "done"
                and snapshot_span.get("framing_valid") is True
                and "Wall proportions measured on the source photo" in span_prompt
                and f"the opening fills about {pct}% of that wall" in span_prompt
                and span_prompt.count(sentence) >= 2
                and "the opening keeps its width relative to its wall" in span_prompt
            )
            if not case_ok:
                span_failures.append((ratio, snapshot_span.get("framing_valid")))
        record(
            "合理帧占比生成百分比块，开口比例 0.9/0.6/0.3 选择对应措辞",
            not span_failures,
            f"failures={span_failures}",
        )

        # 20l. recheck re-scores a finished job and returns the updated job JSON.
        recheck_status, rechecked = fetch_json(
            "/api/remodel/recheck/" + urllib.parse.quote(job_high_id, safe=""),
            method="POST",
        )
        rechecked = rechecked if isinstance(rechecked, dict) else {}
        recheck_checks = (
            rechecked.get("checks")
            if isinstance(rechecked.get("checks"), list)
            else []
        )
        recheck_score = rechecked.get("final_score")
        ok = (
            status in {200, 202}
            and recheck_status == 200
            and rechecked.get("status") == "done"
            and (recheck_score is None or isinstance(recheck_score, (int, float)))
            and bool(recheck_checks)
            and any(
                isinstance(entry, dict) and entry.get("attempt") is not None
                for entry in recheck_checks
            )
        )
        record(
            "POST /api/remodel/recheck/<job_id> 返回带 final_score 与 checks 的任务",
            ok,
            (
                f"HTTP {recheck_status}, status={rechecked.get('status')}, "
                f"final_score={recheck_score}, checks={len(recheck_checks)}"
            ),
        )

        # 20m. the image request builder enforces the hard part counts:
        # img2img = exactly one image (the user's own photo) + one text part;
        # text = zero image parts.
        body_probe_name = "img2img 请求体恰好 1 个 image + 1 个 text，且为 JPEG data URL"
        text_probe_name = "text 请求体 0 个 image part"
        try:
            image_part_url = "data:image/jpeg;base64,AAAA"
            img_payload = gui_server.GalleryStore._build_image_payload(
                "test prompt", "qwen-image-3.0-pro", "1080*1440", image_part_url
            )
            img_content = img_payload["messages"][0]["content"]
            img_images = [part for part in img_content if "image" in part]
            img_texts = [part for part in img_content if "text" in part]
            text_payload = gui_server.GalleryStore._build_image_payload(
                "test prompt", "qwen-image-3.0-pro", "1080*1440"
            )
            text_content = text_payload["messages"][0]["content"]
            text_images = [part for part in text_content if "image" in part]
            text_texts = [part for part in text_content if "text" in part]
            ok = (
                len(img_content) == 2
                and len(img_images) == 1
                and img_images[0].get("image") == image_part_url
                and str(img_images[0].get("image") or "").startswith(
                    "data:image/jpeg;base64,"
                )
                and len(img_texts) == 1
                and img_texts[0].get("text") == "test prompt"
            )
            record(
                body_probe_name,
                ok,
                f"parts={len(img_content)}, images={len(img_images)}, texts={len(img_texts)}",
            )
            ok = (
                len(text_content) == 1
                and not text_images
                and len(text_texts) == 1
            )
            record(text_probe_name, ok, f"parts={len(text_content)}, images={len(text_images)}")
        except Exception as exc:
            record(body_probe_name, False, f"异常：{exc}")
            record(text_probe_name, False, f"异常：{exc}")

        # 20m2. the timeout policy: img2img waits 900 s once, never retries the
        # network, and reports the exact queue-timeout message. The fake urlopen
        # raises immediately, so no real wait is involved.
        timeout_probe_name = "img2img 超时只发 1 次网络请求，错误文案准确"
        timeout_message_expected = (
            f"生成接口排队超时（已等待 {int(gui_server.REMODEL_IMG2IMG_TIMEOUT)} 秒），"
            "稍后重试或改用纯文本模式"
        )
        try:
            class _RequestProbe:
                _build_image_payload = staticmethod(
                    gui_server.GalleryStore._build_image_payload
                )

            net_calls: list[float | None] = []
            original_urlopen = gui_server.urlopen

            def _fake_urlopen(_request, timeout=None):
                net_calls.append(timeout)
                raise TimeoutError("simulated timeout")

            gui_server.urlopen = _fake_urlopen
            timeout_message = ""
            try:
                gui_server.GalleryStore._request_image(
                    _RequestProbe(),
                    "http://127.0.0.1:1",
                    "test-key",
                    "test prompt",
                    "qwen-image-3.0-pro",
                    "1080*1440",
                    image_data_url="data:image/jpeg;base64,AAAA",
                    timeout=gui_server.REMODEL_IMG2IMG_TIMEOUT,
                    retry_network=False,
                )
            except gui_server.RemodelTimeoutError as exc:
                timeout_message = str(exc)
            finally:
                gui_server.urlopen = original_urlopen
            ok = (
                gui_server.REMODEL_INPUT_LONG_EDGE == 1024
                and gui_server.REMODEL_IMG2IMG_TIMEOUT == 900
                and gui_server.REMODEL_TEXT_TIMEOUT == 600
                and len(net_calls) == 1
                and net_calls[0] == gui_server.REMODEL_IMG2IMG_TIMEOUT
                and timeout_message == timeout_message_expected
            )
            record(
                timeout_probe_name,
                ok,
                (
                    f"input_edge={gui_server.REMODEL_INPUT_LONG_EDGE}, "
                    f"img2img_timeout={gui_server.REMODEL_IMG2IMG_TIMEOUT}, "
                    f"text_timeout={gui_server.REMODEL_TEXT_TIMEOUT}, "
                    f"calls={len(net_calls)}, message={timeout_message!r}"
                ),
            )
        except Exception as exc:
            record(timeout_probe_name, False, f"异常：{exc}")

        # 20n. prompt modes: the photo carries geometry in img2img, so the
        # composition lock and wall-proportion numbers are text-only.
        img_prompt_name = "img2img 提示词含原图结构指令，且不含构图锁/墙占比"
        text_prompt_name = "text 提示词仍含构图锁与墙占比"
        try:
            mode_analysis = gui_server.GalleryStore._normalize_analysis(
                gui_server.GalleryStore._stub_analysis()
            )
            mode_pack = {
                "style_en": "cream",
                "style_dna_en": (
                    "warm plaster walls, pale oak floors, linen textiles, soft diffused "
                    "daylight and warm lamp glow"
                ),
                "furniture_plan_en": "a low sofa on the long wall and a round oak table",
                "lighting_plan_en": (
                    "Late afternoon sun with soft overcast weather enters from the main "
                    "window wall, with daylight at 5200 K and practicals at 2700 K. "
                    "A paper lantern pendant, a brass wall sconce, a mushroom table lamp "
                    "and a linear cove LED shape the glow. Shadows fall softly on the "
                    "floor and the window keeps detail with no blown highlights. The light "
                    "reveals boucle nap, walnut grain and linen weave."
                ),
                "layering_plan_en": (
                    "Seating: a cream boucle sofa, a cane lounge chair, a boucle ottoman; "
                    "Table surfaces: a round oak coffee table, a walnut side table; "
                    "Storage/display: an open oak bookshelf, a low walnut sideboard; "
                    "Textiles: linen curtains, a wool rug, boucle cushions, a cotton throw; "
                    "Appliances: a matte record player, a ceramic diffuser; Greenery: a "
                    "potted olive tree, a vase of dried branches; Wall decor: an abstract "
                    "print, a round brass mirror; Tabletop objects: a ceramic vase, art "
                    "books, a brass tray, a pillar candle; Lighting fixtures: a paper "
                    "lantern pendant, a brass wall sconce, a mushroom table lamp. "
                    "Density target: at least 12 distinct furnishing and decor items, "
                    "at least 3 styled surfaces, 2 layered textiles per seating area."
                ),
            }
            img_prompt = gui_server.GalleryStore._build_remodel_prompt(
                mode_pack,
                str(mode_analysis.get("structure_en") or ""),
                "",
                "1080*1440",
                analysis=mode_analysis,
                mode="img2img",
            )
            text_prompt = gui_server.GalleryStore._build_remodel_prompt(
                mode_pack,
                str(mode_analysis.get("structure_en") or ""),
                "",
                "1080*1440",
                analysis=mode_analysis,
                strict=True,
                mode="text",
            )
            instruction = "Renovate this exact room photograph."
            photo_marker = "The input photograph shows:"
            style_marker = "Interior styling, in cream:"
            replacement_marker = (
                "Everything in the original room except the architectural shell is replaced:"
            )
            realism_marker = "Shot as an editorial interior photograph"
            img_ok = (
                instruction in img_prompt
                and photo_marker in img_prompt
                and style_marker in img_prompt
                and replacement_marker in img_prompt
                and realism_marker in img_prompt
                and "Composition lock:" not in img_prompt
                and "Wall proportions measured on the source photo" not in img_prompt
                and "Openings count lock:" not in img_prompt
                and img_prompt.find(instruction)
                < img_prompt.find(photo_marker)
                < img_prompt.find(style_marker)
                < img_prompt.find(replacement_marker)
                < img_prompt.find(realism_marker)
            )
            record(
                img_prompt_name,
                img_ok,
                (
                    f"instruction={instruction in img_prompt}, photo={photo_marker in img_prompt}, "
                    f"composition={'Composition lock:' in img_prompt}, "
                    f"wall_props={'Wall proportions measured on the source photo' in img_prompt}"
                ),
            )
            text_ok = (
                "Composition lock:" in text_prompt
                and "Wall proportions measured on the source photo" in text_prompt
                and "Openings count lock:" in text_prompt
                and photo_marker not in text_prompt
            )
            record(
                text_prompt_name,
                text_ok,
                (
                    f"composition={'Composition lock:' in text_prompt}, "
                    f"wall_props={'Wall proportions measured on the source photo' in text_prompt}"
                ),
            )

            lighting_marker = "LIGHTING DESIGN"
            furnishing_marker = "FURNISHING AND DECOR"
            side_line = (
                "The room is lit by one key light: daylight entering from the left and "
                "front-right opening(s)."
            )
            shadow_line = "Every shadow in the room runs away from that wall."
            practical_line = (
                "Practical fixtures are decorative only: at most two visible fixtures, "
                "warm 2700 K, low output. They do not cast shadows, do not add fill "
                "light, and do not glow."
            )
            no_bloom_line = (
                "No bloom, no halo, no light spill on the ceiling, no lens flare, no "
                "double shadows. The window stays readable, never blown to white."
            )
            exposure_line = (
                "Interior exposure is balanced for the room, not for the view outside."
            )
            keep_openings = (
                "Keep window openings physically unchanged while changing what comes "
                "through them."
            )
            permission = (
                "You may change the lighting completely; the original photo's light is "
                "not a constraint."
            )
            density = (
                "Density target: at least 12 distinct furnishing and decor items, at "
                "least 3 styled surfaces, 2 layered textiles per seating area."
            )
            layering_body = mode_pack["layering_plan_en"].split(
                "Density target:", 1
            )[0].strip()
            lighting_lines = (side_line, shadow_line, practical_line, no_bloom_line, exposure_line)
            img_blocks_ok = (
                lighting_marker in img_prompt
                and furnishing_marker in img_prompt
                and "REALISM" in img_prompt
                and all(line in img_prompt for line in lighting_lines)
                and keep_openings in img_prompt
                and permission in img_prompt
                and density in img_prompt
                and img_prompt.find(lighting_marker)
                < img_prompt.find(furnishing_marker)
                < img_prompt.find("REALISM")
                and "the only practical fixtures" in img_prompt
                and "boucle nap" in img_prompt
                and "lamp glow" not in img_prompt
                and "soft diffused daylight" not in img_prompt
                and layering_body in img_prompt
                and mode_pack["furniture_plan_en"] in img_prompt
            )
            record(
                "img2img 提示词含灯光方向、软装层次与密度目标",
                img_blocks_ok,
                (
                    f"lighting={lighting_marker in img_prompt}, "
                    f"furnishing={furnishing_marker in img_prompt}, "
                    f"side_line={side_line in img_prompt}, "
                    f"density={density in img_prompt}"
                ),
            )
            text_blocks_ok = (
                lighting_marker in text_prompt
                and furnishing_marker in text_prompt
                and "REALISM" in text_prompt
                and all(line in text_prompt for line in lighting_lines)
                and keep_openings in text_prompt
                and permission not in text_prompt
                and density in text_prompt
                and "the only practical fixtures" in text_prompt
                and "boucle nap" in text_prompt
                and "lamp glow" not in text_prompt
                and "soft diffused daylight" not in text_prompt
                and layering_body in text_prompt
            )
            record(
                "text 提示词含灯光与软装但不含原图灯光许可",
                text_blocks_ok,
                (
                    f"lighting={lighting_marker in text_prompt}, "
                    f"furnishing={furnishing_marker in text_prompt}, "
                    f"permission={permission in text_prompt}"
                ),
            )

            lighting_discipline_ok = all(
                line in img_prompt and line in text_prompt for line in lighting_lines
            )
            record(
                "两种模式都含单一主光/实用灯具/无光晕/曝光纪律句",
                lighting_discipline_ok,
                ",".join(
                    f"{index + 1}={'ok' if line in img_prompt and line in text_prompt else 'missing'}"
                    for index, line in enumerate(lighting_lines)
                ),
            )

            no_openings_analysis = dict(mode_analysis)
            no_openings_analysis["openings"] = []
            fallback_prompt = gui_server.GalleryStore._build_remodel_prompt(
                mode_pack,
                str(no_openings_analysis.get("structure_en") or ""),
                "",
                "1080*1440",
                analysis=no_openings_analysis,
                mode="img2img",
            )
            fallback_ok = (
                "largest visible window" not in fallback_prompt
                and "No exterior window" in fallback_prompt
            )
            record(
                "无开口时不假设可见窗，改走无窗灯光分支",
                fallback_ok,
                (
                    f"largest={'largest visible window' in fallback_prompt}, "
                    f"no_window={'No exterior window' in fallback_prompt}"
                ),
            )

            missing_analysis_prompt = gui_server.GalleryStore._build_remodel_prompt(
                mode_pack,
                str(mode_analysis.get("structure_en") or ""),
                "",
                "1080*1440",
                analysis=None,
                mode="img2img",
            )
            missing_analysis_ok = (
                "largest visible window" not in missing_analysis_prompt
                and "Do not invent a window" in missing_analysis_prompt
            )
            record(
                "缺少结构分析时不假设可见窗",
                missing_analysis_ok,
                (
                    f"largest={'largest visible window' in missing_analysis_prompt}, "
                    f"invent_guard={'Do not invent a window' in missing_analysis_prompt}"
                ),
            )

            left_opening_prompt = gui_server.GalleryStore._build_remodel_prompt(
                mode_pack,
                str(mode_analysis.get("structure_en") or ""),
                "",
                "1080*1440",
                analysis={"openings": [{"side": "left"}]},
                mode="img2img",
            )
            left_opening_ok = (
                "left opening(s)" in left_opening_prompt
                and "Every shadow in the room runs away from that wall."
                in left_opening_prompt
            )
            record(
                "已有开口时保留按实际方位的灯光句",
                left_opening_ok,
                f"left_opening={'left opening(s)' in left_opening_prompt}",
            )

            hard_rule = (
                "HARD RULE: never add, move, resize, remove or invent an opening "
                "of any kind"
            )
            hard_rule_ok = all(
                hard_rule in prompt
                for prompt in (
                    img_prompt,
                    text_prompt,
                    fallback_prompt,
                    missing_analysis_prompt,
                    left_opening_prompt,
                )
            )
            record(
                "所有灯光分支都含禁止增改洞口的硬护栏",
                hard_rule_ok,
                f"hard_rule={hard_rule_ok}",
            )

            # 20o. room-function awareness: the detected room type selects the
            # room's furniture list and palette, but it must not forbid the
            # soft-furnishing replacement the product is for.
            room_style_packs = json.loads(
                gui_server.STYLE_PACKS_PATH.read_text(encoding="utf-8")
            )["styles"]
            midcentury_pack = room_style_packs["中古现代"]
            room_structure = "A rectangular room with one window and one door."

            def furnishing_block(prompt: str) -> str:
                if "FURNISHING AND DECOR" not in prompt:
                    return ""
                return prompt.split("FURNISHING AND DECOR", 1)[1].split("\n\n", 1)[0]

            dining_prompt = gui_server.GalleryStore._build_remodel_prompt(
                midcentury_pack,
                room_structure,
                "",
                "1080*1440",
                mode="img2img",
                room_function="dining",
            )
            dining_furnishing = furnishing_block(dining_prompt)
            dining_ok = (
                "ROOM FUNCTION" in dining_prompt
                and "dining table with four to six chairs" in dining_prompt
                and "sofa" not in dining_furnishing.casefold()
                and "coffee table" not in dining_furnishing.casefold()
                and "lounge chair" not in dining_furnishing.casefold()
            )
            record(
                "餐厅房型提示词使用餐厅家具清单且不出现客厅家具",
                dining_ok,
                f"furnishing={dining_furnishing[:180]!r}",
            )

            living_prompt = gui_server.GalleryStore._build_remodel_prompt(
                midcentury_pack,
                room_structure,
                "",
                "1080*1440",
                mode="img2img",
                room_function="living",
            )
            living_ok = "low channel-tufted rust velvet sofa" in living_prompt
            record(
                "客厅房型提示词保持原有家具文案",
                living_ok,
                f"sofa_plan={'low channel-tufted rust velvet sofa' in living_prompt}",
            )

            unknown_prompt = gui_server.GalleryStore._build_remodel_prompt(
                midcentury_pack,
                room_structure,
                "",
                "1080*1440",
                mode="img2img",
                room_function="unknown",
            )
            unknown_furnishing = furnishing_block(unknown_prompt)
            unknown_ok = (
                "Do not convert the room to another function" in unknown_prompt
                and "sofa" not in unknown_furnishing.casefold()
                and "coffee table" not in unknown_furnishing.casefold()
            )
            record(
                "未知房型只做表面焕新且不点名客厅家具",
                unknown_ok,
                f"furnishing={unknown_furnishing[:180]!r}",
            )

            function_lock_phrases = (
                "Never convert",
                "HARD RULE: never change the room",
                "principal furniture are fixed",
                "do not substitute pieces from another room type",
            )
            room_prompt_samples = {
                "dining": dining_prompt,
                "living": living_prompt,
                "unknown": unknown_prompt,
                "img2img": img_prompt,
                "text": text_prompt,
            }
            lock_hits = {
                name: [
                    phrase
                    for phrase in function_lock_phrases
                    if phrase in prompt
                ]
                for name, prompt in room_prompt_samples.items()
            }
            lock_free_ok = not any(lock_hits.values())
            record(
                "所有房型提示词都不含功能/主家具不可变禁令",
                lock_free_ok,
                f"hits={ {name: hits for name, hits in lock_hits.items() if hits} }",
            )

            palette_hexes = [
                str(entry.get("hex") or "").casefold()
                for entry in midcentury_pack.get("palette") or []
                if isinstance(entry, dict) and entry.get("hex")
            ]
            non_living_palette_count = sum(
                1 for hex_value in palette_hexes if hex_value in dining_prompt.casefold()
            )
            non_living_palette_ok = non_living_palette_count >= 3
            record(
                "非客厅房型提示词带入至少 3 个风格 palette 色值",
                non_living_palette_ok,
                f"palette_hexes={non_living_palette_count}/{len(palette_hexes)}",
            )

            living_palette_ok = "Colour and material palette" not in living_prompt
            record(
                "客厅房型提示词不重复追加 palette 段",
                living_palette_ok,
                f"contains_palette_line={'Colour and material palette' in living_prompt}",
            )

            class _RoomFunctionProbe:
                def __init__(self, vision_result="unknown"):
                    self.vision_result = vision_result
                    self.vision_calls = 0

                def _item_source_path(self, _item):
                    return Path("probe.jpg")

                def _vision_room_function(self, _path):
                    self.vision_calls += 1
                    if isinstance(self.vision_result, BaseException):
                        raise self.vision_result
                    return self.vision_result

                def _cache_room_function(self, item, value, source):
                    item["room_function"] = value
                    item["room_function_source"] = source

            probe = _RoomFunctionProbe()
            analysis_priority = gui_server.GalleryStore._detect_room_function(
                probe,
                {"name": "客厅改造", "structure": {"room_function": "dining"}},
            )
            name_priority = gui_server.GalleryStore._detect_room_function(
                probe,
                {"name": "餐厅改造", "structure": {}},
            )
            unavailable_priority = gui_server.GalleryStore._detect_room_function(
                _RoomFunctionProbe(vision_result=RuntimeError("no key")),
                {"name": "改造项目 abcd1234", "structure": {}},
            )
            cached_item = {"name": "改造项目 abcd1234", "structure": {}}
            cache_probe = _RoomFunctionProbe(vision_result="dining")
            cached_first = gui_server.GalleryStore._detect_room_function(
                cache_probe, cached_item
            )
            cached_second = gui_server.GalleryStore._detect_room_function(
                cache_probe, cached_item
            )
            priority_ok = (
                analysis_priority == "dining"
                and name_priority == "dining"
                and unavailable_priority == "unknown"
                and cached_first == "dining"
                and cached_second == "dining"
                and cache_probe.vision_calls == 1
            )
            record(
                "房型识别优先级：结构分析 > 项目名 > 视觉，且视觉结果缓存",
                priority_ok,
                (
                    f"analysis={analysis_priority}, name={name_priority}, "
                    f"unavailable={unavailable_priority}, "
                    f"vision_calls={cache_probe.vision_calls}"
                ),
            )

            negative_terms = (
                "flat lighting",
                "duplicate light sources",
                "mismatched shadow directions",
                "multiple shadow directions",
                "double shadows",
                "bloom",
                "glow halo around lamps",
                "light spill on ceiling",
                "blown out window",
                "lens flare",
                "mixed white balance",
                "uniform flat materials",
                "perfect symmetry",
                "retouched glossy look",
                "showroom emptiness",
                "cgi render",
                "archviz",
                "hdr",
                "plastic materials",
            )
            negative_lower = gui_server.REMODEL_NEGATIVE.lower()
            negative_ok = all(term in negative_lower for term in negative_terms)
            record(
                "负向词含光晕/泛光/双影/窗过曝/渲染词",
                negative_ok,
                f"missing={[term for term in negative_terms if term not in negative_lower]}",
            )

            required_keys = {"floor", "storage", "lighting", "richness", "realism"}
            weights = getattr(gui_server, "REMODEL_SCORE_WEIGHTS", {})
            check_meta_ok = (
                required_keys.issubset(set(gui_server.REMODEL_CHECK_KEYS))
                and "storage" in gui_server.REMODEL_ARCHITECTURE_KEYS
                and "floor" in gui_server.REMODEL_ARCHITECTURE_KEYS
                and "function" not in gui_server.REMODEL_CHECK_KEYS
                and "function" not in gui_server.REMODEL_ARCHITECTURE_KEYS
                and abs(sum(float(value) for value in weights.values()) - 1.0) < 1e-9
                and float(weights.get("architecture", 0)) == 0.55
                and float(weights.get("lighting", 0)) == 0.15
                and float(weights.get("richness", 0)) == 0.15
                and float(weights.get("realism", 0)) == 0.15
            )
            record(
                "自检含 storage/floor、不含 function 且权重 0.55/0.15/0.15/0.15=1.0",
                check_meta_ok,
                f"keys={list(gui_server.REMODEL_CHECK_KEYS)}, weights={weights}",
            )

            check_prompt = gui_server.GalleryStore._self_check_prompt(
                {
                    "geometry": mode_analysis.get("geometry"),
                    "camera": mode_analysis.get("camera"),
                    "openings": mode_analysis.get("openings"),
                    "framing": mode_analysis.get("framing"),
                    "fixed_features": mode_analysis.get("fixed_features"),
                    "floor_en": mode_analysis.get("floor_en"),
                }
            )
            has_lighting_key = '"key": "lighting"' in check_prompt
            has_storage_key = '"key": "storage"' in check_prompt
            check_prompt_ok = (
                "plank direction, tile grid, herringbone, terrazzo" in check_prompt
                and has_lighting_key
                and has_storage_key
                and '"key": "richness"' in check_prompt
                and '"key": "realism"' in check_prompt
                and "The room is lit by one key light: daylight entering from the left "
                "and front-right" in check_prompt
                and "no visible halo or bloom around fixtures" in check_prompt
                and "no uniform plastic surfaces" in check_prompt
                and "bookshelf, shelf unit or sideboard with visible books" in check_prompt
                and '"key": "function"' not in check_prompt
            )
            function_key_present = '"key": "function"' in check_prompt
            record(
                "自检提示词锁定地板/灯光/储物并含新检查项",
                check_prompt_ok,
                (
                    f"floor={'plank direction' in check_prompt}, "
                    f"lighting={has_lighting_key}, storage={has_storage_key}, "
                    f"daylight={'The room is lit by one key light' in check_prompt}, "
                    f"function_key={function_key_present}"
                ),
            )

            check_prompt_lower = check_prompt.casefold()
            function_free_ok = (
                "- function:" not in check_prompt
                and "room function" not in check_prompt_lower
                and "principal furniture" not in check_prompt_lower
            )
            record(
                "自检不再考核房型与主家具",
                function_free_ok,
                (
                    f"function_key={function_key_present}, "
                    f"room_function={'room function' in check_prompt_lower}, "
                    f"principal={'principal furniture' in check_prompt_lower}"
                ),
            )

            index_html = (ROOT / "gui" / "index.html").read_text(encoding="utf-8")
            remodel_js = (ROOT / "gui" / "remodel.js").read_text(encoding="utf-8")
            ui_ok = (
                "灯光与软装会重新设计，建筑结构不变" in index_html
                and 'id="rm-result-lighting"' in index_html
                and 'id="rm-result-decor"' in index_html
                and "灯光设计" in remodel_js
                and "软装层次" in remodel_js
            )
            record("STEP 03 提示与结果灯光/软装行存在", ui_ok)
        except Exception as exc:
            record(img_prompt_name, False, f"异常：{exc}")
            record(text_prompt_name, False, f"异常：{exc}")
            record("img2img 提示词含灯光方向、软装层次与密度目标", False, f"异常：{exc}")
            record("text 提示词含灯光与软装但不含原图灯光许可", False, f"异常：{exc}")
            record("两种模式都含单一主光/实用灯具/无光晕/曝光纪律句", False, f"异常：{exc}")
            record("无开口分析时灯光方向回退到最大可见窗", False, f"异常：{exc}")
            record("负向词含光晕/泛光/双影/窗过曝/渲染词", False, f"异常：{exc}")
            record("自检含 storage/floor 且权重 0.55/0.15/0.15/0.15=1.0", False, f"异常：{exc}")
            record("自检提示词锁定地板/灯光/储物并含新检查项", False, f"异常：{exc}")
            record("STEP 03 提示与结果灯光/软装行存在", False, f"异常：{exc}")

        # 20o. img2img end to end: job JSON records the mode, the 1280-long-edge
        # input, its JPEG bytes, wall-clock model time, and one image part per
        # request (the user's own photo only).
        status, upload_img2img = multipart_json(
            "/api/remodel/upload", "img2img-mode.png", png_bytes(366, 275), "image/png"
        )
        upload_img2img = upload_img2img if isinstance(upload_img2img, dict) else {}
        status, job_img2img = fetch_json(
            "/api/remodel/generate",
            method="POST",
            payload={
                "upload_id": upload_img2img.get("upload_id", ""),
                "style": style_zh,
                "structure_en": structure_en,
                "mode": "img2img",
                "smoke_check": "high",
            },
        )
        job_img2img = job_img2img if isinstance(job_img2img, dict) else {}
        job_img2img_id = str(job_img2img.get("job_id") or "")
        status_img2img, snapshot_img2img = wait_remodel_job(job_img2img_id)
        snapshot_img2img = (
            snapshot_img2img if isinstance(snapshot_img2img, dict) else {}
        )
        img2img_job_json = {}
        try:
            img2img_job_json = json.loads(
                (Path(remodel_dir) / "jobs" / f"{job_img2img_id}.json").read_text(
                    encoding="utf-8"
                )
            )
        except Exception:
            img2img_job_json = {}
        img2img_parts = (
            img2img_job_json.get("request_parts")
            if isinstance(img2img_job_json.get("request_parts"), list)
            else []
        )
        lighting_summary = snapshot_img2img.get("lighting_summary") or {}
        decor_summary = snapshot_img2img.get("decor_summary") or {}
        ok = (
            status in {200, 202}
            and status_img2img == 200
            and snapshot_img2img.get("status") == "done"
            and job_img2img.get("mode") == "img2img"
            and snapshot_img2img.get("mode") == "img2img"
            and snapshot_img2img.get("strict") is None
            and snapshot_img2img.get("input_long_edge") == 366
            and isinstance(snapshot_img2img.get("input_bytes"), int)
            and int(snapshot_img2img.get("input_bytes") or 0) > 0
            and isinstance(snapshot_img2img.get("model_seconds"), (int, float))
            and img2img_job_json.get("mode") == "img2img"
            and img2img_job_json.get("strict") is None
            and img2img_job_json.get("input_long_edge") == 366
            and img2img_job_json.get("input_bytes")
            == snapshot_img2img.get("input_bytes")
            and len(img2img_parts) == 1
            and img2img_parts[0].get("image_parts") == 1
            and img2img_parts[0].get("text_parts") == 1
            and img2img_parts[0].get("timeout") == gui_server.REMODEL_IMG2IMG_TIMEOUT
            and img2img_parts[0].get("retry_network") is False
            and str(img2img_parts[0].get("image_prefix") or "").startswith(
                "data:image/jpeg;base64,"
            )
            and bool(lighting_summary.get("time_en"))
            and bool(lighting_summary.get("kelvin"))
            and int(decor_summary.get("item_count") or 0) >= 12
            and int(decor_summary.get("surface_count") or 0) >= 3
        )
        record(
            "img2img 任务记录 mode/strict=null/输入长边与字节/单图请求体",
            ok,
            (
                f"status={snapshot_img2img.get('status')}, mode={snapshot_img2img.get('mode')}, "
                f"strict={snapshot_img2img.get('strict')}, "
                f"input={snapshot_img2img.get('input_long_edge')}px/"
                f"{snapshot_img2img.get('input_bytes')}B, "
                f"model_seconds={snapshot_img2img.get('model_seconds')}, "
                f"parts={img2img_parts}"
            ),
        )

        # 20o2. the img2img input long edge is capped at 1024 px and never upscaled.
        status, upload_img2img_large = multipart_json(
            "/api/remodel/upload",
            "img2img-large.png",
            png_bytes(2400, 1600),
            "image/png",
        )
        upload_img2img_large = (
            upload_img2img_large if isinstance(upload_img2img_large, dict) else {}
        )
        status, job_img2img_large = fetch_json(
            "/api/remodel/generate",
            method="POST",
            payload={
                "upload_id": upload_img2img_large.get("upload_id", ""),
                "style": style_zh,
                "structure_en": structure_en,
                "mode": "img2img",
                "smoke_check": "high",
            },
        )
        job_img2img_large = (
            job_img2img_large if isinstance(job_img2img_large, dict) else {}
        )
        job_img2img_large_id = str(job_img2img_large.get("job_id") or "")
        status_img2img_large, snapshot_img2img_large = wait_remodel_job(
            job_img2img_large_id
        )
        snapshot_img2img_large = (
            snapshot_img2img_large
            if isinstance(snapshot_img2img_large, dict)
            else {}
        )
        expected_large_edge = min(2400, gui_server.REMODEL_INPUT_LONG_EDGE)
        ok = (
            status in {200, 202}
            and status_img2img_large == 200
            and snapshot_img2img_large.get("status") == "done"
            and snapshot_img2img_large.get("input_long_edge") == expected_large_edge
            and expected_large_edge == 1024
        )
        record(
            "img2img 大图输入长边截到 1024（不放大）",
            ok,
            (
                f"status={snapshot_img2img_large.get('status')}, "
                f"input={snapshot_img2img_large.get('input_long_edge')}px, "
                f"expected={expected_large_edge}px"
            ),
        )

        # 20o3. a simulated gateway timeout fails the job with the exact message,
        # records model_seconds, and stops after exactly one model call/attempt.
        status, upload_img2img_timeout = multipart_json(
            "/api/remodel/upload",
            "img2img-timeout.png",
            png_bytes(369, 278),
            "image/png",
        )
        upload_img2img_timeout = (
            upload_img2img_timeout if isinstance(upload_img2img_timeout, dict) else {}
        )
        status, job_img2img_timeout = fetch_json(
            "/api/remodel/generate",
            method="POST",
            payload={
                "upload_id": upload_img2img_timeout.get("upload_id", ""),
                "style": style_zh,
                "structure_en": structure_en,
                "mode": "img2img",
                "smoke_check": "timeout",
            },
        )
        job_img2img_timeout = (
            job_img2img_timeout if isinstance(job_img2img_timeout, dict) else {}
        )
        job_img2img_timeout_id = str(job_img2img_timeout.get("job_id") or "")
        status_img2img_timeout, snapshot_img2img_timeout = wait_remodel_job(
            job_img2img_timeout_id
        )
        snapshot_img2img_timeout = (
            snapshot_img2img_timeout
            if isinstance(snapshot_img2img_timeout, dict)
            else {}
        )
        timeout_job_json = {}
        try:
            timeout_job_json = json.loads(
                (Path(remodel_dir) / "jobs" / f"{job_img2img_timeout_id}.json").read_text(
                    encoding="utf-8"
                )
            )
        except Exception:
            timeout_job_json = {}
        timeout_parts = (
            timeout_job_json.get("request_parts")
            if isinstance(timeout_job_json.get("request_parts"), list)
            else []
        )
        ok = (
            status in {200, 202}
            and status_img2img_timeout == 200
            and snapshot_img2img_timeout.get("status") == "error"
            and snapshot_img2img_timeout.get("error") == timeout_message_expected
            and float(snapshot_img2img_timeout.get("model_seconds") or 0) > 0
            and len(timeout_parts) == 1
            and timeout_parts[0].get("timeout") == gui_server.REMODEL_IMG2IMG_TIMEOUT
            and timeout_parts[0].get("retry_network") is False
        )
        record(
            "img2img 超时任务 status=error、文案准确、model_seconds 已记录、仅 1 次调用",
            ok,
            (
                f"status={snapshot_img2img_timeout.get('status')}, "
                f"error={snapshot_img2img_timeout.get('error')!r}, "
                f"model_seconds={snapshot_img2img_timeout.get('model_seconds')}, "
                f"parts={len(timeout_parts)}"
            ),
        )

        # 20p. text mode end to end: zero image parts, no input measurement.
        status, upload_text_mode = multipart_json(
            "/api/remodel/upload", "text-mode.png", png_bytes(367, 276), "image/png"
        )
        upload_text_mode = (
            upload_text_mode if isinstance(upload_text_mode, dict) else {}
        )
        status, job_text_mode = fetch_json(
            "/api/remodel/generate",
            method="POST",
            payload={
                "upload_id": upload_text_mode.get("upload_id", ""),
                "style": style_zh,
                "structure_en": structure_en,
                "mode": "text",
                "smoke_check": "high",
            },
        )
        job_text_mode = job_text_mode if isinstance(job_text_mode, dict) else {}
        job_text_mode_id = str(job_text_mode.get("job_id") or "")
        status_text_mode, snapshot_text_mode = wait_remodel_job(job_text_mode_id)
        snapshot_text_mode = (
            snapshot_text_mode if isinstance(snapshot_text_mode, dict) else {}
        )
        text_mode_job_json = {}
        try:
            text_mode_job_json = json.loads(
                (Path(remodel_dir) / "jobs" / f"{job_text_mode_id}.json").read_text(
                    encoding="utf-8"
                )
            )
        except Exception:
            text_mode_job_json = {}
        text_mode_parts = (
            text_mode_job_json.get("request_parts")
            if isinstance(text_mode_job_json.get("request_parts"), list)
            else []
        )
        ok = (
            status in {200, 202}
            and status_text_mode == 200
            and snapshot_text_mode.get("status") == "done"
            and job_text_mode.get("mode") == "text"
            and snapshot_text_mode.get("mode") == "text"
            and snapshot_text_mode.get("strict") is True
            and snapshot_text_mode.get("input_long_edge") is None
            and snapshot_text_mode.get("input_bytes") is None
            and isinstance(snapshot_text_mode.get("model_seconds"), (int, float))
            and text_mode_job_json.get("mode") == "text"
            and text_mode_job_json.get("strict") is True
            and text_mode_job_json.get("input_long_edge") is None
            and text_mode_job_json.get("input_bytes") is None
            and len(text_mode_parts) == 1
            and text_mode_parts[0].get("image_parts") == 0
            and text_mode_parts[0].get("text_parts") == 1
        )
        record(
            "text 任务记录 mode=text、strict=true、无图片输入与 0 图请求体",
            ok,
            (
                f"status={snapshot_text_mode.get('status')}, mode={snapshot_text_mode.get('mode')}, "
                f"strict={snapshot_text_mode.get('strict')}, parts={text_mode_parts}"
            ),
        )

        # 20q. a low-score img2img check retries once; the repair text is appended
        # and every request still carries exactly the one original-photo image.
        status, upload_img2img_retry = multipart_json(
            "/api/remodel/upload", "img2img-retry.png", png_bytes(368, 277), "image/png"
        )
        upload_img2img_retry = (
            upload_img2img_retry if isinstance(upload_img2img_retry, dict) else {}
        )
        status, job_img2img_retry = fetch_json(
            "/api/remodel/generate",
            method="POST",
            payload={
                "upload_id": upload_img2img_retry.get("upload_id", ""),
                "style": style_zh,
                "structure_en": structure_en,
                "mode": "img2img",
                "smoke_check": "low",
            },
        )
        job_img2img_retry = (
            job_img2img_retry if isinstance(job_img2img_retry, dict) else {}
        )
        job_img2img_retry_id = str(job_img2img_retry.get("job_id") or "")
        status, snapshot_img2img_retry = wait_remodel_job(job_img2img_retry_id)
        snapshot_img2img_retry = (
            snapshot_img2img_retry
            if isinstance(snapshot_img2img_retry, dict)
            else {}
        )
        retry_job_json = {}
        try:
            retry_job_json = json.loads(
                (Path(remodel_dir) / "jobs" / f"{job_img2img_retry_id}.json").read_text(
                    encoding="utf-8"
                )
            )
        except Exception:
            retry_job_json = {}
        retry_parts = (
            retry_job_json.get("request_parts")
            if isinstance(retry_job_json.get("request_parts"), list)
            else []
        )
        retry_prompt = str(snapshot_img2img_retry.get("prompt") or "")
        ok = (
            status in {200, 202}
            and snapshot_img2img_retry.get("status") == "done"
            and snapshot_img2img_retry.get("mode") == "img2img"
            and snapshot_img2img_retry.get("attempts") == 2
            and "Hard corrections, these override other instructions:" in retry_prompt
            and len(retry_parts) == 2
            and all(
                entry.get("image_parts") == 1
                and entry.get("text_parts") == 1
                and str(entry.get("image_prefix") or "").startswith(
                    "data:image/jpeg;base64,"
                )
                for entry in retry_parts
            )
        )
        record(
            "低分 img2img 自查重试一次，修复文本追加且每次请求仍只有原图",
            ok,
            (
                f"status={snapshot_img2img_retry.get('status')}, "
                f"attempts={snapshot_img2img_retry.get('attempts')}, "
                f"parts={retry_parts}"
            ),
        )

        # 20r. history entries carry the mode badge field.
        status, history_modes = fetch_json("/api/remodel/history?limit=50")
        history_modes = history_modes if isinstance(history_modes, list) else []
        history_by_id = {
            str(entry.get("job_id") or ""): entry
            for entry in history_modes
            if isinstance(entry, dict)
        }
        ok = (
            status == 200
            and all(
                entry.get("mode") in {"img2img", "text"} for entry in history_modes
            )
            and history_by_id.get(job_img2img_id, {}).get("mode") == "img2img"
            and history_by_id.get(job_text_mode_id, {}).get("mode") == "text"
        )
        record(
            "历史记录为每条改造结果带 mode 徽标字段",
            ok,
            f"HTTP {status}, count={len(history_modes)}, modes={sorted({entry.get('mode') for entry in history_modes})}",
        )

        # 21. remodel media traversal is rejected, raw and encoded
        raw_remodel_status = None
        try:
            connection = http.client.HTTPConnection(HOST, PORT, timeout=TIMEOUT)
            connection.request("GET", "/remodel/uploads/../jobs/secret.json")
            response = connection.getresponse()
            raw_remodel_status = response.status
            response.read()
            connection.close()
        except Exception:
            raw_remodel_status = None
        encoded_remodel_status, _ctype, _body = fetch("/remodel/uploads/%2e%2e/metadata.json")
        ok = raw_remodel_status in {403, 404} and encoded_remodel_status in {403, 404}
        record(
            "改造媒体路径穿越被拒绝",
            ok,
            f"raw={raw_remodel_status}, encoded={encoded_remodel_status}",
        )

        # 22. crop helper: centre-crop 2400x1800 to an upload ratio of 0.75
        crop_source = Image.new("RGB", (2400, 1800), (120, 120, 120))
        cropped, cropped_changed = gui_server.crop_image_to_aspect(crop_source, 0.75)
        crop_ratio = cropped.width / cropped.height
        ok = (
            cropped_changed is True
            and cropped.size == (1350, 1800)
            and abs(crop_ratio - 0.75) <= 0.004
        )
        record(
            "裁剪辅助函数：2400x1800 + 0.75 → 1350x1800",
            ok,
            f"size={cropped.size}, ratio={crop_ratio:.6f}, changed={cropped_changed}",
        )

        same, same_changed = gui_server.crop_image_to_aspect(crop_source, 2400 / 1800)
        ok = same_changed is False and same.size == (2400, 1800)
        record(
            "裁剪辅助函数：比例一致时不裁剪",
            ok,
            f"size={same.size}, changed={same_changed}",
        )

        # 23. wall-clock hard deadline: a server that accepts the connection but
        # never answers must fail at the budget instead of blocking forever.
        wall_clock_name = "墙钟硬超时：请求超过 deadline 即失败（不依赖 socket）"
        try:
            original_urlopen = gui_server.urlopen

            def _slow_urlopen(_request, timeout=None):
                time.sleep(5)
                return io.BytesIO(b"{}")

            gui_server.urlopen = _slow_urlopen
            wall_clock_started = time.monotonic()
            wall_clock_message = ""
            try:
                gui_server.GalleryStore._request_image(
                    _RequestProbe(),
                    "http://127.0.0.1:1",
                    "test-key",
                    "test prompt",
                    "qwen-image-3.0-pro",
                    "1080*1440",
                    image_data_url="data:image/jpeg;base64,AAAA",
                    timeout=1,
                    retry_network=False,
                )
            except gui_server.RemodelTimeoutError as exc:
                wall_clock_message = str(exc)
            finally:
                gui_server.urlopen = original_urlopen
            wall_clock_elapsed = time.monotonic() - wall_clock_started
            expected_wall_clock_message = (
                "生成接口排队超时（已等待 1 秒），稍后重试或改用纯文本模式"
            )
            ok = (
                wall_clock_message == expected_wall_clock_message
                and wall_clock_elapsed <= 3.0
            )
            record(
                wall_clock_name,
                ok,
                f"elapsed={wall_clock_elapsed:.2f}s, message={wall_clock_message!r}",
            )
        except Exception as exc:
            record(wall_clock_name, False, f"异常：{exc}")

        # 24. analysis and self-check chat calls use the same wall clock and
        # the documented budgets (180 s analysis, 240 s self-check).
        chat_clock_name = "分析/自查调用同样受墙钟预算约束（180s / 240s）"
        try:
            original_urlopen = gui_server.urlopen

            def _slow_chat_urlopen(_request, timeout=None):
                time.sleep(5)
                return io.BytesIO(b"{}")

            gui_server.urlopen = _slow_chat_urlopen
            chat_clock_started = time.monotonic()
            chat_timed_out = False
            try:
                gui_server.GalleryStore._chat_completion(
                    "http://127.0.0.1:1", "test-key", {"model": "qwen4-max"}, timeout=1
                )
            except gui_server.RemodelTimeoutError:
                chat_timed_out = True
            finally:
                gui_server.urlopen = original_urlopen
            chat_clock_elapsed = time.monotonic() - chat_clock_started

            captured_budgets: list[tuple[str, float]] = []

            class _VisionProbe:
                @staticmethod
                def _image_data_url(_path):
                    return "data:image/jpeg;base64,AAAA"

                @staticmethod
                def _chat_completion(_base_url, _key, _payload, timeout=180):
                    captured_budgets.append(("analysis", timeout))
                    raise RuntimeError("stop after budget capture")

                @staticmethod
                def _normalize_analysis(obj):
                    return obj

            try:
                gui_server.GalleryStore._vision_analysis(
                    _VisionProbe(), Path("probe.jpg"), "key", "http://127.0.0.1:1"
                )
            except RuntimeError:
                pass

            class _CheckProbe:
                @staticmethod
                def _check_image_data_url(_path):
                    return "data:image/jpeg;base64,AAAA"

                @staticmethod
                def _read_credentials():
                    return "test-key", "http://127.0.0.1:1"

                @staticmethod
                def _chat_completion(_base_url, _key, _payload, timeout=180):
                    captured_budgets.append(("check", timeout))
                    raise RuntimeError("stop after budget capture")

            try:
                gui_server.GalleryStore._check_call(
                    _CheckProbe(),
                    {},
                    Path("upload.jpg"),
                    Path("result.jpg"),
                    "probe prompt",
                    1,
                    False,
                )
            except RuntimeError:
                pass

            ok = (
                chat_timed_out
                and chat_clock_elapsed <= 3.0
                and gui_server.REMODEL_ANALYSIS_TIMEOUT == 180
                and gui_server.REMODEL_SELF_CHECK_TIMEOUT == 240
                and ("analysis", 180) in captured_budgets
                and ("check", 240) in captured_budgets
            )
            record(
                chat_clock_name,
                ok,
                (
                    f"elapsed={chat_clock_elapsed:.2f}s, timed_out={chat_timed_out}, "
                    f"budgets={captured_budgets}"
                ),
            )
        except Exception as exc:
            record(chat_clock_name, False, f"异常：{exc}")

        # 25. stage telemetry: a finished job records ordered transitions,
        # heartbeat timestamps, and one plain-text log line per transition.
        stage_name = "阶段遥测按 requesting→downloading→saving→checking→done 记录"
        try:
            stage_job_path = Path(remodel_dir) / "jobs" / f"{job_text_mode_id}.json"
            stage_job = json.loads(stage_job_path.read_text(encoding="utf-8"))
            stage_log = stage_job.get("stage_log")
            stage_log = stage_log if isinstance(stage_log, list) else []
            stages = [str(entry.get("stage") or "") for entry in stage_log if isinstance(entry, dict)]
            expected_stages = ["requesting", "downloading", "saving", "checking", "done"]
            cursor = 0
            for stage in stages:
                if cursor < len(expected_stages) and stage == expected_stages[cursor]:
                    cursor += 1
            ordered = cursor == len(expected_stages)
            status_stage, snapshot_stage = fetch_json(
                "/api/remodel/jobs/" + urllib.parse.quote(job_text_mode_id, safe="")
            )
            snapshot_stage = snapshot_stage if isinstance(snapshot_stage, dict) else {}
            stage_seconds = snapshot_stage.get("stage_seconds")
            heartbeat_at = str(snapshot_stage.get("heartbeat_at") or "")
            log_path = Path(remodel_dir) / "gui-service.log"
            log_lines = (
                log_path.read_text(encoding="utf-8").splitlines()
                if log_path.is_file()
                else []
            )
            job8 = job_text_mode_id[:8]
            job_log_lines = [
                line for line in log_lines if line.startswith(f"remodel {job8} ")
            ]
            log_pattern = re.compile(r"^remodel [0-9a-f]{8} [a-z]+ \+\d+(?:\.\d+)?s$")
            log_stages = [
                line.split()[2] for line in job_log_lines if log_pattern.match(line)
            ]
            log_cursor = 0
            for stage in log_stages:
                if log_cursor < len(expected_stages) and stage == expected_stages[log_cursor]:
                    log_cursor += 1
            ok = (
                status_stage == 200
                and snapshot_stage.get("stage") == "done"
                and ordered
                and log_cursor == len(expected_stages)
                and len(job_log_lines) == len(log_stages)
                and isinstance(stage_seconds, (int, float))
                and float(stage_seconds) >= 0
                and heartbeat_at.endswith("Z")
                and heartbeat_at >= str(stage_job.get("created_at") or "")
            )
            record(
                stage_name,
                ok,
                (
                    f"stages={stages}, log_stages={log_stages}, "
                    f"stage_seconds={stage_seconds}, heartbeat_at={heartbeat_at}"
                ),
            )
        except Exception as exc:
            record(stage_name, False, f"异常：{exc}")

        # 26. history includes queued/running/error jobs, not only done ones.
        history_state_name = "历史记录包含 running/error 任务及其 stage 与 error"
        try:
            running_job_id = uuid.uuid4().hex
            error_job_id = uuid.uuid4().hex
            now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            jobs_dir = Path(remodel_dir) / "jobs"
            jobs_dir.mkdir(parents=True, exist_ok=True)
            (jobs_dir / f"{running_job_id}.json").write_text(
                json.dumps(
                    {
                        "job_id": running_job_id,
                        "status": "running",
                        "stage": "requesting",
                        "error": None,
                        "style": "奶油风",
                        "style_en": "cream",
                        "created_at": now_iso,
                        "created_ts": time.time(),
                        "mode": "img2img",
                        "size": "1080*1440",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            error_message = (
                f"生成接口排队超时（已等待 {int(gui_server.REMODEL_IMG2IMG_TIMEOUT)} 秒），"
                "稍后重试或改用纯文本模式"
            )
            (jobs_dir / f"{error_job_id}.json").write_text(
                json.dumps(
                    {
                        "job_id": error_job_id,
                        "status": "error",
                        "stage": "error",
                        "error": error_message,
                        "style": "奶油风",
                        "style_en": "cream",
                        "created_at": now_iso,
                        "created_ts": time.time() + 1,
                        "mode": "text",
                        "size": "1080*1440",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            status_hist, history_state = fetch_json("/api/remodel/history?limit=100")
            history_state = history_state if isinstance(history_state, list) else []
            history_by_id = {
                str(entry.get("job_id") or ""): entry
                for entry in history_state
                if isinstance(entry, dict)
            }
            running_entry = history_by_id.get(running_job_id) or {}
            error_entry = history_by_id.get(error_job_id) or {}
            ok = (
                status_hist == 200
                and running_entry.get("status") == "running"
                and running_entry.get("stage") == "requesting"
                and error_entry.get("status") == "error"
                and error_entry.get("stage") == "error"
                and error_entry.get("error") == error_message
            )
            record(
                history_state_name,
                ok,
                (
                    f"running={running_entry.get('status')}/{running_entry.get('stage')}, "
                    f"error={error_entry.get('status')}/{error_entry.get('stage')}"
                ),
            )
        except Exception as exc:
            record(history_state_name, False, f"异常：{exc}")

        # 27. the reaper fails a running job whose current stage outlived its
        # budget by more than 60 s, and leaves a fresh job alone.
        reaper_name = "reaper 将超预算 running 任务置为 error 并写超时文案"
        try:
            reap_now = 10000.0
            stalled_id = "a" * 32
            fresh_id = "b" * 32
            analyzing_id = "c" * 32
            stalled_job = {
                "kind": "remodel",
                "id": stalled_id,
                "status": "running",
                "mode": "img2img",
                "stage": "requesting",
                "stage_started_mono": reap_now - gui_server.REMODEL_IMG2IMG_TIMEOUT - 61,
                "created_mono": reap_now - 2000,
            }
            fresh_job = {
                "kind": "remodel",
                "id": fresh_id,
                "status": "running",
                "mode": "img2img",
                "stage": "requesting",
                "stage_started_mono": reap_now - 10,
                "created_mono": reap_now - 20,
            }
            analyzing_job = {
                "kind": "remodel",
                "id": analyzing_id,
                "status": "running",
                "mode": "text",
                "stage": "analyzing",
                "stage_started_mono": reap_now - gui_server.REMODEL_ANALYSIS_TIMEOUT - 61,
                "created_mono": reap_now - 2000,
            }
            reap_jobs = {
                stalled_id: stalled_job,
                fresh_id: fresh_job,
                analyzing_id: analyzing_job,
            }
            failed_ids = gui_server.reap_stalled_remodel_jobs(
                reap_jobs, now_mono=reap_now
            )
            expected_error = (
                f"生成接口排队超时（已等待 {int(gui_server.REMODEL_IMG2IMG_TIMEOUT)} 秒），"
                "稍后重试或改用纯文本模式"
            )
            expected_analysis_error = (
                f"分析原图超时（已等待 {int(gui_server.REMODEL_ANALYSIS_TIMEOUT)} 秒），"
                "请稍后重试或改用图生图"
            )
            ok = (
                set(failed_ids) == {stalled_id, analyzing_id}
                and stalled_job.get("status") == "error"
                and stalled_job.get("stage") == "error"
                and stalled_job.get("error") == expected_error
                and analyzing_job.get("status") == "error"
                and analyzing_job.get("stage") == "error"
                and analyzing_job.get("error") == expected_analysis_error
                and fresh_job.get("status") == "running"
            )
            record(
                reaper_name,
                ok,
                (
                    f"failed={failed_ids}, status={stalled_job.get('status')}, "
                    f"error={stalled_job.get('error')!r}, "
                    f"analysis_error={analyzing_job.get('error')!r}"
                ),
            )
        except Exception as exc:
            record(reaper_name, False, f"异常：{exc}")

        # ---------- list-first CRUD + immutable generation history ----------

        # 28. empty list route renders for an empty store
        status, listing = fetch_json("/api/remodel/items")
        ok = (
            status == 200
            and isinstance(listing, dict)
            and listing.get("items") == []
            and listing.get("total") == 0
        )
        record(
            "项目列表空库返回 items=[] 与 total=0",
            ok,
            f"HTTP {status}, body={json.dumps(listing, ensure_ascii=False)[:120]}",
        )

        # 29. migration is idempotent, copies legacy pngs, and deletes nothing
        migration_name = "迁移旧 jobs/uploads：复制为项目，重复运行不重复且不删旧文件"
        try:
            items_root = Path(remodel_dir) / "items"
            before_items = (
                len(list(items_root.glob("*/item.json"))) if items_root.is_dir() else 0
            )
            legacy_uid = uuid.uuid4().hex
            legacy_jid = uuid.uuid4().hex
            legacy_uploads = Path(remodel_dir) / "uploads"
            legacy_jobs = Path(remodel_dir) / "jobs"
            legacy_uploads.mkdir(parents=True, exist_ok=True)
            legacy_jobs.mkdir(parents=True, exist_ok=True)
            legacy_source = png_bytes(320, 240)
            (legacy_uploads / f"{legacy_uid}.png").write_bytes(legacy_source)
            (legacy_uploads / f"{legacy_uid}.json").write_text(
                json.dumps(
                    {
                        "structure_en": "Legacy room with a door and a window.",
                        "openings": [],
                        "immovable_zh": ["旧门洞"],
                        "movable_zh": ["旧沙发"],
                        "original_name": "legacy-room.jpg",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (legacy_jobs / f"{legacy_jid}.json").write_text(
                json.dumps(
                    {
                        "job_id": legacy_jid,
                        "upload_id": legacy_uid,
                        "status": "done",
                        "mode": "text",
                        "style": style_zh,
                        "final_score": 0.88,
                        "size_final": "1080*1440",
                        "model_seconds": 12.5,
                        "seconds": 20.0,
                        "created_at": "2026-01-02T03:04:05Z",
                        "created_ts": time.time() - 100,
                        "width_final": 1080,
                        "height_final": 1440,
                        "prompt": "legacy prompt",
                        "negative": "legacy negative",
                        "attempts": 1,
                        "checks": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (legacy_jobs / f"{legacy_jid}.png").write_bytes(png_bytes(1080, 1440))
            (legacy_jobs / f"{legacy_jid}-r2.png").write_bytes(png_bytes(1080, 1440))
            stats_first = gui_server.migrate_remodel_projects(Path(remodel_dir))
            after_first = len(list(items_root.glob("*/item.json")))
            stats_second = gui_server.migrate_remodel_projects(Path(remodel_dir))
            after_second = len(list(items_root.glob("*/item.json")))
            legacy_item_path = items_root / legacy_uid / "item.json"
            legacy_item = (
                json.loads(legacy_item_path.read_text(encoding="utf-8"))
                if legacy_item_path.is_file()
                else {}
            )
            legacy_gen_pngs = list((items_root / legacy_uid / "gen").glob("*.png"))
            ok = (
                stats_first.get("projects", 0) >= 1
                and stats_first.get("gens", 0) >= 2
                and after_first == before_items + stats_first.get("projects", 0)
                and stats_second.get("projects") == 0
                and after_second == after_first
                and legacy_item.get("legacy_upload_id") == legacy_uid
                and legacy_item.get("name") == "legacy-room"
                and len(legacy_item.get("gens") or []) == 2
                and len(legacy_gen_pngs) == 2
                and (legacy_jobs / f"{legacy_jid}.png").is_file()
                and (legacy_jobs / f"{legacy_jid}-r2.png").is_file()
                and (legacy_uploads / f"{legacy_uid}.png").is_file()
                and not (legacy_item.get("legacy_upload_id") is None)
            )
            record(
                migration_name,
                ok,
                (
                    f"first={stats_first}, second={stats_second}, "
                    f"items={before_items}->{after_first}->{after_second}, "
                    f"legacy_gens={len(legacy_gen_pngs)}"
                ),
            )
        except Exception as exc:
            record(migration_name, False, f"异常：{exc}")

        crud_item_id = ""
        crud_first_gid = ""
        crud_second_gid = ""
        crud_gen_dir = None
        crud_first_png = None
        crud_first_json = None
        crud_first_bytes = b""
        crud_first_json_bytes = b""

        # 30. multipart create is instant and does NOT start background analysis.
        create_name = "list-first 创建项目即时可用且不触发结构分析"
        try:
            status, _ctype, body = multipart_form(
                "/api/remodel/items",
                {"name": "客厅改造"},
                "photo",
                "living.png",
                png_bytes(300, 400),
                "image/png",
            )
            created = json.loads(body.decode("utf-8")) if body else {}
            crud_item_id = str(created.get("item_id") or "")
            ok = (
                status == 201
                and re.fullmatch(r"[0-9a-f]{8,32}", crud_item_id or "") is not None
                and created.get("name") == "客厅改造"
                and str(created.get("source_url") or "").startswith(
                    "/remodel/items/" + crud_item_id + "/source/"
                )
                and created.get("gen_count") == 0
                and created.get("analysis_state") == "none"
                and created.get("analysis") is None
                and created.get("structure") is None
                and not created.get("analysis_error")
            )
            record(
                create_name,
                ok,
                (
                    f"HTTP {status}, item={crud_item_id[:8]}, "
                    f"analysis_state={created.get('analysis_state')!r}, "
                    f"analysis={created.get('analysis')!r}"
                ),
            )
        except Exception as exc:
            record(create_name, False, f"异常：{exc}")

        analysis_name = "创建后 5 秒内不自动分析（无 structure/analyzed_at）"
        try:
            time.sleep(5)
            status, crud_item = fetch_json(
                f"/api/remodel/items/{crud_item_id}"
            )
            crud_item = crud_item if isinstance(crud_item, dict) else {}
            structure = (
                crud_item.get("structure")
                if isinstance(crud_item.get("structure"), dict)
                else {}
            )
            item_path = Path(remodel_dir) / "items" / crud_item_id / "item.json"
            disk_item = (
                json.loads(item_path.read_text(encoding="utf-8"))
                if item_path.is_file()
                else {}
            )
            ok = (
                status == 200
                and not str(structure.get("structure_en") or "").strip()
                and not structure.get("analyzed_at")
                and crud_item.get("analysis_state") == "none"
                and not crud_item.get("analysis_error")
                and not (disk_item.get("structure") or {}).get("analyzed_at")
                and not (disk_item.get("structure") or {}).get("structure_en")
            )
            record(
                analysis_name,
                ok,
                (
                    f"HTTP {status}, analysis_state={crud_item.get('analysis_state')!r}, "
                    f"analyzed_at={structure.get('analyzed_at')!r}"
                ),
            )
        except Exception as exc:
            record(analysis_name, False, f"异常：{exc}")

        # 31. first item generation -> gen/<gid>.png|json
        generate_name = "项目生成写入 gen/<gid>.png|json 且 gen_count=1"
        try:
            status, job = fetch_json(
                f"/api/remodel/items/{crud_item_id}/generate",
                method="POST",
                payload={
                    "mode": "text",
                    "style": style_zh,
                    "prompt_override": "加一个书架",
                    "note": "第一版",
                },
            )
            job = job if isinstance(job, dict) else {}
            job_id = str(job.get("job_id") or "")
            submitted_gid = str(job.get("gen_id") or "")
            status_job, snapshot = wait_remodel_job(job_id)
            crud_first_gid = str(snapshot.get("gen_id") or submitted_gid)
            crud_gen_dir = Path(remodel_dir) / "items" / crud_item_id / "gen"
            crud_first_png = crud_gen_dir / f"{crud_first_gid}.png"
            crud_first_json = crud_gen_dir / f"{crud_first_gid}.json"
            meta = (
                json.loads(crud_first_json.read_text(encoding="utf-8"))
                if crud_first_json.is_file()
                else {}
            )
            job_path = Path(remodel_dir) / "jobs" / f"{job_id}.json"
            job_record = (
                json.loads(job_path.read_text(encoding="utf-8"))
                if job_path.is_file()
                else {}
            )
            stages = [
                str(entry.get("stage") or "")
                for entry in (job_record.get("stage_log") or [])
                if isinstance(entry, dict)
            ]
            analyzing_index = (
                stages.index("analyzing") if "analyzing" in stages else -1
            )
            first_request_index = min(
                [
                    index
                    for index, stage in enumerate(stages)
                    if stage in {"requesting", "repairing", "saving"}
                ]
                or [-1]
            )
            auto_prompt = str(job_record.get("prompt") or meta.get("prompt") or "")
            required = {
                "gid",
                "item_id",
                "created_at",
                "mode",
                "style",
                "model",
                "prompt",
                "negative",
                "score",
                "attempts",
                "size_requested",
                "size_final",
                "width",
                "height",
                "model_seconds",
                "total_seconds",
                "checks",
                "source_sid",
                "job_id",
                "note",
            }
            status_item, item_after = fetch_json(f"/api/remodel/items/{crud_item_id}")
            gens = item_after.get("gens") if isinstance(item_after, dict) else []
            auto_structure = (
                (item_after.get("structure") or {}).get("structure_en")
                if isinstance(item_after, dict)
                and isinstance(item_after.get("structure"), dict)
                else ""
            )
            ok = (
                status == 202
                and bool(job_id)
                and status_job == 200
                and snapshot.get("status") == "done"
                and snapshot.get("stage") == "done"
                and snapshot.get("item_id") == crud_item_id
                and snapshot.get("gen_id") == crud_first_gid
                and bool(str(snapshot.get("heartbeat_at") or ""))
                and analyzing_index >= 0
                and first_request_index > analyzing_index
                and "Architectural shell, fixed and unchangeable:" in auto_prompt
                and crud_first_png.is_file()
                and crud_first_json.is_file()
                and required.issubset(meta.keys())
                and meta.get("item_id") == crud_item_id
                and meta.get("job_id") == job_id
                and meta.get("note") == "第一版"
                and status_item == 200
                and bool(str(auto_structure or "").strip())
                and item_after.get("analysis_state") == "ready"
                and item_after.get("gen_count") == 1
                and isinstance(gens, list)
                and len(gens) == 1
                and gens[0].get("gid") == crud_first_gid
                and gens[0].get("note") == "第一版"
                and str(gens[0].get("url") or "").endswith(f"/gen/{crud_first_gid}.png")
            )
            if crud_first_png.is_file():
                crud_first_bytes = crud_first_png.read_bytes()
            if crud_first_json.is_file():
                crud_first_json_bytes = crud_first_json.read_bytes()
            record(
                generate_name,
                ok,
                (
                    f"HTTP {status}/{status_job}, gid={crud_first_gid[:8]}, "
                    f"keys={len(required.intersection(meta.keys()))}/{len(required)}, "
                    f"gens={len(gens) if isinstance(gens, list) else 'n/a'}, "
                    f"stages={stages}, analysis={bool(str(auto_structure or '').strip())}"
                ),
            )
        except Exception as exc:
            record(generate_name, False, f"异常：{exc}")

        # 31a. img2img on an item with analysis=null must generate without any
        # structure text, dangling header or empty paragraph in the prompt.
        img2img_no_analysis_name = "img2img 无分析可直接生成，提示词无空结构段"
        try:
            status_c, _ctype_c, body_c = multipart_form(
                "/api/remodel/items",
                {"name": "无分析图生图"},
                "photo",
                "no-analysis-img2img.png",
                png_bytes(300, 400),
                "image/png",
            )
            created_i = json.loads(body_c.decode("utf-8")) if body_c else {}
            no_analysis_item_id = str(created_i.get("item_id") or "")
            status_g, job_i = fetch_json(
                f"/api/remodel/items/{no_analysis_item_id}/generate",
                method="POST",
                payload={
                    "mode": "img2img",
                    "style": style_zh,
                    "smoke_check": "high",
                },
            )
            job_i = job_i if isinstance(job_i, dict) else {}
            job_i_id = str(job_i.get("job_id") or "")
            status_job_i, snapshot_i = wait_remodel_job(job_i_id)
            snapshot_i = snapshot_i if isinstance(snapshot_i, dict) else {}
            job_i_path = Path(remodel_dir) / "jobs" / f"{job_i_id}.json"
            job_i_record = (
                json.loads(job_i_path.read_text(encoding="utf-8"))
                if job_i_path.is_file()
                else {}
            )
            prompt_i = str(job_i_record.get("prompt") or "")
            paragraphs = prompt_i.split("\n\n")
            status_item_i, item_i = fetch_json(
                f"/api/remodel/items/{no_analysis_item_id}"
            )
            item_i = item_i if isinstance(item_i, dict) else {}
            ok = (
                status_c == 201
                and bool(no_analysis_item_id)
                and created_i.get("analysis_state") == "none"
                and created_i.get("analysis") is None
                and status_g == 202
                and status_job_i == 200
                and snapshot_i.get("status") == "done"
                and prompt_i
                and "The input photograph shows:" not in prompt_i
                and "Architectural shell" not in prompt_i
                and "\n\n\n" not in prompt_i
                and "  " not in prompt_i
                and all(part.strip() == part and part.strip() for part in paragraphs)
                and "Renovate this exact room photograph." in prompt_i
                and status_item_i == 200
                and item_i.get("analysis_state") == "none"
                and item_i.get("structure") is None
            )
            record(
                img2img_no_analysis_name,
                ok,
                (
                    f"HTTP {status_c}/{status_g}/{status_job_i}, "
                    f"analysis_state={item_i.get('analysis_state')!r}, "
                    f"prompt_chars={len(prompt_i)}, "
                    f"paragraphs={len(paragraphs)}"
                ),
            )
        except Exception as exc:
            record(img2img_no_analysis_name, False, f"异常：{exc}")

        # 31b. generation/source thumbnails: 560 px long edge, JPEG, <400 KB,
        # and API thumb_url points at the thumb while url stays the full PNG.
        thumb_name = "生成/源图缩略图长边 560、<400 KB，API thumb_url 与 url 分离"
        try:
            status_item, item_thumbs = fetch_json(
                f"/api/remodel/items/{crud_item_id}"
            )
            item_thumbs = item_thumbs if isinstance(item_thumbs, dict) else {}
            gens_thumbs = item_thumbs.get("gens") if isinstance(item_thumbs.get("gens"), list) else []
            gen_entry = gens_thumbs[0] if gens_thumbs else {}
            gen_thumb_url = str(gen_entry.get("thumb_url") or "")
            gen_url = str(gen_entry.get("url") or "")
            item_thumb_url = str(item_thumbs.get("thumb_url") or "")
            source_thumb_url = str(
                item_thumbs.get("source_thumb_url")
                or (item_thumbs.get("source") or {}).get("thumb_url")
                or ""
            )
            source_url = str(item_thumbs.get("source_url") or "")

            def _local_thumb(rel_url: str) -> Path | None:
                prefix = f"/remodel/items/{crud_item_id}/"
                if not rel_url.startswith(prefix):
                    return None
                return Path(remodel_dir) / "items" / crud_item_id / rel_url[len(prefix):]

            gen_thumb_path = _local_thumb(gen_thumb_url)
            source_thumb_path = _local_thumb(source_thumb_url)
            item_thumb_path = _local_thumb(item_thumb_url)
            thumb_paths = [gen_thumb_path, source_thumb_path, item_thumb_path]
            thumb_sizes = [p.stat().st_size if p and p.is_file() else -1 for p in thumb_paths]
            thumb_edges = []
            for path in thumb_paths:
                edge = None
                if path and path.is_file():
                    try:
                        with Image.open(path) as opened:
                            edge = max(opened.size)
                    except Exception:
                        edge = None
                thumb_edges.append(edge)
            status_thumb, ctype_thumb, thumb_body = fetch(gen_thumb_url)
            ok = (
                status_item == 200
                and crud_first_gid
                and gen_thumb_url.endswith(f"/thumb/{crud_first_gid}.jpg")
                and gen_url.endswith(f"/gen/{crud_first_gid}.png")
                and item_thumb_url == gen_thumb_url
                and source_thumb_url.endswith(".jpg")
                and "/thumb/src-" in source_thumb_url
                and all(path is not None and path.is_file() for path in thumb_paths)
                and all(0 < size < 400 * 1024 for size in thumb_sizes)
                and all(edge == 560 for edge in thumb_edges)
                and status_thumb == 200
                and ctype_thumb in {"image/jpeg", "image/jpg"}
                and len(thumb_body) > 0
                and source_url.endswith(".png")
            )
            record(
                thumb_name,
                ok,
                (
                    f"gen_thumb={gen_thumb_url}, item_thumb={item_thumb_url}, "
                    f"source_thumb={source_thumb_url}, sizes={thumb_sizes}, "
                    f"edges={thumb_edges}, http={status_thumb}/{ctype_thumb}"
                ),
            )
        except Exception as exc:
            record(thumb_name, False, f"异常：{exc}")

        # 32. second generation prepends and never overwrites the first
        immutability_name = "第二次生成 prepend 且第一版 png/json 字节不变"
        try:
            status, job2 = fetch_json(
                f"/api/remodel/items/{crud_item_id}/generate",
                method="POST",
                payload={
                    "mode": "text",
                    "style": style_zh,
                    "prompt_override": "",
                    "note": "第二版",
                },
            )
            job2 = job2 if isinstance(job2, dict) else {}
            job2_id = str(job2.get("job_id") or "")
            status_job2, snapshot2 = wait_remodel_job(job2_id)
            crud_second_gid = str(snapshot2.get("gen_id") or job2.get("gen_id") or "")
            second_png = crud_gen_dir / f"{crud_second_gid}.png"
            status_item, item_after = fetch_json(f"/api/remodel/items/{crud_item_id}")
            gens = item_after.get("gens") if isinstance(item_after, dict) else []
            first_png_now = crud_first_png.read_bytes() if crud_first_png.is_file() else b""
            first_json_now = (
                crud_first_json.read_bytes() if crud_first_json.is_file() else b""
            )
            ok = (
                status == 202
                and status_job2 == 200
                and snapshot2.get("status") == "done"
                and crud_second_gid
                and crud_second_gid != crud_first_gid
                and second_png.is_file()
                and crud_first_png.is_file()
                and crud_first_json.is_file()
                and first_png_now == crud_first_bytes
                and first_json_now == crud_first_json_bytes
                and item_after.get("gen_count") == 2
                and isinstance(gens, list)
                and len(gens) == 2
                and gens[0].get("gid") == crud_second_gid
                and gens[1].get("gid") == crud_first_gid
                and gens[0].get("note") == "第二版"
            )
            record(
                immutability_name,
                ok,
                (
                    f"gids={crud_first_gid[:8]}/{crud_second_gid[:8]}, "
                    f"first_png_unchanged={first_png_now == crud_first_bytes}, "
                    f"first_json_unchanged={first_json_now == crud_first_json_bytes}"
                ),
            )
        except Exception as exc:
            record(immutability_name, False, f"异常：{exc}")

        # 32b. replacing the source invalidates the stored analysis instead of
        # reusing it, keeps source history, and does not auto-analyze.
        replace_name = "替换原图清空旧分析、保留原图历史且不自动分析"
        try:
            status_before, item_before = fetch_json(
                f"/api/remodel/items/{crud_item_id}"
            )
            item_before = item_before if isinstance(item_before, dict) else {}
            old_sid = str(item_before.get("source_current") or "")
            old_ids = list(item_before.get("source_ids") or [])
            status_r, _ctype_r, body_r = multipart_form(
                f"/api/remodel/items/{crud_item_id}/source",
                {},
                "photo",
                "replacement-room.png",
                png_bytes(320, 240),
                "image/png",
            )
            replaced = json.loads(body_r.decode("utf-8")) if body_r else {}
            replaced = replaced if isinstance(replaced, dict) else {}
            new_sid = str(replaced.get("source_current") or "")
            time.sleep(5)
            status_after, item_after_replace = fetch_json(
                f"/api/remodel/items/{crud_item_id}"
            )
            item_after_replace = (
                item_after_replace if isinstance(item_after_replace, dict) else {}
            )
            structure_after = (
                item_after_replace.get("structure")
                if isinstance(item_after_replace.get("structure"), dict)
                else {}
            )
            ok = (
                status_before == 200
                and status_r == 200
                and status_after == 200
                and bool(old_sid)
                and bool(new_sid)
                and new_sid != old_sid
                and replaced.get("source_current") == new_sid
                and replaced.get("structure") is None
                and replaced.get("analysis") is None
                and replaced.get("analysis_state") == "none"
                and not replaced.get("analysis_error")
                and new_sid in list(replaced.get("source_ids") or [])
                and old_sid in list(replaced.get("source_ids") or [])
                and set(list(replaced.get("source_ids") or []))
                == set(old_ids + [new_sid])
                and not str(structure_after.get("structure_en") or "").strip()
                and not structure_after.get("analyzed_at")
                and item_after_replace.get("analysis_state") == "none"
                and not item_after_replace.get("analysis_error")
            )
            record(
                replace_name,
                ok,
                (
                    f"HTTP {status_r}/{status_after}, sid={old_sid[:8]}->{new_sid[:8]}, "
                    f"source_ids={len(replaced.get('source_ids') or [])}, "
                    f"analysis_state={item_after_replace.get('analysis_state')!r}"
                ),
            )
        except Exception as exc:
            record(replace_name, False, f"异常：{exc}")

        # 33. rename + cover + note + list summary fields
        patch_name = "PATCH 重命名/封面/备注，列表返回封面与统计字段"
        try:
            status, patched = fetch_json(
                f"/api/remodel/items/{crud_item_id}",
                method="PATCH",
                payload={"name": "客厅改造 · 定稿", "cover_gen": crud_first_gid},
            )
            patched = patched if isinstance(patched, dict) else {}
            status_note, note_gen = fetch_json(
                f"/api/remodel/items/{crud_item_id}/gens/{crud_first_gid}",
                method="PATCH",
                payload={"note": "封面备注"},
            )
            note_gen = note_gen if isinstance(note_gen, dict) else {}
            status_list, listing_after = fetch_json("/api/remodel/items?limit=100")
            entries = listing_after.get("items") if isinstance(listing_after, dict) else []
            entry = next(
                (
                    row
                    for row in entries
                    if isinstance(row, dict) and row.get("item_id") == crud_item_id
                ),
                {},
            )
            ok = (
                status == 200
                and patched.get("name") == "客厅改造 · 定稿"
                and patched.get("cover_gen") == crud_first_gid
                and str(patched.get("cover_url") or "").endswith(
                    f"/gen/{crud_first_gid}.png"
                )
                and status_note == 200
                and note_gen.get("note") == "封面备注"
                and status_list == 200
                and entry.get("cover_gen") == crud_first_gid
                and str(entry.get("cover_url") or "").endswith(
                    f"/gen/{crud_first_gid}.png"
                )
                and entry.get("gen_count") == 2
                and isinstance(entry.get("styles"), list)
                and len(entry.get("styles") or []) >= 1
                and entry.get("default_mode") in {"img2img", "text"}
                and bool(entry.get("default_style"))
                and str(entry.get("source_url") or "").startswith(
                    "/remodel/items/" + crud_item_id + "/source/"
                )
                and entry.get("running") is False
                and entry.get("running_job") is None
            )
            record(
                patch_name,
                ok,
                (
                    f"HTTP {status}/{status_note}/{status_list}, "
                    f"cover={entry.get('cover_gen')}, gens={entry.get('gen_count')}"
                ),
            )
        except Exception as exc:
            record(patch_name, False, f"异常：{exc}")

        # 33b. default names: analysis room type + generation style, while a
        # user-chosen name is never touched by later generations.
        naming_name = "默认项目名取分析房型与生成风格，用户重命名后不再改动"
        named_item_id = ""
        try:
            status_u, named_upload = multipart_json(
                "/api/remodel/upload", "named-room.png", png_bytes(320, 240), "image/png"
            )
            named_upload = named_upload if isinstance(named_upload, dict) else {}
            named_upload_id = str(named_upload.get("upload_id") or "")
            named_sidecar = {
                "schema_version": gui_server.REMODEL_SCHEMA_VERSION,
                "room_type_en": "living room",
                "geometry": {"room_type_en": "living room"},
                "structure_en": "Living room with a flat ceiling and one left doorway.",
                "immovable_zh": ["左墙门洞"],
                "movable_zh": ["沙发"],
            }
            (
                Path(remodel_dir) / "uploads" / f"{named_upload_id}.json"
            ).write_text(
                json.dumps(named_sidecar, ensure_ascii=False), encoding="utf-8"
            )
            status_c, named_created = fetch_json(
                "/api/remodel/items",
                method="POST",
                payload={"upload_id": named_upload_id},
            )
            named_created = named_created if isinstance(named_created, dict) else {}
            named_item_id = str(named_created.get("item_id") or "")
            created_name = str(named_created.get("name") or "")
            status_analysis, named_item = fetch_json(
                f"/api/remodel/items/{named_item_id}"
            )
            named_item = named_item if isinstance(named_item, dict) else {}

            status_g1, named_job1 = fetch_json(
                f"/api/remodel/items/{named_item_id}/generate",
                method="POST",
                payload={"mode": "text", "style": style_zh},
            )
            named_job1 = named_job1 if isinstance(named_job1, dict) else {}
            wait_remodel_job(str(named_job1.get("job_id") or ""))
            status_n1, named_after_gen = fetch_json(
                f"/api/remodel/items/{named_item_id}"
            )
            named_after_gen = named_after_gen if isinstance(named_after_gen, dict) else {}
            auto_name = str(named_after_gen.get("name") or "")

            status_r, renamed = fetch_json(
                f"/api/remodel/items/{named_item_id}",
                method="PATCH",
                payload={"name": "我的客厅"},
            )
            renamed = renamed if isinstance(renamed, dict) else {}
            status_g2, named_job2 = fetch_json(
                f"/api/remodel/items/{named_item_id}/generate",
                method="POST",
                payload={"mode": "text", "style": style_zh},
            )
            named_job2 = named_job2 if isinstance(named_job2, dict) else {}
            wait_remodel_job(str(named_job2.get("job_id") or ""))
            status_n2, named_final = fetch_json(
                f"/api/remodel/items/{named_item_id}"
            )
            named_final = named_final if isinstance(named_final, dict) else {}
            ok = (
                status_u == 200
                and status_c == 201
                and bool(named_item_id)
                and created_name == "客厅 · 未指定风格"
                and named_created.get("renamed_by_user") is False
                and named_created.get("analysis_state") == "none"
                and status_analysis == 200
                and not str(
                    (named_item.get("structure") or {}).get("structure_en") or ""
                ).strip()
                and status_g1 == 202
                and status_n1 == 200
                and str(
                    (named_after_gen.get("structure") or {}).get("structure_en") or ""
                ).strip()
                and auto_name == f"客厅 · {style_zh}"
                and named_after_gen.get("renamed_by_user") is False
                and status_r == 200
                and renamed.get("renamed_by_user") is True
                and status_g2 == 202
                and status_n2 == 200
                and named_final.get("name") == "我的客厅"
                and named_final.get("renamed_by_user") is True
            )
            record(
                naming_name,
                ok,
                (
                    f"created={created_name!r}, auto={auto_name!r}, "
                    f"final={named_final.get('name')!r}, "
                    f"renamed={named_final.get('renamed_by_user')}"
                ),
            )

            prompt_gens = (
                named_final.get("gens")
                if isinstance(named_final.get("gens"), list)
                else []
            )
            latest_prompt_gen = prompt_gens[0] if prompt_gens else {}
            prompt_chars = len(str(latest_prompt_gen.get("prompt") or ""))
            negative_chars = len(str(latest_prompt_gen.get("negative") or ""))
            ok = (
                status_n2 == 200
                and len(prompt_gens) >= 2
                and prompt_chars > 500
                and negative_chars > 0
            )
            record(
                "多版本项目详情首条 gen 返回完整 prompt 与非空 negative",
                ok,
                (
                    f"HTTP {status_n2}, versions={len(prompt_gens)}, "
                    f"prompt_chars={prompt_chars}, negative_chars={negative_chars}"
                ),
            )
        except Exception as exc:
            record(naming_name, False, f"异常：{exc}")
            record(
                "多版本项目详情首条 gen 返回完整 prompt 与非空 negative",
                False,
                f"异常：{exc}",
            )

        # 33c. a timed-out job stays in the item history as status=error with
        # no png; a text retry is a brand-new generation.
        failed_job_name = "超时任务留在历史为 status=error 且纯文本重试生成新版本"
        try:
            status_t, timeout_job = fetch_json(
                f"/api/remodel/items/{named_item_id}/generate",
                method="POST",
                payload={
                    "mode": "img2img",
                    "style": style_zh,
                    "smoke_check": "timeout",
                },
            )
            timeout_job = timeout_job if isinstance(timeout_job, dict) else {}
            timeout_job_id = str(timeout_job.get("job_id") or "")
            status_tj, timeout_snapshot = wait_remodel_job(timeout_job_id)
            timeout_snapshot = timeout_snapshot if isinstance(timeout_snapshot, dict) else {}
            status_item_t, item_with_failure = fetch_json(
                f"/api/remodel/items/{named_item_id}"
            )
            item_with_failure = (
                item_with_failure if isinstance(item_with_failure, dict) else {}
            )
            failed_jobs = (
                item_with_failure.get("failed_jobs")
                if isinstance(item_with_failure.get("failed_jobs"), list)
                else []
            )
            failed_entry = next(
                (
                    entry
                    for entry in failed_jobs
                    if isinstance(entry, dict)
                    and entry.get("job_id") == timeout_job_id
                ),
                {},
            )
            status_h, history_with_failure = fetch_json("/api/remodel/history?limit=100")
            history_with_failure = (
                history_with_failure if isinstance(history_with_failure, list) else []
            )
            history_entry = next(
                (
                    entry
                    for entry in history_with_failure
                    if isinstance(entry, dict)
                    and entry.get("job_id") == timeout_job_id
                ),
                {},
            )
            gens_before_retry = (
                item_with_failure.get("gens")
                if isinstance(item_with_failure.get("gens"), list)
                else []
            )
            status_retry, retry_job = fetch_json(
                f"/api/remodel/items/{named_item_id}/generate",
                method="POST",
                payload={"mode": "text", "style": style_zh},
            )
            retry_job = retry_job if isinstance(retry_job, dict) else {}
            status_rj, retry_snapshot = wait_remodel_job(
                str(retry_job.get("job_id") or "")
            )
            retry_snapshot = retry_snapshot if isinstance(retry_snapshot, dict) else {}
            status_item_r, item_after_retry = fetch_json(
                f"/api/remodel/items/{named_item_id}"
            )
            item_after_retry = (
                item_after_retry if isinstance(item_after_retry, dict) else {}
            )
            gens_after_retry = (
                item_after_retry.get("gens")
                if isinstance(item_after_retry.get("gens"), list)
                else []
            )
            failed_after = (
                item_after_retry.get("failed_jobs")
                if isinstance(item_after_retry.get("failed_jobs"), list)
                else []
            )
            ok = (
                status_t == 202
                and status_tj == 200
                and timeout_snapshot.get("status") == "error"
                and "排队超时" in str(timeout_snapshot.get("error") or "")
                and status_item_t == 200
                and failed_entry.get("status") == "error"
                and failed_entry.get("url") is None
                and failed_entry.get("style") == style_zh
                and "排队超时" in str(failed_entry.get("error") or "")
                and status_h == 200
                and history_entry.get("status") == "error"
                and status_retry == 202
                and status_rj == 200
                and retry_snapshot.get("status") == "done"
                and retry_snapshot.get("mode") == "text"
                and status_item_r == 200
                and len(gens_after_retry) == len(gens_before_retry) + 1
                and any(
                    isinstance(entry, dict)
                    and entry.get("job_id") == timeout_job_id
                    for entry in failed_after
                )
            )
            record(
                failed_job_name,
                ok,
                (
                    f"timeout={timeout_snapshot.get('status')}, "
                    f"failed_entry={failed_entry.get('status')}/{failed_entry.get('url')}, "
                    f"gens={len(gens_before_retry)}->{len(gens_after_retry)}, "
                    f"retry={retry_snapshot.get('status')}/{retry_snapshot.get('mode')}"
                ),
            )
        except Exception as exc:
            record(failed_job_name, False, f"异常：{exc}")

        # 33d. thumbnail backfill is idempotent across the whole store.
        backfill_name = "POST /api/remodel/thumbnails/backfill 幂等且重建缺失缩略图"
        try:
            named_item_dir = Path(remodel_dir) / "items" / named_item_id
            named_thumb_dir = named_item_dir / "thumb"
            removed = 0
            if named_thumb_dir.is_dir():
                for path in named_thumb_dir.glob("*.jpg"):
                    path.unlink()
                    removed += 1
            status_b1, backfill_first = fetch_json(
                "/api/remodel/thumbnails/backfill", method="POST", payload={}
            )
            backfill_first = backfill_first if isinstance(backfill_first, dict) else {}
            status_b2, backfill_second = fetch_json(
                "/api/remodel/thumbnails/backfill", method="POST", payload={}
            )
            backfill_second = backfill_second if isinstance(backfill_second, dict) else {}
            rebuilt = list(named_thumb_dir.glob("*.jpg")) if named_thumb_dir.is_dir() else []
            ok = (
                status_b1 == 200
                and status_b2 == 200
                and removed >= 2
                and int(backfill_first.get("created") or 0) >= removed
                and int(backfill_first.get("errors") or 0) == 0
                and int(backfill_second.get("created") or 0) == 0
                and int(backfill_second.get("skipped") or 0) >= removed
                and len(rebuilt) >= removed
            )
            record(
                backfill_name,
                ok,
                (
                    f"removed={removed}, first={backfill_first}, "
                    f"second={backfill_second}, rebuilt={len(rebuilt)}"
                ),
            )
        except Exception as exc:
            record(backfill_name, False, f"异常：{exc}")

        # 33e. list/detail frontend contract: thumbs, pagination, fresh reads,
        # history filters and the text-retry button.
        frontend_name = "列表/详情前端使用 thumb_url、24 条分页、刷新与超时重试按钮"
        try:
            remodel_js = (ROOT / "gui" / "remodel.js").read_text(encoding="utf-8")
            remodel_html = (ROOT / "gui" / "index.html").read_text(encoding="utf-8")
            ok = (
                "limit=${state.pageSize}" in remodel_js
                or "limit=24" in remodel_js
            ) and (
                "加载更多" in remodel_js
                and "版本历史" in remodel_html
                and "data-gen-filter" in remodel_js
                and "visibilitychange" in remodel_js
                and "freshUrl(" in remodel_js
                and "模型排队中，图生图通常 3-6 分钟，最长约 15 分钟" in remodel_js
                and "改用纯文本重试" in remodel_js
                and "thumb_url" in remodel_js
                and "failed_jobs" in remodel_js
            )
            record(frontend_name, ok)
        except Exception as exc:
            record(frontend_name, False, f"异常：{exc}")

        # 33e2. the served detail page contains the current-version prompt panel.
        current_prompt_name = "首页 HTML 含当前结果生图提示词面板"
        try:
            status_home, _ctype_home, body_home = fetch("/")
            home_html = body_home.decode("utf-8", "replace")
            ok = status_home == 200 and 'id="rm-current-prompt"' in home_html
            record(
                current_prompt_name,
                ok,
                f"HTTP {status_home}, html_chars={len(home_html)}",
            )
        except Exception as exc:
            record(current_prompt_name, False, f"异常：{exc}")

        # 33f. TASK18 create-dialog photo preview: interaction contract driven
        # through Chrome CDP (synthetic change/drop events, object-URL lifecycle).
        preview_name = "新建项目对话框：照片预览、拖放、校验与对象 URL 生命周期"
        try:
            if chrome_binary():
                ok, detail = preview_interaction_contract()
            else:
                ok, detail = preview_source_contract()
                detail += "（Chrome 不可用，降级源码检查）"
            record(preview_name, ok, detail)
        except Exception as exc:
            record(preview_name, False, f"异常：{exc}")

        # 33f2. TASK19 detail-image lightbox: rendered detail page, fit/1:1 size,
# ArrowRight wrap, Esc/focus restore, history thumbnails and list thumbnails.
        lightbox_name = "详情图片灯箱：无分析文案/打开/1:1/方向键/Esc，列表缩略图仍导航"
        lightbox_item_id = ""
        try:
            status_lc, _ctype_lc, body_lc = multipart_form(
                "/api/remodel/items",
                {"name": "灯箱测试"},
                "photo",
                "lightbox-room.png",
                png_bytes(300, 400),
                "image/png",
            )
            created_lb = json.loads(body_lc.decode("utf-8")) if body_lc else {}
            created_lb = created_lb if isinstance(created_lb, dict) else {}
            lightbox_item_id = str(created_lb.get("item_id") or "")
            generated = []
            for index in (1, 2):
                status_g, job_lb = fetch_json(
                    f"/api/remodel/items/{lightbox_item_id}/generate",
                    method="POST",
                    payload={
                        "mode": "img2img",
                        "style": style_zh,
                        "note": f"灯箱版本 {index}",
                        "smoke_check": "high",
                    },
                )
                job_lb = job_lb if isinstance(job_lb, dict) else {}
                status_job, snapshot_lb = wait_remodel_job(
                    str(job_lb.get("job_id") or "")
                )
                generated.append(
                    status_g == 202
                    and status_job == 200
                    and isinstance(snapshot_lb, dict)
                    and snapshot_lb.get("status") == "done"
                )
            status_lb_item, lightbox_item = fetch_json(
                f"/api/remodel/items/{lightbox_item_id}"
            )
            lightbox_item = (
                lightbox_item if isinstance(lightbox_item, dict) else {}
            )
            if chrome_binary():
                ok, detail, _out = lightbox_interaction_contract(lightbox_item_id)
                list_ok, list_detail = list_navigation_contract()
                ok = ok and list_ok
                detail = detail + "；列表点击=" + list_detail
            else:
                ok = False
                detail = "Chrome 不可用，无法验证灯箱交互"
            ok = (
                ok
                and status_lc == 201
                and bool(lightbox_item_id)
                and all(generated)
                and status_lb_item == 200
                and lightbox_item.get("analysis_state") == "none"
                and lightbox_item.get("structure") is None
                and lightbox_item.get("gen_count") == 2
            )
            record(lightbox_name, ok, detail)
        except Exception as exc:
            record(lightbox_name, False, f"异常：{exc}")

        if lightbox_item_id:
            try:
                fetch_json(
                    f"/api/remodel/items/{lightbox_item_id}?purge=1",
                    method="DELETE",
                )
            except Exception:
                pass

        # 33g. TASK16 history-card contract, checked against Chrome-rendered DOM
        # (with a source-level fallback when no Chrome binary is installed).
        history_card_name = "历史卡片：单一尺寸/单一⋯菜单/紧凑失败态/头部失败计数"
        try:
            status_cards, card_item = fetch_json(
                f"/api/remodel/items/{named_item_id}"
            )
            card_item = card_item if isinstance(card_item, dict) else {}
            versions = len(card_item.get("gens") or [])
            failures = len(card_item.get("failed_jobs") or [])
            dom = chrome_dom(
                f"{BASE}/#remodel/{urllib.parse.quote(named_item_id, safe='')}"
            )
            if dom:
                ok, detail = rendered_history_contract(dom, versions, failures)
            else:
                ok, detail = source_history_contract()
            ok = status_cards == 200 and bool(named_item_id) and ok
            record(history_card_name, ok, detail)
        except Exception as exc:
            record(history_card_name, False, f"异常：{exc}")

        if named_item_id:
            try:
                fetch_json(
                    f"/api/remodel/items/{named_item_id}?purge=1", method="DELETE"
                )
            except Exception:
                pass

        # 34. delete one generation only
        delete_gen_name = "DELETE 单版本只移除该 png+json 并从 gens 中移除"
        try:
            status, _payload = fetch_json(
                f"/api/remodel/items/{crud_item_id}/gens/{crud_second_gid}",
                method="DELETE",
            )
            status_item, item_after = fetch_json(f"/api/remodel/items/{crud_item_id}")
            gens = item_after.get("gens") if isinstance(item_after, dict) else []
            ok = (
                status == 200
                and not (crud_gen_dir / f"{crud_second_gid}.png").exists()
                and not (crud_gen_dir / f"{crud_second_gid}.json").exists()
                and crud_first_png.is_file()
                and status_item == 200
                and item_after.get("gen_count") == 1
                and isinstance(gens, list)
                and len(gens) == 1
                and gens[0].get("gid") == crud_first_gid
            )
            record(
                delete_gen_name,
                ok,
                f"HTTP {status}, remaining={item_after.get('gen_count')}",
            )
        except Exception as exc:
            record(delete_gen_name, False, f"异常：{exc}")

        # 35. soft delete moves the whole project directory into trash
        soft_delete_name = "DELETE 项目默认移入 trash 且详情 404"
        try:
            status, payload = fetch_json(
                f"/api/remodel/items/{crud_item_id}", method="DELETE"
            )
            payload = payload if isinstance(payload, dict) else {}
            trash_dir = Path(remodel_dir) / "trash" / crud_item_id
            item_dir = Path(remodel_dir) / "items" / crud_item_id
            status_get, _missing = fetch_json(f"/api/remodel/items/{crud_item_id}")
            ok = (
                status == 200
                and payload.get("purged") is False
                and trash_dir.is_dir()
                and not item_dir.exists()
                and status_get == 404
            )
            record(
                soft_delete_name,
                ok,
                f"HTTP {status}, trash={trash_dir.is_dir()}, get={status_get}",
            )
        except Exception as exc:
            record(soft_delete_name, False, f"异常：{exc}")

        # 36. JSON create from an upload + purge removes the directory
        purge_name = "JSON upload_id 创建项目 + ?purge=1 永久删除目录"
        try:
            status_u, upload_json = multipart_json(
                "/api/remodel/upload",
                "json-create.png",
                png_bytes(320, 240),
                "image/png",
            )
            upload_json = upload_json if isinstance(upload_json, dict) else {}
            status_c, created_json = fetch_json(
                "/api/remodel/items",
                method="POST",
                payload={
                    "name": "JSON 项目",
                    "upload_id": upload_json.get("upload_id", ""),
                },
            )
            created_json = created_json if isinstance(created_json, dict) else {}
            purge_id = str(created_json.get("item_id") or "")
            status_p, payload = fetch_json(
                f"/api/remodel/items/{purge_id}?purge=1", method="DELETE"
            )
            payload = payload if isinstance(payload, dict) else {}
            ok = (
                status_u == 200
                and status_c == 201
                and bool(purge_id)
                and created_json.get("name") == "JSON 项目"
                and str(created_json.get("source_url") or "").startswith(
                    "/remodel/items/" + purge_id + "/source/"
                )
                and status_p == 200
                and payload.get("purged") is True
                and not (Path(remodel_dir) / "items" / purge_id).exists()
                and not (Path(remodel_dir) / "trash" / purge_id).exists()
            )
            record(
                purge_name,
                ok,
                f"HTTP {status_u}/{status_c}/{status_p}, id={purge_id[:8]}",
            )
        except Exception as exc:
            record(purge_name, False, f"异常：{exc}")

        # 37. path traversal / malformed ids are rejected with 400 JSON errors
        traversal_name = "项目接口拒绝路径穿越与非法 ID（400 JSON error）"
        try:
            traversal_status, traversal_body = fetch_json(
                "/api/remodel/items/..%2f..%2fmetadata.json"
            )
            patch_status, patch_body = fetch_json(
                "/api/remodel/items/zz", method="PATCH", payload={"name": "x"}
            )
            generate_status, generate_body = fetch_json(
                "/api/remodel/items/..%2f..%2fetc/generate",
                method="POST",
                payload={"mode": "text", "style": style_zh},
            )
            delete_status, delete_body = fetch_json(
                "/api/remodel/items/..%2f..%2fetc", method="DELETE"
            )
            bodies = [
                traversal_body,
                patch_body,
                generate_body,
                delete_body,
            ]
            ok = (
                traversal_status == 400
                and patch_status == 400
                and generate_status == 400
                and delete_status == 400
                and all(
                    isinstance(body, dict)
                    and isinstance(body.get("error"), dict)
                    and body["error"].get("code")
                    for body in bodies
                )
            )
            record(
                traversal_name,
                ok,
                (
                    f"statuses={traversal_status}/{patch_status}/"
                    f"{generate_status}/{delete_status}"
                ),
            )
        except Exception as exc:
            record(traversal_name, False, f"异常：{exc}")

        # 38. unknown fields are rejected with 400
        unknown_name = "PATCH/生成拒绝未声明字段（400）"
        try:
            status_patch, body_patch = fetch_json(
                f"/api/remodel/items/{legacy_uid}",
                method="PATCH",
                payload={"bogus": 1},
            )
            status_generate, body_generate = fetch_json(
                f"/api/remodel/items/{legacy_uid}/generate",
                method="POST",
                payload={
                    "mode": "text",
                    "style": style_zh,
                    "bogus": "x",
                },
            )
            ok = (
                status_patch == 400
                and status_generate == 400
                and isinstance(body_patch, dict)
                and isinstance(body_patch.get("error"), dict)
                and isinstance(body_generate, dict)
                and isinstance(body_generate.get("error"), dict)
            )
            record(
                unknown_name,
                ok,
                f"HTTP {status_patch}/{status_generate}",
            )
        except Exception as exc:
            record(unknown_name, False, f"异常：{exc}")

        # 38a. "Add details" option. Enabled jobs must carry the English
        # INTERIOR DETAILS section (translated, no CJK); disabled jobs must
        # keep the prompt byte-identical to the prompt without the option.
        detail_item_id = ""
        detail_enabled_name = "添加细节：提示词含 INTERIOR DETAILS、英文翻译与 Requested details，且无中文"
        detail_snapshot_name = "添加细节：job 状态回传 detail_enabled/detail_zh"
        detail_disabled_name = "添加细节关闭：提示词不含 INTERIOR DETAILS 且与原提示词逐字符一致"
        detail_validation_name = "添加细节参数校验：非法布尔值与超长文本返回 400"
        try:
            detail_zh = "几盆绿植、两幅挂画、叠搭地毯"
            detail_status, _detail_ctype, detail_body = multipart_form(
                "/api/remodel/items",
                {"name": "添加细节测试"},
                "photo",
                "detail-option.png",
                png_bytes(300, 400),
                "image/png",
            )
            detail_created = (
                json.loads(detail_body.decode("utf-8")) if detail_body else {}
            )
            detail_item_id = str(detail_created.get("item_id") or "")
            gen_status, detail_job = fetch_json(
                f"/api/remodel/items/{detail_item_id}/generate",
                method="POST",
                payload={
                    "mode": "img2img",
                    "style": style_zh,
                    "detail_enabled": True,
                    "detail_zh": detail_zh,
                    "smoke_check": "high",
                },
            )
            detail_job = detail_job if isinstance(detail_job, dict) else {}
            detail_job_id = str(detail_job.get("job_id") or "")
            detail_job_status, detail_snapshot = wait_remodel_job(detail_job_id)
            detail_snapshot = (
                detail_snapshot if isinstance(detail_snapshot, dict) else {}
            )
            detail_job_path = Path(remodel_dir) / "jobs" / f"{detail_job_id}.json"
            detail_job_record = (
                json.loads(detail_job_path.read_text(encoding="utf-8"))
                if detail_job_path.is_file()
                else {}
            )
            detail_prompt = str(detail_job_record.get("prompt") or "")
            expected_detail_en = (
                "Integrate the requested addition into the furnishing plan."
            )
            detail_gid = str(
                detail_snapshot.get("gen_id") or detail_job.get("gen_id") or ""
            )
            detail_meta_path = (
                Path(remodel_dir)
                / "items"
                / detail_item_id
                / "gen"
                / f"{detail_gid}.json"
            )
            detail_meta = (
                json.loads(detail_meta_path.read_text(encoding="utf-8"))
                if detail_meta_path.is_file()
                else {}
            )
            previews = [
                detail_prompt.find(marker)
                for marker in (
                    "FURNISHING AND DECOR",
                    "INTERIOR DETAILS (allowed additions)",
                    "Everything in the original room except the architectural shell",
                )
            ]
            enabled_ok = (
                detail_status == 201
                and gen_status == 202
                and detail_job_status == 200
                and detail_snapshot.get("status") == "done"
                and "INTERIOR DETAILS (allowed additions)" in detail_prompt
                and expected_detail_en in " ".join(detail_prompt.split())
                and "Requested details:" in detail_prompt
                and gui_server.CJK_RE.search(detail_prompt) is None
                and all(index >= 0 for index in previews)
                and previews == sorted(previews)
                and detail_meta.get("detail_enabled") is True
                and detail_meta.get("detail_zh") == detail_zh
            )
            record(
                detail_enabled_name,
                enabled_ok,
                (
                    f"HTTP {detail_status}/{gen_status}/{detail_job_status}, "
                    f"section={'INTERIOR DETAILS' in detail_prompt}, "
                    f"requested={'Requested details:' in detail_prompt}, "
                    f"cjk={bool(gui_server.CJK_RE.search(detail_prompt))}, "
                    f"meta={detail_meta.get('detail_enabled')!r}/"
                    f"{detail_meta.get('detail_zh')!r}"
                ),
            )
            snapshot_ok = (
                detail_job_status == 200
                and detail_snapshot.get("detail_enabled") is True
                and detail_snapshot.get("detail_zh") == detail_zh
            )
            record(
                detail_snapshot_name,
                snapshot_ok,
                (
                    f"detail_enabled={detail_snapshot.get('detail_enabled')!r}, "
                    f"detail_zh={detail_snapshot.get('detail_zh')!r}"
                ),
            )

            disabled_status, disabled_job = fetch_json(
                f"/api/remodel/items/{detail_item_id}/generate",
                method="POST",
                payload={
                    "mode": "img2img",
                    "style": style_zh,
                    "detail_enabled": False,
                    "detail_zh": "",
                    "smoke_check": "high",
                },
            )
            disabled_job = disabled_job if isinstance(disabled_job, dict) else {}
            disabled_job_id = str(disabled_job.get("job_id") or "")
            disabled_job_status, disabled_snapshot = wait_remodel_job(
                disabled_job_id
            )
            disabled_snapshot = (
                disabled_snapshot if isinstance(disabled_snapshot, dict) else {}
            )
            disabled_job_path = (
                Path(remodel_dir) / "jobs" / f"{disabled_job_id}.json"
            )
            disabled_job_record = (
                json.loads(disabled_job_path.read_text(encoding="utf-8"))
                if disabled_job_path.is_file()
                else {}
            )
            disabled_prompt = str(disabled_job_record.get("prompt") or "")
            baseline_pack = {
                "style_en": "cream",
                "style_dna_en": "warm plaster walls and pale oak floors",
                "furniture_plan_en": "a low sofa on the long wall",
                "lighting_plan_en": "Soft daylight at 5200 K with warm practicals at 2700 K.",
                "layering_plan_en": (
                    "Seating: a cream boucle sofa; Textiles: a wool rug, "
                    "boucle cushions; Greenery: a potted olive tree. "
                    "Density target: at least 12 distinct furnishing and decor "
                    "items, at least 3 styled surfaces."
                ),
            }
            baseline_prompt = gui_server.GalleryStore._build_remodel_prompt(
                baseline_pack,
                "A rectangular room with one window and one door.",
                "",
                "1080*1440",
                mode="img2img",
            )
            flagged_prompt = gui_server.GalleryStore._build_remodel_prompt(
                baseline_pack,
                "A rectangular room with one window and one door.",
                "",
                "1080*1440",
                mode="img2img",
                detail_enabled=False,
                detail_en="a vase",
            )
            disabled_ok = (
                disabled_status == 202
                and disabled_job_status == 200
                and disabled_snapshot.get("status") == "done"
                and disabled_prompt
                and "INTERIOR DETAILS" not in disabled_prompt
                and "Requested details:" not in disabled_prompt
                and baseline_prompt == flagged_prompt
            )
            record(
                detail_disabled_name,
                disabled_ok,
                (
                    f"HTTP {disabled_status}/{disabled_job_status}, "
                    f"section={'INTERIOR DETAILS' in disabled_prompt}, "
                    f"baseline_equal={baseline_prompt == flagged_prompt}"
                ),
            )

            bad_bool_status, bad_bool_body = fetch_json(
                f"/api/remodel/items/{detail_item_id}/generate",
                method="POST",
                payload={
                    "mode": "img2img",
                    "style": style_zh,
                    "detail_enabled": "yes",
                },
            )
            too_long_status, too_long_body = fetch_json(
                f"/api/remodel/items/{detail_item_id}/generate",
                method="POST",
                payload={
                    "mode": "img2img",
                    "style": style_zh,
                    "detail_enabled": True,
                    "detail_zh": "细" * 301,
                },
            )
            validation_ok = (
                bad_bool_status == 400
                and too_long_status == 400
                and isinstance(bad_bool_body, dict)
                and isinstance(bad_bool_body.get("error"), dict)
                and isinstance(too_long_body, dict)
                and isinstance(too_long_body.get("error"), dict)
            )
            record(
                detail_validation_name,
                validation_ok,
                f"HTTP {bad_bool_status}/{too_long_status}",
            )
        except Exception as exc:
            record(detail_enabled_name, False, f"异常：{exc}")
            record(detail_snapshot_name, False, f"异常：{exc}")
            record(detail_disabled_name, False, f"异常：{exc}")
            record(detail_validation_name, False, f"异常：{exc}")
        finally:
            if detail_item_id:
                try:
                    fetch_json(
                        f"/api/remodel/items/{detail_item_id}?purge=1",
                        method="DELETE",
                    )
                except Exception:
                    pass

        # 38b. Detail optimize: newest result becomes the img2img input and a
        # new, additive version is written with a refine prompt.
        refine_empty_name = "细节优化：无结果图返回 400 且文案匹配"
        refine_job_name = "细节优化：base=current 且 refine_of 指向最新结果版本"
        refine_prompt_name = "细节优化：REFINE PASS、无中文、含 Requested details"
        refine_item_id = ""
        refine_empty_id = ""
        try:
            empty_status, empty_created = multipart_json(
                "/api/remodel/items",
                "refine-empty.png",
                png_bytes(300, 400),
                "image/png",
            )
            empty_created = (
                empty_created if isinstance(empty_created, dict) else {}
            )
            refine_empty_id = str(empty_created.get("item_id") or "")
            empty_http, empty_body = fetch_json(
                f"/api/remodel/items/{refine_empty_id}/detail-optimize",
                method="POST",
                payload={"detail_zh": ""},
            )
            empty_error = (
                empty_body.get("error")
                if isinstance(empty_body, dict)
                else None
            )
            empty_message = (
                str(empty_error.get("message") or "")
                if isinstance(empty_error, dict)
                else ""
            )
            record(
                refine_empty_name,
                empty_status == 201
                and empty_http == 400
                and empty_message == "还没有生成结果，无法做细节优化",
                f"HTTP {empty_status}/{empty_http}, message={empty_message!r}",
            )

            created_status, created = multipart_json(
                "/api/remodel/items",
                "refine-source.png",
                png_bytes(300, 400),
                "image/png",
            )
            created = created if isinstance(created, dict) else {}
            refine_item_id = str(created.get("item_id") or "")
            source_gid = uuid.uuid4().hex
            item_path = (
                Path(remodel_dir) / "items" / refine_item_id / "item.json"
            )
            gen_dir = Path(remodel_dir) / "items" / refine_item_id / "gen"
            gen_dir.mkdir(parents=True, exist_ok=True)
            (gen_dir / f"{source_gid}.png").write_bytes(png_bytes(300, 400))
            forged_meta = {
                "gid": source_gid,
                "item_id": refine_item_id,
                "created_at": "2026-11-18T12:00:00Z",
                "mode": "img2img",
                "style": style_zh,
                "width": 300,
                "height": 400,
                "note": "伪造结果",
            }
            (gen_dir / f"{source_gid}.json").write_text(
                json.dumps(forged_meta, ensure_ascii=False),
                encoding="utf-8",
            )
            item_data = json.loads(item_path.read_text(encoding="utf-8"))
            item_data["gens"] = [source_gid]
            item_path.write_text(
                json.dumps(item_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            refine_detail_zh = "几盆绿植、两幅挂画"
            opt_status, opt_job = fetch_json(
                f"/api/remodel/items/{refine_item_id}/detail-optimize",
                method="POST",
                payload={"detail_zh": refine_detail_zh},
            )
            opt_job = opt_job if isinstance(opt_job, dict) else {}
            opt_job_id = str(opt_job.get("job_id") or "")
            job_http, snapshot = wait_remodel_job(opt_job_id)
            snapshot = snapshot if isinstance(snapshot, dict) else {}
            refine_gid = str(
                snapshot.get("gen_id") or opt_job.get("gen_id") or ""
            )
            refine_meta_path = gen_dir / f"{refine_gid}.json"
            refine_meta = (
                json.loads(refine_meta_path.read_text(encoding="utf-8"))
                if refine_meta_path.is_file()
                else {}
            )
            added_job_ok = (
                created_status == 201
                and opt_status == 202
                and job_http == 200
                and snapshot.get("status") == "done"
                and snapshot.get("base") == "current"
                and snapshot.get("refine_of") == source_gid
                and opt_job.get("base") == "current"
                and opt_job.get("refine_of") == source_gid
                and refine_gid
                and refine_gid != source_gid
                and refine_meta.get("refine_of") == source_gid
                and refine_meta.get("detail_enabled") is True
                and refine_meta.get("detail_zh") == refine_detail_zh
                and refine_meta.get("note") == "细节优化"
                and (gen_dir / f"{source_gid}.png").is_file()
            )
            record(
                refine_job_name,
                added_job_ok,
                (
                    f"HTTP {opt_status}/{job_http}, base={snapshot.get('base')!r}, "
                    f"refine_of={snapshot.get('refine_of')!r}, "
                    f"source={source_gid[:8]}, new={refine_gid[:8]}"
                ),
            )
            prompt = str(snapshot.get("prompt") or "")
            prompt_ok = (
                "REFINE PASS" in prompt
                and gui_server.CJK_RE.search(prompt) is None
                and "Requested details:" in prompt
                and "Keep everything already in the image unchanged" in prompt
            )
            record(
                refine_prompt_name,
                prompt_ok,
                (
                    f"len={len(prompt)}, refine_pass={'REFINE PASS' in prompt}, "
                    f"requested={'Requested details:' in prompt}, "
                    f"cjk={bool(gui_server.CJK_RE.search(prompt))}"
                ),
            )
        except Exception as exc:
            record(refine_empty_name, False, f"异常：{exc}")
            record(refine_job_name, False, f"异常：{exc}")
            record(refine_prompt_name, False, f"异常：{exc}")
        finally:
            for cleanup_id in (refine_item_id, refine_empty_id):
                if cleanup_id:
                    try:
                        fetch_json(
                            f"/api/remodel/items/{cleanup_id}?purge=1",
                            method="DELETE",
                        )
                    except Exception:
                        pass

        # 38c. Supplementary requirement handling: a bare room word selects the
        # room type without going through translation; an explicit room word
        # outranks the project name and structure analysis; an invented
        # translation falls back to the verbatim Chinese; and the gen record
        # exposes both the original and the English form.
        room_extra_shortcut_name = "补充要求只写房型词时直接采用房型且不调翻译"
        room_extra_priority_name = "房型词优先级高于项目名与结构分析"
        room_extra_verbatim_name = "翻译出现可疑扩写时退回原文"
        room_extra_gen_name = "gen 记录保存补充要求原文与译文"
        room_extra_item_id = ""
        room_priority_item_id = ""
        try:
            extra_status, _extra_ctype, extra_body = multipart_form(
                "/api/remodel/items",
                {"name": "补充要求测试"},
                "photo",
                "room-extra.png",
                png_bytes(300, 400),
                "image/png",
            )
            extra_created = (
                json.loads(extra_body.decode("utf-8")) if extra_body else {}
            )
            room_extra_item_id = str(extra_created.get("item_id") or "")
            gen_status, extra_job = fetch_json(
                f"/api/remodel/items/{room_extra_item_id}/generate",
                method="POST",
                payload={
                    "mode": "img2img",
                    "style": style_zh,
                    "prompt_override": "餐厅",
                },
            )
            extra_job = extra_job if isinstance(extra_job, dict) else {}
            extra_job_id = str(extra_job.get("job_id") or "")
            extra_snapshot_status, extra_snapshot = wait_remodel_job(extra_job_id)
            extra_snapshot = (
                extra_snapshot if isinstance(extra_snapshot, dict) else {}
            )
            extra_prompt = str(extra_snapshot.get("prompt") or "")
            shortcut_ok = (
                extra_status == 201
                and gen_status == 202
                and extra_snapshot_status == 200
                and extra_snapshot.get("status") == "done"
                and extra_snapshot.get("room_function") == "dining"
                and "This room is a dining room" in extra_prompt
                and "Additional requirement: dining room" in extra_prompt
                and "sofa" not in extra_prompt.casefold()
                and "Add a sideboard" not in extra_prompt
                and "verbatim Chinese" not in extra_prompt
            )
            record(
                room_extra_shortcut_name,
                shortcut_ok,
                (
                    f"room={extra_snapshot.get('room_function')!r}, "
                    f"dining_rule={'This room is a dining room' in extra_prompt}, "
                    f"requirement={'Additional requirement: dining room' in extra_prompt}, "
                    f"sofa={'sofa' in extra_prompt.casefold()}"
                ),
            )

            extra_gid = str(
                extra_snapshot.get("gen_id") or extra_job.get("gen_id") or ""
            )
            extra_meta_path = (
                Path(remodel_dir)
                / "items"
                / room_extra_item_id
                / "gen"
                / f"{extra_gid}.json"
            )
            extra_meta = (
                json.loads(extra_meta_path.read_text(encoding="utf-8"))
                if extra_meta_path.is_file()
                else {}
            )
            gen_ok = (
                extra_meta.get("extra_zh") == "餐厅"
                and extra_meta.get("extra_en") == "dining room"
                and extra_meta.get("extra_verbatim") is False
                and extra_meta.get("room_function") == "dining"
            )
            record(
                room_extra_gen_name,
                gen_ok,
                (
                    f"extra_zh={extra_meta.get('extra_zh')!r}, "
                    f"extra_en={extra_meta.get('extra_en')!r}, "
                    f"verbatim={extra_meta.get('extra_verbatim')!r}"
                ),
            )

            # Priority: the item name says living and the analysis says living,
            # but the explicit supplementary room word must win.
            prio_status, _prio_ctype, prio_body = multipart_form(
                "/api/remodel/items",
                {"name": "客厅改造"},
                "photo",
                "room-priority.png",
                png_bytes(300, 400),
                "image/png",
            )
            prio_created = json.loads(prio_body.decode("utf-8")) if prio_body else {}
            room_priority_item_id = str(prio_created.get("item_id") or "")
            prio_item_path = (
                Path(remodel_dir) / "items" / room_priority_item_id / "item.json"
            )
            prio_item = (
                json.loads(prio_item_path.read_text(encoding="utf-8"))
                if prio_item_path.is_file()
                else {}
            )
            prio_item["structure"] = {
                "room_function": "living",
                "structure_en": "Living room with a sofa and a coffee table.",
            }
            prio_item["room_function"] = "living"
            prio_item["room_function_source"] = "analysis"
            gui_server.atomic_write_json(prio_item_path, prio_item)
            prio_gen_status, prio_job = fetch_json(
                f"/api/remodel/items/{room_priority_item_id}/generate",
                method="POST",
                payload={
                    "mode": "img2img",
                    "style": style_zh,
                    "prompt_override": "餐厅",
                },
            )
            prio_job = prio_job if isinstance(prio_job, dict) else {}
            prio_job_id = str(prio_job.get("job_id") or "")
            prio_snapshot_status, prio_snapshot = wait_remodel_job(prio_job_id)
            prio_snapshot = prio_snapshot if isinstance(prio_snapshot, dict) else {}
            prio_prompt = str(prio_snapshot.get("prompt") or "")
            prio_item_after = (
                json.loads(prio_item_path.read_text(encoding="utf-8"))
                if prio_item_path.is_file()
                else {}
            )
            priority_ok = (
                prio_status == 201
                and prio_gen_status == 202
                and prio_snapshot_status == 200
                and prio_snapshot.get("status") == "done"
                and prio_snapshot.get("room_function") == "dining"
                and "This room is a dining room" in prio_prompt
                and prio_item_after.get("room_function") == "dining"
                and prio_item_after.get("room_function_source") == "extra"
            )
            record(
                room_extra_priority_name,
                priority_ok,
                (
                    f"analysis={prio_item.get('structure', {}).get('room_function')!r}, "
                    f"name=living, final={prio_snapshot.get('room_function')!r}, "
                    f"source={prio_item_after.get('room_function_source')!r}"
                ),
            )

            # A model that invents a longer request must not reach the prompt.
            class _ExpansionProbe(gui_server.GalleryStore):
                def __init__(self):
                    pass

                def _read_credentials(self):
                    return "smoke-key", "http://127.0.0.1:1"

                def _chat_completion(self, *_args, **_kwargs):
                    return {
                        "choices": [
                            {
                                "message": {
                                    "content": (
                                        "Add a sideboard to the dining room and "
                                        "completely redesign all the wall decor."
                                    )
                                }
                            }
                        ]
                    }

            probe = _ExpansionProbe()
            previous_smoke_mode = gui_server.SMOKE_MODE
            gui_server.SMOKE_MODE = False
            try:
                verbatim_text, verbatim_flag = probe._translate_extra_to_english("餐厅")
            finally:
                gui_server.SMOKE_MODE = previous_smoke_mode
            room_packs = json.loads(
                gui_server.STYLE_PACKS_PATH.read_text(encoding="utf-8")
            )["styles"]
            verbatim_prompt = gui_server.GalleryStore._build_remodel_prompt(
                room_packs[style_zh],
                "A rectangular room with one window and one door.",
                gui_server.EXTRA_VERBATIM_PREFIX + verbatim_text,
                "1080*1440",
                mode="img2img",
                room_function="dining",
                extra_verbatim=True,
            )
            cjk_guard_kept = False
            try:
                gui_server.GalleryStore._build_remodel_prompt(
                    room_packs[style_zh],
                    "A rectangular room with one window and one door.",
                    gui_server.EXTRA_VERBATIM_PREFIX + verbatim_text,
                    "1080*1440",
                    mode="img2img",
                    room_function="dining",
                )
            except ValueError:
                cjk_guard_kept = True
            verbatim_ok = (
                verbatim_text == "餐厅"
                and verbatim_flag is True
                and "verbatim Chinese" in verbatim_prompt
                and "餐厅" in verbatim_prompt
                and "Add a sideboard" not in verbatim_prompt
                and cjk_guard_kept
            )
            record(
                room_extra_verbatim_name,
                verbatim_ok,
                (
                    f"text={verbatim_text!r}, verbatim={verbatim_flag!r}, "
                    f"marker={'verbatim Chinese' in verbatim_prompt}, "
                    f"cjk_guard={cjk_guard_kept}"
                ),
            )
        except Exception as exc:
            record(room_extra_shortcut_name, False, f"异常：{exc}")
            record(room_extra_priority_name, False, f"异常：{exc}")
            record(room_extra_verbatim_name, False, f"异常：{exc}")
            record(room_extra_gen_name, False, f"异常：{exc}")
        finally:
            for cleanup_id in (room_extra_item_id, room_priority_item_id):
                if cleanup_id:
                    try:
                        fetch_json(
                            f"/api/remodel/items/{cleanup_id}?purge=1",
                            method="DELETE",
                        )
                    except Exception:
                        pass

        # 39. TASK21 credentials API + real settings modal.
        try:
            for name, passed, detail in credentials_contract(
                credentials_path, Path(env["HOMAI_SERVICE_LOG"])
            ):
                record(name, passed, detail)
        except Exception as exc:
            record("TASK21 凭据设置", False, f"异常：{exc}")

    finally:
        if credentials_backup is None:
            try:
                credentials_path.unlink()
            except FileNotFoundError:
                pass
        else:
            credentials_path.write_bytes(credentials_backup)
            if credentials_mode is not None:
                os.chmod(credentials_path, credentials_mode)
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        shutil.rmtree(remodel_dir, ignore_errors=True)

    failed = [name for name, ok, _detail in results if not ok]
    print()
    print(f"结果：{len(results) - len(failed)} 项通过，{len(failed)} 项失败")
    if failed:
        for name in failed:
            print("FAILED:", name)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())