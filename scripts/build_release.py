#!/usr/bin/env python3
"""Build a deterministic, credential-free Home Assistant release archive."""

import ast
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile

ROOT = Path(__file__).resolve().parents[1]


def build_release() -> Path:
    manifest = json.loads((ROOT / "custom_components/central_heating/manifest.json").read_text())
    version = manifest["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Release version must use major.minor.patch")
    constants = ast.parse((ROOT / "custom_components/central_heating/const.py").read_text())
    code_version = next(ast.literal_eval(node.value) for node in constants.body if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets))
    if code_version != version:
        raise ValueError("Manifest and integration versions differ")
    files = [ROOT / "README.md", ROOT / "LICENSE", ROOT / "scripts/install.sh"]
    for directory in ("custom_components/central_heating", "config"):
        files.extend(path for path in (ROOT / directory).rglob("*") if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc")
    for path in files:
        if path.suffix == ".py":
            compile(path.read_text(), str(path), "exec")
    output = ROOT / "dist" / f"central-heating-system-v{version}.tar.gz"
    output.parent.mkdir(exist_ok=True)
    with output.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                for path in sorted(set(files)):
                    data = path.read_bytes()
                    info = tarfile.TarInfo(str(path.relative_to(ROOT)))
                    info.size = len(data)
                    info.mode = 0o755 if path.name == "install.sh" else 0o644
                    archive.addfile(info, io.BytesIO(data))
                data = f"{version}\n".encode()
                info = tarfile.TarInfo("VERSION")
                info.size = len(data)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(data))
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(f"{digest}  {output.name}\n")
    return output


if __name__ == "__main__":
    print(build_release())

