#!/usr/bin/env python3
"""What the macOS service must never do while it runs as root: hand system files to the user through a link,
or write, import or delete through a recordings folder that leads somewhere else. Needs root (CI: sudo).

    sudo python tests/test_root_safety.py

It pretends to be the service of user 4242 (SUDO_UID), the way install_service.sh starts it."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
USER = 4242
FAILURES = []


def check(condition, what, detail=""):
    print(("  ok    " if condition else "  FAIL  ") + what + ("  ({})".format(detail) if detail and not condition else ""))
    if not condition:
        FAILURES.append(what)
    return condition


def owner(path):
    return os.lstat(str(path)).st_uid


def ownership(tmp):
    import record_d455 as rd
    print("hand_back_ownership never goes through a link")
    protected = tmp / "protected"
    protected.mkdir()
    (protected / "program.py").write_text("x = 1\n")
    link = tmp / "sessions-link"
    os.symlink(str(protected), str(link))
    rd.hand_back_ownership(link, recursive=False)
    rd.hand_back_ownership(link, recursive=True)
    check(owner(protected) == 0 and owner(protected / "program.py") == 0, "a linked folder keeps its owner (root)")

    session = tmp / "session"
    (session / "recordings").mkdir(parents=True)
    (session / "recordings" / "a.mcap").write_bytes(b"x")
    os.symlink(str(protected / "program.py"), str(session / "planted"))
    os.symlink(str(protected), str(session / "recordings" / "planted-folder"))
    rd.hand_back_ownership(session, recursive=True)
    check(owner(session) == USER and owner(session / "recordings" / "a.mcap") == USER, "the session itself is handed over")
    check(owner(protected / "program.py") == 0 and owner(protected) == 0, "files that links inside it point to are not")


def folder_rules(tmp):
    import d455_studio as studio
    print("which recordings folders the service may write to")
    mine = tmp / "mine"
    mine.mkdir()
    os.chown(str(mine), USER, USER)
    check(studio.unsafe_root(Path(os.path.realpath(str(mine)))) == "", "the user's own folder is fine")
    foreign = tmp / "foreign"
    foreign.mkdir()
    check("不属于" in studio.unsafe_root(Path(os.path.realpath(str(foreign)))), "a folder of another account is not")
    hop = tmp / "hop"
    os.symlink(str(mine), str(hop))
    check("符号链接" in studio.unsafe_root(hop / "x"), "a path with a link in it is not")
    for place in ("/Library/D455Studio/app", "/usr/local/x", "/etc"):
        check(studio.unsafe_root(Path(place)) != "", "system location refused: " + place)


def request(port, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request("http://127.0.0.1:{}{}".format(port, path), data=data, method=method)
    if method == "POST":
        req.add_header("Content-Type", "application/json")
        req.add_header("Origin", "http://127.0.0.1:{}".format(port))
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status, json.loads(response.read().decode() or "null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode() or "null")


def server(tmp, sessions, name):
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    env = dict(os.environ, SUDO_UID=str(USER), SUDO_GID=str(USER), PYTHONUNBUFFERED="1", PYTHONUTF8="1")
    log = open(str(tmp / (name + ".log")), "ab")
    process = subprocess.Popen([sys.executable, str(ROOT / "d455_studio.py"), "--simulate", "--no-browser",
                                "--port", str(port), "--sessions", str(sessions), "--settings", str(tmp / "settings.json"),
                                "--log-dir", str(tmp / "logs")], stdout=log, stderr=subprocess.STDOUT, cwd=str(ROOT), env=env)
    for _ in range(120):
        try:
            if request(port, "GET", "/api/ping")[0] == 200:
                break
        except OSError:
            pass
        time.sleep(0.5)
    return process, port


def service(tmp):
    print("the running service refuses a recordings folder that leads elsewhere")
    protected = tmp / "app"
    protected.mkdir()
    (protected / "keep.json").write_text("{}")
    # a user process swapped the recordings folder for a link to a root-owned folder
    link = tmp / "user-home-sessions"
    os.symlink(str(protected), str(link))
    process, port = server(tmp, link, "unsafe")
    try:
        status, answer = request(port, "POST", "/api/import/begin", {"name": "x", "bytes": 1, "kind": "folder"})
        check(status == 409 and "停用" in str(answer), "import refused", (status, answer))
        status, answer = request(port, "POST", "/api/sessions/delete", {"ids": ["keep.json"]})
        check(status == 409 and (protected / "keep.json").exists(), "delete refused", (status, answer))
        status, answer = request(port, "GET", "/api/diagnostics?fresh=1")
        storage = [item for item in (answer or {}).get("findings", []) if item.get("key") == "storage"]
        check(bool(storage) and storage[0]["level"] == "bad", "the self check says why", storage)
        check(owner(protected) == 0 and not any(path.name.startswith(".") for path in protected.iterdir()),
              "the linked folder is untouched (owner, no staging or probe files)", sorted(os.listdir(str(protected))))
    finally:
        process.kill()

    print("and works normally on the user's own folder")
    sessions = tmp / "home-sessions"
    sessions.mkdir()
    os.chown(str(sessions), USER, USER)
    process, port = server(tmp, sessions, "safe")
    try:
        status, answer = request(port, "POST", "/api/import/begin", {"name": "copied", "bytes": 1, "kind": "folder"})
        check(status == 200, "import allowed", (status, answer))
        body = b'{"status": "complete"}'
        req = urllib.request.Request("http://127.0.0.1:{}/api/import/file?name=copied&path=manifest.json".format(port),
                                     data=body, method="POST")
        req.add_header("Content-Type", "application/octet-stream")
        with urllib.request.urlopen(req, timeout=20) as response:
            check(response.status == 200, "file uploaded")
        status, answer = request(port, "POST", "/api/import/finish", {"name": "copied", "kind": "folder"})
        check(status == 200 and owner(sessions / "copied") == USER and owner(sessions / "copied" / "manifest.json") == USER,
              "the imported folder belongs to the user", (status, answer))
    finally:
        process.kill()


def main():
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        print("needs root: sudo {} {}".format(sys.executable, __file__))
        return 2
    os.environ["SUDO_UID"] = os.environ["SUDO_GID"] = str(USER)  # as install_service.sh sets them for the service
    tmp = Path(tempfile.mkdtemp(prefix="studio-root-"))
    os.chmod(str(tmp), 0o755)
    try:
        ownership(tmp)
        folder_rules(tmp)
        service(tmp)
    finally:
        for name in ("unsafe.log", "safe.log"):
            if FAILURES and (tmp / name).exists():
                print("--- " + name)
                print((tmp / name).read_text(errors="replace")[-3000:])
        shutil.rmtree(str(tmp), ignore_errors=True)
    print()
    print("FAILED: {}".format(", ".join(FAILURES)) if FAILURES else "ALL PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
