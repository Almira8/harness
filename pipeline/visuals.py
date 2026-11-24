import time

import httpx

from app import providers

from app.config import settings
from app.providers import download_video
from app.media import probe
from schemas.pipeline import ScriptPlan
from .common import dump, read
from services import background_manager as backgrounds
from services.background_manager import fingerprint


def request(method, url, **kwargs):
    response = httpx.request(method, url, headers={"X-Api-Key": settings.heygen_api_key,
        **kwargs.pop("headers", {})}, timeout=120, **kwargs)
    if url.endswith("/v2/video/generate") and response.status_code in {400,401,403,404,422,429}:
        # A definite validation/authentication rejection did not create a video.
        response.raise_for_status()
    if not response.is_success:
        raise ValueError(f"HeyGen HTTP {response.status_code}: {response.text[:350]}")
    data = response.json()
    if data.get("error"):
        raise ValueError(f"HeyGen: {data['error']}")
    return data["data"]


def render(directory, aspect="9:16", *, studio_title=None, bg_mode=None, bg="studio_dark"):
    plan = ScriptPlan.model_validate(read(directory/"3_script_plan.json"))
    specs = backgrounds.scene_specs(plan, bg_mode, bg) if bg_mode else None
    edit_backgrounds = None
    if bg_mode == "remotion_greenscreen":
        edit_backgrounds = [backgrounds.stage_for_remotion(spec, directory) for spec in specs]
        specs = [backgrounds.GREEN] * len(plan.scenes)
    # Preserve requests already submitted by the earlier studio and by the CLI.
    legacy_key = None
    if studio_title is None or any(directory.glob("heygen-*.json")):
        try:
            if specs is None:
                specs = backgrounds.scene_specs(plan, None, bg)
        except ValueError:
            if studio_title is None:
                raise
        else:
            legacy_key = fingerprint([settings.heygen_avatar_id, settings.heygen_voice_id, aspect,
                [(s.spoken_text, s.camera_framing, b) for s, b in zip(plan.scenes, specs)]])
    legacy = studio_title is None or bool(legacy_key and (directory/f"heygen-{legacy_key}.json").exists())
    script = "\n\n".join(s.spoken_text.strip() for s in plan.scenes)
    primary_background = None
    if not legacy:
        primary_background = backgrounds.legacy_spec(plan.scenes[0], backgrounds.library())
        if primary_background["type"] == "video":
            raise ValueError("В студии для HeyGen v3 выберите цвет или изображение для фона")
    key = legacy_key if legacy else fingerprint([
        "avatar-v3", directory.name, settings.heygen_avatar_id, settings.heygen_voice_id,
        settings.heygen_engine, aspect, script, primary_background])

    raw = directory/"4_heygen_raw.mp4"
    manifest = directory/"4_render.json"
    render_info = {"key": key, "aspect": aspect}
    if edit_backgrounds is not None:
        render_info["backgrounds"] = edit_backgrounds
    if raw.exists() and manifest.exists():
        previous = read(manifest)
        if previous.get("key") == key:
            current = dict(render_info, video_id=previous.get("video_id"))
            if current != previous:
                dump(manifest, current)
            return raw
    if not all((settings.heygen_api_key,settings.heygen_voice_id,settings.heygen_avatar_id)):
        raise ValueError("Заполните HEYGEN_API_KEY, HEYGEN_VOICE_ID и HEYGEN_AVATAR_ID")
    state = directory/f"heygen-{key}.json"
    if not state.exists():
        if not legacy:
            background = backgrounds.resolve(primary_background, directory, aspect, request)
            if asset_id := background.pop("image_asset_id", None):
                background["asset_id"] = asset_id
            dump(state, {"status": "submitting"})
            try:
                data = providers.heygen_request("POST", "videos", {
                    "type": "avatar", "avatar_id": settings.heygen_avatar_id,
                    "voice_id": settings.heygen_voice_id, "script": script,
                    "title": studio_title, "engine": {"type": settings.heygen_engine},
                    "resolution": "1080p", "aspect_ratio": aspect, "background": background,
                }, key)
            except providers.HeyGenHTTPError as exc:
                if exc.status_code in {400, 401, 403, 404, 422, 429}:
                    state.unlink()
                raise
        else:
            inputs = [{"character":{"type":"avatar","avatar_id":settings.heygen_avatar_id,
                       "avatar_style":"normal","scale":1.35 if s.camera_framing == "closeup" else 1.0},
                       "voice":{"type":"text","voice_id":settings.heygen_voice_id,"input_text":s.spoken_text},
                       "background":backgrounds.resolve(b, directory, aspect, request)} for s,b in zip(plan.scenes,specs)]
            w,h = (1080,1920) if aspect == "9:16" else (1920,1080)
            dump(state, {"status":"submitting"})
            try:
                data = request("POST","https://api.heygen.com/v2/video/generate",
                               json={"video_inputs":inputs,"dimension":{"width":w,"height":h}})
            except httpx.HTTPStatusError as exc:
                state.unlink()
                raise ValueError(f"HeyGen отклонил v2/video/generate: HTTP {exc.response.status_code}") from exc
        video_id = data.get("video_id") or data.get("id")
        if not video_id:
            raise ValueError("HeyGen принял запрос без video_id. Проверьте кабинет")
        dump(state, {"video_id": video_id})
    video_id = read(state).get("video_id")
    if not video_id:
        raise ValueError(f"Отправка HeyGen не подтверждена. Сверьте кабинет и укажите video_id в {state.name}")
    deadline = time.monotonic()+1800
    while time.monotonic() < deadline:
        info = (request("GET", "https://api.heygen.com/v1/video_status.get", params={"video_id":video_id})
                if legacy else providers.heygen_request("GET", "videos/" + video_id))
        if info["status"] == "completed":
            temp = directory/"download.mp4"
            download_video(info["video_url"],temp)
            streams = {stream["codec_type"] for stream in probe(temp)["streams"]}
            if not {"audio", "video"} <= streams:
                raise ValueError(f"В результате HeyGen {video_id} нет видео или звуковой дорожки")
            # Clear old identity before replacing media so interrupted writes cannot reuse stale output.
            manifest.unlink(missing_ok=True)
            temp.replace(raw)
            dump(manifest, dict(render_info, video_id=video_id))
            return raw
        if info["status"] in {"failed","cancelled"}:
            raise ValueError(f"HeyGen: {info['status']} ({video_id})")
        time.sleep(10)
    raise TimeoutError(f"HeyGen ещё обрабатывает {video_id}. Повтор продолжит ожидание.")
