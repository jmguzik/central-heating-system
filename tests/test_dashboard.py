"""Check user interactions in a real browser at desktop and phone widths."""

from pathlib import Path
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import re
import shutil
import subprocess
import tempfile
from threading import Thread
import unittest

BROWSER = shutil.which("google-chrome") or shutil.which("chromium-browser") or shutil.which("chromium")
FIXTURE = Path(__file__).with_name("dashboard-smoke.html").resolve()


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


@unittest.skipUnless(BROWSER, "Chrome/Chromium is required for browser interaction checks")
class DashboardTests(unittest.TestCase):
    def test_desktop_and_mobile_interactions(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(FIXTURE.parents[1])))
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/tests/{FIXTURE.name}"
            for size in ("1280,1000", "390,844"):
                with self.subTest(size=size), tempfile.TemporaryDirectory(prefix="heating-browser-") as profile:
                    result = subprocess.run([
                        BROWSER, "--headless=new", "--disable-gpu", "--no-first-run",
                        "--no-default-browser-check", f"--user-data-dir={profile}",
                        f"--window-size={size}", "--virtual-time-budget=5000", "--dump-dom", url,
                    ], capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                    error = re.search(r'data-error="([^"]*)"', result.stdout)
                    self.assertTrue('data-result="pass"' in result.stdout, error.group(1) if error else result.stdout[-500:])
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


if __name__ == "__main__":
    unittest.main()
