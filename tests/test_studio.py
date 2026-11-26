#!/usr/bin/env python3
"""End-to-end test of d455_studio.py with the simulated camera. No camera, no browser, standard library only.

    python tests/test_studio.py

What it checks, against a real server process:
  - the camera is not opened before a page asks for it, then streams; live images of every stream
  - camera options, a timed recording, its verify result, playback (timeline, frames, thumbnail)
  - deleting (also: a running recording cannot be deleted, the service cannot be closed while recording)
  - importing a copied session folder and a loose .mcap file, a session one folder down, odd folder names
  - Host / Origin checks and path checks
  - on-demand start like launchd's: the process exits when unused, a request that arrives meanwhile waits on
    the open listening socket and is answered by the next process (POSIX only)
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAILURES = []


def check(condition, what, detail=""):
    print(("  ok    " if condition else "  FAIL  ") + what + ("  ({})".format(detail) if detail and not condition else ""))
    if not condition:
        FAILURES.append(what)
    return condition


class Client:
    def __init__(self, port):
        self.base = "http://127.0.0.1:{}".format(port)
        self.origin = self.base

    def request(self, method, path, body=None, headers=None, raw=None, timeout=30):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if method == "POST":
            request.add_header("Origin", self.origin)
            request.add_header("Content-Type", "application/octet-stream" if raw is not None else "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def json(self, method, path, body=None, **kwargs):
        status, _headers, data = self.request(method, path, body, **kwargs)
        try:
            return status, json.loads(data.decode() or "null")
        except ValueError:
            return status, {"raw": data[:200]}

    def state(self, want=True):
        return self.json("GET", "/api/state?want={}".format(1 if want else 0))[1]


def quote(session_id):
    return urllib.parse.quote(session_id, safe="")


def start_server(tmp, port, listener, exit_after):
    args = [sys.executable, str(ROOT / "d455_studio.py"), "--simulate", "--no-browser", "--port", str(port),
            "--sessions", str(tmp / "sessions"), "--settings", str(tmp / "settings.json"), "--log-dir", str(tmp / "logs"),
            "--exit-after", str(exit_after)]
    kwargs = {}
    if listener is not None:
        args += ["--listen-fd", str(listener.fileno())]
        kwargs["pass_fds"] = (listener.fileno(),)
    log = open(str(tmp / "server.log"), "ab")
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONUTF8="1")
    return subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, cwd=str(ROOT), env=env, **kwargs)


def wait_for(predicate, timeout, step=0.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return None


def upload_session(client, name, folder, kind="folder"):
    files = [path for path in sorted(folder.rglob("*")) if path.is_file()] if kind == "folder" else [folder]
    total = sum(path.stat().st_size for path in files)
    status, answer = client.json("POST", "/api/import/begin", {"name": name, "bytes": total, "kind": kind})
    if status != 200:
        return status, answer
    for path in files:
        relative = path.relative_to(folder).as_posix() if kind == "folder" else name
        query = urllib.parse.urlencode({"name": name, "path": relative, "mtime": str(path.stat().st_mtime)})
        status, _h, data = client.request("POST", "/api/import/file?" + query, raw=path.read_bytes())
        if status != 200:
            return status, data
    return client.json("POST", "/api/import/finish", {"name": name, "kind": kind})


def main():
    for stream in (sys.stdout, sys.stderr):  # Windows CI: a pipe in the ANSI code page cannot show Chinese
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    tmp = Path(tempfile.mkdtemp(prefix="studio-test-"))
    print("work folder:", tmp)
    listener = None
    if os.name == "posix":  # stands in for launchd: this process owns the listening socket
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        port = listener.getsockname()[1]
    else:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
    exit_after = 12
    server = start_server(tmp, port, listener, exit_after)
    client = Client(port)
    try:
        run(client, tmp, server, listener, port, exit_after)
    finally:
        for process in (server,):
            if process.poll() is None:
                process.kill()
        print("--- server log (last 40 lines)")
        try:
            print("\n".join((tmp / "server.log").read_text(encoding="utf-8", errors="replace").splitlines()[-40:]))
        except OSError:
            pass
    print()
    print("FAILED: {}".format(", ".join(FAILURES)) if FAILURES else "ALL PASSED")
    if not FAILURES:
        shutil.rmtree(str(tmp), ignore_errors=True)
    return 1 if FAILURES else 0


def run(client, tmp, server, listener, port, exit_after):
    print("start")
    pong = wait_for(lambda: client.get("/api/ping", timeout=2)[0] == 200 if server.poll() is None else None, 90)
    if not check(bool(pong), "server answers /api/ping"):
        return
    first = client.state(want=False)
    check(first["camera"]["state"] in ("idle", "starting"), "camera stays closed until a page asks for it",
          first["camera"]["state"])
    streaming = wait_for(lambda: client.state()["camera"]["state"] == "streaming", 40)
    check(bool(streaming), "camera streams once a page asks (want=1)")
    state = client.state()
    names = [stream["name"] for stream in state["streams"] if stream["kind"] == "video"]
    check(set(names) >= {"color", "depth", "ir_left"}, "default streams", names)
    for name in names:
        status, headers, data = client.get("/api/live/{}?after=0&w=640".format(name))
        check(status == 200 and headers.get("Content-Type") == "image/jpeg" and len(data) > 1000,
              "live image " + name, status)

    print("options")
    status, options = client.json("GET", "/api/options")
    check(status == 200 and "color" in options, "GET /api/options", status)
    status, options = client.json("POST", "/api/options", {"role": "color", "name": "enable_auto_exposure", "value": 0})
    check(status == 200, "set an option", options)
    status, options = client.json("POST", "/api/options/reset", {"role": "color"})
    check(status == 200, "reset options", options)

    print("recording")
    status, answer = client.json("POST", "/api/record/start", {"scene": {"location": "ci test"}, "duration": 3})
    check(status == 200 and answer.get("session_id"), "start a 3 s recording", answer)
    result = wait_for(lambda: client.state()["last_result"], 60)
    check(bool(result) and result["ok"] and not result["empty"] and result["result"] in ("PASS", "WARN"),
          "recording finished and verified", result)
    first_id = result["session_id"] if result else ""

    status, listing = client.json("GET", "/api/sessions")
    ids = [item["id"] for item in listing["sessions"]]
    check(first_id in ids, "the new session is listed", ids)
    status, timeline = client.json("GET", "/api/sessions/{}/timeline".format(quote(first_id)))
    check(status == 200 and timeline["duration_ms"] > 2000, "timeline", status)
    for name in ("color", "depth", "ir_left"):
        status, headers, data = client.get("/api/sessions/{}/frame/{}/5".format(quote(first_id), name))
        check(status == 200 and len(data) > 1000, "playback frame " + name, status)
    status, _headers, data = client.get("/api/sessions/{}/thumb".format(quote(first_id)))
    check(status == 200 and data[:2] == b"\xff\xd8", "thumbnail", status)

    print("guards while recording")
    status, answer = client.json("POST", "/api/record/start", {"scene": {}, "duration": 0})
    live_id = answer.get("session_id", "")
    wait_for(lambda: (client.state().get("recording") or {}).get("phase") == "recording", 20)
    status, answer = client.json("POST", "/api/sessions/delete", {"ids": [live_id]})
    check(status == 200 and not answer["deleted"] and answer["failed"], "a running recording cannot be deleted", answer)
    status, answer = client.json("POST", "/api/service/quit")
    check(status == 409, "cannot close the program while recording", status)
    client.json("POST", "/api/record/stop")
    wait_for(lambda: not client.state().get("recording") and (client.state()["last_result"] or {}).get("session_id") == live_id, 60)

    print("import")
    sessions = tmp / "sessions"
    source = tmp / "elsewhere" / first_id
    shutil.copytree(str(sessions / first_id), str(source))
    status, answer = upload_session(client, "copied A", source)
    check(status == 200 and answer.get("id") == "copied A", "import a session folder (name with a space)", answer)
    status, answer = upload_session(client, "copied A", source)
    check(status == 409, "importing the same name again is refused", status)
    mcap = sorted((source / "recordings").glob("*.mcap"))[0]
    status, answer = upload_session(client, "loose.mcap", mcap, kind="file")
    check(status == 200, "import a loose .mcap file", answer)
    nested = sessions / "外部 拷贝" / "session x"
    shutil.copytree(str(source), str(nested))
    (sessions / "外部 拷贝" / "._junk.mcap").write_bytes(b"AppleDouble")
    status, listing = client.json("GET", "/api/sessions")
    by_id = {item["id"]: item for item in listing["sessions"]}
    for session_id, kind in (("copied A", "session"), ("loose.mcap", "file"), ("外部 拷贝/session x", "session")):
        item = by_id.get(session_id)
        check(item is not None and item["kind"] == kind, "listed: " + session_id, sorted(by_id))
        status, timeline = client.json("GET", "/api/sessions/{}/timeline".format(quote(session_id)))
        check(status == 200 and timeline.get("duration_ms", 0) > 2000, "plays back: " + session_id, status)
    loose = by_id.get("loose.mcap") or {}
    check(loose.get("duration_s") and str(loose.get("created", "")).startswith(time.strftime("%Y-")),
          "a loose file gets its date and duration from the MCAP itself", loose)
    check(not any(".importing-" in key or "._junk" in key for key in by_id), "no half imports or ._ files listed")

    print("security")
    status, _h, _d = client.get("/api/state", headers={"Host": "evil.example:{}".format(port)})
    check(status == 403, "foreign Host header refused", status)
    client.origin = "http://evil.example"
    status, _a = client.json("POST", "/api/record/stop")
    client.origin = client.base
    check(status == 403, "cross-origin POST refused", status)
    for bad in ("..", "../sessions", ".hidden", "a/b/c"):
        status, _a = client.json("GET", "/api/sessions/{}/timeline".format(quote(bad)))
        check(status in (400, 404), "bad session id refused: " + bad, status)
    status, _a = client.json("POST", "/api/import/begin", {"name": "../escape", "bytes": 1})
    check(status in (400, 409), "import name with a separator refused", status)
    client.json("POST", "/api/import/begin", {"name": "probe", "bytes": 1})
    status, _h, _d = client.request("POST", "/api/import/file?" + urllib.parse.urlencode({"name": "probe", "path": "../../x"}), raw=b"x")
    check(status in (400, 409) and not (tmp / "x").exists(), "import path with .. refused", status)
    client.json("POST", "/api/import/cancel", {"name": "probe"})

    print("delete")
    victims = ["copied A", "loose.mcap", "外部 拷贝/session x"]
    status, answer = client.json("POST", "/api/sessions/delete", {"ids": victims})
    check(status == 200 and sorted(answer["deleted"]) == sorted(victims) and answer["freed"] > 0, "delete three", answer)
    check(not (sessions / "copied A").exists() and not (sessions / "loose.mcap").exists() and not nested.exists(),
          "their files are gone")
    status, listing = client.json("GET", "/api/sessions")
    check(sorted(item["id"] for item in listing["sessions"]) == sorted([first_id, live_id]), "list after delete",
          [item["id"] for item in listing["sessions"]])
    check(not list(sessions.glob(".importing-*")), "no staging folders left")

    print("on-demand: exit when unused")
    started = time.time()
    code = None
    try:
        code = server.wait(timeout=exit_after + 40)
    except subprocess.TimeoutExpired:
        pass
    check(code == 0, "exits by itself after {} s without requests".format(exit_after), code)
    print("    exited after {:.0f} s".format(time.time() - started))
    if listener is None:
        return
    answers = {}

    def early_request():
        answers["ping"] = client.get("/api/ping", timeout=60)[0]

    waiting = threading.Thread(target=early_request)
    waiting.start()  # the connection waits in the listening socket's queue, like with launchd
    time.sleep(1.0)
    second = start_server(tmp, port, listener, exit_after)
    waiting.join(90)
    check(answers.get("ping") == 200, "a request made while it was not running is answered by the next start",
          answers)
    status, answer = client.json("POST", "/api/service/quit")
    check(status == 200, "close from the page", answer)
    try:
        code = second.wait(timeout=30)
    except subprocess.TimeoutExpired:
        second.kill()
        code = None
    check(code == 0, "closed from the page: the process ends", code)


if __name__ == "__main__":
    sys.exit(main())
