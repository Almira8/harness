"""Select, generate, stage and upload avatar backgrounds."""
import base64
import fcntl
import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path

from app.config import ROOT, settings
from app.media import probe
from app.providers import public_url
from pipeline.common import CONFIG, client, digest, dump, ffmpeg, read

MODES = ("preset", "auto_match", "ai_generate", "remotion_greenscreen")
MEDIA = {".jpg": ("image", "image/jpeg"), ".jpeg": ("image", "image/jpeg"),
         ".png": ("image", "image/png"), ".mp4": ("video", "video/mp4")}
GREEN = {"type": "color", "value": "#00FF00"}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def library():
    return read(CONFIG / "backgrounds.json")


def local_asset(name):
    path = (CONFIG / name).resolve()
    if not path.is_relative_to(CONFIG.resolve()) or not path.is_file():
        raise ValueError(f"Файл пресета отсутствует в config/: {name}")
    return path


def background_file(name):
    root = (ROOT / "assets" / "backgrounds").resolve()
    path = (ROOT / name).resolve()
    if path.is_relative_to(root) and path.is_file():
        return path
    # Existing installations may still store their media relative to config/.
    return local_asset(name)


def preset_spec(name, presets):
    if name not in presets:
        raise ValueError(f"Неизвестный фон: {name}. Проверьте config/backgrounds.json")
    spec = {key: value for key, value in presets[name].items() if key != "description"}
    kind = spec["type"]
    if kind == "color":
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", spec.get("value", "")):
            raise ValueError(f"Для фона {name} нужен цвет #RRGGBB")
    elif kind not in {"image", "video"} or bool(spec.get("path")) == bool(spec.get("url")):
        raise ValueError(f"Для фона {name} укажите тип image/video и один источник path или url")
    if "path" in spec:
        path = background_file(spec["path"])
        if MEDIA.get(path.suffix.lower(), (None,))[0] != kind:
            raise ValueError(f"Расширение файла не соответствует типу фона {name}")
        spec["sha256"] = digest(path)
    return spec


def legacy_spec(scene, presets):
    name = scene.background_prompt_or_asset
    if name in presets:
        spec = preset_spec(name, presets)
    elif scene.background_type == "color" and re.fullmatch(r"#[0-9a-fA-F]{6}", name):
        return {"type": "color", "value": name}
    elif scene.background_type == "image" and settings.generate_backgrounds:
        return {"type": "image", "prompt": name, "model": settings.background_image_model}
    else:
        raise ValueError(f"Неизвестный фон: {name}. Проверьте config/backgrounds.json")
    if spec["type"] != scene.background_type:
        raise ValueError(f"Тип фона не совпадает: {name}")
    return spec


def generate(directory, filename, prompt, aspect, model):
    path = directory / filename
    stamp = path.with_suffix(".json")
    size = (1080, 1920) if aspect == "9:16" else (1920, 1080)
    key = fingerprint([model, prompt, size])
    if path.is_file() and stamp.is_file() and read(stamp).get("key") == key:
        return path
    response = client().images.generate(model=model, prompt=prompt,
                                       size="1024x1536" if aspect == "9:16" else "1536x1024")
    with tempfile.TemporaryDirectory(dir=directory) as temp:
        original = Path(temp) / "original.png"
        original.write_bytes(base64.b64decode(response.data[0].b64_json))
        cropped = original.with_name("cropped.png")
        width, height = size
        ffmpeg("-i", original, "-vf",
               f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1",
               "-frames:v", 1, "-threads", 1, cropped)
        stamp.unlink(missing_ok=True)
        cropped.replace(path)
        dump(stamp, {"key": key})
    return path


def scene_specs(plan, mode, bg):
    presets = library()
    if mode is None:
        return [legacy_spec(scene, presets) for scene in plan.scenes]
    if mode == "preset":
        return [preset_spec(bg, presets)] * len(plan.scenes)
    if mode in {"auto_match", "remotion_greenscreen"}:
        return [preset_spec(scene.background_prompt_or_asset, presets) for scene in plan.scenes]
    return [
        {"type": "image", "generated": f"bg_scene_{index}.png",
         "model": settings.background_image_model,
         "prompt": f"Create a clean background plate behind a centered speaking avatar. No people, text, logos or foreground objects. "
                   f"Leave the center visually quiet. Topic: {plan.title}. Scene: {scene.spoken_text}. "
                   f"Art direction: {scene.background_prompt_or_asset}"}
        for index, scene in enumerate(plan.scenes, 1)
    ]


def resolve(spec, directory, aspect, request):
    kind = spec["type"]
    if kind == "color":
        return {"type": kind, "value": spec["value"]}
    if "url" in spec:
        return {"type": kind, "url": public_url(spec["url"])}
    if "path" in spec:
        path = background_file(spec["path"])
    else:
        name = spec.get("generated", f"image-{fingerprint([spec, aspect])}.png")
        path = generate(directory, name, spec["prompt"], aspect, spec["model"])
    mime = MEDIA[path.suffix.lower()][1]
    cache = settings.data_dir / "work" / "heygen-backgrounds"
    cache.mkdir(parents=True, exist_ok=True)
    identity = fingerprint([settings.heygen_api_key, mime, digest(path)])
    stored = cache / f"{identity}.json"
    with stored.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if stored.is_file():
            return read(stored)
        with path.open("rb") as stream:
            data = request("POST", "https://upload.heygen.com/v1/asset", content=stream,
                           headers={"Content-Type": mime})
        asset_id = data.get("asset_id") or data.get("id")
        if not asset_id:
            raise ValueError("HeyGen Asset API не вернул asset_id")
        result = {"type": kind, f"{kind}_asset_id": asset_id}
        dump(stored, result)
        return result


def stage_for_remotion(spec, directory):
    kind = spec["type"]
    if kind == "color":
        return {"kind": kind, "color": spec["value"]}
    if "path" not in spec:
        raise ValueError("Для фонового слоя Remotion укажите локальный path в пресете")
    path = background_file(spec["path"])
    target = directory / "backgrounds" / f'{spec["sha256"]}{path.suffix.lower()}'
    target.parent.mkdir(exist_ok=True)
    if not target.is_file():
        partial = target.with_suffix(".part")
        shutil.copyfile(path, partial)
        partial.replace(target)
    item = {"kind": kind, "src": target.relative_to(directory).as_posix()}
    if kind == "video":
        item["mediaDuration"] = float(probe(target)["format"]["duration"])
    return item
