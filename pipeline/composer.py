import difflib
import os
import re
import shutil
import subprocess
from pathlib import Path

from app.config import ROOT
from app.media import probe
from schemas.pipeline import Phrase, ScriptPlan
from .common import digest, dump, ffmpeg, read, whisper
from .edit_director import BackgroundCue, Callout, CoverHook, EditDirector, RenderProps, Word, Zoom, direct
from services.background_manager import local_asset

STUDIO = ROOT / "remotion-studio"
IMAGE = {".jpg",".jpeg",".png",".webp"}
VIDEO = {".mov",".mp4",".webm",".m4v"}


def preflight(plan):
    if not all(shutil.which(binary) for binary in ("ffmpeg", "ffprobe", "npx")):
        raise ValueError("Для монтажа нужны ffmpeg, ffprobe и npx")
    if not (STUDIO / "node_modules" / ".bin" / "remotion").is_file():
        raise ValueError("Не установлены зависимости Remotion. Пересобери контейнер worker.")
    if plan.music.asset:
        local_asset(plan.music.asset)


def source(directory, name):
    relative = Path(name)
    path = (directory / relative).resolve()
    if relative.is_absolute() or ".." in relative.parts or not path.is_relative_to(directory) or not path.is_file():
        raise ValueError(f"Нет медиафайла в папке выпуска: {name}")
    return path


def words_for(directory, raw, identity):
    cache = directory / f"words-{identity}.json"
    if not cache.exists():
        audio = directory / "4_voice.wav"
        ffmpeg("-i",raw,"-vn","-ar",16000,"-ac",1,audio)
        phrases = whisper(audio,"word")
        if not phrases:
            raise ValueError("Whisper не вернул пословные таймкоды")
        dump(cache,[p.model_dump() for p in phrases])
    phrases = [Phrase.model_validate(p) for p in read(cache)]
    return [Word(word=p.text,startSec=p.start,endSec=p.end) for p in phrases]


def asset_catalog(directory):
    catalog = []
    for p in sorted((directory/"broll").rglob("*")):
        extension = p.suffix.lower()
        if not p.is_file() or extension not in IMAGE | VIDEO:
            continue
        name = p.relative_to(directory).as_posix()
        entry = {"src":name}
        path = source(directory,name)
        if extension in VIDEO:
            entry["duration"] = float(probe(path)["format"]["duration"])
        catalog.append(entry)
    return catalog


def validate_cues(props, duration, directory):
    for layer in (props.zoom_events, props.b_roll, props.callouts, props.backgrounds):
        previous = 0
        for cue in layer:
            if cue.startSec < previous or cue.endSec > duration + 0.034:
                raise ValueError("Интервалы монтажа должны идти по порядку внутри ролика")
            previous = cue.endSec
    for cue in props.b_roll:
        path = source(directory,cue.src)
        if path.suffix.lower() not in VIDEO | IMAGE:
            raise ValueError("Неизвестный формат B-roll")
        if path.suffix.lower() in VIDEO and cue.endSec-cue.startSec > float(probe(path)["format"]["duration"])+0.034:
            raise ValueError(f"Перебивка {cue.src} короче выбранного интервала")


def normalize_cues(cues, duration, assets=None):
    result = []
    previous = 0.0
    for cue in sorted(cues, key=lambda cue: cue.startSec):
        start = max(previous, cue.startSec)
        end = min(duration, cue.endSec)
        updates = {}
        if assets is not None:
            if cue.src not in assets:
                continue
            length = assets[cue.src].get("duration")
            if length is not None:
                end = min(end, start + length)
        if end - start < 0.12:
            continue
        if hasattr(cue, "fromScale"):
            updates["fromScale"] = result[-1].toScale if result else 1.0
        result.append(cue.model_copy(update={"startSec": start, "endSec": end, **updates}))
        previous = end
    return result


def background_cues(scenes, backgrounds, words, duration):
    if len(scenes) != len(backgrounds):
        raise ValueError("Список фонов устарел. Повторите этап 4.")
    scene_tokens = [re.findall(r"\w+", scene.spoken_text.casefold()) or [""] for scene in scenes]
    spoken = [(token, word.startSec) for word in words
              for token in re.findall(r"\w+", word.word.casefold())]
    tokens = [token for scene in scene_tokens for token in scene]
    matches = difflib.SequenceMatcher(None, tokens, [token for token, _ in spoken], autojunk=False)
    timed = {a + i: spoken[b + i][1] for a, b, size in matches.get_matching_blocks() for i in range(size)}
    boundaries = [0.0]
    offset = 0
    for index, scene in enumerate(scene_tokens[:-1], 1):
        offset += len(scene)
        fallback = duration * offset / len(tokens)
        next_words = (timed[i] for i in range(offset, offset + len(scene_tokens[index])) if i in timed)
        start = next(next_words, fallback)
        if not boundaries[-1] < start < duration:
            start = boundaries[-1] + (duration - boundaries[-1]) * len(scene) / (len(tokens) - offset + len(scene))
        boundaries.append(start)
    return [
        BackgroundCue(startSec=start, endSec=end, **spec)
        for start, end, spec in zip(boundaries, boundaries[1:] + [duration], backgrounds)
    ]


def transparent_avatar(directory, raw, raw_id):
    path = directory / f"avatar-keyed-{raw_id[:16]}.webm"
    if not path.is_file():
        partial = path.with_name("avatar-keyed-rendering.webm")
        ffmpeg("-i", raw, "-vf", "chromakey=0x00FF00:0.18:0.07,format=yuva420p,despill=type=green",
               "-c:v", "libvpx-vp9", "-crf", 22, "-b:v", 0, "-auto-alt-ref", 0,
               "-c:a", "libopus", partial)
        partial.replace(path)
    return path


def local_timeline(plan, duration):
    script_words = [word for scene in plan.scenes for word in scene.spoken_text.split()]
    if not script_words:
        raise ValueError("Для монтажа нужен утверждённый текст реплик")
    weights = [max(3, len(word.strip(".,!?…"))) for word in script_words]
    total = sum(weights)
    words, elapsed = [], 0
    for word, weight in zip(script_words, weights):
        start = duration * elapsed / total
        elapsed += weight
        words.append(Word(word=word, startSec=start, endSec=duration * elapsed / total))
    cuts = EditDirector(zoom_events=[], callouts=[], b_roll=[])
    offset = 0
    scale = 1.0
    for scene in plan.scenes:
        length = len(scene.spoken_text.split())
        if not length:
            continue
        start = words[offset].startSec
        offset += length
        end = words[offset - 1].endSec
        next_scale = 1.1 if scene.camera_framing == "closeup" else 1.0
        if scale != next_scale:
            cuts.zoom_events.append(Zoom(startSec=start, endSec=min(end, start + 0.5),
                                        fromScale=scale, toScale=next_scale))
            scale = next_scale
        visible_start = max(start, plan.cover_hook_duration_sec if plan.cover_hook_text else 0)
        if scene.overlay_text and end - visible_start >= 0.12:
            cuts.callouts.append(Callout(startSec=visible_start, endSec=min(end, visible_start + 2.5),
                                        title=scene.overlay_text[:140], subtitle=None))
    return words, cuts


def prepare(directory, raw, raw_id, duration, feedback="", backgrounds=(), local_edit=False):
    plan = ScriptPlan.model_validate(read(directory/"3_script_plan.json"))
    if local_edit:
        words, cuts = local_timeline(plan, duration)
        catalog = []
    else:
        words = words_for(directory, raw, raw_id)
        catalog = asset_catalog(directory)
        previous = read(directory / "remotion_props.json") if feedback and (directory / "remotion_props.json").exists() else None
        cuts = direct(plan, words, duration, catalog, feedback, previous)
    cuts.zoom_events = normalize_cues(cuts.zoom_events, duration)
    cuts.callouts = normalize_cues(cuts.callouts, duration)
    cuts.b_roll = normalize_cues(cuts.b_roll, duration, {a["src"]: a for a in catalog})
    music = None
    if plan.music.asset:
        track = local_asset(plan.music.asset)
        dest = directory/"music"/track.name
        dest.parent.mkdir(exist_ok=True)
        shutil.copy2(track,dest)
        music = dest.relative_to(directory).as_posix()
    avatar = transparent_avatar(directory, raw, raw_id) if backgrounds else raw
    cues = background_cues(plan.scenes, backgrounds, words, duration) if backgrounds else []
    cover = (CoverHook(text=plan.cover_hook_text, endSec=min(duration, plan.cover_hook_duration_sec))
             if plan.cover_hook_text else None)
    return RenderProps(**cuts.model_dump(), coverHook=cover,jobId=directory.name,avatar=avatar.name,
        transparentAvatar=bool(backgrounds), backgrounds=cues,
        words=words,music=music,volume=min(plan.music.volume,0.12),accent="#CBFF68",
        subtitle_style=plan.subtitle_style)


def extract_cover(video, destination):
    partial = destination.with_name(destination.stem + "-extracting.jpg")
    partial.unlink(missing_ok=True)
    try:
        for seconds in ("0.200", "0"):
            ffmpeg("-ss", seconds, "-i", video, "-map", "0:v:0", "-frames:v", 1, "-q:v", 2, partial)
            if partial.is_file():
                partial.replace(destination)
                return
        raise ValueError("Не удалось извлечь кадр обложки")
    finally:
        partial.unlink(missing_ok=True)


def render(directory, props, props_path=None):
    stage = STUDIO/"public"/"assets"/directory.name
    output = directory/"5_final_output.mp4"
    partial = directory/"5_rendering.mp4"
    for name in {props.avatar, *(c.src for c in props.b_roll), *(c.src for c in props.backgrounds if c.src), *([props.music] if props.music else [])}:
        path = source(directory,name)
        target = stage / name
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(path,target)
    cmd = ["npx","--no-install","remotion","render","src/index.ts","MainComposition",str(partial),
           "--props="+str(props_path or directory/"remotion_props.json"),"--concurrency=2","--overwrite"]
    if browser := os.getenv("REMOTION_BROWSER_EXECUTABLE"):
        cmd.append("--browser-executable="+browser)
    proc = subprocess.run(cmd,cwd=STUDIO,capture_output=True,text=True,timeout=1800)
    if proc.returncode:
        raise ValueError("Remotion: "+(proc.stdout+"\n"+proc.stderr)[-2200:])
    metadata = probe(partial)
    streams = {stream["codec_type"] for stream in metadata["streams"]}
    if not {"video", "audio"} <= streams:
        raise ValueError("В финальном MP4 отсутствует видео или звук")
    extract_cover(partial, directory / "5_cover.jpg")
    partial.replace(output)
    shutil.rmtree(stage)
    return output


def compose(directory, render_only=False, feedback="", local_edit=False):
    directory = directory.resolve()
    raw = directory/"4_heygen_raw.mp4"
    path = directory/"remotion_props.json"
    manifest = directory/"5_inputs.json"
    render_manifest = directory / "4_render.json"
    backgrounds = read(render_manifest).get("backgrounds", []) if render_manifest.exists() else []
    previous_inputs = read(manifest) if manifest.exists() else {}
    background_changed = backgrounds != previous_inputs.get("backgrounds", [])
    keyed_source_changed = bool(backgrounds) and previous_inputs.get("raw") != digest(raw)
    # Explicit step 5 treats manual edits as authoritative, even without a script plan.
    if render_only and path.exists() and not (background_changed or keyed_source_changed):
        props = RenderProps.model_validate(read(path))
    else:
        raw_id = digest(raw)
        inputs = {"edit_version":3,"raw":raw_id,"local_edit":local_edit,"feedback":feedback,"backgrounds":backgrounds,"script":digest(directory/"3_script_plan.json"),
                  "broll":[] if local_edit else [[a["src"], digest(source(directory, a["src"]))]
                                                  for a in asset_catalog(directory)]}
        if path.exists() and manifest.exists() and read(manifest)==inputs:
            props = RenderProps.model_validate(read(path))
        else:
            duration = float(probe(raw)["format"]["duration"])
            props = prepare(directory, raw, raw_id, duration, feedback, backgrounds, local_edit)
            validate_cues(props,duration,directory)
            dump(path,props.model_dump())
            dump(manifest,inputs)
    if props.jobId != directory.name:
        raise ValueError("jobId в remotion_props.json должен совпадать с папкой выпуска")
    avatar = raw if props.transparentAvatar and raw.is_file() else source(directory, props.avatar)
    duration = float(probe(avatar)["format"]["duration"])
    validate_cues(props,duration,directory)
    return render(directory,props)


def replace_background(directory, new_background):
    props_path = directory / "remotion_props.json"
    manifest = directory / "4_render.json"
    inputs = directory / "5_inputs.json"
    props = RenderProps.model_validate(read(props_path))
    if not props.transparentAvatar or not props.backgrounds:
        raise ValueError("Смена фона доступна для готового видео на хромакее")
    props.backgrounds = [
        BackgroundCue(startSec=cue.startSec, endSec=cue.endSec, **new_background)
        for cue in props.backgrounds
    ]
    original = read(manifest)
    backgrounds = [new_background] * len(original["backgrounds"])
    temporary = directory / "remotion_background_props.json"
    dump(temporary, props.model_dump())
    try:
        output = render(directory, props, temporary)
        dump(props_path, props.model_dump())
        dump(manifest, {**original, "backgrounds": backgrounds})
        if inputs.exists():
            dump(inputs, {**read(inputs), "backgrounds": backgrounds})
        return output
    finally:
        temporary.unlink(missing_ok=True)
