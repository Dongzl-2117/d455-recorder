#!/usr/bin/env python3
"""repair_recording keeps the original file unless the repaired copy is complete.

    python tests/test_repair.py

A cut-off file is repaired; a read error in the middle, or too little free space, leaves the original as it
was and no partial copy behind."""
import builtins
import errno
import shutil
import sys
import tempfile
from collections import namedtuple
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import record_d455 as rd  # noqa: E402

FAILURES = []


def check(condition, what, detail=""):
    print(("  ok    " if condition else "  FAIL  ") + what + ("  ({})".format(detail) if detail and not condition else ""))
    if not condition:
        FAILURES.append(what)
    return condition


def cut_off_file(path):
    from mcap.writer import Writer
    with open(str(path), "wb") as handle:
        writer = Writer(handle, chunk_size=64 * 1024)
        writer.start()
        schema = writer.register_schema(name="test", encoding="jsonschema", data=b"{}")
        channel = writer.register_channel(topic="/test", message_encoding="json", schema_id=schema)
        for number in range(2000):
            writer.add_message(channel_id=channel, log_time=number * 1000, publish_time=number * 1000,
                               data=b'{"n": %d, "pad": "%s"}' % (number, b"x" * 200))
        writer.finish()
    data = path.read_bytes()
    path.write_bytes(data[:int(len(data) * 0.8)])  # power loss: no summary, no footer, a torn last chunk
    return path.read_bytes()


class FailingReader:
    """A file that returns an I/O error halfway through, like a bad sector or an unplugged drive."""

    def __init__(self, handle, limit):
        self.handle, self.limit, self.done = handle, limit, 0

    def read(self, size=-1):
        if self.done >= self.limit:
            raise OSError(errno.EIO, "Input/output error")
        block = self.handle.read(size if size >= 0 else self.limit - self.done)
        self.done += len(block)
        return block

    def __getattr__(self, name):
        return getattr(self.handle, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.handle.close()


def main():
    tmp = Path(tempfile.mkdtemp(prefix="repair-test-"))
    try:
        print("cut-off file")
        path = tmp / "a.mcap"
        cut_off_file(path)
        count = rd.repair_recording(path, "s", 0, {})
        check(rd.is_finalized(path) and count > 1000, "repaired and indexed", count)
        check(not (tmp / "a.mcap.repair").exists(), "no temporary file left")

        print("read error in the middle")
        path = tmp / "b.mcap"
        original = cut_off_file(path)
        real_open = builtins.open

        def failing_open(file, mode="r", *args, **kwargs):
            handle = real_open(file, mode, *args, **kwargs)
            return FailingReader(handle, len(original) // 3) if str(file) == str(path) and "r" in mode else handle

        rd.open = failing_open  # the module looks up open() in its own globals first
        try:
            rd.repair_recording(path, "s", 0, {})
            check(False, "a read error is reported")
        except OSError:
            check(True, "a read error is reported")
        finally:
            del rd.open
        check(path.read_bytes() == original, "the original is unchanged (not replaced by a shorter copy)")
        check(not (tmp / "b.mcap.repair").exists(), "no partial copy left")

        print("too little free space")
        usage = namedtuple("usage", "total used free")
        real_usage = rd.shutil.disk_usage
        rd.shutil.disk_usage = lambda _path: usage(10 ** 12, 10 ** 12, 1000)
        try:
            rd.repair_recording(path, "s", 0, {})
            check(False, "refused before writing")
        except RuntimeError as error:
            check("free space" in str(error), "refused before writing, with the space needed", error)
        finally:
            rd.shutil.disk_usage = real_usage
        check(path.read_bytes() == original and not (tmp / "b.mcap.repair").exists(), "nothing changed on disk")
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)
    print()
    print("FAILED: {}".format(", ".join(FAILURES)) if FAILURES else "ALL PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
