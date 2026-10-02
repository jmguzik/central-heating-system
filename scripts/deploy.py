#!/usr/bin/env python3
"""Build and install over SSH without storing a password or HA API token."""

import argparse
import getpass
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile

from build_release import build_release


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="192.168.68.59")
    parser.add_argument("--user", default="root")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--password-env", help="Name of an environment variable containing the SSH password")
    parser.add_argument("--ssh-key", help="Use SSH keys without prompting for a password", action="store_true")
    parser.add_argument("--known-hosts", type=Path, help="Optional SSH known-hosts file")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9_.:-]+", args.host) or not re.fullmatch(r"[a-zA-Z0-9_-]+", args.user):
        parser.error("Invalid SSH host or username")
    archive = build_release()
    environment = os.environ.copy()
    with tempfile.TemporaryDirectory(prefix="central-heating-ssh-") as temporary:
        if not args.ssh_key:
            password = os.environ.get(args.password_env, "") if args.password_env else getpass.getpass("Home Assistant SSH password: ")
            if not password:
                parser.error("SSH password is empty")
            askpass = Path(temporary) / "askpass.sh"
            askpass.write_text('#!/bin/sh\nprintf "%s\\n" "$CENTRAL_HEATING_SSH_PASSWORD"\n')
            askpass.chmod(0o700)
            environment.update({"SSH_ASKPASS": str(askpass), "SSH_ASKPASS_REQUIRE": "force", "DISPLAY": "central-heating", "CENTRAL_HEATING_SSH_PASSWORD": password})
        ssh = ["ssh", "-F", "/dev/null", "-p", str(args.port), "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10"]
        if args.known_hosts:
            ssh += ["-o", f"UserKnownHostsFile={args.known_hosts}"]
        ssh += [f"{args.user}@{args.host}"]

        def remote(command, payload=None, capture=False):
            return subprocess.run(ssh + [command], input=payload, env=environment, check=True, stdout=subprocess.PIPE if capture else None)

        result = remote("mktemp -d /tmp/central-heating-release.XXXXXX", capture=True)
        stage = result.stdout.decode().strip()
        if not re.fullmatch(r"/tmp/central-heating-release\.[A-Za-z0-9]+", stage):
            raise RuntimeError("SSH returned an unexpected staging directory")
        try:
            remote(f"tar -xzf - -C {shlex.quote(stage)}", archive.read_bytes())
            remote(f"bash {shlex.quote(stage + '/scripts/install.sh')} {shlex.quote(stage)}")
        finally:
            remote(f"rm -rf -- {shlex.quote(stage)}")


if __name__ == "__main__":
    main()

