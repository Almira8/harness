import base64
import shutil
from types import SimpleNamespace

from app.config import settings
from app.media import probe
from pipeline import composer, visuals
from pipeline.common import dump, ffmpeg, read
from pipeline.edit_director import Word
from services import background_manager as manager
from test_pipeline import plan


def test_hands_each_local_media_type_to_heygen_and_reuses_upload(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "pipeline_workspace", tmp_path / "workspace")
    monkeypatch.setattr(manager, "ROOT", tmp_path)
    assets = tmp_path / "assets" / "backgrounds"
    assets.mkdir(parents=True)
    calls = []

    def upload(method, url, *, content, headers):
        assert method == "POST" and url == "https://upload.heygen.com/v1/asset"
        calls.append((headers["Content-Type"], content.read()))
        return {"asset_id": str(len(calls))}

    for suffix, (kind, mime) in manager.MEDIA.items():
        path = assets / f"example{suffix}"
        path.write_bytes(suffix.encode())
        spec = manager.preset_spec("demo", {"demo": {
            "type": kind, "path": path.relative_to(tmp_path).as_posix(),
        }})
        result = manager.resolve(spec, tmp_path / "job-one", "9:16", upload)
        assert calls[-1] == (mime, suffix.encode())
        assert result == {"type": kind, f"{kind}_asset_id": str(len(calls))}
        assert manager.resolve(spec, tmp_path / "job-two", "16:9", upload) == result
    assert len(calls) == len(manager.MEDIA)
    assert len(read(tmp_path / "workspace" / "asset_cache.json")) == len(manager.MEDIA)


def test_ai_frame_is_portrait_png_and_cached(tmp_path, monkeypatch):
    original = tmp_path / "sample.png"
    ffmpeg("-f", "lavfi", "-i", "color=blue:s=120x180", "-frames:v", 1, "-threads", 1, original)
    calls = []

    def generate(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(original.read_bytes()))])

    monkeypatch.setattr(manager, "client", lambda: SimpleNamespace(images=SimpleNamespace(generate=generate)))
    target = manager.generate(tmp_path, "bg_scene_1.png", "Офис", "9:16", "gpt-image-1")
    assert target.name == "bg_scene_1.png"
    image = probe(target)["streams"][0]
    assert (image["codec_name"], image["width"], image["height"]) == ("png", 1080, 1920)
    assert manager.generate(tmp_path, target.name, "Офис", "9:16", "gpt-image-1") == target
    assert len(calls) == 1


def test_greenscreen_keeps_preset_selection_outside_heygen(tmp_path, monkeypatch):
    for name in ("heygen_api_key", "heygen_voice_id", "heygen_avatar_id"):
        monkeypatch.setattr(settings, name, "test-only")
    draft = plan()
    draft.scenes[1].background_prompt_or_asset = "tech_neon"
    dump(tmp_path / "3_script_plan.json", draft.model_dump())
    source = tmp_path / "test.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=green:s=180x320:r=30", "-f", "lavfi", "-i",
           "sine=frequency=440", "-t", 2, source)
    calls = []

    def request(method, url, **kwargs):
        calls.append(method)
        if method == "POST":
            assert [item["background"] for item in kwargs["json"]["video_inputs"]] == [manager.GREEN] * 2
            return {"video_id": "test"}
        return {"status": "completed", "video_url": "fixture"}

    monkeypatch.setattr(visuals, "request", request)
    monkeypatch.setattr(visuals, "download_video", lambda url, dest: shutil.copyfile(source, dest))
    visuals.render(tmp_path, bg_mode="remotion_greenscreen")
    specs = read(tmp_path / "4_render.json")["backgrounds"]
    assert [spec["color"] for spec in specs] == ["#20352F", "#5426A8"]
    cues = composer.background_cues(draft.scenes, specs, [
        Word(word="Второй", startSec=1.25, endSec=1.5),
    ], 2)
    assert [(cue.startSec, cue.endSec) for cue in cues] == [(0, 1.25), (1.25, 2)]
    draft.scenes[1].background_prompt_or_asset = "minimal_beige"
    dump(tmp_path / "3_script_plan.json", draft.model_dump())
    visuals.render(tmp_path, bg_mode="remotion_greenscreen")
    assert read(tmp_path / "4_render.json")["backgrounds"][1]["color"] == "#DCC9AF"
    assert calls == ["POST", "GET"]


def test_explicit_web_video_preset_uses_v2_and_asset_ids(tmp_path, monkeypatch):
    for name in ("heygen_api_key", "heygen_voice_id", "heygen_avatar_id"):
        monkeypatch.setattr(settings, name, "test-only")
    monkeypatch.setattr(settings, "pipeline_workspace", tmp_path / "workspace")
    monkeypatch.setattr(manager, "ROOT", tmp_path)
    library = tmp_path / "assets" / "backgrounds"
    library.mkdir(parents=True)
    clip = library / "loop.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=s=64x96", "-f", "lavfi", "-i",
           "sine=frequency=440", "-t", 1, clip)
    dump(tmp_path / "3_script_plan.json", plan().model_dump())
    calls = []

    def request(method, url, **kwargs):
        calls.append(url)
        if "upload.heygen.com" in url:
            assert kwargs["headers"]["Content-Type"] == "video/mp4"
            return {"asset_id": "loop"}
        if method == "POST":
            assert url.endswith("/v2/video/generate")
            assert [item["background"] for item in kwargs["json"]["video_inputs"]] == [
                {"type": "video", "video_asset_id": "loop"},
            ] * 2
            return {"video_id": "test"}
        return {"status": "completed", "video_url": "fixture"}

    monkeypatch.setattr(visuals, "request", request)
    monkeypatch.setattr(visuals, "download_video", lambda url, dest: shutil.copyfile(clip, dest))
    visuals.render(tmp_path, studio_title="Тест", bg_mode="preset",
                   bg="file:loop.mp4", presets=manager.studio_library())
    assert sum("upload.heygen.com" in url for url in calls) == 1
