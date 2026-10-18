"""The built-in local examiner and the shared job queue."""

import threading
import time

import pytest
from fastapi.testclient import TestClient

from forensic import api, jobs as jobs_mod
from forensic.accounts import Accounts
from forensic.audit import Actor
from forensic.config import load_settings
from forensic.jobs import JobQueue, Kind
from forensic.workbench import Workbench

from .conftest import EXAMINER, ingest, sign_in


@pytest.fixture()
def client(tmp_path):
    api._wb = Workbench(load_settings(tmp_path / "data"))
    with TestClient(api.app) as c:
        yield c
    api.jobs().stop()
    api._wb.db.close()
    api._wb = None


# The local examiner ------------------------------------------------------------------------


def test_no_sign_in_needed(client):
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/cases").status_code == 200
    assert client.get("/api/jobs").status_code == 200
    me = client.get("/api/me").json()
    assert me["username"] == "local" and me["role"] == "admin" and me["full_name"]
    # The sign-in and account screens are gone.
    for path in ("/api/auth/login", "/api/auth/setup", "/api/users"):
        assert client.post(path, json={}).status_code in (404, 405)


def test_audit_uses_the_local_examiner_not_a_header(client):
    client.patch("/api/me", json={"full_name": "  Dana   Examiner "}).raise_for_status()
    assert client.get("/api/me").json()["full_name"] == "Dana Examiner"
    assert client.patch("/api/me", json={"full_name": "   "}).status_code == 400
    case = client.post("/api/cases", json={"name": "Case", "examiner": "Dana Examiner"},
                       headers={"X-Examiner": "Someone Else"}).json()
    actors = {e["actor"] for e in client.get(f"/api/cases/{case['id']}/audit").json()["entries"]}
    assert actors == {"Dana Examiner"}


def test_local_examiner_is_created_once_and_keeps_its_name(wb, monkeypatch):
    monkeypatch.setenv("FORENSIC_EXAMINER", "Lior Example")
    acc = Accounts(wb.db)
    assert acc.local_user()["full_name"] == "Lior Example"
    acc.rename("Someone Else")
    monkeypatch.setenv("FORENSIC_EXAMINER", "Ignored Now")
    assert Accounts(wb.db).local_user()["full_name"] == "Someone Else"
    assert wb.db.one("SELECT COUNT(*) AS n FROM users")["n"] == 1


def test_upgraded_installation_keeps_the_administrator_name(wb):
    wb.db.execute("INSERT INTO users (username, full_name, role, password_hash, disabled, created_at)"
                  " VALUES ('lior', 'Lior Admin', 'admin', 'x', 0, '2026-01-01')")
    assert Accounts(wb.db).local_user() == {"username": "local", "full_name": "Lior Admin", "role": "admin"}


# Job queue ----------------------------------------------------------------------------------


@pytest.fixture()
def blocking_kind(monkeypatch):
    """A job kind that waits until the test releases it, and records the order jobs started."""
    started, release = [], {}

    def run(wb, p, actor):
        started.append(p["n"])
        release.setdefault(p["n"], threading.Event()).wait(10)
        if p.get("fail"):
            raise ValueError("this job failed")
        return {"n": p["n"], "by": actor.name}

    monkeypatch.setitem(jobs_mod.KINDS, "test.block",
                        Kind("Test", lambda wb, p: (None, str(p["n"]), f"job {p['n']}"), run))

    def go(n):
        release.setdefault(n, threading.Event()).set()

    return started, go


def _wait(pred, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError("timed out")


def test_queue_is_fair_between_examiners(wb, blocking_kind):
    started, go = blocking_kind
    q = JobQueue(wb, cpu_workers=1, gpu_jobs=1)
    alice, bob = Actor("Alice"), Actor("Bob")
    a1 = q.submit("test.block", {"n": "a1"}, alice, "alice")
    _wait(lambda: started == ["a1"])
    a2 = q.submit("test.block", {"n": "a2"}, alice, "alice")
    a3 = q.submit("test.block", {"n": "a3"}, alice, "alice")
    b1 = q.submit("test.block", {"n": "b1"}, bob, "bob")
    # Bob has nothing running, so his job goes ahead of Alice's second and third.
    assert [q.get(j["id"])["position"] for j in (b1, a2, a3)] == [1, 2, 3]
    view = q.overview()
    assert view["running"]["cpu"] == 1 and view["queued"]["cpu"] == 3
    assert q.get(a1["id"])["status"] == "running"

    with pytest.raises(PermissionError):
        q.cancel(a3["id"], "bob", is_admin=False)
    assert q.cancel(a3["id"], "alice", is_admin=False)["status"] == "cancelled"
    with pytest.raises(ValueError):
        q.cancel(a1["id"], "alice", is_admin=True)  # already running

    for n in ("a1", "b1", "a2"):
        go(n)
    _wait(lambda: q.get(a2["id"])["status"] == "done")
    assert started == ["a1", "b1", "a2"]
    assert q.get(b1["id"])["result"] == {"n": "b1", "by": "Bob"}
    q.stop()


def test_waiting_caller_gets_the_result_or_the_error(wb, blocking_kind):
    _, go = blocking_kind
    q = JobQueue(wb, cpu_workers=2)
    go("ok")
    go("bad")
    assert q.run("test.block", {"n": "ok"}, EXAMINER, "examiner") == {"n": "ok", "by": "Test Examiner"}
    with pytest.raises(ValueError, match="this job failed"):
        q.run("test.block", {"n": "bad", "fail": True}, EXAMINER, "examiner")
    failed = [j for j in q.overview()["recent"] if j["status"] == "failed"]
    assert failed and failed[0]["error"] == "this job failed"
    q.stop()


def test_jobs_running_at_a_restart_are_marked_failed(wb, blocking_kind):
    started, go = blocking_kind
    q = JobQueue(wb, cpu_workers=1)
    j = q.submit("test.block", {"n": "x"}, EXAMINER, "examiner")
    _wait(lambda: started == ["x"])
    q.stop()
    q2 = JobQueue(wb, cpu_workers=1)
    assert q2.get(j["id"])["status"] == "failed" and "restarted" in q2.get(j["id"])["error"]
    go("x")
    q2.stop()


def test_learned_model_jobs_use_the_gpu_lane(wb, case, jpeg_path):
    ev = ingest(wb, case, jpeg_path)
    sr = wb.create_chain(case["id"], ev["id"], "sr", EXAMINER, {"source": {}, "nodes": [
        {"id": "n1", "filter": "ai.super_resolution", "params": {"scale": "2"}}]})
    plain = wb.create_chain(case["id"], ev["id"], "plain", EXAMINER, {"source": {}, "nodes": [
        {"id": "n1", "filter": "exposure.brightness_contrast", "params": {"brightness": 0.1}}]})
    q = JobQueue(wb, cpu_workers=1, gpu_jobs=1)
    gpu = q.submit("chain.render", {"chain_id": sr["id"]}, EXAMINER, "examiner")
    cpu = q.submit("chain.render", {"chain_id": plain["id"]}, EXAMINER, "examiner")
    assert gpu["lane"] == "gpu" and cpu["lane"] == "cpu"
    assert gpu["label"] == "Render chain: sr" and gpu["case_id"] == case["id"]
    _wait(lambda: all(q.get(j["id"])["status"] == "done" for j in (gpu, cpu)), timeout=60)
    q.stop()


def test_api_queue(client, jpeg_path):
    sign_in(client)
    case = client.post("/api/cases", json={"name": "Q", "examiner": "Test Examiner"}).json()
    ev = client.post(f"/api/cases/{case['id']}/evidence", files={"file": ("a.jpg", jpeg_path.read_bytes())}).json()
    ch = client.post(f"/api/cases/{case['id']}/chains", json={"evidence_id": ev["id"], "name": "c", "graph": {
        "source": {}, "nodes": [{"id": "n1", "filter": "exposure.brightness_contrast", "params": {"brightness": 0.1}}]}}).json()

    # The usual endpoints wait for their queued job and answer as before.
    assert client.post(f"/api/chains/{ch['id']}/render").json()["output_hash"]
    # Or queue without waiting, and follow the job.
    job = client.post("/api/jobs", json={"kind": "chain.verify", "params": {"chain_id": ch["id"]}}).json()
    assert job["status"] in ("queued", "running", "done") and job["owner_name"] == "Test Examiner"
    _wait(lambda: client.get(f"/api/jobs/{job['id']}").json()["status"] == "done")
    assert client.get(f"/api/jobs/{job['id']}").json()["result"]["reproduced"] is True

    view = client.get("/api/jobs").json()
    assert {j["kind"] for j in view["recent"]} >= {"chain.render", "chain.verify"}
    assert view["slots"]["gpu"] >= 1
    assert client.post("/api/jobs", json={"kind": "nope", "params": {}}).status_code == 400
    assert client.post("/api/jobs", json={"kind": "chain.render", "params": {"chain_id": "missing"}}).status_code == 404
