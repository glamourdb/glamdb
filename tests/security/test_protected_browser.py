"""Exercise the packaged UI in Chromium through real Caddy authentication."""

import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]


def test_browser_through_https_gate(protected_site):
    environment = os.environ.copy()
    environment.update(GLAMDB_PROTECTED_TEST_URL=protected_site.url,
                       GLAMDB_PROTECTED_TEST_USER=protected_site.user,
                       GLAMDB_PROTECTED_TEST_PASSWORD=protected_site.password)
    subprocess.run(["npm", "--prefix", "ui", "run", "test:e2e:protected"],
                   env=environment, cwd=ROOT, check=True, timeout=150)
