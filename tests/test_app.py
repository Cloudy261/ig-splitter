"""HTTP API: upload, split, download, and the cross-site request guards.

Run with: python3 -m unittest discover tests
"""

import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402
import auth  # noqa: E402


class ApiTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "in").mkdir()
        env = {"IG_SPLITTER_INPUT": str(root / "in"), "IG_SPLITTER_OUTPUT": str(root / "out"),
               "IG_SPLITTER_HOSTS": "photos.example"}
        self._env = mock.patch.dict(os.environ, env)
        self._env.start()
        self.root = root
        self.server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.server.auth = auth.Auth(self.password_hash())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def password_hash(self):
        return None  # no login

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self._env.stop()
        self._tmp.cleanup()

    def request(self, method, path, body=None, headers=None, host=None):
        conn = HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        conn.putrequest(method, path, skip_host=True)
        conn.putheader("Host", host or f"127.0.0.1:{self.server.server_port}")
        for key, value in {"Content-Length": str(len(body or b"")), **(headers or {})}.items():
            conn.putheader(key, value)
        conn.endheaders(body)
        res = conn.getresponse()
        return res.status, res.read(), dict(res.getheaders())

    def post_json(self, path, data):
        return self.request("POST", path, json.dumps(data).encode(), {"Content-Type": "application/json"})

    def test_upload_split_and_download_keep_bytes(self):
        photo = io.BytesIO()
        Image.new("RGB", (480, 160), (200, 30, 90)).save(photo, "JPEG", quality=90)
        status, body, _ = self.request("POST", "/api/upload", photo.getvalue(),
                                       {"Content-Type": "application/octet-stream", "X-Filename": "my%20pano.jpg"})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["name"], "my pano.jpg")
        self.assertEqual((self.root / "in" / "my pano.jpg").read_bytes(), photo.getvalue())
        # a second upload with the same name doesn't overwrite
        _, body, _ = self.request("POST", "/api/upload", photo.getvalue(),
                                  {"Content-Type": "application/octet-stream", "X-Filename": "my%20pano.jpg"})
        self.assertEqual(json.loads(body)["name"], "my pano-1.jpg")

        status, body, _ = self.post_json("/api/split", {"name": "my pano.jpg", "rect": [0, 0, 480, 160], "tiles": 3})
        self.assertEqual(status, 200, body)
        files = json.loads(body)["files"]
        self.assertEqual(files, ["my pano_1of3.jpg", "my pano_2of3.jpg", "my pano_3of3.jpg"])

        status, data, headers = self.request("GET", "/api/download?name=my%20pano.jpg&file=my%20pano_2of3.jpg")
        self.assertEqual(status, 200)
        self.assertEqual(data, (self.root / "out" / "my pano" / "my pano_2of3.jpg").read_bytes())
        self.assertIn("attachment", headers["Content-Disposition"])

        query = "&".join(f"file={f.replace(' ', '%20')}" for f in files)
        status, data, _ = self.request("GET", f"/api/zip?name=my%20pano.jpg&{query}")
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            self.assertEqual(z.namelist(), files)
            self.assertEqual(z.read(files[0]), (self.root / "out" / "my pano" / files[0]).read_bytes())

    def test_rejects_bad_uploads_and_paths(self):
        status, body, _ = self.request("POST", "/api/upload", b"not an image",
                                       {"Content-Type": "application/octet-stream", "X-Filename": "x.png"})
        self.assertEqual(status, 400)
        self.assertEqual(list((self.root / "in").iterdir()), [])
        status, _, _ = self.request("GET", "/api/download?name=a.jpg&file=..%2F..%2Fetc%2Fpasswd")
        self.assertEqual(status, 400)
        # ".." as the photo name would point one level above the output folder
        (self.root / "out").mkdir()
        (self.root / "secret.txt").write_text("not a tile")
        for path in ("/api/download?name=..&file=secret.txt", "/api/zip?name=..&file=secret.txt"):
            self.assertEqual(self.request("GET", path)[0], 400, path)

    def test_rejects_invalid_content_length(self):
        # a negative length made the server read until the client stopped sending (open to everyone via login)
        for length in ("-1", "abc"):
            status, _, _ = self.request("POST", "/api/login", b"{}",
                                        {"Content-Type": "application/json", "Content-Length": length})
            self.assertEqual(status, 400, length)

    def test_drops_silent_connections(self):
        self.assertTrue(app.Handler.timeout)
        with mock.patch.object(app.Handler, "timeout", 0.2), \
                socket.create_connection(("127.0.0.1", self.server.server_port), timeout=5) as conn:
            self.assertEqual(conn.recv(1), b"")  # closed by the server, long before our own timeout

    def test_guards_against_other_sites(self):
        self.assertEqual(self.request("GET", "/api/health", host="evil.example")[0], 403)
        self.assertEqual(self.request("GET", "/api/health", host="photos.example:8765")[0], 200)
        status, _, _ = self.request("POST", "/api/login", b"{}", {"Content-Type": "text/plain"})
        self.assertEqual(status, 415)

    def test_folders_fixed_by_environment(self):
        _, body, _ = self.request("GET", "/api/config")
        config = json.loads(body)
        self.assertTrue(config["locked"])
        self.assertFalse(config["can_pick"])
        status, body, _ = self.post_json("/api/config", {"input_dir": "/tmp"})
        self.assertEqual(status, 400)



class LoginTest(ApiTest):
    PASSWORD = "correct horse battery staple"

    def password_hash(self):
        return auth.hash_password(self.PASSWORD, iterations=100_000)  # cheaper than the real 1,000,000

    def login(self, password):
        return self.post_json("/api/login", {"password": password})

    def get(self, path, cookie=None):
        return self.request("GET", path, headers={"Cookie": cookie} if cookie else None)

    # the inherited API tests run logged out here, so they're replaced by these
    test_upload_split_and_download_keep_bytes = None
    test_rejects_bad_uploads_and_paths = None
    test_folders_fixed_by_environment = None

    def test_hash_round_trip(self):
        stored = auth.hash_password("secret password", iterations=100_000)
        self.assertTrue(stored.startswith("pbkdf2-sha256:100000:"))
        self.assertNotIn("$", stored)
        self.assertTrue(auth.verify_password("secret password", stored))
        self.assertFalse(auth.verify_password("secret passworD", stored))
        self.assertEqual(auth.hash_password("x" * 12).split(":")[1], "1000000")
        with self.assertRaises(ValueError):
            auth.Auth("pbkdf2-sha256:1000:abc:def")  # too few iterations
        with self.assertRaises(ValueError):
            auth.Auth(stored[:-3])  # cut off while pasting: still decodes, but to a 30-byte digest

    def test_everything_but_the_login_needs_a_session(self):
        status, body, _ = self.get("/api/images")
        self.assertEqual((status, json.loads(body)["login"]), (401, True))
        for path in ("/", "/static/app.js", "/api/health"):
            self.assertEqual(self.get(path)[0], 200, path)
        self.assertEqual(json.loads(self.get("/api/session")[1]), {"auth": True, "logged_in": False})
        status, _, _ = self.request("POST", "/api/upload", b"x" * 100,
                                    {"Content-Type": "application/octet-stream", "X-Filename": "a.png"})
        self.assertEqual(status, 401)
        self.assertEqual(list((self.root / "in").iterdir()), [])

    def test_login_session_and_logout(self):
        status, _, headers = self.login(self.PASSWORD)
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        session = cookie.split(";")[0]
        self.assertEqual(self.get("/api/images", session)[0], 200)
        self.assertTrue(json.loads(self.get("/api/session", session)[1])["logged_in"])
        self.request("POST", "/api/logout", b"{}", {"Content-Type": "application/json", "Cookie": session})
        self.assertEqual(self.get("/api/images", session)[0], 401)
        self.assertEqual(self.get("/api/images", "ig_session=made-up-token")[0], 401)

    def test_five_wrong_passwords_lock_the_ip(self):
        for _ in range(4):
            self.assertEqual(self.login("wrong")[0], 401)
        status, body, _ = self.login("wrong")
        self.assertEqual(status, 429)
        self.assertIn("15 min", json.loads(body)["error"])
        self.assertEqual(self.login(self.PASSWORD)[0], 429)  # even the right password, while locked
        status, _, _ = self.request("POST", "/api/login", b"x" * 70000, {"Content-Type": "application/json"})
        self.assertEqual(status, 413)

    def test_login_can_be_turned_off(self):
        with mock.patch.dict(os.environ, {app.PASSWORD_ENV: self.password_hash()}):
            self.assertTrue(app.login_gate("0.0.0.0").enabled)
            with mock.patch.dict(os.environ, {app.LOGIN_ENV: "off"}):
                self.assertFalse(app.login_gate("0.0.0.0").enabled)
        with mock.patch.dict(os.environ, {app.LOGIN_ENV: "maybe"}), self.assertRaises(SystemExit):
            app.login_gate("127.0.0.1")

    def run_app(self, **env):
        """Start app.py with these environment changes (None removes a variable); it should exit."""
        env = {k: v for k, v in {**os.environ, **env}.items() if v is not None}
        return subprocess.run([sys.executable, str(Path(app.__file__)), "--no-browser"],
                              env=env, capture_output=True, text=True, timeout=30)

    def test_refuses_to_listen_on_the_network_without_a_password(self):
        proc = self.run_app(**{app.LISTEN_ENV: "0.0.0.0", app.PASSWORD_ENV: None, app.LOGIN_ENV: None})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("without a login", proc.stderr)

    def test_refuses_to_listen_on_the_network_without_fixed_folders(self):
        # otherwise anyone who can use the app could point its folders anywhere on the machine
        proc = self.run_app(**{app.LISTEN_ENV: "0.0.0.0", app.LOGIN_ENV: "off", "IG_SPLITTER_OUTPUT": None})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("needs fixed folders", proc.stderr)


if __name__ == "__main__":
    unittest.main()
