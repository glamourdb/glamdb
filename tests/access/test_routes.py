import pytest
from uuid import uuid4
from apps.workbench_access.authorization import classify


def test_scan_is_read_and_append_is_write_and_unknown_is_denied():
    cid = str(uuid4())
    op = str(uuid4())
    assert classify("POST", f"/v0/collections/{cid}/scan").action == "read"
    assert classify("POST", f"/v0/collections/{cid}/append").action == "write"
    assert (
        classify("GET", f"/v0/collections/{cid}/operations/{op}").action == "metadata"
    )
    assert classify("GET", "/v0/collections:resolve").action == "metadata"
    assert classify("GET", f"/v0/collections/{cid}/stats").metric == "statistics"
    for path in [
        "/v0/../admin",
        "/v0/collections/%2e%2e/scan",
        f"/v0/collections/{cid}/scan/",
        f"/v0/collections/{cid.upper()}/scan",
        "/v0/future",
    ]:
        with pytest.raises(ValueError):
            classify("POST", path)
    with pytest.raises(ValueError):
        classify("DELETE", f"/v0/collections/{cid}")
