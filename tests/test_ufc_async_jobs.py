from __future__ import annotations

import time

import pytest

pytest.importorskip("torch")
import UFC_Elf.app as ufc_app


def _wait_for_terminal_state(client, job_id: str, timeout_seconds: float = 10.0):
    started = time.time()
    while (time.time() - started) < timeout_seconds:
        response = client.get(f"/ufc/api/jobs/{job_id}")
        assert response.status_code == 200
        payload = response.get_json()
        state = str(payload["job"]["state"]).lower()
        if state in {"succeeded", "failed"}:
            return payload["job"]
        time.sleep(0.05)
    raise AssertionError(f"Job {job_id} did not finish within {timeout_seconds} seconds")


def _wait_for_no_active_job(client, timeout_seconds: float = 10.0):
    started = time.time()
    while (time.time() - started) < timeout_seconds:
        response = client.get("/ufc/api/jobs/active")
        assert response.status_code == 200
        payload = response.get_json()
        if payload.get("job") is None:
            return
        time.sleep(0.05)
    raise AssertionError("Active job did not clear in time")


def test_async_job_rejects_invalid_action(client):
    response = client.post("/ufc/api/jobs", json={"action": "invalid_action"})
    assert response.status_code == 400
    payload = response.get_json()
    assert payload["ok"] is False


def test_async_update_job_reports_status_and_completes(client, monkeypatch):
    class FakePredictor:
        def reload_data(self):
            return {"data_rows": 123}

        def retrain_models(self, include_siamese=False, progress_cb=None):
            return {"data_rows": 123}

    def fake_scrape(progress_cb=None):
        if progress_cb is not None:
            progress_cb(18, "Scraping Data", "Processing 2 event(s).")
            progress_cb(60, "Scraping Data", "Completed 1/2 events...")
            progress_cb(88, "Scraping Data", "Scraper finished successfully.")
        return "Run complete."

    monkeypatch.setattr(ufc_app, "_get_predictor", lambda: FakePredictor())
    monkeypatch.setattr(ufc_app, "run_scraper_update", fake_scrape)

    start = client.post("/ufc/api/jobs", json={"action": "update_data"})
    assert start.status_code == 202
    payload = start.get_json()
    assert payload["ok"] is True
    job_id = payload["job"]["job_id"]
    assert isinstance(job_id, str) and job_id

    final_job = _wait_for_terminal_state(client, job_id)
    assert final_job["state"] == "succeeded"
    assert int(final_job["progress_pct"]) == 100
    assert final_job["result"]["system_status"]["data_rows"] == 123


def test_async_job_busy_returns_conflict(client, monkeypatch):
    class FakePredictor:
        def reload_data(self):
            return {"data_rows": 200}

        def retrain_models(self, include_siamese=False, progress_cb=None):
            return {"data_rows": 200}

    def slow_scrape(progress_cb=None):
        if progress_cb is not None:
            progress_cb(20, "Scraping Data", "Processing 1 event(s).")
        time.sleep(0.6)
        if progress_cb is not None:
            progress_cb(88, "Scraping Data", "Scraper finished successfully.")
        return "Run complete."

    monkeypatch.setattr(ufc_app, "_get_predictor", lambda: FakePredictor())
    monkeypatch.setattr(ufc_app, "run_scraper_update", slow_scrape)

    first = client.post("/ufc/api/jobs", json={"action": "update_data"})
    assert first.status_code == 202
    first_job_id = first.get_json()["job"]["job_id"]

    second = client.post("/ufc/api/jobs", json={"action": "update_data"})
    assert second.status_code == 409
    second_payload = second.get_json()
    assert second_payload["ok"] is False
    assert second_payload["active_job"] is not None

    final_job = _wait_for_terminal_state(client, first_job_id)
    assert final_job["state"] == "succeeded"
    _wait_for_no_active_job(client)
