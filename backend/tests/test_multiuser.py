"""Several examiners on one workbench: accounts, sign-in, and the shared job queue."""

import threading
import time

import pytest
from fastapi.testclient import TestClient

from forensic import api, jobs as jobs_mod
from forensic.accounts import Accounts, AuthError
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


# Accounts ----------------------------------------------------------------------------------


def test_everything_needs_sign_in_except_health_and_setup(client):
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/auth/status").json() == {"setup_required": True, "user": None}
    assert client.get("/api/cases").status_code == 401
    # A name in a header is no longer enough to act as an examiner.
    assert client.post("/api/cases", json={"name": "x", "examiner": "x"}, headers={"X-Examiner": "Anyone"}).status_code == 401
    assert client.get("/api/jobs").status_code == 401


def test_first_admin_adds_examiners_and_audit_uses_the_account(client):
    sign_in(client, "lior", "Lior Admin")
    assert client.post("/api/auth/setup", json={"username": "eve", "full_name": "Eve", "password": "12345678"}).status_code == 403
    assert client.get("/api/auth/status").json()["user"]["role"] == "admin"
    r = client.post("/api/users", json={"username": "dana", "full_name": "Dana Examiner", "password": "dana-pass-1"})
    assert r.status_code == 200 and r.json()["role"] == "examiner"
    assert client.post("/api/users", json={"username": "dana2", "full_name": "dana examiner", "password": "12345678"}).status_code == 400
    assert client.post("/api/users", json={"username": "x", "full_name": "Short", "password": "1"}).status_code == 400

    dana = TestClient(api.app)
    assert dana.post("/api/auth/login", json={"username": "dana", "password": "nope-nope"}).status_code == 401
    dana.post("/api/auth/login", json={"username": "Dana", "password": "dana-pass-1"}).raise_for_status()
    assert dana.get("/api/users").status_code == 403
    case = dana.post("/api/cases", json={"name": "Case", "examiner": "Dana Examiner"},
                     headers={"X-Examiner": "Someone Else"}).json()
    actors = {e["actor"] for e in client.get(f"/api/cases/{case['id']}/audit").json()["entries"]}
    assert actors == {"Dana Examiner"}

    # Disabling an account ends its sessions at once.
    client.patch("/api/users/dana", json={"disabled": True}).raise_for_status()
    assert dana.get("/api/cases").status_code == 401
    assert dana.post("/api/auth/login", json={"username": "dana", "password": "dana-pass-1"}).status_code == 401
    # The last administrator cannot lock everyone out.
    assert client.patch("/api/users/lior", json={"role": "examiner"}).status_code == 400

    client.post("/api/auth/logout").raise_for_status()
    assert client.get("/api/cases").status_code == 401


def test_repeated_wrong_passwords_lock_the_name(wb):
    acc = Accounts(wb.db)
    acc.create_user("ana", "Ana", "right-password", first_admin=True)
    for _ in range(5):
        with pytest.raises(AuthError):
            acc.login("ana", "wrong-password")
    with pytest.raises(AuthError, match="wait"):
        acc.login("ana", "right-password")


def test_password_change(wb):
    acc = Accounts(wb.db)
    acc.create_user("ana", "Ana", "first-password", first_admin=True)
    with pytest.raises(ValueError):
        acc.change_password("ana", "not-it", "second-password")
    acc.change_password("ana", "first-password", "second-password")
    assert acc.login("ana", "second-password")[1]["username"] == "ana"


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
