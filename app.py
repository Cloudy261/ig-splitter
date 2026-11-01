#!/usr/bin/env python3
"""IG Splitter: cut wide photos into seamless Instagram carousel tiles.

Run `python3 app.py`; the UI opens at http://localhost:8765.

Environment (used by the Docker image):
  IG_SPLITTER_LISTEN                      address to listen on, set by the Docker image only;
                                          without it the app is only reachable from this computer
  IG_SPLITTER_INPUT / IG_SPLITTER_OUTPUT  fix the folders (the UI can't change them then);
                                          mandatory when listening beyond localhost
  IG_SPLITTER_HOSTS                       extra host names the UI may be opened with,
                                          comma-separated (e.g. "192.168.1.20,photos.local")
  IG_SPLITTER_PASSWORD_HASH               require a login (create it with --hash-password);
                                          mandatory when listening beyond localhost
  IG_SPLITTER_LOGIN                       "on" (default) or "off": off never asks for a
                                          password, also on the network
"""

import argparse
import functools
import getpass
import io
import json
import mimetypes
import os
import re
import subprocess
import sys
import traceback
import webbrowser
import zipfile
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import auth
import splitter

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
CONFIG_FILE = ROOT / "config.json"
DEFAULT_CONFIG = {
    "input_dir": "~/Pictures/ig-splitter/input",
    "output_dir": "~/Pictures/ig-splitter/output",
}
FOLDER_ENV = {"input_dir": "IG_SPLITTER_INPUT", "output_dir": "IG_SPLITTER_OUTPUT"}
PASSWORD_ENV = "IG_SPLITTER_PASSWORD_HASH"
LOGIN_ENV = "IG_SPLITTER_LOGIN"
LISTEN_ENV = "IG_SPLITTER_LISTEN"
LOGIN_VALUES = {"on": True, "true": True, "yes": True, "1": True, "off": False, "false": False, "no": False, "0": False}
LOOPBACK = ("127.0.0.1", "localhost", "::1")
OPEN_PATHS = {"/", "/api/health", "/api/session", "/api/login"}  # everything else needs a login


def fixed_folders() -> dict:
    return {key: os.environ[env] for key, env in FOLDER_ENV.items() if os.environ.get(env)}


def load_config() -> dict:
    try:
        saved = json.loads(CONFIG_FILE.read_text())
    except FileNotFoundError:
        saved = {}
    return {**DEFAULT_CONFIG, **saved, **fixed_folders()}


def save_config(changes: dict) -> dict:
    if fixed_folders():
        raise ValueError("The folders are fixed by the server configuration")
    config = load_config()
    for key in DEFAULT_CONFIG:
        if key in changes:
            value = str(changes[key]).strip()
            if key == "input_dir" and not Path(value).expanduser().is_dir():
                raise ValueError(f"Input folder not found: {value}")
            config[key] = value
    CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")
    return config


def public_config() -> dict:
    local_mac = sys.platform == "darwin" and not fixed_folders()
    return {**load_config(), "locked": bool(fixed_folders()), "can_reveal": local_mac, "can_pick": local_mac}


def folder(key: str) -> Path:
    return Path(load_config()[key]).expanduser()


def input_file(name: str) -> Path:
    path = folder("input_dir") / name
    if Path(name).name != name or not path.is_file():
        raise ValueError(f"No image named {name!r} in the input folder")
    return path


def export_dir(name: str) -> Path:
    """Folder for the tiles of photo `name`, always directly inside the output folder."""
    base = folder("output_dir").resolve()
    path = (base / Path(name).stem).resolve()
    if Path(name).name != name or path.parent != base:  # e.g. ".." or "...jpg"
        raise ValueError(f"Not a valid photo name: {name!r}")
    return path


def output_file(name: str, file: str) -> Path:
    # Only tiles of this photo: the output folder can point anywhere, so never serve other files from it.
    stem, suffix = Path(name).stem, Path(name).suffix
    tile = re.fullmatch(rf"{re.escape(stem)}_\d+of\d+{re.escape(suffix)}", file)
    path = export_dir(name) / file
    if suffix.lower() not in splitter.SUFFIXES or not tile or not path.is_file():
        raise ValueError(f"No exported tile named {file!r}")
    return path


def list_images() -> dict:
    directory = folder("input_dir")
    if not directory.is_dir():
        return {"dir": str(directory), "images": [], "missing": True}
    images = []
    files = sorted(directory.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files:
        if path.suffix.lower() in splitter.SUFFIXES and not path.name.startswith("."):
            try:
                images.append(splitter.image_info(path))
            except Exception:
                continue  # unreadable or not really an image
    return {"dir": str(directory), "images": images}


def save_upload(name: str, data: bytes) -> str:
    """Store an uploaded photo in the input folder without touching its bytes."""
    name = Path(name).name
    stem, suffix = Path(name).stem, Path(name).suffix
    if suffix.lower() not in splitter.SUFFIXES or name.startswith("."):
        raise ValueError(f"{name}: only JPEG and PNG files can be added")
    directory = folder("input_dir")
    target, i = directory / name, 1
    while target.exists():
        target, i = directory / f"{stem}-{i}{suffix}", i + 1
    target.write_bytes(data)
    try:
        splitter.image_info(target)
    except Exception:
        target.unlink()
        raise ValueError(f"{name} is not a readable JPEG or PNG")
    return target.name


def pick_folder(start: Path) -> str | None:
    """Open the native Finder folder dialog on this Mac; None if cancelled."""
    default = f" default location (POSIX file {json.dumps(str(start), ensure_ascii=False)})" if start.is_dir() else ""
    script = f'POSIX path of (choose folder with prompt "Choose a folder"{default})'
    proc = subprocess.run(["osascript", "-e", "activate", "-e", script], capture_output=True, text=True)
    if proc.returncode:  # cancelled
        return None
    return proc.stdout.strip().rstrip("/") or "/"


def login_gate(host: str) -> auth.Auth:
    """The server's login, from the environment; exits on an invalid or unsafe setup."""
    setting = (os.environ.get(LOGIN_ENV) or "on").strip().lower()
    if setting not in LOGIN_VALUES:
        sys.exit(f"{LOGIN_ENV} must be on or off, not {setting!r}")
    if not LOGIN_VALUES[setting]:
        return auth.Auth(None)
    try:
        gate = auth.Auth(os.environ.get(PASSWORD_ENV))
    except ValueError as e:
        sys.exit(f"{PASSWORD_ENV}: {e}")
    if not gate.enabled and host not in LOOPBACK:
        sys.exit(f"Refusing to listen on {host} without a login. Run `python3 app.py --hash-password` and put "
                 f"the result into {PASSWORD_ENV}, or turn the login off with {LOGIN_ENV}=off.")
    return gate


@functools.lru_cache(maxsize=128)
def cached_preview(path: str, mtime: int, size: int) -> bytes:
    return splitter.preview(Path(path), size)


class Handler(BaseHTTPRequestHandler):
    timeout = 60  # seconds a connection may stay silent before it's dropped

    def do_GET(self):
        url = urlparse(self.path)
        params = parse_qs(url.query)
        query = {k: v[0] for k, v in params.items()}
        if not self._trusted() or not self._logged_in(url.path):
            return
        try:
            if url.path == "/":
                return self._file(STATIC / "index.html")
            if url.path == "/api/health":
                return self._json({"ok": True})
            if url.path == "/api/session":
                return self._json({"auth": self.server.auth.enabled, "logged_in": self.server.auth.valid(self._token())})
            if url.path.startswith("/static/"):
                return self._file(STATIC / url.path.removeprefix("/static/"))
            if url.path == "/api/config":
                return self._json(public_config())
            if url.path == "/api/images":
                return self._json(list_images())
            if url.path == "/api/image":
                return self._json(splitter.image_info(input_file(query["name"])))
            if url.path == "/api/preview":
                path = input_file(query["name"])
                data = cached_preview(str(path), path.stat().st_mtime_ns, int(query.get("size", 3000)))
                return self._send(200, data, "image/jpeg")
            if url.path == "/api/download":
                path = output_file(query["name"], query["file"])
                return self._attachment(path.read_bytes(), path.name)
            if url.path == "/api/zip":
                archive = io.BytesIO()
                with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as z:  # stored: bytes stay untouched
                    for file in params.get("file", []):
                        z.write(output_file(query["name"], file), file)
                return self._attachment(archive.getvalue(), f"{Path(query['name']).stem}.zip")
            self._send(404, b"Not found", "text/plain")
        except Exception as e:
            self._error(e)

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._trusted() or not self._logged_in(path):
            return
        # Only content types that browsers can't send cross-site without a CORS preflight,
        # which this server never grants.
        expected = "application/octet-stream" if path == "/api/upload" else "application/json"
        if self.headers.get("Content-Type", "").split(";")[0].strip() != expected:
            return self._send(415, b"Unsupported content type", "text/plain")
        length = self.headers.get("Content-Length", "0").strip()
        if not (length.isascii() and length.isdigit()):  # a negative length would read until the client stops
            return self._send(400, b"Invalid Content-Length", "text/plain")
        length = int(length)
        if path != "/api/upload" and length > 65536:  # /api/login is open, keep its bodies small
            return self._send(413, b"Request too large", "text/plain")
        try:
            data = self.rfile.read(length)
            if path == "/api/upload":
                return self._json({"name": save_upload(unquote(self.headers.get("X-Filename", "")), data)})
            body = json.loads(data or b"{}")
            if path == "/api/login":
                return self._login(str(body.get("password", "")))
            if path == "/api/logout":
                self.server.auth.logout(self._token())
                return self._send(200, b"{}", "application/json", {"Set-Cookie": self._cookie("", 0)})
            if path == "/api/config":
                save_config(body)
                return self._json(public_config())
            if path == "/api/pick-folder":
                if not public_config()["can_pick"]:
                    raise ValueError("Folder dialogs only work when the app runs on your Mac")
                chosen = pick_folder(folder(body["key"]))
                if chosen:
                    save_config({body["key"]: chosen})
                return self._json({**public_config(), "cancelled": not chosen})
            if path == "/api/split":
                src = input_file(body["name"])
                out_dir = export_dir(src.name)
                outs = splitter.split(src, body["rect"], int(body["tiles"]), out_dir,
                                      border=body.get("border"), size=body.get("size"))
                return self._json({"dir": str(out_dir), "files": [p.name for p in outs]})
            if path == "/api/reveal":
                if not public_config()["can_reveal"]:
                    raise ValueError("Only available when the app runs on your Mac")
                subprocess.run(["open", str(export_dir(input_file(body["name"]).name))], check=True)
                return self._json({})
            self._send(404, b"Not found", "text/plain")
        except Exception as e:
            self._error(e)

    def _token(self) -> str | None:
        morsel = SimpleCookie(self.headers.get("Cookie", "")).get(auth.COOKIE)
        return morsel.value if morsel else None

    def _cookie(self, value: str, max_age: int) -> str:
        # No `Secure` flag: the app is served over plain HTTP in the home network.
        return f"{auth.COOKIE}={value}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Strict"

    def _logged_in(self, path: str) -> bool:
        if path in OPEN_PATHS or path.startswith("/static/") or self.server.auth.valid(self._token()):
            return True
        self._json({"error": "Please log in", "login": True}, 401)
        return False

    def _login(self, password: str):
        ip = self.client_address[0]
        if not self.server.auth.enabled:
            return self._json({"ok": True})
        if len(password) > 1024:
            return self._json({"error": "Password too long"}, 400)
        try:
            token = self.server.auth.login(ip, password)
        except auth.LoginError as e:
            print(f"login failed from {ip}: {e}", flush=True)
            return self._json({"error": str(e)}, e.status)
        print(f"login from {ip}", flush=True)
        self._send(200, b'{"ok": true}', "application/json", {"Set-Cookie": self._cookie(token, auth.SESSION_SECONDS)})

    def _trusted(self) -> bool:
        # Reject requests addressed to an unknown host name (DNS rebinding).
        host = self.headers.get("Host", "").rsplit(":", 1)[0].lower()
        extra = os.environ.get("IG_SPLITTER_HOSTS", "")
        if host in {"localhost", "127.0.0.1", *(h.strip().lower() for h in extra.split(",") if h.strip())}:
            return True
        self._send(403, b"Forbidden: unknown host name (see IG_SPLITTER_HOSTS)", "text/plain")
        return False

    def _file(self, path: Path):
        path = path.resolve()
        if not path.is_relative_to(STATIC) or not path.is_file():
            return self._send(404, b"Not found", "text/plain")
        self._send(200, path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream")

    def _attachment(self, data: bytes, filename: str):
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        self._send(200, data, content_type, {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})

    def _json(self, data, status=200):
        self._send(status, json.dumps(data).encode(), "application/json")

    def _error(self, e: Exception):
        if not isinstance(e, (ValueError, RuntimeError, KeyError)):
            traceback.print_exc()
        self._json({"error": str(e)}, 400)

    def _send(self, status: int, data: bytes, content_type: str, headers: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass  # keep the terminal quiet; errors are printed in _error


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="don't open the browser")
    parser.add_argument("--hash-password", action="store_true",
                        help=f"ask for a password and print the hash for {PASSWORD_ENV}")
    args = parser.parse_args()

    if args.hash_password:
        password = getpass.getpass("New password (12+ characters): ")
        if len(password) < 12:
            sys.exit("Use at least 12 characters.")
        if getpass.getpass("Repeat it: ") != password:
            sys.exit("The passwords don't match.")
        print(auth.hash_password(password))
        return

    host = os.environ.get(LISTEN_ENV) or "127.0.0.1"
    gate = login_gate(host)
    if host not in LOOPBACK and len(fixed_folders()) < len(FOLDER_ENV):
        # Otherwise anyone who can use the app could point its folders anywhere on this machine.
        sys.exit(f"Listening on {host} needs fixed folders: set {' and '.join(FOLDER_ENV.values())}.")

    config = load_config()
    for key in DEFAULT_CONFIG:  # create the default folders on first run
        if config[key] == DEFAULT_CONFIG[key]:
            folder(key).mkdir(parents=True, exist_ok=True)

    server = ThreadingHTTPServer((host, args.port), Handler)
    server.auth = gate
    url = f"http://localhost:{args.port}"
    print(f"IG Splitter running at {url}  (Ctrl+C to stop)", flush=True)
    print(f"Input folder:  {folder('input_dir')}", flush=True)
    print(f"Output folder: {folder('output_dir')}", flush=True)
    print(f"Login: {'required' if gate.enabled else 'off'}", flush=True)
    if not gate.enabled and host not in LOOPBACK:
        print(f"Warning: the login is off, so anyone who can reach {host}:{args.port} can use the app.",
              flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
