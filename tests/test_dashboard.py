"""Check user interactions in a real browser at desktop and phone widths."""

from pathlib import Path
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import shutil
import subprocess
import tempfile
from threading import Thread
import unittest

BROWSER = shutil.which("google-chrome") or shutil.which("chromium-browser") or shutil.which("chromium")
NODE = shutil.which("node")
FIXTURE = Path(__file__).with_name("dashboard-smoke.html").resolve()
RUNNER = FIXTURE.with_suffix(".mjs")


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


@unittest.skipUnless(BROWSER and NODE, "Chrome/Chromium and Node 22+ are required for browser interaction checks")
class DashboardTests(unittest.TestCase):
    def test_desktop_and_mobile_interactions(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(FIXTURE.parents[1])))
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/tests/{FIXTURE.name}"
            for size in ("1280,1000", "390,844"):
                with self.subTest(size=size), tempfile.TemporaryDirectory(prefix="heating-browser-") as profile:
                    width, height = size.split(",")
                    result = subprocess.run([
                        NODE, str(RUNNER), BROWSER, url, width, height, profile,
                    ], capture_output=True, text=True, timeout=35)
                    self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                    report = json.loads(result.stdout)
                    self.assertEqual(report.get("result"), "pass", report)
                    self.assertEqual(report.get("width"), int(width), report)
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


if __name__ == "__main__":
    unittest.main()
