import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_temporary = tempfile.TemporaryDirectory()
os.environ["DATABASE_URL"] = "sqlite:///" + str(Path(_temporary.name) / "test.db")
os.environ["DATA_DIR"] = str(Path(_temporary.name) / "data")

from app.config import settings
from app.db import Asset, Base, Job, Project, Reference, Session, engine, session
from app.main import app
from app.schemas import EditPlan, Script
from app import worker


@pytest.fixture(autouse=True)
def clean_database(monkeypatch):
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "heygen_api_key", "")
    monkeypatch.setattr(settings, "heygen_avatar_id", "")
    monkeypatch.setattr(settings, "heygen_voice_id", "")
    monkeypatch.setattr(settings, "metricool_token", "")
    monkeypatch.setattr(settings, "metricool_user_id", "")


@pytest.fixture
def api():
    with TestClient(app) as client:
        yield client


def script():
    return {
        "title": "Проверяем ИИ", "hook_options": ["Первый", "Второй", "Третий"],
        "rationale": "Показать результат", "duration": 10, "caption": "Описание",
        "facts_to_verify": [],
        "scenes": [{"start": start, "end": end, "narration": "Реплика",
                    "screen_text": "Пример", "graphic": "Карточка", "layout": "presenter",
                    "animation": "rise", "filming": "Сидеть в кадре", "materials": []}
                   for start, end in [(0, 5), (5, 10)]],
        "seconds": [{"second": second, "speech": "Речь", "action": "Автор в кадре",
                     "animation": "Карточка появляется", "sound": "Голос"} for second in range(10)],
    }


def project(api):
    return api.post("/api/projects", json={"title": "Тест", "brief": "Снять Reels"}).json()["id"]


def approve(api, identifier):
    assert api.put(f"/api/projects/{identifier}/script", json=script()).status_code == 200
    assert api.post(f"/api/projects/{identifier}/approve-script").status_code == 200


def test_production_waits_for_approved_storyboard(api):
    identifier = project(api)
    assert api.post(f"/api/projects/{identifier}/production", json={"mode": "own"}).status_code == 409
    approve(api, identifier)
    response = api.post(f"/api/projects/{identifier}/production", json={"mode": "own"})
    assert response.status_code == 200
    assert response.json()["status"] == "production"
    assert api.post(f"/api/projects/{identifier}/approve-video").status_code == 409


def test_avatar_is_optional_and_configuration_is_required(api):
    identifier = project(api)
    approve(api, identifier)
    assert api.post(f"/api/projects/{identifier}/production", json={"mode": "avatar"}).status_code == 422
    assert api.get(f"/api/projects/{identifier}").json()["status"] == "script_approved"
    assert api.get("/api/status").json()["heygen"] is False


def test_storyboard_requires_every_second_and_contiguous_scenes(api):
    identifier = project(api)
    invalid = script()
    invalid["seconds"].pop(4)
    assert api.put(f"/api/projects/{identifier}/script", json=invalid).status_code == 422
    invalid = script()
    invalid["scenes"][1]["start"] = 6
    assert api.put(f"/api/projects/{identifier}/script", json=invalid).status_code == 422


def test_edits_invalidate_previous_approval(api):
    identifier = project(api)
    approve(api, identifier)
    assert api.put(f"/api/projects/{identifier}/script", json=script()).status_code == 200
    assert api.post(f"/api/projects/{identifier}/production", json={"mode": "own"}).status_code == 409


def test_duplicate_generation_is_blocked(api, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "test-only")
    identifier = project(api)
    assert api.post(f"/api/projects/{identifier}/generate", json={}).status_code == 200
    assert api.post(f"/api/projects/{identifier}/generate", json={}).status_code == 409
    assert worker.claim_job() is not None
    assert worker.claim_job() is None


def test_worker_stores_generated_script_and_job_result(api, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "test-only")
    monkeypatch.setattr(worker.agents, "generate_script", lambda *args: script())
    identifier = project(api)
    job_id = api.post(f"/api/projects/{identifier}/generate", json={}).json()["id"]
    assert worker.claim_job() == job_id
    worker.execute(job_id)
    result = api.get(f"/api/projects/{identifier}").json()
    assert result["status"] == "script_review"
    assert result["script"]["duration"] == 10
    assert result["jobs"][0]["status"] == "completed"


def test_uncertain_external_post_is_not_retried_blindly(api):
    identifier = project(api)
    with session() as db:
        job = Job(project_id=identifier, kind="schedule", status="attention", payload={})
        db.add(job)
        db.flush()
        job_id = job.id
    assert api.post(f"/api/jobs/{job_id}/retry").status_code == 409


def test_known_heygen_id_can_resume_without_new_generation(api):
    identifier = project(api)
    with session() as db:
        job = Job(project_id=identifier, kind="avatar", status="attention", payload={},
                  result={"video_id": "existing-video"})
        db.add(job)
        db.flush()
        job_id = job.id
    assert api.post(f"/api/jobs/{job_id}/retry").status_code == 200


def test_montage_cannot_read_reference_or_overrun_source(api):
    identifier = project(api)
    approve(api, identifier)
    api.post(f"/api/projects/{identifier}/production", json={"mode": "own"})
    with session() as db:
        db.add(Asset(id="footage", project_id=identifier, name="Record", path="uploads/x.mp4",
                     kind="footage", duration=6))
    plan = {"duration": 10, "clips": [{"asset_id": "footage", "start": 0,
            "source_start": 0, "duration": 10, "layout": "presenter", "muted": False,
            "zoom": 1}], "captions": [], "overlays": [], "notes": ""}
    assert api.put(f"/api/projects/{identifier}/edit", json=plan).status_code == 422
    plan["duration"] = 6
    plan["clips"][0]["duration"] = 6
    assert api.put(f"/api/projects/{identifier}/edit", json=plan).status_code == 200


def test_secrets_are_not_in_status_response_and_cross_site_writes_fail(api, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "secret-test-key")
    assert "secret-test-key" not in api.get("/api/status").text
    assert api.post("/api/projects", headers={"Origin": "https://other.example"},
                    json={"title": "Тест", "brief": "Тест"}).status_code == 403
    assert api.get("/media/../../.env").status_code == 404


def test_automatic_edit_stops_at_video_review(api, monkeypatch):
    identifier = project(api)
    with session() as db:
        job = Job(project_id=identifier, kind="edit", payload={"auto_render": True})
        db.add(job)
    monkeypatch.setattr(worker.agents, "create_edit", lambda *args: {"notes": "preview"})
    monkeypatch.setattr(worker, "render", lambda *args: "renders/preview.mp4")
    worker.execute(worker.claim_job())
    render_id = worker.claim_job()
    assert render_id
    worker.execute(render_id)
    result = api.get(f"/api/projects/{identifier}").json()
    assert result["status"] == "video_review"
    assert result["render_path"] == "renders/preview.mp4"
    assert worker.claim_job() is None


def test_selected_reference_is_analyzed_and_other_examples_are_excluded(api, monkeypatch):
    identifier = project(api)
    monkeypatch.setattr(settings, "openai_api_key", "test-only")
    with session() as db:
        db.add(Asset(id="source", name="sample", path="uploads/sample.mov", kind="reference", duration=12))
        db.add_all([Reference(id="chosen", asset_id="source", notes="Монтаж"),
                    Reference(id="other", notes="Лимон")])
    received = {}
    def capture(prompt, data, schema, project_id):
        received.update(data)
        return Script.model_validate(script())
    monkeypatch.setattr(worker.agents, "structured", capture)
    monkeypatch.setattr(worker.agents, "analyze_asset", lambda asset: ({}, "Монтаж по референсу"))
    assert api.post(f"/api/projects/{identifier}/generate", json={"reference_id": "chosen"}).status_code == 200
    worker.execute(worker.claim_job())
    assert received["adapt_selected_reference"]
    assert received["references"] == [{"url": "", "notes": "Монтаж", "analysis": "Монтаж по референсу"}]
