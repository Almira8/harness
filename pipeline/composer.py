import os
import shutil
import subprocess
from pathlib import Path

from app.config import ROOT
from app.media import probe
from schemas.pipeline import Phrase, ScriptPlan
from .common import digest, dump, ffmpeg, read, whisper
from .edit_director import RenderProps, Word, direct
from .visuals import local_asset

STUDIO = ROOT / "remotion-studio"
IMAGE = {".jpg",".jpeg",".png",".webp"}
VIDEO = {".mov",".mp4",".webm",".m4v"}


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
    for p in sorted((directory/"broll").glob("*")):
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
    for layer in (props.zoom_events,props.b_roll,props.callouts):
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


def prepare(directory, raw, raw_id, duration):
    plan = ScriptPlan.model_validate(read(directory/"3_script_plan.json"))
    words = words_for(directory,raw,raw_id)
    catalog = asset_catalog(directory)
    cuts = direct(plan,words,duration,catalog)
    if any(c.src not in {a["src"] for a in catalog} for c in cuts.b_roll):
        raise ValueError("EditDirector выбрал отсутствующую перебивку")
    music = None
    if plan.music.asset:
        track = local_asset(plan.music.asset)
        dest = directory/"music"/track.name
        dest.parent.mkdir(exist_ok=True)
        shutil.copy2(track,dest)
        music = dest.relative_to(directory).as_posix()
    return RenderProps(**cuts.model_dump(),jobId=directory.name,avatar=raw.name,
        words=words,music=music,volume=min(plan.music.volume,0.12),accent="#CBFF68",
        subtitle_style=plan.subtitle_style)


def render(directory, props):
    stage = STUDIO/"public"/"assets"/directory.name
    output = directory/"5_final_output.mp4"
    partial = directory/"5_rendering.mp4"
    for name in {props.avatar, *(c.src for c in props.b_roll), *([props.music] if props.music else [])}:
        path = source(directory,name)
        target = stage / name
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(path,target)
    cmd = ["npx","--no-install","remotion","render","src/index.ts","MainComposition",str(partial),
           "--props="+str(directory/"remotion_props.json"),"--concurrency=2","--overwrite"]
    if browser := os.getenv("REMOTION_BROWSER_EXECUTABLE"):
        cmd.append("--browser-executable="+browser)
    proc = subprocess.run(cmd,cwd=STUDIO,capture_output=True,text=True,timeout=1800)
    if proc.returncode:
        raise ValueError("Remotion: "+(proc.stdout+"\n"+proc.stderr)[-2200:])
    probe(partial)
    partial.replace(output)
    shutil.rmtree(stage)
    return output


def compose(directory, render_only=False):
    directory = directory.resolve()
    raw = directory/"4_heygen_raw.mp4"
    path = directory/"remotion_props.json"
    manifest = directory/"5_inputs.json"
    # Explicit step 5 treats manual edits as authoritative, even without a script plan.
    if render_only and path.exists():
        props = RenderProps.model_validate(read(path))
    else:
        raw_id = digest(raw)
        inputs = {"raw":raw_id,"script":digest(directory/"3_script_plan.json"),
                  "broll":[a["src"] for a in asset_catalog(directory)]}
        if path.exists() and manifest.exists() and read(manifest)==inputs:
            props = RenderProps.model_validate(read(path))
        else:
            duration = float(probe(raw)["format"]["duration"])
            props = prepare(directory,raw,raw_id,duration)
            validate_cues(props,duration,directory)
            dump(path,props.model_dump())
            dump(manifest,inputs)
    if props.jobId != directory.name:
        raise ValueError("jobId в remotion_props.json должен совпадать с папкой выпуска")
    duration = float(probe(source(directory,props.avatar))["format"]["duration"])
    validate_cues(props,duration,directory)
    return render(directory,props)
