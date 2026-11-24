import base64
import hashlib
import json
import mimetypes
import re
import time

import httpx

from app import providers

from app.config import settings
from app.providers import download_video, public_url
from app.media import probe
from schemas.pipeline import ScriptPlan
from .common import CONFIG, client, digest, dump, ffmpeg, read


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


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


def local_asset(name):
    path = (CONFIG / name).resolve()
    if not path.is_relative_to(CONFIG.resolve()) or not path.is_file():
        raise ValueError(f"Файл пресета отсутствует в config/: {name}")
    return path


def background_spec(scene, presets):
    key = scene.background_prompt_or_asset
    spec = presets.get(key)
    if spec is None:
        if scene.background_type == "color" and re.fullmatch(r"#[\da-fA-F]{6}", key):
            spec = {"type":"color", "value":key}
        elif scene.background_type == "image" and settings.generate_backgrounds:
            spec = {"type":"image", "prompt":key, "model":settings.background_image_model}
        else:
            raise ValueError(f"Неизвестный фон: {key}. Проверьте config/backgrounds.json")
    spec = dict(spec)
    if spec["type"] != scene.background_type:
        raise ValueError(f"Тип фона не совпадает: {key}")
    if "path" in spec:
        spec["sha256"] = digest(local_asset(spec["path"]))
    return spec


def resolve(spec, directory, aspect):
    kind = spec["type"]
    if kind == "color":
        if not re.fullmatch(r"#[\da-fA-F]{6}", spec["value"]):
            raise ValueError("Для фона нужен цвет #RRGGBB")
        return {"type":"color", "value":spec["value"]}
    if "url" in spec:
        return {"type":kind, "url":public_url(spec["url"])}
    key = fingerprint([spec, aspect])
    cached = directory / f"background-{key}.json"
    if cached.exists():
        return read(cached)
    if "path" in spec:
        path = local_asset(spec["path"])
    else:
        if kind != "image" or not settings.generate_backgrounds:
            raise ValueError("Для фона нужен url или path")
        portrait = aspect == "9:16"
        source = directory / f"image-{key}.png"
        if not source.exists():
            image = client().images.generate(model=settings.background_image_model,
                prompt=spec["prompt"], size="1024x1536" if portrait else "1536x1024")
            temp = source.with_suffix(".tmp")
            temp.write_bytes(base64.b64decode(image.data[0].b64_json))
            temp.replace(source)
        path = source.with_suffix(".jpg")
        width,height = (1080,1920) if portrait else (1920,1080)
        ffmpeg("-i", source, "-vf", f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}", "-frames:v",1,path)
    with path.open("rb") as stream:
        data = request("POST","https://upload.heygen.com/v1/asset",content=stream,
                       headers={"Content-Type":mimetypes.guess_type(path.name)[0] or "application/octet-stream"})
    result = {"type":kind, f"{kind}_asset_id":data["id"]}
    dump(cached,result)
    return result


def render(directory, aspect="9:16", *, studio_title=None):
    plan = ScriptPlan.model_validate(read(directory/"3_script_plan.json"))
    presets = read(CONFIG/"backgrounds.json")
    # Preserve requests already submitted by the earlier studio and by the CLI.
    legacy_key = None
    if studio_title is None or any(directory.glob("heygen-*.json")):
        try:
            specs = [background_spec(s, presets) for s in plan.scenes]
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
        primary_background = background_spec(plan.scenes[0], presets)
        if primary_background["type"] == "video":
            raise ValueError("В студии для HeyGen v3 выберите цвет или изображение для фона")
    key = legacy_key if legacy else fingerprint([
        "avatar-v3", directory.name, settings.heygen_avatar_id, settings.heygen_voice_id,
        settings.heygen_engine, aspect, script, primary_background])

    raw = directory/"4_heygen_raw.mp4"
    manifest = directory/"4_render.json"
    if raw.exists() and manifest.exists() and read(manifest).get("key") == key:
        return raw
    if not all((settings.heygen_api_key,settings.heygen_voice_id,settings.heygen_avatar_id)):
        raise ValueError("Заполните HEYGEN_API_KEY, HEYGEN_VOICE_ID и HEYGEN_AVATAR_ID")
    state = directory/f"heygen-{key}.json"
    if not state.exists():
        if not legacy:
            background = resolve(primary_background, directory, aspect)
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
                       "background":resolve(b,directory,aspect)} for s,b in zip(plan.scenes,specs)]
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
            dump(manifest,{"key":key,"video_id":video_id,"aspect":aspect})
            return raw
        if info["status"] in {"failed","cancelled"}:
            raise ValueError(f"HeyGen: {info['status']} ({video_id})")
        time.sleep(10)
    raise TimeoutError(f"HeyGen ещё обрабатывает {video_id}. Повтор продолжит ожидание.")
