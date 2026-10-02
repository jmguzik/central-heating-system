"""Check user interactions in a real browser at desktop and phone widths."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

BROWSER = shutil.which("google-chrome") or shutil.which("chromium-browser") or shutil.which("chromium")
FIXTURE = Path(__file__).with_name("dashboard-smoke.html").resolve()


@unittest.skipUnless(BROWSER, "Chrome/Chromium is required for browser interaction checks")
class DashboardTests(unittest.TestCase):
    def test_desktop_and_mobile_interactions(self):
        for size in ("1280,1000", "390,844"):
            with self.subTest(size=size), tempfile.TemporaryDirectory(prefix="heating-browser-") as profile:
                result = subprocess.run([
                    BROWSER, "--headless=new", "--disable-gpu", "--no-first-run",
                    "--no-default-browser-check", f"--user-data-dir={profile}",
                    f"--window-size={size}", "--virtual-time-budget=1500", "--dump-dom", FIXTURE.as_uri(),
                ], capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                self.assertIn('data-result="pass"', result.stdout, result.stdout[-3000:])


if __name__ == "__main__":
    unittest.main()
