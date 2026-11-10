"""Exercise the packaged UI in Chromium through real Caddy authentication."""

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def test_browser_through_https_gate(protected_site):
    environment = os.environ.copy()
    environment.update(GLAMDB_PROTECTED_TEST_URL=protected_site.url,
                       GLAMDB_PROTECTED_TEST_USER=protected_site.user,
                       GLAMDB_PROTECTED_TEST_PASSWORD=protected_site.password)
    subprocess.run(["npm", "--prefix", "ui", "run", "test:e2e:protected"],
                   env=environment, cwd=ROOT, check=True, timeout=150)


def test_browser_groups_and_database_isolation(group_gateway):
    environment = os.environ.copy()
    environment["GLAMDB_GROUP_TEST_URL"] = group_gateway.url
    for group, (username, password) in group_gateway.accounts.items():
        environment[f"GLAMDB_GROUP_TEST_{group.upper()}_USER"] = username
        environment[f"GLAMDB_GROUP_TEST_{group.upper()}_PASSWORD"] = password
    subprocess.run(["npm", "--prefix", "ui", "run", "test:e2e:groups"],
                   env=environment, cwd=ROOT, check=True, timeout=240)


def test_browser_organization_lifecycle(organization_gateway):
    environment = os.environ.copy()
    environment["GLAMDB_GROUP_TEST_URL"] = organization_gateway.url
    environment["GLAMDB_ORGANIZATION_TEST_SOCKET"] = str(organization_gateway.socket_path)
    environment["GLAMDB_ORGANIZATION_TEST_PYTHON"] = sys.executable
    for organization, (username, password) in organization_gateway.fleet.accounts.items():
        environment[f"GLAMDB_GROUP_TEST_{organization.upper()}_USER"] = username
        environment[f"GLAMDB_GROUP_TEST_{organization.upper()}_PASSWORD"] = password
    subprocess.run(["npm", "--prefix", "ui", "run", "test:e2e:organizations"],
                   env=environment, cwd=ROOT, check=True, timeout=300)
