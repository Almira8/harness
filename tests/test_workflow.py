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
        db.add(Asset(id="source", name="sample", path="uploads/sample.jpeg", kind="reference", duration=12))
        db.add_all([Reference(id="chosen", asset_id="source", notes="Монтаж"),
                    Reference(id="other", notes="Лимон")])
    received = {}
    def capture(prompt, data, schema, project_id, images=None):
        if schema is worker.agents.ReferenceReview:
            assert images == [{"type": "input_image", "image_url": "sample"}]
            assert data["transcript"] == {}
            return schema(enough_evidence=True, faithful=True, correction="")
        received.update(data)
        return Script.model_validate(script())
    monkeypatch.setattr(worker.agents, "sample_frames", lambda *args: [{"type": "input_image", "image_url": "sample"}])
    monkeypatch.setattr(worker.agents, "structured", capture)
    monkeypatch.setattr(worker.agents, "analyze_asset", lambda asset: ({}, "Монтаж по референсу"))
    assert api.post(f"/api/projects/{identifier}/generate", json={"reference_id": "chosen"}).status_code == 200
    worker.execute(worker.claim_job())
    assert received["adapt_selected_reference"]
    assert received["references"] == [{"url": "", "notes": "Монтаж", "analysis": "Монтаж по референсу"}]


def test_studio_accepts_jpeg_and_starts_script(api, monkeypatch):
    import subprocess
    from app.media import sample_frames

    jpeg = subprocess.check_output(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
                                    "color=red:s=64x96", "-frames:v", "1", "-f", "mjpeg", "pipe:1"])
    asset = api.post("/api/assets", data={"kind": "reference"},
                     files={"file": ("room.jpeg", jpeg, "image/jpeg")}).json()
    assert len(sample_frames(settings.data_dir / asset["path"], asset["duration"])) == 1
    ref = api.post("/api/references", json={"asset_id": asset["id"], "notes": "Свет"}).json()
    monkeypatch.setattr(settings, "openai_api_key", "test-only")
    p = api.post("/api/studio/from-reference", params={"reference_id": ref["id"]}).json()
    detail = api.get("/api/projects/" + p["id"]).json()
    assert detail["jobs"][0]["payload"]["reference_id"] == ref["id"]
    assert api.get("/api/queue").json() == []


def test_frame_analysis_without_shared_work_files(tmp_path, monkeypatch):
    import base64
    import subprocess
    from app.media import sample_frames

    source = tmp_path / "portrait.jpeg"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=s=96x128",
                    "-frames:v", "1", str(source)], check=True)
    monkeypatch.setattr(settings, "data_dir", tmp_path / "missing")
    frames = sample_frames(source, 0)
    assert len(frames) == 1
    assert base64.b64decode(frames[0]["image_url"].split(",")[1]).startswith(b"\xff\xd8")


def test_unrelated_reference_drafts_are_withheld(api, monkeypatch):
    identifier = project(api)
    with session() as db:
        db.add(Reference(id="montage", analysis="Исходники и примеры монтажа, правила склеек и субтитров"))
        stored = db.get(Project, identifier)
    drafts = []
    def model(prompt, data, schema, project_id, images=None):
        if schema is Script:
            drafts.append(data.copy())
            result = script()
            result["scenes"][0]["narration"] = "Наливаем кофе"
            return Script.model_validate(result)
        assert data["draft"]["scenes"][0]["narration"] == "Наливаем кофе"
        return schema(enough_evidence=True, faithful=False, correction="Верни демонстрацию склеек")
    monkeypatch.setattr(worker.agents, "structured", model)
    with pytest.raises(ValueError, match="дважды"):
        worker.agents.generate_script(stored, "", reference_id="montage")
    assert len(drafts) == 2
    assert "Сохрани задачу, метод и результат" in drafts[1]["required_corrections"]
    assert "Верни демонстрацию склеек" not in str(drafts[1])


def test_revising_pipeline_edit_keeps_video_until_replacement(api, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "test-only")
    identifier = project(api)
    with session() as db:
        record = db.get(Project, identifier)
        record.script = {**script(), "pipeline_job_id": identifier}
        record.mode, record.status = "avatar", "video_review"
        record.render_path = "renders/current.mp4"
        db.add(Asset(id="avatar", project_id=identifier, name="HeyGen",
                     kind="avatar", path="uploads/avatar.mp4", duration=10))
    result = api.post(f"/api/projects/{identifier}/edit", json={
        "feedback": "Убрать первую плашку", "auto_render": True,
    })
    assert result.status_code == 200
    assert result.json()["kind"] == "pipeline_edit"
    assert api.get(f"/api/projects/{identifier}").json()["render_path"] == "renders/current.mp4"

    def revision(record, job_id, assets, feedback):
        assert record.id == identifier
        assert feedback == "Убрать первую плашку"
        return "renders/revised.mp4"

    monkeypatch.setattr(worker.studio, "revise_edit", revision)
    worker.execute(worker.claim_job())
    assert api.get(f"/api/projects/{identifier}").json()["render_path"] == "renders/revised.mp4"


def test_video_script_exposes_concrete_screen_beats(api, monkeypatch, tmp_path):
    from pipeline import studio
    from pipeline.common import dump

    monkeypatch.setattr(settings, "openai_api_key", "test-only")
    monkeypatch.setattr(settings, "pipeline_workspace", tmp_path)
    with session() as db:
        db.add(Asset(id="source", name="sample.mov", path="uploads/sample.mov", kind="reference", duration=10))
        db.add(Reference(id="chosen", asset_id="source"))

    (settings.data_dir / "uploads" / "sample.mov").write_bytes(b"fixture")
    passes = []

    def extract(source, directory):
        passes.append("extract")
        dump(directory / "1_extracted.json", {"transcript": [], "screen_text": [], "ocr_warning": None})

    def analyze(directory):
        passes.append("analyze")
        dump(directory / "2_analysis.json", {"topic_problem": "Речь трудно разобрать", "core_insight": "Субтитры",
             "hook_formula": "Вопрос", "narrative_formula": "Проблема → решение", "visual_rhythm": "Перебивка"})

    def write(directory, *args, **kwargs):
        dump(directory / "3_script_plan.json", {
            "title": "Субтитры", "caption": "", "music": {"asset": None, "volume": 0},
            "subtitle_style": "minimal", "scenes": [{
                "scene_id": 1, "spoken_text": "Включаем субтитры.",
                "camera_framing": "closeup", "background_type": "color",
                "background_prompt_or_asset": "studio_dark", "overlay_text": "Субтитры",
                "b_roll_query": None, "screen_beats": [
                    {"speech": "Включаем", "action": "Ведущая крупно, справа окно редактора.",
                     "screen_text": "", "animation": ""},
                    {"speech": "субтитры.", "action": "В редакторе видна дорожка субтитров.",
                     "screen_text": "Субтитры", "animation": "Белая надпись плавно поднимается."},
                ],
            }],
        })

    monkeypatch.setattr(studio.extractor, "extract", extract)
    monkeypatch.setattr(studio.analyzer, "analyze", analyze)
    monkeypatch.setattr(studio.analyzer, "draft_check", lambda directory: lambda plan: True)
    monkeypatch.setattr(studio.scriptwriter, "write", write)
    identifier = project(api)
    result = api.post(f"/api/projects/{identifier}/generate", json={"reference_id": "chosen"}).json()
    assert not result["payload"].get("pipeline")
    worker.execute(worker.claim_job())
    saved = api.get(f"/api/projects/{identifier}").json()
    assert saved["jobs"][0]["result"]["script_strategy"] == "concept_rewrite"
    seconds = saved["script"]["seconds"]
    rewritten = api.post("/api/studio/from-reference", params={"reference_id": "chosen"}).json()
    worker.execute(worker.claim_job())
    assert api.get(f"/api/projects/{rewritten['id']}").json()["status"] == "script_review"
    assert passes == ["extract", "analyze"]
    assert [second["second"] for second in seconds] == [0, 1]
    assert seconds[0]["screen_text"] == ""
    assert seconds[1] == {
        "second": 1, "speech": "субтитры.", "action": "В редакторе видна дорожка субтитров.",
        "screen_text": "Субтитры", "animation": "Белая надпись плавно поднимается.", "sound": "Голос",
    }


def test_studio_upload_routes_background_to_pipeline(api, monkeypatch):
    from pipeline.common import ffmpeg

    for key in ("openai_api_key", "heygen_api_key", "heygen_avatar_id", "heygen_voice_id"):
        monkeypatch.setattr(settings, key, "test-only")
    file = settings.data_dir / "work" / "background.png"
    ffmpeg("-f", "lavfi", "-i", "color=s=64x96", "-frames:v", 1, "-threads", 1, file)
    response = api.post("/api/assets", data={"kind": "background"},
                        files={"file": ("room.png", file.read_bytes(), "image/png")})
    assert response.status_code == 201
    key = "upload:" + response.json()["id"]
    assert any(item["id"] == key for item in api.get("/api/backgrounds").json())
    identifier = project(api)
    approve(api, identifier)
    response = api.post(f"/api/projects/{identifier}/production", json={
        "mode": "avatar", "auto_render": True, "background_mode": "preset", "background": key,
    })
    assert response.status_code == 200
    job = api.get(f"/api/projects/{identifier}").json()["jobs"][0]
    assert job["kind"] == "pipeline_video" and job["payload"]["background"] == key


def test_background_swap_keeps_old_video_until_local_render_succeeds(api, monkeypatch, tmp_path):
    from pipeline.common import dump

    monkeypatch.setattr(settings, "pipeline_workspace", tmp_path)
    identifier = project(api)
    with session() as db:
        record = db.get(Project, identifier)
        record.mode = "avatar"
        record.status = "video_approved"
        record.render_path = "renders/old.mp4"
    directory = tmp_path / identifier
    directory.mkdir()
    dump(directory / "4_render.json", {"backgrounds": [{"kind": "color", "color": "#20352F"}]})
    assert api.get(f"/api/projects/{identifier}").json()["can_change_background"]

    def replace(record, job_id, preset):
        assert preset == "dark_minimal"
        return "renders/new.mp4"

    monkeypatch.setattr(worker.studio, "change_background", replace)
    response = api.post(f"/api/projects/{identifier}/background", json={"background": "dark_minimal"})
    assert response.status_code == 200 and response.json()["kind"] == "pipeline_background"
    assert api.get(f"/api/projects/{identifier}").json()["render_path"] == "renders/old.mp4"
    worker.execute(worker.claim_job())
    result = api.get(f"/api/projects/{identifier}").json()
    assert result["status"] == "video_review" and result["render_path"] == "renders/new.mp4"


def test_topic_route_can_choose_hook_and_save_approved_lines(api, monkeypatch, tmp_path):
    from pipeline import studio
    from schemas.pipeline import ScriptPlan

    monkeypatch.setattr(settings, "openai_api_key", "test-only")
    monkeypatch.setattr(settings, "pipeline_workspace", tmp_path)

    def prepare(topic, notes, hook_style, *, directory, feedback):
        assert (topic, notes, hook_style) == ("Субтитры", "Пример на рецепте", "mistake")
        from pipeline.common import dump

        dump(directory / "3_script_plan.json", ScriptPlan.model_validate({
            "title": topic, "caption": "", "music": {"asset": None, "volume": 0},
            "subtitle_style": "minimal", "selected_hook": 0, "cover_hook_text": "Где теряются слова в кадре",
            "alternative_hooks": [
                {"spoken_hook": "Смотри на экран.", "cover_hook_text": "Где теряются слова в кадре"},
                {"spoken_hook": "Ты добавляешь титры вручную?", "cover_hook_text": "Твоя речь пропадает без титров"},
                {"spoken_hook": "В ролике пропал звук?", "cover_hook_text": "Как сохранить речь без звука"},
            ],
            "scenes": [{"scene_id": 1, "spoken_text": "Смотри на экран. Добавим дорожку.",
                        "camera_framing": "closeup", "background_type": "color",
                        "background_prompt_or_asset": "studio_dark", "overlay_text": None, "b_roll_query": None}],
        }).model_dump())

    monkeypatch.setattr(studio.scriptwriter, "generate_from_topic", prepare)
    monkeypatch.setattr(studio.extractor, "extract", lambda *_: pytest.fail("Extracted a reference"))
    monkeypatch.setattr(studio.analyzer, "analyze", lambda *_: pytest.fail("Analyzed a reference"))
    result = api.post("/api/studio/from-topic", json={
        "topic": "Субтитры", "notes": "Пример на рецепте", "hook_style": "mistake",
    })
    identifier = result.json()["id"]
    worker.execute(worker.claim_job())
    saved = api.get(f"/api/projects/{identifier}").json()
    assert saved["jobs"][0]["result"]["script_strategy"] == "topic"
    assert len(saved["script"]["alternative_hooks"]) == 3
    response = api.post(f"/api/projects/{identifier}/script-review", json={
        "cover_hook_text": "Твоя речь пропадает без титров",
        "scenes": ["Ты добавляешь титры вручную? Добавим дорожку."],
        "selected_hook": 1,
    })
    assert response.status_code == 200
    assert api.post(f"/api/projects/{identifier}/approve-script").status_code == 200
    with session() as db:
        record = db.get(Project, identifier)
        approved = studio.approved_plan(record, tmp_path / identifier)
    assert approved.selected_hook == 1
    assert approved.cover_hook_text == "Твоя речь пропадает без титров"
    assert approved.scenes[0].spoken_text.startswith("Ты добавляешь титры вручную?")
