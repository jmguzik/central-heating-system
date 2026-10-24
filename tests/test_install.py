"""Exercise installer Recorder configuration and rollback without a running HA."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).parents[1]


@unittest.skipUnless(shutil.which("jq"), "installer requires jq")
class InstallerRecorderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config"
        self.config.mkdir()
        (self.config / ".storage").mkdir()
        self.capture = self.root / "captured"
        self.stage = self.root / "release"
        (self.stage / "custom_components/central_heating").mkdir(parents=True)
        (self.stage / "custom_components/central_heating/new.py").write_text("new release")
        (self.stage / "config").mkdir()
        for name in ("central_heating.yaml", "dashboard.yaml", "recorder.yaml"):
            shutil.copyfile(ROOT / "config" / name, self.stage / "config" / name)
        (self.stage / "VERSION").write_text("test")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        # Stop at the configuration check; preserve its input before requesting rollback.
        ha = self.bin / "ha"
        ha.write_text('''#!/usr/bin/env bash
set -eu
[[ "$*" == "core check" ]]
cp -a "$TEST_CONFIG/central-heating" "$TEST_CAPTURE"
cp "$TEST_CONFIG/configuration.yaml" "$TEST_CAPTURE/configuration.yaml"
exit 1
''')
        ha.chmod(0o755)
        self.script = self.root / "install.sh"
        source = (ROOT / "scripts/install.sh").read_text()
        # Redirect only absolute config paths; retain "$stage/config/..." sources.
        self.script.write_text(re.sub(r"(?<![A-Za-z0-9_$])/config\b", str(self.config), source))
        self.original = "default_config:\nrecorder_test_marker: preserved\n"
        (self.config / "configuration.yaml").write_text(self.original)

    def run_installer(self):
        return subprocess.run(["bash", str(self.script), str(self.stage)], capture_output=True, text=True,
            env={**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                 "TEST_CONFIG": str(self.config), "TEST_CAPTURE": str(self.capture)}, timeout=10)

    def test_adds_managed_recorder_exclusions_and_preserves_renamed_statuses(self):
        entities = [
            {"platform": "central_heating", "unique_id": "heating_status", "entity_id": "sensor.custom_status_name"},
            {"platform": "central_heating", "unique_id": "heating_room_status", "entity_id": "sensor.disabled_room", "disabled_by": "user"},
            {"platform": "central_heating", "unique_id": "room_temperature", "entity_id": "sensor.temperature"},
            {"platform": "other", "unique_id": "other_status", "entity_id": "sensor.foreign_status"},
        ]
        (self.config / ".storage/core.entity_registry").write_text(json.dumps({"data": {"entities": entities}}))
        result = self.run_installer()
        self.assertEqual(result.returncode, 1, result.stderr)
        configured = (self.capture / "configuration.yaml").read_text()
        self.assertIn(self.original, configured)
        self.assertIn("recorder: !include central-heating/recorder.yaml", configured)
        recorder = (self.capture / "recorder.yaml").read_text()
        for expected in ("sensor.central_heating_status", "sensor.central_heating_*_status", "sensor.custom_status_name", "sensor.disabled_room"):
            self.assertIn(expected, recorder)
        for foreign in ("sensor.temperature", "sensor.foreign_status", "purge_keep_days", "auto_purge"):
            self.assertNotIn(foreign, recorder)
        self.assertEqual((self.config / "configuration.yaml").read_text(), self.original)

    def test_rejects_unmanaged_recorder_without_changing_existing_files(self):
        existing = "default_config:\nrecorder:\n  purge_keep_days: 30\n  exclude:\n    entities:\n      - sensor.private\n"
        (self.config / "configuration.yaml").write_text(existing)
        result = self.run_installer()
        self.assertEqual(result.returncode, 1)
        self.assertIn("existing Recorder options must be preserved", result.stderr)
        self.assertEqual((self.config / "configuration.yaml").read_text(), existing)
        self.assertFalse(self.capture.exists())
        self.assertFalse((self.config / "custom_components").exists())

    def test_failed_config_check_restores_component_site_and_managed_config(self):
        original = self.original + "# BEGIN CENTRAL-HEATING-SYSTEM\nrecorder: !include old.yaml\n# END CENTRAL-HEATING-SYSTEM\n"
        (self.config / "configuration.yaml").write_text(original)
        component = self.config / "custom_components/central_heating"
        component.mkdir(parents=True)
        (component / "old.py").write_text("previous release")
        site = self.config / "central-heating"
        site.mkdir()
        (site / "recorder.yaml").write_text("old recorder exclusions\n")
        result = self.run_installer()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("configuration check failed", result.stderr)
        self.assertEqual((self.capture / "configuration.yaml").read_text().count("recorder:"), 1)
        self.assertEqual((self.config / "configuration.yaml").read_text(), original)
        self.assertEqual((component / "old.py").read_text(), "previous release")
        self.assertFalse((component / "new.py").exists())
        self.assertEqual((site / "recorder.yaml").read_text(), "old recorder exclusions\n")
        self.assertFalse((site / "dashboard.yaml").exists())


if __name__ == "__main__":
    unittest.main()
